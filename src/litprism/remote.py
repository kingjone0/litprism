"""从候选取全文：目前只支持 arXiv 直链 PDF。

边界（故意保守）：
- 只允许 arXiv 官方主机，跟随重定向后仍要复查主机；
- 只写本库数据目录下的 ``papers/``，文件名用规范化 arXiv ID；
- 下载后先验 ``%PDF`` 魔数与大小上限，再交给现有 ``load_document`` 解析；
- 解析/入库失败不会把候选标成 imported，也不会留下半条 paper 记录以外的状态。
"""
from __future__ import annotations

import re
from pathlib import Path

from .candidates import set_candidate_status
from .config import Config
from .db import KnowledgeBase, material_label

ARXIV_HOSTS = ('arxiv.org', 'export.arxiv.org', 'www.arxiv.org')
MAX_BYTES = 80 * 1024 * 1024
_ARXIV_ID = re.compile(r'(?<!\d)(\d{4}\.\d{4,5})(v\d+)?')


def arxiv_id_of(record: dict) -> str | None:
    """从 arxiv_id 字段或 URL/DOI 里取出规范化的 arXiv ID（保留版本号）。"""
    raw = (record.get('arxiv_id') or '').strip()
    if raw:
        text = raw
    else:
        text = f"{record.get('url') or ''} {record.get('doi') or ''}"
    if not text.strip():
        return None
    text = text.replace('arXiv:', '').replace('arxiv:', '')
    # DOI 形式 10.48550/arXiv.1607.01759：先剥掉前缀，否则点号会挡住匹配
    text = re.sub(r'10\.48550/arxiv\.?', '', text, flags=re.I)
    match = _ARXIV_ID.search(text)
    if not match:
        return None
    return match.group(1) + (match.group(2) or '')


def arxiv_pdf_url(arxiv_id: str) -> str:
    return f'https://arxiv.org/pdf/{arxiv_id}'


def _check_host(url: str) -> None:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or '').lower()
    if host not in ARXIV_HOSTS:
        raise ValueError(f'拒绝从非 arXiv 主机下载：{host or "未知"}')


