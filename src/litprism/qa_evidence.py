"""Program-owned description of the actual QA input, never a model verdict."""
from __future__ import annotations

import re

from .textutil import parse_citations


def manifest(kb, excerpts):
    groups = {}
    for e in excerpts:
        if e.get('source_kind', 'paper') != 'paper' or not e.get('paper_id'):
            continue
        groups.setdefault(e['paper_id'], []).append(e)
    papers = []
    for pid, sources in groups.items():
        paper = kb.get_paper(pid) or {}
        current = {c['id'] for c in kb.get_chunks(pid)}
        provided = {s.get('chunk_id') for s in sources}
        papers.append({'paper_id': pid, 'title': paper.get('title', ''),
                       'material_level': paper.get('material_level', 'unknown'),
                       'provided_chunks': len(provided), 'current_chunks': len(current),
                       'complete_current_material': bool(current and provided == current)})
    return {'papers': papers, 'delivered_chunks': len(excerpts),
            'table_warning_tags': [e['tag'] for e in excerpts if '【表格待核对】' in e['text']],
            'meaning': '仅记录实际送达材料，不证明理解、语义正确或人工确认。'}


def material_risks(answer, excerpts, delivery):
    issues = []
    papers = delivery.get('papers', [])
    all_full = bool(papers and all(p['material_level'] == 'fulltext' and p['complete_current_material'] for p in papers))
    # Statements about what was supplied differ from deliberately citing only
    # an abstract. The latter is allowed even when full material was supplied.
    if all_full and re.search(r'(?:所给|提供的|本轮)[^。！？\n]{0,45}(?:仅有|只有|仅为|多为|只包含)[^。！？\n]{0,18}(?:首页|摘要)', answer):
        issues.append('程序已送达当前全部全文材料，回答却将所给材料描述为仅有或多为首页/摘要。')
    by_tag = {e['tag']: e for e in excerpts}
    for clause in re.findall(r'[^。！？\n]+[。！？]?(?:\s*\[P\d+[^\]]*\])*', answer):
        if not re.search(r'表格待核对|表格待审|(?:表格|行列|表头)[^。；]{0,24}(?:未可靠恢复|未纳入可引用)', clause):
            continue
        if re.search(r'(?:没有|并未|未曾|并非|不是)[^。；]{0,8}(?:标|表格待)', clause):
            continue
        used = [by_tag[tag] for tag, _ in parse_citations(clause) if tag in by_tag]
        if used and not any('【表格待核对】' in s['text'] for s in used):
            issues.append('回答声称引用的表格有解析待核对标记，但实际引用原文没有该标记。')
    return list(dict.fromkeys(issues))
