"""完整原文的有预算分批读取；不截掉尾部，不用生成摘要替代证据。"""
from __future__ import annotations

from .context.meter import Meter


def excerpt_text(excerpts: list[dict]) -> str:
    return '\n\n'.join(
        f"[{e['tag']}] {e.get('document_tag', '')}《{e['paper_title']}》 "
        + (f"p.{e['page']}" if e['page'] is not None else '无页码')
        + f" chunk_id={e['id']}\n{e['text']}" for e in excerpts)


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
    def cost(batch):
        return sum(meter.estimate_message(m) for m in [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': instruction+'\n\n'+excerpt_text(batch)}])
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
