"""从候选取全文：arXiv 直链 + 合法开放副本（Unpaywall / OpenAlex）。

边界（故意保守）：
- 付费墙内容不绕过、不使用任何账户凭据，查不到就明确失败；
- 下载主机必须是公网 http(s)，拒绝本机/内网地址（防 SSRF），重定向后再校验一次；
- 下载后先验 ``%PDF`` 魔数与大小上限，再交给现有 ``load_document`` 解析；
- 解析/入库失败不会把候选标成 imported，也不会留下半条 paper 记录以外的状态。
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

from .candidates import set_candidate_status
from .config import Config
from .db import KnowledgeBase, material_label
from .fulltext import resolve_open_copy
from .textutil import normalize_doi

ARXIV_HOSTS = ('arxiv.org', 'export.arxiv.org', 'www.arxiv.org')
MAX_BYTES = 80 * 1024 * 1024
# 不少出版社对非浏览器 UA 直接 403；保留产品标识但用浏览器前缀
_DOWNLOAD_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0 Safari/537.36 LitPrism/1.0'),
    'Accept': 'application/pdf,application/octet-stream;q=0.9,*/*;q=0.8',
}
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


class FulltextUnavailable(ValueError):
    """没有可用的开放全文；status 用于区分"没有"和"没拿到"。"""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _check_arxiv_host(url: str) -> None:
    host = (urlparse(url).hostname or '').lower()
    if host not in ARXIV_HOSTS:
        raise ValueError(f'拒绝从非 arXiv 主机下载：{host or "未知"}')


def _check_public_url(url: str) -> None:
    """开放副本可能落在任意出版社域名，因此改为"必须是公网 http(s)"。"""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f'拒绝非 http(s) 链接：{url}')
    host = (parsed.hostname or '').lower()
    if not host or host == 'localhost' or host.endswith('.local'):
        raise ValueError(f'拒绝本机/本地域名：{host or "未知"}')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名，交给 DNS 与 TLS；重定向时会再校验一次
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError(f'拒绝内网地址：{host}')


def download_pdf(url: str, dest: Path, *, timeout: int = 60, allow_public: bool = False) -> Path:
    """下载并做最小校验；失败抛 ValueError（调用方据此决定是否改候选状态）。

    默认只允许 arXiv 主机；开放副本必须显式 ``allow_public=True``（公网 http(s) + 禁内网）。
    """
    import requests

    check = _check_public_url if allow_public else _check_arxiv_host
    check(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.part')
    try:
        resp = requests.get(url, stream=True, timeout=timeout,
                            headers=_DOWNLOAD_HEADERS)
    except requests.RequestException as exc:
        raise ValueError(f'下载失败：网络错误（{exc.__class__.__name__}）') from exc
    with resp:
        if resp.status_code >= 400:
            raise ValueError(f'下载失败：HTTP {resp.status_code}')
        final = getattr(resp, 'url', url) or url
        check(final)  # 防重定向到别处
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


def _record_provenance(kb: KnowledgeBase, cid: str, *, provider: str, url: str,
                       license: str | None = None) -> None:
    """把"这篇全文从哪来"写进候选的 extra，供后续审计。"""
    import json
    from datetime import datetime, timezone

    row = kb._con.execute('SELECT extra FROM source_candidates WHERE id=?', (cid,)).fetchone()
    data = {}
    if row and row['extra']:
        try:
            data = json.loads(row['extra'])
        except (TypeError, ValueError):
            data = {}
    data['fulltext'] = {'provider': provider, 'url': url, 'license': license,
                        'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    with kb.tx() as con:
        con.execute('UPDATE source_candidates SET extra=?, updated_at=? WHERE id=?',
                    (json.dumps(data, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(timespec='seconds'), cid))


def _target_name(candidate: dict, arxiv_id: str | None, doi: str | None) -> str:
    if arxiv_id:
        return f'{arxiv_id.replace("/", "_")}.pdf'
    if doi:
        return 'doi_' + re.sub(r'[^0-9A-Za-z._-]+', '_', doi)[:80] + '.pdf'
    basis = (candidate.get('title') or 'paper').strip().lower()
    return 'title_' + hashlib.sha1(basis.encode('utf-8')).hexdigest()[:16] + '.pdf'


def fetch_candidate_pdf(kb: KnowledgeBase, cfg: Config, verifier, cid: str, *, material_policy: str = 'replace') -> dict:
    """把一条候选取成可解读的论文：下载 → 解析 → 建论文/原文块 → 回链候选。

    arXiv 走官方直链；其余先查开放副本（Unpaywall/OpenAlex），查不到就明确失败，
    不绕过付费墙、不使用账户凭据。
    """
    rows = kb._con.execute('SELECT * FROM source_candidates WHERE id=?', (cid,)).fetchone()
    if rows is None:
        raise ValueError('候选不存在。')
    candidate = dict(rows)
    arxiv_id = arxiv_id_of(candidate)
    doi = normalize_doi(candidate.get('doi') or candidate.get('url'))

    license = None
    if arxiv_id:
        url, provider = arxiv_pdf_url(arxiv_id), 'arxiv'
    else:
        lookup = resolve_open_copy(candidate, cfg)
        if lookup.status != 'ok' or lookup.copy is None:
            raise FulltextUnavailable(lookup.status,
                                      f'没有可自动获取的全文（{lookup.status}）：{lookup.detail}')
        url, provider = lookup.copy.url, lookup.copy.provider
        license = lookup.copy.license

    target = Path(cfg.data_path) / 'papers' / _target_name(candidate, arxiv_id, doi)
    reused = target.exists() and target.stat().st_size > 0
    if not reused:
        download_pdf(url, target, allow_public=not arxiv_id)

    from .ingest import chunk_document, load_document

    doc = load_document(target)
    if not (doc.full_text or '').strip():
        raise ValueError('PDF 已下载但未能解析出文本（可能是扫描件），未入库。')

    result = verifier.verify(title=doc.title or candidate['title'], doi=doi if not arxiv_id else None,
                             arxiv_id=arxiv_id, authors=doc.authors or None, year=doc.year)
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
            source=provider,
            origin=url,
            full_text=doc.full_text,
            material_level='fulltext',
            tags=['arXiv 自动取回'] if provider == 'arxiv' else ['开放全文自动取回'],
            verify_status=result.level, verify_level=result.level,
            verify_confidence=float(result.confidence), verify_evidence=result.to_dict(),
        )
        chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
        added = kb.add_chunks(pid, chunks, policy=material_policy, level='fulltext', source=provider,
                              origin=url, full_text=doc.full_text)
    set_candidate_status(kb, cid, 'imported', paper_id=pid)
    _record_provenance(kb, cid, provider=provider, url=url, license=license)
    return {'paper_id': pid, 'arxiv_id': arxiv_id, 'reused_file': reused, 'provider': provider,
            'source_url': url, 'chunks': added, 'pages': len(doc.pages),
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
    try:
        # 非 arXiv：先试开放副本；查不到或取不回（403/HTML/解析失败）
        # 都退回题录导入，不让"附带能力"把导入本身卡死
        return fetch_candidate_pdf(kb, cfg, verifier, cid, material_policy=material_policy)
    except (FulltextUnavailable, ValueError):
        pass

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
