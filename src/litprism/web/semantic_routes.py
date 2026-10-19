"""Version-bound human claim review. Does not alter paper verification status."""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException

from ..evidence import stored_excerpts
from ..knowledge import create_source, get_source, now, review_source
from ..semantics import payload_hash


def snapshot(kb, pid, run=False):
    if run:
        row = kb._con.execute('SELECT * FROM semantic_runs WHERE id=?', (pid,)).fetchone()
        if row is None:
            raise HTTPException(404, '核查记录不存在。')
        paper = {'id': pid, 'title': row['title'], 'url': ''}
        payload = json.loads(row['payload'])
    else:
        paper = kb.resolve_paper(pid)
        if not paper or paper.get('deleted_at'):
            raise HTTPException(404, '文献不存在或已移入回收站。')
        payload = ((kb.get_analysis(paper['id']) or {}).get('payload') or {})
    digest = payload_hash(payload)
    report = payload.get('_semantic_review') or {}
    column = 'run_id' if run else 'paper_id'
    records = [dict(r) for r in kb._con.execute(
        f'SELECT * FROM semantic_reviews WHERE {column}=? AND analysis_hash=? ORDER BY rowid', (paper['id'], digest))]
    return paper, payload, digest, report, records


def register_semantics(app, kb):
    def read(pid, run=False):
        with kb._tx_lock:
            paper, payload, digest, report, records = snapshot(kb, pid, run)
            sources = []
            for ref in payload.get('_citations') or []:
                chunk = kb._con.execute('SELECT c.*, p.title AS paper_title FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?', (ref.get('chunk_id'),)).fetchone()
                if chunk:
                    # 带上原文：右栏要展示并高亮"哪一句"支撑了这条陈述。
                    # 只回被引用的分块，不整篇回传。
                    sources.append({'tag': ref['tag'], 'chunk_id': chunk['id'], 'paper_title': chunk['paper_title'],
                                    'page': chunk['page'], 'section': chunk['section'], 'text': chunk['text']})
            return {'paper_id': paper['id'], 'analysis_hash': digest, 'report': report, 'reviews': records,
                    'sources': sources, 'candidates': payload.get('_candidates') or []}

    def write(pid, claim_id, req, run=False):
        note, action = req.get('note'), req.get('action')
        if action not in ('confirm', 'reject', 'revoke') or not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise HTTPException(400, '请选择审核动作并填写人工核对依据。')
        with kb.tx() as con:
            paper, payload, digest, report, records = snapshot(kb, pid, run)
            if req.get('analysis_hash') != digest:
                raise HTTPException(409, '解读版本已变化，请重新读取陈述与原文后审核。')
            claim = next((c for c in report.get('items', []) if c['id'] == claim_id), None)
            if claim is None:
                raise HTTPException(404, '陈述不存在，不能替换成其他陈述。')
            previous = next((r for r in reversed(records) if r['claim_id'] == claim_id), None)
            if previous and previous['action'] == action:
                raise HTTPException(409, '此项已经是该审核状态。')
            if action != 'revoke':
                try:
                    stored_excerpts(kb, payload.get('_citations') or [])
                except ValueError as exc:
                    raise HTTPException(409, '来源状态已变化，请重新核对。') from exc
            source_id = None
            reviewed_text = req.get('reviewed_text')
            if action == 'confirm':
                if not isinstance(reviewed_text, str) or not reviewed_text.strip() or len(reviewed_text) > 20000:
                    raise HTTPException(400, '请填写已核对并必要时修正后的知识正文（最多 2 万字符）。')
                sources = {e['tag']: e for e in stored_excerpts(kb, payload.get('_citations') or [])}
                provenance = []
                related_ids = {claim_id}
                for difference in report.get('differences', []):
                    if claim_id in difference['claim_ids']:
                        related_ids.update(difference['claim_ids'])
                tags = {tag for item in report['items'] if item['id'] in related_ids for tag in item['candidate_tags']}
                for tag in sorted(tags):
                    e = sources.get(tag)
                    if e:
                        provenance.append(f"《{e.get('paper_title') or paper['title']}》 p.{e.get('page')} chunk_id={e['id']}\n{e['text']}")
                text = ('人工核对的知识陈述：\n'+reviewed_text.strip()+'\n\n核对依据：'+note.strip()
                        +'\n\n原始来源（非本条陈述的自动证明）：\n'+'\n\n'.join(provenance)
                        +'\n\n解读版本：'+digest+'\n陈述 ID：'+claim_id)
                source = create_source(kb, kind='knowledge', title='人工核对 · '+paper['title'], content=text,
                                       url='')
                review_source(kb, source['id'], action='confirm', note=note, content_hash=source['content_hash'])
                source_id = source['id']
            if previous and previous.get('knowledge_id') and action in ('reject', 'revoke'):
                source = get_source(kb, previous['knowledge_id'])
                if source and source['status'] == 'manual_confirmed':
                    review_source(kb, source['id'], action='revoke', note=note, content_hash=source['content_hash'])
                source_id = previous['knowledge_id']
            if action == 'revoke' and not source_id:
                raise HTTPException(409, '该项没有可撤销的入库记录。')
            con.execute('INSERT INTO semantic_reviews VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('sr_'+uuid.uuid4().hex[:12], None if run else paper['id'], paper['id'] if run else None, digest, claim_id, action,
                         note.strip(), reviewed_text if isinstance(reviewed_text, str) else None, source_id, now()))
        return {'ok': True, 'knowledge_id': source_id, 'analysis_hash': digest}

    @app.get('/api/paper/{pid}/semantic')
    def detail(pid: str):
        return read(pid)

    @app.post('/api/paper/{pid}/semantic/{claim_id}/review')
    def review(pid: str, claim_id: str, req: dict):
        return write(pid, claim_id, req)

    @app.get('/api/semantic-runs/{rid}')
    def run_detail(rid: str):
        return read(rid, True)

    @app.post('/api/semantic-runs/{rid}/{claim_id}/review')
    def run_review(rid: str, claim_id: str, req: dict):
        return write(rid, claim_id, req, True)
