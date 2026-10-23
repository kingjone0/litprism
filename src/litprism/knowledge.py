"""非论文证据：不可变快照、人工审核和原文检索。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def source_chunk(kb, cid):
    row = kb._con.execute('''SELECT c.id, c.id AS chunk_id, c.source_id, c.text,
             s.kind AS source_kind, s.title AS paper_title, s.url, s.status AS trust_level,
             s.content_hash, s.review_note, s.reviewed_at, NULL AS paper_id, NULL AS page,
             '原文快照' AS section
             FROM evidence_chunks c JOIN evidence_sources s ON s.id=c.source_id WHERE c.id=?''', (cid,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    from .provenance import availability
    dep = availability(kb, result['source_id'])
    result['review_status'] = result['trust_level']
    result['citable'] = result['trust_level'] == 'manual_confirmed' and dep['citable']
    result['dependency_status'] = dep
    if result['trust_level'] == 'manual_confirmed' and dep['state'] != 'current':
        result['trust_level'] = dep['state']
    return result


def create_source(kb, *, kind, title, content, url='', dependencies=None, accept_pending=False, search_text=None):
    if kind not in ('web', 'knowledge'):
        raise ValueError('来源类型必须是网页或知识条目。')
    if not isinstance(title, str) or not isinstance(content, str) or (url is not None and not isinstance(url, str)):
        raise ValueError('标题、原文和 URL 必须是文本。')
    title, content, url = title.strip(), content.strip(), (url or '').strip()
    if not title or not content or len(content) > 400000:
        raise ValueError('标题与原文不能为空；单条原文最多 40 万字符。')
    if search_text is not None and (not isinstance(search_text,str) or not search_text.strip() or len(search_text)>400000):
        raise ValueError('可检索的人工正文必须为非空文本。')
    indexed = content if search_text is None else search_text.strip()
    if (url or kind == 'web') and (urlparse(url).scheme not in ('http', 'https') or not urlparse(url).hostname):
        raise ValueError('网页来源必须提供有效的 http/https 地址。')
    sid = 'ev_'+uuid.uuid4().hex[:12]
    digest = hashlib.sha256(json.dumps([kind, title, url, content], ensure_ascii=False).encode()).hexdigest()
    with kb.tx() as con:
        con.execute('INSERT INTO evidence_sources (id,kind,title,url,content,content_hash,status,created_at) VALUES (?,?,?,?,?,?,?,?)',
                    (sid, kind, title, url, content, digest, 'pending', now()))
        if dependencies is not None:
            from .provenance import bind
            bind(kb, sid, dependencies, accept_pending=accept_pending)
        for ordinal, start in enumerate(range(0, len(indexed), 1200)):
            con.execute('INSERT INTO evidence_chunks (id,source_id,ordinal,text) VALUES (?,?,?,?)',
                        ('ec_'+uuid.uuid4().hex[:12], sid, ordinal, indexed[start:start+1200]))
    return get_source(kb, sid)


def get_source(kb, sid):
    row = kb._con.execute('SELECT * FROM evidence_sources WHERE id=?', (sid,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['chunks'] = [dict(r) for r in kb._con.execute('SELECT id,ordinal,text FROM evidence_chunks WHERE source_id=? ORDER BY ordinal', (sid,))]
    result['reviews'] = [dict(r) for r in kb._con.execute('SELECT * FROM evidence_reviews WHERE source_id=? ORDER BY rowid', (sid,))]
    from .provenance import availability
    result['dependency_status'] = availability(kb, sid)
    result['citable'] = result['status'] == 'manual_confirmed' and result['dependency_status']['citable']
    result['effective_status'] = (result['dependency_status']['state'] if result['status']=='manual_confirmed'
                                  and result['dependency_status']['state'] != 'current' else result['status'])
    return result


def review_source(kb, sid, *, action, note, content_hash):
    if action not in ('confirm', 'reject', 'revoke') or not isinstance(note, str) or not note.strip():
        raise ValueError('请选择审核动作并填写依据。')
    with kb.tx() as con:
        row = con.execute('SELECT * FROM evidence_sources WHERE id=?', (sid,)).fetchone()
        if row is None or content_hash != row['content_hash']:
            raise ValueError('来源不存在或内容版本已变化，请重新查看原文后审核。')
        if action == 'confirm':
            from .provenance import availability
            state = availability(kb, sid)
            if not state['citable']:
                raise ValueError('依据失效，需依据当前原文修订知识：'+'；'.join(state['issues']))
        status = {'confirm': 'manual_confirmed', 'reject': 'rejected', 'revoke': 'revoked'}[action]
        if row['status'] == status:
            raise ValueError('当前来源已经是该状态，请刷新查看审核记录。')
        con.execute('UPDATE evidence_sources SET status=?,reviewed_at=?,review_note=? WHERE id=?', (status, now(), note.strip(), sid))
        con.execute('INSERT INTO evidence_reviews VALUES (?,?,?,?,?,?)',
                    ('er_'+uuid.uuid4().hex[:12], sid, content_hash, action, note.strip(), now()))
    return get_source(kb, sid)


def search_knowledge(kb, query, top_k):
    tokens = re.findall(r'[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}', query.lower())
    terms = []
    for token in tokens:
        terms.append(token)
        if re.search(r'[\u4e00-\u9fff]', token):
            terms.extend(token[i:i+2] for i in range(len(token)-1))
    terms = list(dict.fromkeys(terms))[:60]
    if not terms:
        return []
    rows = kb._con.execute('''SELECT c.id,c.source_id,c.text,s.title AS paper_title,s.kind AS source_kind,
          s.url, NULL AS paper_id,NULL AS page,'原文快照' AS section
          FROM evidence_chunks c JOIN evidence_sources s ON s.id=c.source_id WHERE s.status='manual_confirmed' ''')
    hits = []
    for row in rows:
        hit = dict(row)
        actual = source_chunk(kb, hit['id'])
        if not actual['citable']:
            continue
        hit.update(dependency_status=actual['dependency_status'], trust_level=actual['trust_level'])
        text = (hit['paper_title']+' '+hit['text']).lower()
        matched = sum(term in text for term in terms)
        if matched:
            hit['score'] = matched/len(terms)
            hits.append(hit)
    return sorted(hits, key=lambda h: -h['score'])[:top_k]
