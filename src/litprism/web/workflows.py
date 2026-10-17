"""桌面研究任务入口。生成仍走内核，下载前统一复核引用。"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime

from fastapi import HTTPException
from fastapi.responses import Response

from ..deliverables import DeliverableStore, _safe_filename
from ..evidence import require_citable


def register_workflows(app, kb, cfg, task_llm):
    @app.post('/api/skills/{skill_id}/run')
    def run_skill(skill_id: str, req: dict):
        from ..skills.runner import SkillRunner
        r = SkillRunner(kb, cfg, llm=task_llm()).run(skill_id, req.get('inputs') or {}, dry_run=bool(req.get('dry_run')))
        return {**r.to_dict(), 'answer': r.answer, 'excerpts': r.excerpts}

    @app.post('/api/deliverables')
    def create_deliverable(req: dict):
        store = DeliverableStore(kb)
        from ..deliverables import Deliverable
        draft = Deliverable(id='', type=req.get('type') or 'notes', title=req.get('title') or '',
                            content=req.get('content') or '', citations=req.get('citations') or [],
                            source_scope=req.get('source_scope') or {})
        try:
            store.validate_export(draft)
            did = store.add(type=draft.type, title=draft.title, content=draft.content,
                            citations=draft.citations, source_scope=draft.source_scope, model=req.get('model'))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'id': did}

    @app.get('/api/deliverables/{did}/download')
    def download_deliverable(did: str):
        store = DeliverableStore(kb)
        d = store.get(did)
        if d is None:
            raise HTTPException(404, '交付物不存在')
        try:
            store.validate_export(d)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        content = store.render_markdown(d)
        if d.status == 'draft':
            store.update(d.id, status='exported')
        return Response(content, media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="litprism-notes.md"'})

    @app.get('/api/deliverables-archive')
    def download_archive():
        store = DeliverableStore(kb)
        items = store.list(limit=10000)
        if not items:
            raise HTTPException(400, '没有可导出的交付物')
        try:
            for d in items:
                store.validate_export(d)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for d in items:
                archive.writestr(_safe_filename(d.title)+'-'+d.id+'.md', store.render_markdown(d))
        for d in items:
            if d.status == 'draft':
                store.update(d.id, status='exported')
        return Response(output.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="litprism-deliverables.zip"'})

    @app.post('/api/library/export')
    def export_bibliography(req: dict):
        ids = req.get('paper_ids')
        papers = [kb.get_paper(pid) for pid in ids] if ids is not None else kb.list_papers(limit=10000)
        fmt = req.get('format') or 'bibtex'
        if fmt not in ('bibtex', 'ris', 'json', 'markdown'):
            raise HTTPException(400, '不支持的导出格式')
        if not papers or any(p is None for p in papers):
            raise HTTPException(400, '请先选择存在的文献')
        try:
            for p in papers:
                require_citable(p.get('verify_level'))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        texts = []
        if fmt == 'json':
            return Response(json.dumps(papers, ensure_ascii=False, indent=2), media_type='application/json')
        for p in papers:
            if fmt == 'markdown':
                from ..extract import format_analysis
                a = (kb.get_analysis(p['id']) or {}).get('payload') or {}
                if not a.get('_citation_check', {}).get('passed'):
                    raise HTTPException(409, '解读未通过引用检查，请重新解读。')
                from ..deliverables import Deliverable
                d = Deliverable(id='', type='notes', title=p['title'], content=format_analysis(p, a), citations=a.get('_citations') or [])
                store = DeliverableStore(kb)
                try:
                    store.validate_export(d)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
                texts.append(store.render_markdown(d))
            elif fmt == 'ris':
                def line(value):
                    return str(value or '').replace('\n', ' ').replace('\r', ' ')
                parts = ['TY  - JOUR', 'TI  - '+line(p['title'])]
                parts += ['AU  - '+line(a) for a in p['authors']]
                parts += [f'PY  - {line(p.get("year"))}', f'DO  - {line(p.get("doi"))}', 'ER  - ']
                texts.append('\n'.join(parts))
            else:
                def esc(value):
                    return str(value or '').replace('\\', '\\textbackslash{}').replace('{', '\\{').replace('}', '\\}').replace('\n', ' ')
                fields = {'title': p['title'], 'author': ' and '.join(p['authors']), 'year': p.get('year'), 'doi': p.get('doi'), 'journal': p.get('venue')}
                texts.append('@article{'+p['id']+',\n'+',\n'.join('  '+k+' = {'+esc(v)+'}' for k, v in fields.items() if v)+'\n}')
        return Response('\n\n'.join(texts), media_type='text/plain; charset=utf-8')

    @app.get('/api/restore/{block_id}')
    def restore_block(block_id: str):
        from ..context.restore import Restorer
        r = Restorer(kb, cfg).restore_block(block_id)
        if not r.ok:
            raise HTTPException(404, r.error)
        return {'messages': r.messages, 'citations': r.citations, 'missing_chunks': r.missing_chunks}

    @app.patch('/api/memories/{mid}')
    def update_memory(mid: str, req: dict):
        from ..memory.store import MemoryStore
        store = MemoryStore(kb, cfg)
        try:
            ok = store.update(mid, **{k: v for k, v in req.items() if k in ('value', 'kind', 'key', 'pinned', 'priority')})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not ok:
            raise HTTPException(404, '偏好不存在')
        return store.get(mid).to_dict()

    @app.post('/api/memories/{mid}/restore')
    def restore_memory(mid: str):
        from ..memory.store import MemoryStore
        return {'ok': MemoryStore(kb, cfg).restore(mid)}

    @app.get('/api/collections')
    def collections():
        return {'items': [dict(r) for r in kb._con.execute('SELECT * FROM collections WHERE deleted_at IS NULL ORDER BY sort_order,name')]}

    @app.post('/api/collections')
    def create_collection(req: dict):
        name = str(req.get('name') or '').strip()
        if not name:
            raise HTTPException(400, '分组名称不能为空')
        cid = 'col_'+uuid.uuid4().hex[:12]
        with kb.tx() as con:
            con.execute('INSERT INTO collections (id,name,sort_order,created_at) VALUES (?,?,?,?)',
                        (cid, name, int(req.get('sort_order') or 0), datetime.now().isoformat()))
        return {'id': cid}

    @app.patch('/api/collections/{cid}')
    def update_collection(cid: str, req: dict):
        if not kb._con.execute('SELECT id FROM collections WHERE id=? AND deleted_at IS NULL', (cid,)).fetchone():
            raise HTTPException(404, '分组不存在')
        with kb.tx() as con:
            if 'name' in req:
                name = str(req['name']).strip()
                if not name:
                    raise HTTPException(400, '分组名称不能为空')
                con.execute('UPDATE collections SET name=? WHERE id=?', (name, cid))
            if 'sort_order' in req:
                con.execute('UPDATE collections SET sort_order=? WHERE id=?', (int(req['sort_order']), cid))
        return {'ok': True}

    @app.delete('/api/collections/{cid}')
    def delete_collection(cid: str):
        with kb.tx() as con:
            con.execute('UPDATE papers SET collection_id=NULL WHERE collection_id=?', (cid,))
            con.execute('UPDATE collections SET deleted_at=? WHERE id=?', (datetime.now().isoformat(), cid))
        return {'ok': True}

    @app.get('/api/trash')
    def trash():
        return {'items': [kb._row_to_paper(r) for r in kb._con.execute('SELECT * FROM papers WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC')]}

    @app.post('/api/trash/{pid}/restore')
    def restore_paper(pid: str):
        with kb.tx() as con:
            cur = con.execute('UPDATE papers SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL', (pid,))
        return {'ok': cur.rowcount > 0}
