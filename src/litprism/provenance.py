"""Structured dependencies; effective availability never rewrites human history."""
from __future__ import annotations

import hashlib
import json


def digest(source):
    return hashlib.sha256(json.dumps([source.get(k) for k in ('paper_id','page','text')],
                                    ensure_ascii=False).encode()).hexdigest()


def bind(kb, sid, excerpts, *, accept_pending=False):
    refs = []
    for e in excerpts:
        refs.append({'chunk_id': e.get('id') or e['chunk_id'], 'paper_id': e.get('paper_id'),
                     'material_id': e.get('material_id'), 'source_id': e.get('source_id'),
                     'content_hash': e.get('content_hash'), 'url': e.get('url'),
                     'hash': digest(e), 'accepted_pending': bool(accept_pending and
                         e.get('source_kind') == 'web' and e.get('web_grant_id'))})
    kb._con.execute('INSERT INTO evidence_lineage(source_id,refs) VALUES (?,?)',
                    (sid, json.dumps(refs, ensure_ascii=False)))


def availability(kb, sid, seen=None):
    seen = set(seen or ())
    if sid in seen:
        return {'citable': False, 'state': 'needs_review', 'issues': ['来源依赖出现循环。'], 'refs': []}
    seen.add(sid)
    row = kb._con.execute('SELECT refs FROM evidence_lineage WHERE source_id=?',(sid,)).fetchone()
    refs = json.loads(row['refs']) if row else []
    issues, warnings, details = [], [], []
    if not row and kb._con.execute('SELECT 1 FROM semantic_reviews WHERE knowledge_id=? LIMIT 1',(sid,)).fetchone():
        issues.append('旧派生知识缺少结构化来源依赖，请依据原文重新核对并生成修订知识。')
    for ref in refs:
        item = dict(ref, issues=[], historical=False)
        details.append(item)
        chunk = kb._con.execute('''SELECT c.*,p.verify_level,p.deleted_at,p.current_material_id,p.title
            FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?''',(ref['chunk_id'],)).fetchone()
        if chunk:
            e = dict(chunk)
            item.update(title=e['title'], page=e['page'], kind='paper')
            if e['deleted_at'] or e['verify_level'] not in ('verified','manual_confirmed'):
                item['issues'].append('上游文献已回收或不再可信。')
            if e['paper_id'] != ref.get('paper_id') or e.get('material_id') != ref.get('material_id') or digest(e) != ref['hash']:
                item['issues'].append('上游原文内容或定位已变化。')
            if e.get('material_id') != e.get('current_material_id'):
                item['historical'] = True
                warnings.append('知识依据为历史材料版本；不能当作当前版本结论。')
        else:
            parent = kb._con.execute('''SELECT c.*,s.status,s.title,s.kind,s.url,s.content_hash FROM evidence_chunks c
                JOIN evidence_sources s ON s.id=c.source_id WHERE c.id=?''',(ref['chunk_id'],)).fetchone()
            if not parent:
                item['issues'].append('上游来源已不存在。')
            else:
                e = dict(parent, paper_id=None, page=None)
                item.update(title=e['title'], page=None, kind=e['kind'], url=e['url'])
                allowed = e['status'] == 'manual_confirmed' or (
                    e['status'] == 'pending' and e['kind'] == 'web' and ref.get('accepted_pending'))
                if not allowed:
                    item['issues'].append('上游知识/网页已拒绝、撤销或未获确认。')
                if (e['source_id'] != ref.get('source_id') or digest(e) != ref['hash']
                        or e.get('content_hash') != ref.get('content_hash') or e.get('url') != ref.get('url')):
                    item['issues'].append('上游快照内容或归属已变化。')
                state = availability(kb, e['source_id'], seen)
                item['issues'].extend(state['issues'])
                if state['state'] == 'historical':
                    item['historical'] = True
                    warnings.append('知识依赖含历史材料版本。')
        issues.extend(item['issues'])
    return {'citable': not issues, 'state': 'needs_review' if issues else 'historical' if warnings else 'current',
            'issues': list(dict.fromkeys(issues)), 'warnings': list(dict.fromkeys(warnings)), 'refs': details}
