"""Request-bound web snapshots, independent of global knowledge admission."""
import uuid
import hashlib
import json

from .knowledge import now, source_chunk


def grant(kb, conversation_id, user_message_id, query, items):
    ids = []
    with kb.tx() as con:
        for item in items:
            actual = con.execute('SELECT * FROM evidence_sources WHERE id=?',(item['id'],)).fetchone()
            if not actual or actual['kind'] != 'web' or actual['status'] not in ('pending','manual_confirmed') or actual['content_hash'] != item['content_hash']:
                raise ValueError('本轮搜索快照已变化，请重新检索。')
            old = con.execute('SELECT id,content_hash FROM chat_evidence_grants WHERE user_message_id=? AND source_id=?',
                              (user_message_id,item['id'])).fetchone()
            if old and old['content_hash'] != actual['content_hash']:
                raise ValueError('已存搜索证据与当前快照不一致。')
            gid = old['id'] if old else 'wg_'+uuid.uuid4().hex
            if not old:
                con.execute('INSERT INTO chat_evidence_grants VALUES (?,?,?,?,?,?,?)',
                    (gid,conversation_id,user_message_id,item['id'],actual['content_hash'],query,now()))
            ids.append(gid)
    return ids


def allowed(kb, excerpt, grant_id):
    if not grant_id or excerpt.get('source_kind') != 'web':
        return False
    row = kb._con.execute('SELECT * FROM chat_evidence_grants WHERE id=? AND source_id=?',
                          (grant_id,excerpt.get('source_id'))).fetchone()
    source = kb._con.execute('SELECT * FROM evidence_sources WHERE id=?',(excerpt.get('source_id'),)).fetchone()
    if not row or not source:
        return False
    actual_hash = hashlib.sha256(json.dumps([source[k] for k in ('kind','title','url','content')],ensure_ascii=False).encode()).hexdigest()
    chunk = kb._con.execute('SELECT ordinal FROM evidence_chunks WHERE id=? AND source_id=?',
                            (excerpt['id'], source['id'])).fetchone()
    if not chunk or source['content'][chunk['ordinal']*1200:(chunk['ordinal']+1)*1200] != excerpt['text']:
        return False
    return bool(row and row['content_hash'] == excerpt.get('content_hash') and (
        excerpt.get('review_status') == 'pending' or excerpt.get('citable')) and actual_hash == row['content_hash'])


def excerpts(kb, grant_ids):
    hits = []
    for gid in grant_ids:
        row = kb._con.execute('SELECT * FROM chat_evidence_grants WHERE id=?',(gid,)).fetchone()
        if not row:
            raise ValueError('本轮外部证据授权不存在。')
        for chunk in kb._con.execute('SELECT id FROM evidence_chunks WHERE source_id=? ORDER BY ordinal',(row['source_id'],)):
            e = source_chunk(kb, chunk['id'])
            if not allowed(kb, e, gid):
                raise ValueError('本轮搜索来源已撤销或变化，请重新核对。')
            hits.append(dict(e, web_grant_id=gid, material_level='search_summary', acquired_at=row['created_at'],
                             score=1, section='本轮搜索摘要 · 未人工审核' if e['review_status']=='pending' else '本轮搜索摘要 · 人工确认'))
    return hits
