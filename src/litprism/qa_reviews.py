"""Immutable reply review snapshots; human decisions never rewrite chat facts.

Uses semantic_runs/reviews so existing backups include both the candidate and audit.
The delivered source inventory is server-owned, independent of model evidence_map.
"""
from __future__ import annotations

import json
import uuid

from .evidence import citation_issues, stored_excerpts
from .knowledge import create_source, get_source, now, review_source
from .semantics import payload_hash
from .textutil import parse_citations


def source_hash(source):
    return payload_hash({k: source.get(k) for k in ('paper_id', 'page', 'text')})


def save(kb, rid, message, conversation_id, question, answer, scope):
    """Call in the same transaction as append_message; no model call or promotion."""
    sources = [{
        'tag': e['tag'], 'chunk_id': e['chunk_id'], 'paper_id': e.get('paper_id'),
        'page': e.get('page'), 'material_id': e.get('material_id'),
        'source_hash': source_hash(e),
        'web_grant_id': e.get('web_grant_id'),
    } for e in answer.excerpts]
    payload = {'_kind': 'qa_review', 'message_id': message['id'], 'conversation_id': conversation_id,
               'question': question, 'draft': answer.draft_answer, 'status': answer.status,
               'issues': list(dict.fromkeys(answer.citation_issues + answer.guard_issues)),
               'scope': scope, '_citations': sources,
               'evidence_audit': answer.evidence_audit}
    kb._con.execute('INSERT INTO semantic_runs VALUES (?,?,?,?)',
                    (rid, '对话人工审核 · '+question[:100], json.dumps(payload, ensure_ascii=False), now()))


def snapshot(kb, rid):
    from fastapi import HTTPException
    row = kb._con.execute('SELECT * FROM semantic_runs WHERE id=?', (rid,)).fetchone()
    if row is None:
        raise HTTPException(404, '回复审核快照不存在。')
    payload = json.loads(row['payload'])
    if payload.get('_kind') != 'qa_review':
        raise HTTPException(404, '不是对话回复审核快照。')
    digest = payload_hash(payload)
    records = [dict(r) for r in kb._con.execute(
        "SELECT * FROM semantic_reviews WHERE run_id=? AND analysis_hash=? AND claim_id='reply' ORDER BY rowid", (rid, digest))]
    return payload, digest, records


