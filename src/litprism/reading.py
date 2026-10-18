"""完整原文的有预算分批读取；不截掉尾部，不用生成摘要替代证据。"""
from __future__ import annotations

from .context.meter import Meter


def excerpt_text(excerpts: list[dict]) -> str:
    return '\n\n'.join(
        f"[{e['tag']}] {e.get('document_tag', '')}《{e['paper_title']}》 "
        + (f"p.{e['page']}" if e['page'] is not None else '无页码')
        + f" chunk_id={e['id']}\n{e['text']}" for e in excerpts)


# ---------------------------------------------------------------------------
# 缓存友好的上下文布局
#
# 前缀缓存匹配的是**最长公共前缀**。要命中，必须满足两条：
#   1. 原文在前、指令在后（变化的只有尾部）；
#   2. 原文序列化字节稳定（同样的片段必然渲染出同样的字符串）。
#
# 旧布局是 `instruction + '\n\n' + 原文`，指令在最前面 —— 每次调用
# 从第一个 token 起就不匹配，缓存永远不命中。
# ---------------------------------------------------------------------------
PAPER_HEADER = '以下为论文原文（按阅读顺序编号）。\n'
TASK_SEPARATOR = '\n\n=== 本次任务 ===\n'


def excerpt_context(excerpts: list[dict]) -> str:
    """把原文渲染成**字节稳定**的缓存前缀。

    稳定性来自两处：片段顺序由调用方保证（按 chunk 序号），
    渲染用固定格式、不含时间戳/随机量。
    """
    return PAPER_HEADER + excerpt_text(excerpts)


def build_messages(system: str, paper_text: str, directive: str) -> list[dict]:
    """构造缓存友好的消息：system 固定、原文固定、只有 directive 变化。"""
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': paper_text + TASK_SEPARATOR + directive},
    ]


def prefix_of(messages: list[dict], directive: str) -> str:
    """取出「除指令外的公共前缀」，供测试与缓存键计算使用。"""
    user = messages[-1]['content']
    return user[: len(user) - len(directive)] if directive else user


def reading_batches(kb, cfg, papers: list[dict], system: str, instruction: str = ''):
    meter = Meter(cfg)
    available = cfg.context.available_tokens
    queues = [list(kb.get_chunks(p['id'])) for p in papers]
    if any(not q for q in queues):
        raise ValueError('文献没有可读取的原文。')
    items = []
    # 交错排列论文，尽可能在同一批中同时保留多个对比对象。
    for i in range(max(map(len, queues))):
        for j, (paper, chunks) in enumerate(zip(papers, queues, strict=True), 1):
            if i < len(chunks):
                c = chunks[i]
                items.append(dict(c, tag=f'P{len(items)+1}', paper_title=paper['title'], document_tag=f'D{j}'))
    # 预算按**实际消息布局**估算：system 固定 + 原文 + 任务分隔 + 指令。
    # 布局改了估算不改，就会出现"估得下、实际装不下"的偏差。
    _fixed = meter.estimate_text(system) + meter.estimate_text(PAPER_HEADER + TASK_SEPARATOR + instruction)

    def cost(batch):
        return _fixed + meter.estimate_text(excerpt_text(batch))
    batches, current = [], []
    for item in items:
        remaining = item['text']
        while remaining:
            full = dict(item, text=remaining)
            if cost(current+[full]) <= available:
                current.append(full)
                break
            if current:
                batches.append(current)
                current = []
                continue
            lo, hi = 0, len(remaining)
            while lo < hi:
                mid = (lo+hi+1)//2
                if cost([dict(item, text=remaining[:mid])]) <= available:
                    lo = mid
                else:
                    hi = mid-1
            if '【表格行】' in item['text']:
                # A valid table row must never lose half its header/value map
                # when a deliberately small model window forces another split.
                boundary = remaining.rfind('\n', 0, lo)
                if boundary < 1:
                    raise ValueError('上下文预算无法容纳完整表格行，请提高窗口或减少输出预留。')
                lo = boundary+1
            if lo < 1:
                raise ValueError('上下文预算无法容纳任务指令与原文，请提高窗口或减少输出预留。')
            batches.append([dict(item, text=remaining[:lo])])
            remaining = remaining[lo:]
    if current:
        batches.append(current)
    coverage = {'complete': True, 'batches': len(batches), 'total_chunks': len(items),
                'read_chunks': len(items), 'omitted_chunks': [],
                'sections': sorted({e.get('section') or '正文' for e in items}),
                'basis': 'indexed_chunks',
                'note': '全部可引用片段已分批送入模型；目录、参考文献及未恢复的表格数值不计入此覆盖。引用检查不代表语义核对通过，跨批结果不自动推断。'}
    return batches, items, coverage
