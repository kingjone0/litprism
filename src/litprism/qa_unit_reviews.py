"""Independent human unit decisions, immutable parent answers and exact evidence.

One parent uses either whole-reply or unit decisions. Review rows reuse schema
v15; each unit decision also has an immutable audit payload in semantic_runs.
Confirmed knowledge indexes only the human text. Parent drafts never become
partial factual history; knowledge must be retrieved again through provenance.
"""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException

from .evidence import citation_issues
from .knowledge import create_source, get_source, now, review_source
from .qa_answer import span
from .qa_anchors import build, resolve as resolve_anchor
from .textutil import parse_citations


def inventory(payload):
    answer = payload['draft']
    out = []
    for item in (payload.get('evidence_audit') or {}).get('items', []):
        b = item.get('answer_binding') or {}
        s = b.get('span') or {}
        start, end = s.get('start'), s.get('end')
        if (b.get('state') != 'bound' or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(answer)):
            continue
        expected = span(answer, start, end)
        if (any(s.get(k) != v for k, v in expected.items()) or not isinstance(item.get('claim'), str)
                or expected['text'] != item['claim'].strip()):
            continue
        out.append({'id': expected['id'], 'item_id': item['id'], 'text': expected['text'], 'answer_span': expected,
                    'sources': item.get('sources', []), 'original_evidence': item.get('quotes', [])})
    # Duplicate auxiliary mappings cannot create two identities for one position.
    return [u for u in out if sum(o['id'] == u['id'] for o in out) == 1]


def review_mode(kb, rid):
    rows = list(kb._con.execute('SELECT claim_id FROM semantic_reviews WHERE run_id=?', (rid,)))
    return 'reply' if any(r[0] == 'reply' for r in rows) else 'units' if rows else 'unreviewed'


def unit_state(kb, rid, digest, uid):
    records = [dict(r) for r in kb._con.execute('''SELECT * FROM semantic_reviews
        WHERE run_id=? AND analysis_hash=? AND claim_id=? ORDER BY rowid''', (rid, digest, uid))]
    for r in records:
        saved = kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', ('qud_'+r['id'],)).fetchone()
        r['evidence_snapshot'] = json.loads(saved['payload']).get('selected_evidence', []) if saved else []
    last = records[-1] if records else None
    knowledge = get_source(kb, last['knowledge_id']) if last and last.get('knowledge_id') else None
    return {'reviews': records, 'review_token': last['id'] if last else '',
            'decision': last['action'] if last else 'unreviewed',
            'knowledge_id': last.get('knowledge_id') if last else None,
            'knowledge_status': knowledge['effective_status'] if knowledge else None,
            'knowledge_citable': knowledge['citable'] if knowledge else False}


def current(kb, rid, uid):
    from .qa_reviews import snapshot
    payload, digest, _ = snapshot(kb, rid)
    unit = next((u for u in inventory(payload) if u['id'] == uid), None)
    if unit is None:
        raise HTTPException(404, '单元不存在或没有可靠的回答位置，不能猜测绑定。')
    return payload, digest, unit


def completion(payload, units):
    """Aggregate actual unit decisions without inventing a whole-reply review."""
    total = len(units)
    pending = sum(u['decision'] == 'unreviewed' for u in units)
    confirmed = sum(u['decision'] in ('confirm', 'revise') and u['knowledge_citable'] for u in units)
    rejected = sum(u['decision'] == 'reject' for u in units)
    revoked = sum(u['decision'] == 'revoke' for u in units)
    invalidated = sum(u['decision'] in ('confirm', 'revise') and not u['knowledge_citable'] for u in units)
    end, covered = 0, bool(units)
    for unit in sorted(units, key=lambda u: u['answer_span']['start']):
        position = unit['answer_span']
        if position['start'] < end or payload['draft'][end:position['start']].strip():
            covered = False
        end = max(end, position['end'])
    covered = covered and not payload['draft'][end:].strip()
    processed = total-pending
    state, label = 'unreviewed', ''
    if processed:
        if confirmed == total:
            state, label = 'all_confirmed', f'全部 {total} 条已确认入库'
        elif not pending:
            state, label = 'all_reviewed', f'全部 {total} 条已处理 · 入库 {confirmed} · 拒绝 {rejected} · 撤销 {revoked}'
        else:
            state, label = 'partial', f'已处理 {processed}/{total} 条 · 已入库 {confirmed} 条'
        if invalidated:
            label += f' · {invalidated} 条知识依据已失效'
        if not covered:
            label += ' · 另有未对应正文'
    return {'state': state, 'label': label, 'total': total, 'processed': processed, 'pending': pending,
            'confirmed': confirmed, 'rejected': rejected, 'revoked': revoked, 'invalidated': invalidated,
            'whole_answer_covered': bool(covered), 'complete': bool(total and not pending and covered)}


