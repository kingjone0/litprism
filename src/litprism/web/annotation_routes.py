"""阅读标注接口：原文摘录与批注的增删改查。"""
from __future__ import annotations

from fastapi import HTTPException, Response

from .. import annotations


def register_annotations(app, kb):
    @app.get('/api/paper/{paper_id}/annotations')
    def annotations_list(paper_id: str):
        with kb._tx_lock:
            paper = kb.resolve_paper(paper_id)
            if not paper:
                raise HTTPException(404, '未找到论文')
            return {'items': annotations.list_for(kb, paper['id'])}

    @app.get('/api/annotations')
    def annotations_all(paper_id: str | None = None, limit: int = 200):
        with kb._tx_lock:
            pid = None
            if paper_id:
                paper = kb.resolve_paper(paper_id)
                if not paper:
                    raise HTTPException(404, '未找到论文')
                pid = paper['id']
            return {'items': annotations.list_all(kb, paper_id=pid, limit=limit)}

    @app.post('/api/paper/{paper_id}/annotations')
    def annotations_add(paper_id: str, body: dict):
        with kb._tx_lock:
            paper = kb.resolve_paper(paper_id)
            if not paper:
                raise HTTPException(404, '未找到论文')
            try:
                page = body.get('page')
                return annotations.add(
                    kb, paper['id'],
                    quote=body.get('quote') or '',
                    note=body.get('note') or '',
                    chunk_id=body.get('chunk_id') or None,
                    page=int(page) if page not in (None, '') else None,
                    material_id=body.get('material_id') or None,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.patch('/api/annotations/{annotation_id}')
    def annotations_update(annotation_id: str, body: dict):
        with kb._tx_lock:
            try:
                return annotations.update(kb, annotation_id, note=(body or {}).get('note') or '')
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.delete('/api/annotations/{annotation_id}')
    def annotations_delete(annotation_id: str):
        with kb._tx_lock:
            try:
                annotations.delete(kb, annotation_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            return {'ok': True}


    @app.get('/api/paper/{paper_id}/annotations/export')
    def annotations_export(paper_id: str, format: str = 'md'):
        from urllib.parse import quote as urlquote

        with kb._tx_lock:
            paper = kb.resolve_paper(paper_id)
            if not paper:
                raise HTTPException(404, '未找到论文')
            if format == 'csv':
                body, media, ext = annotations.to_csv(kb, paper['id']), 'text/csv', 'csv'
            elif format == 'md':
                body, media, ext = annotations.to_markdown(kb, paper['id']), 'text/markdown', 'md'
            else:
                raise HTTPException(400, '仅支持 md 或 csv。')
            name = f'annotations-{paper["id"]}.{ext}'
            return Response(content=body.encode('utf-8'), media_type=f'{media}; charset=utf-8',
                            headers={'Content-Disposition': f"attachment; filename*=UTF-8''{urlquote(name)}"})
