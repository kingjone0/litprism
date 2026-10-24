"""Program-owned description of the actual QA input, never a model verdict."""
from __future__ import annotations

import re

from .textutil import parse_citations


def source_metadata(excerpt):
    """Exact program-owned labels supplied beside evidence, never paper prose."""
    if excerpt.get('source_kind', 'paper') == 'paper':
        return {}
    fields = {}
    if excerpt.get('acquired_at'):
        fields['acquired_at'] = '获取时间：'+excerpt['acquired_at']
    context = '地址：'+(excerpt.get('url') or '用户知识条目')+'（不是自动核验的论文）'
    if (excerpt.get('dependency_status') or {}).get('state') == 'historical':
        fields['material_history'] = '历史版本依据，不代表当前材料结论'
        context += '【'+fields['material_history']+'】'
    fields['source_context'] = context
    return fields


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
            'sources': [{**{k: e.get(k) for k in ('tag', 'paper_id', 'material_level')},
                         'source_kind': e.get('source_kind', 'paper'),
                         'title': e.get('title') or e.get('paper_title') or ''} for e in excerpts],
            'table_warning_tags': [e['tag'] for e in excerpts if '【表格待核对】' in e['text']],
            'meaning': '仅记录实际送达材料，不证明理解、语义正确或人工确认。'}


def material_risks(answer, excerpts, delivery):
    issues = []
    papers = delivery.get('papers', [])
    # Statements about what was supplied differ from deliberately citing only
    # an abstract. The latter is allowed even when full material was supplied.
    supplied = delivery.get('sources') or excerpts
    def subjects(text):
        matched = []
        for e in supplied:
            title = e.get('title') or e.get('paper_title') or ''
            aliases = [title] if len(title) >= 2 else []
            # Explicit title prefixes such as "BERT: ..." are metadata names,
            # not model-inferred aliases or fuzzy subject classification.
            prefix = re.match(r'^([A-Z][A-Z0-9-]{1,20})[:：]', title)
            if prefix:
                aliases.append(prefix[1])
            if any(re.search(r'(?<![A-Za-z0-9_])'+re.escape(a)+r'(?![A-Za-z0-9_])', text, re.I)
                   if a.isascii() else a in text for a in aliases):
                matched.append(e)
        return matched
    pattern = r'(?:所给|提供的|本轮)[^。！？\n]{0,45}(?:仅有|只有|仅为|多为|只包含)[^。！？\n]{0,18}(?:首页|摘要)'
    for match in re.finditer(pattern, answer):
        start = max(answer.rfind(c, 0, match.start()) for c in '。！？\n') + 1
        scope = answer[start:match.end()]
        if re.search(r'(?:不能|不应|并非|不是|并未|没有)[^。；]{0,12}(?:所给|提供的|本轮)', scope):
            continue
        local = re.split(r'[，,；;]', scope)[-1]
        named = subjects(local) or subjects(scope)
        prefix = re.split(r'仅有|只有|仅为|多为|只包含', match.group())[0]
        global_scope = bool(re.fullmatch(
            r'(?:所给|提供的|本轮)(?:的|所给|提供的|送达|取得|片段|材料|这些|各|全部|所有|论文|文献|各篇)*', prefix.strip()))
        # An unrecognised named subject is unknown, not every full paper in the
        # request. Only explicit global claims may inherit global coverage.
        target = [p for p in papers if (not named and global_scope) or any(
            e.get('paper_id') == p.get('paper_id') and e.get('source_kind', 'paper') == 'paper' for e in named)]
        if any(p['material_level'] == 'fulltext' and p['complete_current_material'] for p in target):
            issues.append('程序已送达所称对象的当前全文材料，回答却将其描述为仅有或多为首页/摘要。')
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