def display_units(kb, rid):
    """Read-only presentation; never reintroduce reviewed drafts into model history.

    The caller must keep citation/semantic blocking ahead of this projection.
    Only server-validated positions identify a unit; legacy gaps stay as text.
    """
    from .qa_reviews import snapshot
    with kb._tx_lock:
        payload, digest, _ = snapshot(kb, rid)
        units = []
        for unit in inventory(payload):
            state = unit_state(kb, rid, digest, unit['id'])
            previous = next((r for r in reversed(state['reviews']) if r.get('reviewed_text')), None)
            units.append({k: unit[k] for k in ('id', 'text', 'answer_span')} | {
                k: state[k] for k in ('decision', 'knowledge_id', 'knowledge_status', 'knowledge_citable')
            } | {'reviewed_text': previous['reviewed_text'] if previous else None})
        return {'draft': payload['draft'], 'units': units, 'mode': review_mode(kb, rid),
                'summary': completion(payload, units)}


def source(kb, payload, tag):
    from .qa_reviews import resolve
    ref = next((r for r in payload['_citations'] if r['tag'] == tag), None)
    if ref is None:
        raise HTTPException(400, '不是本轮实际送达的来源。')
    try:
        e = resolve(kb, ref, payload['scope'])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return dict(e, chunk_id=ref['chunk_id'], source_kind=e.get('source_kind', 'paper'))