def download_pdf(url: str, dest: Path, *, timeout: int = 60) -> Path:
    """下载并做最小校验；失败抛 ValueError（调用方据此决定是否改候选状态）。"""
    import requests

    _check_host(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.part')
    with requests.get(url, stream=True, timeout=timeout,
                      headers={'User-Agent': 'LitPrism/1.0 (local research tool)'}) as resp:
        if resp.status_code >= 400:
            raise ValueError(f'下载失败：HTTP {resp.status_code}')
        final = getattr(resp, 'url', url) or url
        _check_host(final)  # 防重定向到别处
        size = 0
        with tmp.open('wb') as fh:
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_BYTES:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise ValueError('文件超过 80 MB 上限，已中止。')
                fh.write(chunk)
    head = tmp.open('rb').read(5)
    if not head.startswith(b'%PDF'):
        tmp.unlink(missing_ok=True)
        raise ValueError('下载内容不是 PDF（缺少 %PDF 头），已丢弃。')
    tmp.replace(dest)
    return dest


def fetch_candidate_pdf(kb: KnowledgeBase, cfg: Config, verifier, cid: str, *, material_policy: str = 'replace') -> dict:
    """把一条候选取成可解读的论文：下载 → 解析 → 建论文/原文块 → 回链候选。"""
    rows = kb._con.execute('SELECT * FROM source_candidates WHERE id=?', (cid,)).fetchone()
    if rows is None:
        raise ValueError('候选不存在。')
    candidate = dict(rows)
    arxiv_id = arxiv_id_of(candidate)
    if not arxiv_id:
        raise ValueError('该候选没有 arXiv 标识，暂不支持自动取全文（请手动下载后导入）。')

    target = Path(cfg.data_path) / 'papers' / f'{arxiv_id.replace("/", "_")}.pdf'
    reused = target.exists() and target.stat().st_size > 0
    if not reused:
        download_pdf(arxiv_pdf_url(arxiv_id), target)

    from .ingest import chunk_document, load_document

    doc = load_document(target)
    if not (doc.full_text or '').strip():
        raise ValueError('PDF 已下载但未能解析出文本（可能是扫描件），未入库。')

    result = verifier.verify(title=doc.title or candidate['title'], arxiv_id=arxiv_id,
                             authors=doc.authors or None, year=doc.year)
    from .evidence import require_citable
    require_citable(result.level)
    with kb.tx():
        pid = kb.add_paper(
            title=doc.title or candidate['title'],
            authors=doc.authors or candidate.get('authors') or [],
            year=doc.year or candidate.get('year'),
            venue=doc.venue,
            abstract=doc.abstract or candidate.get('abstract'),
            doi=doc.doi or candidate.get('doi'),
            arxiv_id=arxiv_id,
            url=candidate.get('url'),
            source='arxiv',
            origin=arxiv_pdf_url(arxiv_id),
            full_text=doc.full_text,
            material_level='fulltext',
            tags=['arXiv 自动取回'],
            verify_status=result.level, verify_level=result.level,
            verify_confidence=float(result.confidence), verify_evidence=result.to_dict(),
        )
        chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
        added = kb.add_chunks(pid, chunks, policy=material_policy, level='fulltext', source='arxiv',
                              origin=arxiv_pdf_url(arxiv_id), full_text=doc.full_text)
    set_candidate_status(kb, cid, 'imported', paper_id=pid)
    return {'paper_id': pid, 'arxiv_id': arxiv_id, 'reused_file': reused,
            'chunks': added, 'pages': len(doc.pages),
            'verify_level': result.level, 'material_level': material_label('fulltext'),
            'title': doc.title or candidate['title'], 'warnings': doc.warnings}


def import_candidate(kb: KnowledgeBase, cfg: Config, verifier, cid: str, *, material_policy: str = 'replace') -> dict:
    """把一条候选导入知识库。

    - 有 arXiv 标识：直接走取全文（题录导入对它是降级）；
    - 其余论文：只导入题录/摘要，必须过真实性核验；不通过就拒绝，不做"先入库再说"。
    """
    row = kb._con.execute('SELECT * FROM source_candidates WHERE id=?', (cid,)).fetchone()
    if row is None:
        raise ValueError('候选不存在。')
    candidate = dict(row)
    if candidate.get('kind') != 'paper':
        raise ValueError('只有论文类候选可以导入知识库；网页请保存为来源快照。')
    if arxiv_id_of(candidate):
        return fetch_candidate_pdf(kb, cfg, verifier, cid, material_policy=material_policy)

    import json as _json
    authors = candidate.get('authors')
    authors = _json.loads(authors) if isinstance(authors, str) else (authors or [])
    result = verifier.verify(title=candidate['title'], doi=candidate.get('doi'),
                             authors=authors or None, year=candidate.get('year'), log=True)
    if not result.exists or (not result.best_match and not result.manual):
        raise ValueError(f'未通过核验，拒绝导入。{result.message}')

    from .search import import_record
    from .verify import SourceRecord

    best = result.best_match or SourceRecord(source='whitelist', title=candidate['title'],
                                             doi=candidate.get('doi'))
    pid, created = import_record(
        kb,
        {'title': best.title or candidate['title'], 'authors': best.authors, 'year': best.year,
         'venue': best.venue, 'doi': best.doi or candidate.get('doi'), 'url': best.url or candidate.get('url'),
         'abstract': best.abstract or candidate.get('abstract'), 'origin': 'candidate_pool'},
        verification=result, tags=['候选导入'],
    )
    set_candidate_status(kb, cid, 'imported', paper_id=pid)
    return {'paper_id': pid, 'created': created, 'title': best.title or candidate['title'],
            'verification': result.to_dict(), 'material_level': material_label('abstract'),
            'chunks': len(kb.get_chunks(pid))}
