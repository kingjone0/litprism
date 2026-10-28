"""空态不能只靠文字指路：说明里出现"点某某按钮"时，必须同时给出可点的按钮。"""
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


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding='utf-8')


def test_no_text_only_navigation_hints():
    sources = {name: _js(name) for name in ('app.js', 'workflows.js')}
    for name, text in sources.items():
        for hint in FORBIDDEN_HINTS:
            assert hint not in text, f'{name} 仍在用纯文字指路：{hint}'


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