def register_unit_reviews(app, kb):
    @app.get('/api/chat-reviews/{rid}/units/{uid}')
    def detail(rid: str, uid: str):
        from .web.evidence_routes import evidence_detail
        with kb._tx_lock:
            payload, digest, unit = current(kb, rid, uid)
            state = unit_state(kb, rid, digest, uid)
            sources = []
            for ref in payload['_citations']:
                item = {'tag': ref['tag'], 'citable': False, 'title': ref['tag'], 'page': ref.get('page')}
                try:
                    d = evidence_detail(kb, ref['chunk_id'])
                    item.update(title=d.get('title') or d.get('paper_title'), source_kind=d['source_kind'])
                    source(kb, payload, ref['tag'])
                    item['citable'] = True
                except HTTPException as exc:
                    item['issue'] = str(exc.detail)
                sources.append(item)
            previous = next((r for r in reversed(state['reviews']) if r.get('reviewed_text')), None)
            return {**unit, **state, 'parent_id': rid, 'snapshot_hash': digest,
                    'review_mode': review_mode(kb, rid), 'sources_available': sources,
                    'suggested_text': previous['reviewed_text'] if previous else unit['text'],
                    'suggested_evidence': previous['evidence_snapshot'] if previous else unit['original_evidence']}

    @app.get('/api/chat-reviews/{rid}/units/{uid}/sources/{tag}')
    def context(rid: str, uid: str, tag: str):
        with kb._tx_lock:
            payload, _, unit = current(kb, rid, uid)
            e = source(kb, payload, tag)
            return {'tag': tag, 'title': e.get('paper_title') or e.get('title') or tag, 'page': e.get('page'),
                    'text': e['text'], 'anchors': build(e),
                    'original_evidence': [q for q in unit['original_evidence'] if q['source'] == tag],
                    'notice': '补充上下文不是原模型已选证据；勾选后只用于本次人工裁决。'}

    @app.post('/api/chat-reviews/{rid}/units/{uid}/review')
    def decide(rid: str, uid: str, req: dict):
        action, note = req.get('action'), req.get('note')
        if action not in ('confirm', 'revise', 'reject', 'revoke') or not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise HTTPException(400, '请选择单元审核动作并填写核对依据（最多5000字符）。')
        with kb.tx() as con:
            payload, digest, unit = current(kb, rid, uid)
            if review_mode(kb, rid) == 'reply':
                raise HTTPException(409, '此回复已有整答裁决，请使用原整答入口，不能混用单元审核。')
            state = unit_state(kb, rid, digest, uid)
            if req.get('snapshot_hash') != digest or req.get('review_token') != state['review_token']:
                raise HTTPException(409, '单元或审核状态已变化，请重新打开。')
            active = state['decision'] in ('confirm', 'revise')
            if action == 'confirm' and active:
                raise HTTPException(409, '单元已有入库记录，请使用修订或撤销。')
            if action in ('revise', 'revoke') and (not active or not state['knowledge_id']):
                raise HTTPException(409, '没有可修订或撤销的单元入库记录。')
            if action == 'reject' and state['decision'] == 'reject':
                raise HTTPException(409, '该单元已拒绝。')
            selected, excerpts, text = [], {}, None
            if action in ('confirm', 'revise'):
                text, evidence = req.get('reviewed_text'), req.get('evidence')
                if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    raise HTTPException(400, '请填写核对后的单元知识正文（最多2万字符）。')
                if not isinstance(evidence, list) or not evidence:
                    raise HTTPException(400, '请勾选实际支持修订正文的原文短句或短段。')
                keys = set()
                for selection in evidence:
                    if not isinstance(selection, dict) or not isinstance(selection.get('source'), str) or not isinstance(selection.get('anchor_id'), str):
                        raise HTTPException(400, '证据选择格式错误。')
                    tag, aid = selection['source'], selection['anchor_id']
                    if not aid.startswith('qa2_') or (tag, aid) in keys:
                        raise HTTPException(400, '请选择本轮短原文，每处只选择一次；旧段落不自动改成细句。')
                    keys.add((tag, aid))
                    if tag not in excerpts:
                        excerpts[tag] = source(kb, payload, tag)
                    try:
                        q = resolve_anchor(excerpts[tag], aid)
                    except ValueError as exc:
                        raise HTTPException(409, str(exc)) from exc
                    selected.append({'source': tag, **q})
                issues = citation_issues(text, list(excerpts.values()))
                if {tag for tag, _ in parse_citations(text)} != set(excerpts):
                    issues.append('修订正文与所选证据的来源编号须一致。')
                if issues:
                    raise HTTPException(400, '单元引用仍有问题：'+'；'.join(issues))
                provenance = '\n\n'.join(f"[{q['source']}]《{excerpts[q['source']].get('paper_title') or '原文'}》"
                    + f" chunk_id={q['chunk_id']} anchor_id={q['anchor_id']}\n{q['quote']}" for q in selected)
                content = ('人工核对的单元知识正文：\n'+text.strip()+'\n\n核对依据：'+note.strip()
                           +'\n\n人工所选原文（不改写原模型选择）：\n'+provenance
                           +'\n\n父回复审核快照：'+rid+'\n回答单元：'+uid+'\n父快照哈希：'+digest)
                if len(content) > 400000:
                    raise HTTPException(400, '所选原文过多，请缩小核对范围。')
            old_id = state['knowledge_id']
            if old_id:
                old = get_source(kb, old_id)
                if old and old['status'] == 'manual_confirmed':
                    review_source(kb, old_id, action='revoke', note=note, content_hash=old['content_hash'])
            sid = old_id
            if action in ('confirm', 'revise'):
                knowledge = create_source(kb, kind='knowledge', title='单元人工确认 · '+payload['question'][:100],
                    content=content, dependencies=list(excerpts.values()), accept_pending=True, search_text=text.strip())
                review_source(kb, knowledge['id'], action='confirm', note=note, content_hash=knowledge['content_hash'])
                sid = knowledge['id']
            review_id = 'sr_'+uuid.uuid4().hex[:12]
            decision = {'_kind': 'qa_unit_decision', 'parent_id': rid, 'parent_hash': digest, 'unit_id': uid,
                        'prior_review': state['review_token'], 'action': action, 'reviewed_text': text,
                        'selected_evidence': selected, 'knowledge_id': sid}
            con.execute('INSERT INTO semantic_runs VALUES (?,?,?,?)',
                        ('qud_'+review_id, '单元人工裁决', json.dumps(decision, ensure_ascii=False), now()))
            con.execute('INSERT INTO semantic_reviews VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (review_id, None, rid, digest, uid, action, note.strip(), text.strip() if text else None, sid, now()))
        return {'ok': True, 'knowledge_id': sid, 'review_token': review_id}