def latest_decision(kb, rid):
    if not rid:
        return None
    row = kb._con.execute("SELECT action,knowledge_id FROM semantic_reviews WHERE run_id=? AND claim_id='reply' ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
    if row:
        return dict(row)
    if kb._con.execute('SELECT 1 FROM semantic_reviews WHERE run_id=? LIMIT 1', (rid,)).fetchone():
        return {'action': 'partial', 'knowledge_id': None, 'scope': 'units'}
    return None


def resolve(kb, ref, scope):
    """Current trust and exact frozen material/content, never substitute another chunk."""
    e = stored_excerpts(kb, [ref], allow_session=True)[0]
    if source_hash(e) != ref['source_hash'] or e.get('page') != ref.get('page'):
        raise ValueError('原文内容或页码已变化，请重新生成回复后审核。')
    if e.get('paper_id'):
        ids = scope.get('paper_ids')
        if scope.get('strict', True) and ids is not None and e['paper_id'] not in ids:
            raise ValueError('来源不属于本轮指定论文范围。')
        paper = kb.get_paper(e['paper_id'])
        if e.get('material_id') != paper.get('current_material_id'):
            raise ValueError('来源已是历史材料，请依据当前材料重新生成回复后审核。')
    return e


def register_qa_reviews(app, kb):
    from fastapi import HTTPException
    @app.get('/api/chat-reviews/{rid}')
    def detail(rid: str):
        from .web.evidence_routes import evidence_detail
        with kb._tx_lock:
            payload, digest, records = snapshot(kb, rid)
            sources = []
            used = {tag for tag, _ in parse_citations(payload['draft'])}
            for ref in payload['_citations']:
                item = {**ref, 'citable': False, 'cited': ref['tag'] in used}
                try:
                    d = evidence_detail(kb, ref['chunk_id'])
                    item.update(title=d.get('title') or d.get('paper_title'), text=d['text'],
                                page=d.get('page'), source_kind=d['source_kind'], trust_level=d['trust_level'])
                    resolve(kb, ref, payload['scope'])
                    item['citable'] = True
                except (ValueError, HTTPException) as exc:
                    item['issue'] = str(exc.detail if isinstance(exc, HTTPException) else exc)
                sources.append(item)
            last = records[-1] if records else None
            knowledge = get_source(kb, last['knowledge_id']) if last and last['knowledge_id'] else None
            from .qa_unit_reviews import inventory, review_mode, unit_state, completion
            from .qa_claims import presentation_warnings
            units = [dict(u, **unit_state(kb, rid, digest, u['id'])) for u in inventory(payload)]
            return {'id': rid, 'snapshot_hash': digest, 'review_token': last['id'] if last else '',
                    'question': payload['question'], 'draft': payload['draft'], 'status': payload['status'],
                    'issues': payload['issues'], 'sources': sources, 'reviews': records,
                    'evidence_audit': payload.get('evidence_audit') or {'state': 'missing', 'items': [], 'issues': []},
                    'knowledge_status': knowledge['effective_status'] if knowledge else None,
                    'review_mode': review_mode(kb, rid), 'units': units,
                    'review_summary': completion(payload, units),
                    'audit_display': dict(zip(('warnings', 'notices'), presentation_warnings(
                        (payload.get('evidence_audit') or {}).get('warnings', []))))}

    @app.post('/api/chat-reviews/{rid}/review')
    def review(rid: str, req: dict):
        action, note = req.get('action'), req.get('note')
        if action not in ('confirm', 'reject', 'revoke') or not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise HTTPException(400, '请选择审核动作并填写核对依据（最多 5000 字符）。')
        with kb.tx() as con:
            payload, digest, records = snapshot(kb, rid)
            from .qa_unit_reviews import review_mode
            if review_mode(kb, rid) == 'units':
                raise HTTPException(409, '此回复已按单元裁决，请继续使用单元入口，不能混用整答审核。')
            last = records[-1] if records else None
            if req.get('snapshot_hash') != digest or req.get('review_token') != (last['id'] if last else ''):
                raise HTTPException(409, '回复或审核状态已变化，请重新打开审核面板。')
            if last and last['action'] == action:
                raise HTTPException(409, '已执行该审核动作，请刷新审核记录。')
            source_id = None
            text = None
            if action == 'confirm':
                text, tags = req.get('reviewed_text'), req.get('source_tags')
                if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    raise HTTPException(400, '请填写核对后的知识正文（最多 2 万字符）。')
                pool = {r['tag']: r for r in payload['_citations']}
                if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or t not in pool for t in tags):
                    raise HTTPException(400, '请选择本轮实际送达的原文来源。')
                try:
                    excerpts = [resolve(kb, pool[t], payload['scope']) for t in dict.fromkeys(tags)]
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
                issues = citation_issues(text, excerpts)
                if issues:
                    raise HTTPException(400, '修订正文的引用仍有问题：'+'；'.join(issues))
                # Never place unselected draft claims or the blocked draft into searchable knowledge.
                provenance = '\n\n'.join(f"[{e['tag']}]《{e.get('paper_title') or '原文'}》"
                    + (f" p.{e['page']}" if e.get('page') is not None else '')
                    + f" chunk_id={e['id']}"
                    + (f"\n原始地址：{e['url']}" if e.get('url') else '')
                    + f"\n{e['text']}" for e in excerpts)
                content = ('人工核对的知识正文：\n'+text.strip()+'\n\n核对依据：'+note.strip()
                           +'\n\n选定原始来源（定位不代表自动证明）：\n'+provenance
                           +'\n\n回复审核快照：'+rid+'\n快照哈希：'+digest)
                if len(content) > 400000:
                    raise HTTPException(400, '所选来源过多，请缩小为支持当前知识正文的原文。')
                source = create_source(kb, kind='knowledge', title='对话人工确认 · '+payload['question'][:100], content=content,
                                       dependencies=excerpts, accept_pending=True, search_text=text.strip())
                review_source(kb, source['id'], action='confirm', note=note, content_hash=source['content_hash'])
                source_id = source['id']
            elif last and last.get('knowledge_id'):
                source = get_source(kb, last['knowledge_id'])
                if source and source['status'] == 'manual_confirmed':
                    review_source(kb, source['id'], action='revoke', note=note, content_hash=source['content_hash'])
                source_id = last['knowledge_id']
            if action == 'revoke' and not source_id:
                raise HTTPException(409, '此回复没有可撤销的知识入库记录。')
            con.execute('INSERT INTO semantic_reviews VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('sr_'+uuid.uuid4().hex[:12], None, rid, digest, 'reply', action,
                         note.strip(), text, source_id, now()))
        return {'ok': True, 'knowledge_id': source_id}

    from .qa_unit_reviews import register_unit_reviews
    register_unit_reviews(app, kb)
