"""Deterministic QA evidence assembly over current materials, without new models."""
from __future__ import annotations

import re


def as_excerpts(hits):
    return [dict(tag=f'P{i}', chunk_id=h.get('id'), paper_id=h.get('paper_id'),
                 title=h.get('paper_title') or '', year=h.get('paper_year'), page=h.get('page'),
                 section=h.get('section') or '', score=h.get('score'), text=h['text'],
                 source_id=h.get('source_id'), source_kind=h.get('source_kind', 'paper'), url=h.get('url'),
                 material_id=h.get('material_id'), web_grant_id=h.get('web_grant_id'),
                 trust_level=h.get('trust_level'), dependency_status=h.get('dependency_status'),
                 material_level=h.get('material_level'), acquired_at=h.get('acquired_at'))
            for i, h in enumerate(hits, 1)]


def source_block(head, tag, text):
    """Frame one source without changing its header or original body bytes."""
    return head+f'\n【来源正文 {tag} 开始】\n'+text+f'\n【来源正文 {tag} 结束】'


def material_text(excerpts):
    blocks = []
    for e in excerpts:
        head = f"[{e['tag']}] 论文：《{e['title']}》"
        if e.get('source_kind') != 'paper':
            from .qa_evidence import source_metadata
            metadata = source_metadata(e)
            label = '本轮搜索摘要 · 未人工审核' if e.get('web_grant_id') and e.get('trust_level') == 'pending' else '人工确认的'+('网页快照' if e['source_kind'] == 'web' else '知识条目')
            head = f"[{e['tag']}] {label}：《{e['title']}》 "+metadata['source_context']
            if metadata.get('acquired_at'):
                head += ' '+metadata['acquired_at']
        if e.get('year'):
            head += f"（{e['year']}）"
        loc = []
        if e.get('page'):
            loc.append(f"第 {e['page']} 页")
        if e.get('section'):
            loc.append(f"章节：{e['section']}")
        if loc:
            head += '  '+' · '.join(loc)
        blocks.append(source_block(head,e['tag'],e['text']))
    return '\n\n---\n\n'.join(blocks)


