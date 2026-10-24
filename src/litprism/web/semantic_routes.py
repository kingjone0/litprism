"""Version-bound human claim review. Does not alter paper verification status."""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException

from ..evidence import stored_excerpts
from ..knowledge import create_source, get_source, now, review_source
from ..manual_claims import ClaimRejected, add, delete, list_claims, recall
from ..quality import summarize
from ..semantics import payload_hash


def review_report(payload):
    report = dict(payload.get('_semantic_review') or {})
    items = {i['id']: i for i in report.get('items') or []}
    for c in payload.get('_candidates') or []:
        items.setdefault(c['id'], {**c, 'status': 'candidate', 'reason': '未经机器核查，可直接核对原文并人工审核。',
                                   'quotes': [], 'facts': []})
    report['items'] = list(items.values())
    report['pending'] = sum(i.get('status') != 'machine_supported' for i in items.values())
    return report


def snapshot(kb, pid, run=False, version_id=None):
    if run:
        row = kb._con.execute('SELECT * FROM semantic_runs WHERE id=?', (pid,)).fetchone()
        if row is None:
            raise HTTPException(404, '核查记录不存在。')
        paper = {'id': pid, 'title': row['title'], 'url': ''}
        payload = json.loads(row['payload'])
        if str(payload.get('_kind', '')).startswith('qa_'):
            raise HTTPException(409, '对话回复须使用专用审核入口，重新核对所选来源。')
    else:
        paper = kb.resolve_paper(pid)
        if not paper or paper.get('deleted_at'):
            raise HTTPException(404, '文献不存在或已移入回收站。')
        analysis = kb.get_analysis(paper['id'], version_id=version_id) or {}
        if version_id and not analysis:
            raise HTTPException(404, '历史分析不存在。')
        if not version_id:
            from ..candidate_snapshots import pending
            analysis = pending(kb, paper['id'], analysis) or analysis
        payload = analysis.get('payload') or {}
    digest = payload_hash(payload)
    report = review_report(payload)
    column = 'run_id' if run else 'paper_id'
    records = [dict(r) for r in kb._con.execute(
        f'SELECT * FROM semantic_reviews WHERE {column}=? AND analysis_hash=? ORDER BY rowid', (paper['id'], digest))]
    return paper, payload, digest, report, records


