"""统一来源详情和右栏人工审核入口。"""
from __future__ import annotations

from fastapi import HTTPException

from ..knowledge import create_source, get_source, review_source, source_chunk
from ..whitelist import is_citable


def evidence_detail(kb, ref):
    with kb._tx_lock:
        return _evidence_detail(kb, ref)


def _evidence_detail(kb, ref):
    chunk = source_chunk(kb, ref)
    source = get_source(kb, chunk['source_id'] if chunk else ref)
    if source:
        return {**source, 'source_kind': source['kind'], 'trust_level': source['status'],
                'text': chunk['text'] if chunk else source['content'][:5000], 'page': None,
                'citable': source['status'] == 'manual_confirmed', 'chunk_id': chunk['id'] if chunk else None}
    row = kb._con.execute('''SELECT c.*,p.title,p.url,p.doi,p.authors,p.verify_level,p.verify_evidence,
                      p.deleted_at FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?''', (ref,)).fetchone()
    if row is None:
        raise HTTPException(404, '来源已不存在，不能替换为其他来源。')
    data = dict(row)
    return {**data, 'source_kind': 'paper', 'chunk_id': data['id'], 'trust_level': 'deleted' if data['deleted_at'] else data['verify_level'],
            'citable': not data['deleted_at'] and is_citable(data['verify_level'])}


def register_evidence(app, kb, cfg, verifier):
    @app.get('/api/evidence')
    def sources():
        return {'items': [dict(r) for r in kb._con.execute('SELECT id,kind,title,url,status,created_at FROM evidence_sources ORDER BY rowid DESC LIMIT 300')]}

    @app.post('/api/evidence')
    def create(req: dict):
        try:
            return create_source(kb, kind=req.get('kind'), title=req.get('title'), content=req.get('content'), url=req.get('url'))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/evidence/search')
    def search(req: dict):
        return discover_web(kb, cfg, req.get('query'))

    @app.get('/api/evidence/{ref}')
    def detail(ref: str):
        return evidence_detail(kb, ref)

    @app.post('/api/evidence/{ref}/review')
    def review(ref: str, req: dict):
        current = evidence_detail(kb, ref)
        if current['source_kind'] == 'paper':
            from ..whitelist import Whitelist
            if current.get('deleted_at'):
                raise HTTPException(409, '文献已在回收站，请先恢复。')
            if req.get('action') != 'confirm' or not str(req.get('note') or '').strip():
                raise HTTPException(400, '论文放行需要填写人工确认依据。')
            latest = verifier.verify(title=current['title'], doi=current.get('doi'))
            try:
                Whitelist(kb).add(title=current['title'], doi=current.get('doi'), reason=req['note'],
                                  level_at_confirm=latest.level, verify_evidence=latest.to_dict())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            try:
                review_source(kb, current['id'], action=req.get('action'), note=req.get('note'), content_hash=req.get('content_hash'))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return evidence_detail(kb, ref)


def discover_web(kb, cfg, query):
    import requests

    from ..secrets import SecretStore
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise HTTPException(400, '搜索问题必须是 1–2000 字符的文本。')
    query = query.strip()
    if cfg.websearch.provider != 'tavily':
        raise HTTPException(503, '当前未启用 Tavily 网页候选搜索。')
    key = SecretStore(cfg.data_path).get(cfg.websearch.api_key_ref)
    if not key:
        raise HTTPException(503, '网页搜索密钥未配置或无法解密；可先手动保存网页原文片段。')
    try:
        response = requests.post('https://api.tavily.com/search', headers={'Authorization': 'Bearer '+key},
            json={'query': query, 'search_depth': 'basic', 'max_results': min(10, max(1, cfg.websearch.max_results)),
                  'include_answer': False, 'include_raw_content': False}, timeout=30)
        response.raise_for_status()
        records = response.json().get('results') or []
        if not isinstance(records, list):
            raise ValueError('Invalid results schema')
    except Exception as exc:
        raise HTTPException(502, '网页搜索失败，请检查服务配置或稍后重试。') from exc
    items = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            items.append(create_source(kb, kind='web', title=record.get('title'), content=record.get('content'), url=record.get('url')))
        except ValueError:
            continue
    return {'items': items, 'note': '搜索摘要候选，尚未人工审核，不作为知识库事实依据。'}