def retrieve(kb, question, query, *, paper_ids, top_k, llm, search,
             cfg=None, fits_full=None, prior_refs=(), strict_scope=True, external=()):
    papers = [dict(r) for r in kb._con.execute(
        "SELECT id,title,year,material_level,current_material_id FROM papers "
        "WHERE deleted_at IS NULL AND verify_level IN ('verified','manual_confirmed') ORDER BY rowid")
        if paper_ids is None or r['id'] in paper_ids]
    ids = [p['id'] for p in papers]
    cross = bool(re.search(r'这些论文|这些研究|各篇|分别|跨论文|两篇|多篇|对比|比较|\b(compare|papers|studies)\b', question, re.I))
    overview = bool(re.search(r'整篇|全文|概览|概述|研究(?:了)?什么|研究问题|主要解决什么|\b(overview|summarize|research questions?)\b', question, re.I))
    mode = 'overview' if overview else 'cross_paper' if cross and len(ids) > 1 else 'passages'
    queues = {}
    def chunks(pid):
        if pid not in queues:
            queues[pid] = kb.get_chunks(pid)
        return queues[pid]
    titles = {p['id']: p for p in papers}
    def hit(chunk, score=0):
        p = titles[chunk['paper_id']]
        return dict(chunk, paper_title=p['title'], paper_year=p['year'], score=score)

    extra, restored_knowledge = [], []
    if not strict_scope or paper_ids is None:
        from .knowledge import search_knowledge, source_chunk
        for ref in prior_refs:
            if ref.get('source_kind') not in ('knowledge', 'web') or not ref.get('source_id'):
                continue
            source = source_chunk(kb, ref.get('chunk_id'))
            if (source and source['source_id'] == ref['source_id'] and source['citable']
                    and source['source_kind'] == ref['source_kind']):
                restored_knowledge.append(dict(source, score=1.0))
        extra = restored_knowledge + [h for h in search_knowledge(kb, query, top_k) if h['score'] >= kb.cfg.retrieval.min_score]
        if paper_ids:
            extra += search(kb, query, top_k=top_k, paper_ids=None, llm=None, citable_only=True)
    supplemental = extra + list(external)
    full_eligible = bool(ids and paper_ids and set(ids) == set(paper_ids) and cfg
                         and cfg.llm.model_window_tokens and fits_full)
    whole = [hit(c) for pid in ids for c in chunks(pid)] if full_eligible else []
    full_fits = bool(whole and fits_full(whole + supplemental))
    full_papers = ids[:] if full_fits else []
    completion_candidates, expanded_sections = [], []
    if full_fits:
        hits, mode = whole, 'full_material'
    elif overview and ids:
        groups = [chunks(pid) for pid in ids]
        hits = [hit(group[i]) for i in range(max(map(len, groups), default=0))
                for group in groups if i < len(group)]
    else:
        hits = search(kb, query, top_k=top_k, paper_ids=paper_ids, llm=llm, citable_only=True)
        if mode == 'cross_paper':
            # Balancing an already-truncated global list cannot recover absent
            # papers. Retrieve within each paper without extra model expansion.
            groups = [search(kb, query, top_k=top_k, paper_ids=[pid], llm=None, citable_only=True)
                      for pid in ids]
            balanced = [group[i] for i in range(max(map(len, groups), default=0))
                        for group in groups if i < len(group)]
            hits = balanced[:max(top_k, len([g for g in groups if g]))]

    section = None
    if len(ids) == 1 and not re.search(r'多少|几|数值|\b(how many|how long|gpu)\b', question, re.I):
        for pattern, role in [(r'局限|limitations?|future work', '局限与未来工作'),
                              (r'方法|methodology|approach', '方法'),
                              (r'结论|conclusions?', '结论')]:
            if re.search(pattern, question, re.I):
                section = role
                break
    if section and not overview and not full_fits:
        selected = [c for c in chunks(ids[0]) if c.get('section_role') == section or section in (c.get('section') or '')]
        if selected:
            hits, mode = [hit(c) for c in selected], 'section'

    restored = list(restored_knowledge)
    if mode == 'passages':
        # Stable old source handles locate fresh current text, never old answer
        # prose or a stale P-number. Scope and material are checked again here.
        for ref in prior_refs:
            pid = ref.get('paper_id')
            if pid not in titles:
                continue
            restored.extend(hit(c) for c in chunks(pid) if c['id'] == ref.get('chunk_id'))
        hits = restored + hits
        expanded, seen = [], set()
        for h in hits:
            neighbors = []
            if h.get('paper_id') in titles:
                group = chunks(h['paper_id'])
                index = next((i for i, c in enumerate(group) if c['id'] == h['id']), None)
                if index is not None:
                    neighbors = [hit(c, h['score']) for c in group[max(0, index-1):index+2]
                                 if c.get('section') == h.get('section')]
            for c in [h, *neighbors]:
                if c['id'] not in seen:
                    seen.add(c['id'])
                    expanded.append(c)
        hits = expanded
    def unique_sources(sources):
        unique = {}
        for h in sources:
            unique.setdefault(h['id'], h)
        return list(unique.values())

    # Default scope still starts with relevant lexical matches, not every paper
    # in the library. Complete their current materials only when the actual
    # prompt fits, retaining all seed hits, knowledge and external evidence.
    if paper_ids is None and mode in ('passages', 'cross_paper') and cfg and cfg.llm.model_window_tokens and fits_full:
        completion_candidates = list(dict.fromkeys(h['paper_id'] for h in hits if h.get('paper_id') in titles))
        full_eligible = bool(completion_candidates)

        def replace_group(sources, pid, group, section=None):
            out, inserted = [], False
            for source in sources:
                matches = source.get('paper_id') == pid and (section is None or source.get('section') == section)
                if matches:
                    if not inserted:
                        out.extend(group)
                        inserted = True
                else:
                    out.append(source)
            return out

        whole = [hit(c) for pid in completion_candidates for c in chunks(pid)]
        nonpapers = [h for h in hits if h.get('paper_id') not in titles]
        full_fits = bool(whole and fits_full(unique_sources(whole + nonpapers + supplemental)))
        if full_fits:
            hits, mode, full_papers = whole + nonpapers, 'full_material', completion_candidates[:]
        else:
            for pid in completion_candidates:
                candidate = replace_group(hits, pid, [hit(c) for c in chunks(pid)])
                if fits_full(unique_sources(candidate + supplemental)):
                    hits = candidate
                    full_papers.append(pid)
                    continue
                # An oversized paper may still have a complete relevant section
                # that fits. Empty headings do not establish a section boundary.
                sections = list(dict.fromkeys(h.get('section') for h in hits if h.get('paper_id') == pid and h.get('section')))
                for section_name in sections:
                    group = [hit(c) for c in chunks(pid) if c.get('section') == section_name]
                    candidate = replace_group(hits, pid, group, section_name)
                    if {h['id'] for h in candidate} != {h['id'] for h in hits} and fits_full(unique_sources(candidate + supplemental)):
                        hits = candidate
                        expanded_sections.append({'paper_id': pid, 'section': section_name})
    hits = unique_sources(hits + supplemental)
    report = {'mode': mode, 'requested_paper_ids': paper_ids, 'scope_papers': ids,
              'scope_policy': 'strict' if strict_scope else 'focus_and_related_knowledge',
              'external_chunks': len(external),
              'full_material_considered': full_eligible, 'full_material_fits': full_fits,
              'full_material_candidate_paper_ids': ids if paper_ids and full_eligible else completion_candidates,
              'full_material_paper_ids': full_papers, 'expanded_sections': expanded_sections,
              'restored_chunk_ids': [h['id'] for h in restored],
              'candidate_chunks': len(hits), 'materials': {p['id']: p['current_material_id'] for p in papers},
              'material_levels': {p['id']: p['material_level'] for p in papers},
              'coverage_kind': 'indexed_material_not_semantic_completeness'}
    return hits, report