def register_semantics(app, kb):
    def read(pid, run=False, version_id=None):
        with kb._tx_lock:
            paper, payload, digest, report, records = snapshot(kb, pid, run, version_id)
            sources = []
            for ref in payload.get('_citations') or []:
                chunk = kb._con.execute('SELECT c.*, p.title AS paper_title FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?', (ref.get('chunk_id'),)).fetchone()
                if chunk:
                    # 带上原文：右栏要展示并高亮"哪一句"支撑了这条陈述。
                    # 只回被引用的分块，不整篇回传。
                    sources.append({'tag': ref['tag'], 'chunk_id': chunk['id'], 'paper_title': chunk['paper_title'],
                                    'page': chunk['page'], 'section': chunk['section'], 'text': chunk['text']})
            # 人工补录：机器漏提的陈述。与机器陈述分开返回 —— 一个是采集，一个是复核。
            manual = [] if run else list_claims(kb, paper['id'], digest)
            machine = len(report.get('items') or [])
            analysis = {} if run else (kb.get_analysis(paper['id'], version_id=version_id) or {})
            if not run and not version_id:
                from ..candidate_snapshots import pending
                analysis = pending(kb, paper['id'], analysis) or analysis
            return {'paper_id': paper['id'], 'analysis_hash': digest, 'report': report, 'reviews': records,
                    'snapshot_id': analysis.get('id') if payload.get('_stage') == 'candidate' else None,
                    'stage': payload.get('_stage', 'analysis'),
                    'material_id': analysis.get('material_id'), 'material_current': analysis.get('material_current'),
                    'sources': sources, 'candidates': payload.get('_candidates') or [],
                    'manual': manual, 'recall': recall(machine, len(manual))}

    def write(pid, claim_id, req, run=False):
        note, action = req.get('note'), req.get('action')
        if action not in ('confirm', 'reject', 'revoke') or not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise HTTPException(400, '请选择审核动作并填写人工核对依据。')
        with kb.tx() as con:
            paper, payload, digest, report, records = snapshot(kb, pid, run)
            if req.get('analysis_hash') != digest:
                raise HTTPException(409, '解读版本已变化，请重新读取陈述与原文后审核。')
            pool = {c['id']: c for c in payload.get('_candidates') or []}
            pool.update({c['id']: c for c in report.get('items') or []})
            claim = pool.get(claim_id)
            if claim is None:
                raise HTTPException(404, '陈述不存在，不能替换成其他陈述。')
            previous = next((r for r in reversed(records) if r['claim_id'] == claim_id), None)
            if previous and previous['action'] == action:
                raise HTTPException(409, '此项已经是该审核状态。')
            if action != 'revoke':
                try:
                    current = kb.get_paper(paper['id']) if not run else None
                    sources_now = stored_excerpts(kb, payload.get('_citations') or [])
                    if payload.get('_stage') == 'candidate' and any(e.get('material_id') != (current or {}).get('current_material_id') for e in sources_now):
                        raise ValueError('审核对象不是当前原文材料。')
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
                tags = {tag for item in pool.values() if item['id'] in related_ids for tag in item.get('candidate_tags') or []}
                if not tags or not tags <= sources.keys():
                    raise HTTPException(409, '陈述缺少可回溯的原文依据，不能确认入库。')
                for tag in sorted(tags):
                    e = sources.get(tag)
                    if e:
                        provenance.append(f"《{e.get('paper_title') or paper['title']}》 p.{e.get('page')} chunk_id={e['id']}\n{e['text']}")
                text = ('人工核对的知识陈述：\n'+reviewed_text.strip()+'\n\n核对依据：'+note.strip()
                        +'\n\n原始来源（非本条陈述的自动证明）：\n'+'\n\n'.join(provenance)
                        +'\n\n解读版本：'+digest+'\n陈述 ID：'+claim_id)
                source = create_source(kb, kind='knowledge', title='人工核对 · '+paper['title'], content=text,
                                        url='', dependencies=[sources[tag] for tag in sorted(tags)], search_text=reviewed_text.strip())
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
    def detail(pid: str, version_id: str | None = None):
        return read(pid, version_id=version_id)

    @app.get('/api/paper/{pid}/quality')
    def quality(pid: str):
        """质量面板：分字段的审核/补录/召回。

        与 ``tools/eval_extraction.py`` 共用 ``litprism.quality`` 的聚合 ——
        同一份数据不允许出现两个数字。
        """
        with kb._tx_lock:
            paper, payload, digest, report, records = snapshot(kb, pid)
            manual = list_claims(kb, paper['id'], digest)
            return {'paper_id': paper['id'], 'analysis_hash': digest,
                    **summarize(report, records, manual, analysis_present=bool(payload))}

    @app.post('/api/paper/{pid}/semantic/claims')
    def add_claim(pid: str, req: dict):
        """人工补录一条机器未提出的陈述。

        必须带原文引用（chunk_id + quote），否则不计入召回 —— 没有依据的补录
        无法与"凭印象写一条"区分。
        """
        with kb.tx() as con:
            paper, payload, digest, _report, _records = snapshot(kb, pid)
            try:
                claim = add(kb, paper['id'], digest, req)
            except ClaimRejected as exc:
                raise HTTPException(exc.status, exc.reason) from exc
            machine = len((payload.get('_semantic_review') or {}).get('items') or [])
            total = len(list_claims(kb, paper['id'], digest))
        return {'ok': True, 'claim': claim, 'recall': recall(machine, total)}

    @app.delete('/api/paper/{pid}/semantic/claims/{claim_id}')
    def drop_claim(pid: str, claim_id: str):
        with kb.tx() as con:
            paper, payload, digest, _report, _records = snapshot(kb, pid)
            if not delete(kb, paper['id'], digest, claim_id):
                raise HTTPException(404, '补录不存在或属于其他解读版本。')
            machine = len((payload.get('_semantic_review') or {}).get('items') or [])
            total = len(list_claims(kb, paper['id'], digest))
        return {'ok': True, 'recall': recall(machine, total)}

    @app.post('/api/paper/{pid}/semantic/{claim_id}/review')
    def review(pid: str, claim_id: str, req: dict):
        return write(pid, claim_id, req)

    @app.get('/api/semantic-runs/{rid}')
    def run_detail(rid: str):
        return read(rid, True)

    @app.post('/api/semantic-runs/{rid}/{claim_id}/review')
    def run_review(rid: str, claim_id: str, req: dict):
        return write(rid, claim_id, req, True)
