"""空态不能只靠文字指路：说明里指挥用户去别处找控件时，必须同时给出可点的按钮。"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'src' / 'litprism' / 'web' / 'static'

# 这些说法把找按钮的负担丢给用户 —— 已经改成按钮，不能再回来
FORBIDDEN_HINTS = [
    '点击「导入文献」',
    '点「解读论文」',
    '点「获取可用模型」',
    '点击「对比所选」',
    '点「与知识库核对」',
]

# 说明里出现这些说法，等于在指挥用户去别处找控件
NAV_MARKERS = ('「', '点击', '在上方', '在下方', '在右栏', '在左侧')

# 纯信息型空态：只解释"这里会出现什么"，本身不指挥动作，允许没有按钮
INFORMATIONAL_TITLES = ('还没有交付物', '白名单为空', '还没有研究偏好', '回收站为空')


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding='utf-8')


def _empty_blocks(src: str):
    """按括号配平切出每个 emptyState({...}) 块，返回 (起始位置, 块文本)。"""
    for m in re.finditer(r'emptyState\(\{', src):
        i = m.end() - 1
        depth = 0
        for j in range(i, min(i + 1500, len(src))):
            if src[j] == '(':
                depth += 1
            elif src[j] == ')':
                depth -= 1
                if depth == 0:
                    break
        yield m.start(), src[i:j]


def _field(block: str, name: str) -> str:
    """取字段值；三元表达式会跨行，这里取到 action: 或块尾，足以做标记判断。"""
    start = block.find(name + ':')
    if start < 0:
        return ''
    stop = block.find('action:', start + 1)
    return block[start:stop if stop > 0 else len(block)]


def test_no_text_only_navigation_hints():
    sources = {name: _js(name) for name in ('app.js', 'workflows.js')}
    for name, text in sources.items():
        for hint in FORBIDDEN_HINTS:
            assert hint not in text, f'{name} 仍在用纯文字指路：{hint}'


def test_empty_states_with_navigation_hints_offer_a_button():
    """结构性断言：说明里指路就必须有 emptyCta，纯信息型空态除外。"""
    for name in ('app.js', 'workflows.js'):
        src = _js(name)
        for pos, block in _empty_blocks(src):
            desc = _field(block, 'desc')
            if not any(mark in desc for mark in NAV_MARKERS):
                continue
            if 'emptyCta' in block:
                continue
            title = _field(block, 'title')
            assert any(t in title for t in INFORMATIONAL_TITLES), (
                f'{name}:{src[:pos].count(chr(10)) + 1} 空态说明里指路但没有按钮：{desc[:60]}')


def test_empty_actions_point_at_existing_controls():
    app = _js('app.js')
    mapping = dict(re.findall(r"^\s{2}'?([a-z-]+)'?: '([^']+)',$", app, re.M))
    used = set(re.findall(r"emptyCta\('([a-z:-]+)'", app + _js('workflows.js')))
    assert used, '至少要有空态按钮'
    for action in used:
        if action.startswith('view:'):
            continue
        assert action in mapping, f'空态动作 {action} 没有映射目标'
        selector = mapping[action]
        target = selector.lstrip('#[').rstrip(']')
        assert target in app or target in _js('index.html'), f'映射目标 {selector} 不存在'
