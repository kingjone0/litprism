"""统一候选池：学术检索与网页搜索的候选都留档。

为什么要有这一层：M01 的两条发现路径各自把结果直接交给前端，换个入口就要重搜，
也无法回答"这条候选是怎么来的、后来怎么处理的"。候选池只负责**记录与状态**，
不决定能否引用——引用仍然由论文核验/人工放行决定。
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from urllib.parse import urlparse

from .db import KnowledgeBase
from .knowledge import now

STATUSES = ('new', 'imported', 'rejected', 'ignored')
KINDS = ('paper', 'web', 'knowledge')


def _norm(text: str | None) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text or '').split()).casefold()


def normalize_url(url: str | None) -> str:
    """去掉协议与追踪参数，只留可比较的主干。"""
    if not url:
        return ''
    try:
        parts = urlparse(url if '://' in url else 'https://' + url)
    except ValueError:
        return _norm(url)
    host = (parts.hostname or '').lower().removeprefix('www.')
    path = parts.path.rstrip('/')
    return f'{host}{path}'


def candidate_fingerprint(record: dict) -> str:
    """去重键：DOI > arXiv > 规范化 URL > 标题+年份。

    顺序有意义：同一篇论文常以 DOI、arXiv、落地页三种形式出现，
    优先用最稳定的标识，避免"预印本和正式版算两条"。
    """
    doi = _norm(record.get('doi'))
    if doi:
        return 'doi:' + doi.removeprefix('https://doi.org/')
    arxiv = _norm(record.get('arxiv_id'))
    if arxiv:
        return 'arxiv:' + arxiv
    url = normalize_url(record.get('url'))
    if url:
        return 'url:' + url
    return 'title:' + _norm(record.get('title')) + '|' + str(record.get('year') or '')


def find_library_paper(kb: KnowledgeBase, record: dict) -> str | None:
    """候选对应的论文是否已在库里。按 DOI → arXiv → 标题+年份 依次匹配。

    只做**识别**，不改任何核验状态；目的是让候选池不再重复展示已有的论文。
    """
    doi = _norm(record.get('doi')).removeprefix('https://doi.org/')
    if doi:
        row = kb._con.execute('SELECT id FROM papers WHERE lower(doi)=? AND deleted_at IS NULL', (doi,)).fetchone()
        if row:
            return row['id']
    arxiv = _norm(record.get('arxiv_id'))
    if arxiv:
        row = kb._con.execute('SELECT id FROM papers WHERE lower(arxiv_id)=? AND deleted_at IS NULL',
                              (arxiv,)).fetchone()
        if row:
            return row['id']
    from .textutil import paper_fingerprint
    year = record.get('year')
    fp = paper_fingerprint(record.get('title') or '', year if isinstance(year, int) else None)
    row = kb._con.execute('SELECT id FROM papers WHERE fingerprint=? AND deleted_at IS NULL', (fp,)).fetchone()
    return row['id'] if row else None


def save_candidates(kb: KnowledgeBase, records: list[dict], *, query: str = '',
                    kind: str = 'paper', source: str | None = None) -> dict:
    """把一批检索结果写入候选池。已存在的按 fingerprint 跳过（但会刷新 updated_at）。"""
    if kind not in KINDS:
        raise ValueError(f'未知候选类型：{kind}')
    added, existing = [], []
    stamp = now()
    with kb.tx() as con:
        for record in records or []:
            title = (record.get('title') or '').strip()
            if not title:
                continue
            fingerprint = candidate_fingerprint(record)
            row = con.execute('SELECT id FROM source_candidates WHERE fingerprint=?', (fingerprint,)).fetchone()
            if row:
                con.execute('UPDATE source_candidates SET updated_at=? WHERE id=?', (stamp, row['id']))
                existing.append(row['id'])
                continue
            # 必须哈希：hex 是前缀保持的，直接截断会让 doi:10.1/a 与 doi:10.1/b 撞 id
            cid = 'sc_' + hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]
            # 已在库的直接记为 imported，避免候选池越滚越大。
            known = find_library_paper(kb, record)
            con.execute(
                '''INSERT INTO source_candidates
                   (id, kind, source, query, title, url, doi, arxiv_id, abstract, year, authors,
                    fingerprint, status, paper_id, extra, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?, ?, ?)''',
                (cid, kind, source or record.get('origin') or record.get('source'), query.strip(), title,
                 record.get('url'), record.get('doi'), record.get('arxiv_id'), record.get('abstract'),
                 record.get('year'), json.dumps(record.get('authors') or [], ensure_ascii=False),
                 fingerprint, 'imported' if known else 'new', known,
                 json.dumps({k: v for k, v in record.items()
                                          if k not in ('title', 'url', 'doi', 'arxiv_id', 'abstract',
                                                       'year', 'authors')}, ensure_ascii=False),
                 stamp, stamp))
            added.append(cid)
    return {'added': added, 'existing': existing, 'total': len(added) + len(existing)}


def list_candidates(kb: KnowledgeBase, *, kind: str | None = None, status: str | None = None,
                    query: str | None = None, hide_in_library: bool = False,
                    limit: int = 50) -> list[dict]:
    sql = 'SELECT * FROM source_candidates WHERE 1=1'
    args: list = []
    if kind:
        if kind not in KINDS:
            raise ValueError(f'未知候选类型：{kind}')
        sql += ' AND kind=?'
        args.append(kind)
    if status:
        if status not in STATUSES:
            raise ValueError(f'未知候选状态：{status}')
        sql += ' AND status=?'
        args.append(status)
    if query:
        sql += ' AND query LIKE ?'
        args.append(f'%{query.strip()}%')
    sql += ' ORDER BY created_at DESC, rowid DESC LIMIT ?'
    args.append(max(1, min(int(limit), 500)))
    rows = kb._con.execute(sql, args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item['authors'] = json.loads(item.get('authors') or '[]')
        item['extra'] = json.loads(item.get('extra') or '{}')
        # 已在库：显式记过的 paper_id 优先，否则按标识现查一次（老候选没有回链）。
        library_id = item.get('paper_id') or find_library_paper(kb, item)
        item['library_paper_id'] = library_id
        item['in_library'] = bool(library_id)
        if hide_in_library and item['in_library']:
            continue
        out.append(item)
    return out


def add_manual_candidate(kb: KnowledgeBase, *, title: str, url: str | None = None, doi: str | None = None,
                         arxiv_id: str | None = None, year: int | None = None,
                         note: str | None = None) -> dict:
    """手动补充一条候选：用户自己粘贴标题/链接，不联网抓取。

    arXiv 标识存在时会先尝试本地匹配；命中已在库则直接记 imported。
    """
    if not (title or '').strip():
        raise ValueError('请填写标题。')
    kind = 'paper' if (arxiv_id or doi) else 'web'
    result = save_candidates(kb, [{'title': title.strip(), 'url': url, 'doi': doi, 'arxiv_id': arxiv_id,
                                   'year': year, 'note': note}],
                             query='手动补充', kind=kind, source='manual')
    cid = result['added'][0] if result['added'] else result['existing'][0]
    row = dict(kb._con.execute('SELECT * FROM source_candidates WHERE id=?', (cid,)).fetchone())
    kind_source = 'existed' if not result['added'] else 'created'
    return {'id': cid, 'created': kind_source == 'created', 'candidate': row}


def sync_library(kb: KnowledgeBase) -> dict:
    """把候选池与当前知识库对一遍：命中的记为 imported 并回链。"""
    rows = kb._con.execute(
        "SELECT * FROM source_candidates WHERE status != 'imported' OR paper_id IS NULL").fetchall()
    linked = 0
    for row in rows:
        item = dict(row)
        pid = find_library_paper(kb, item)
        if pid:
            with kb.tx() as con:
                con.execute("UPDATE source_candidates SET status='imported', paper_id=?, updated_at=? WHERE id=?",
                            (pid, now(), item['id']))
            linked += 1
    return {'checked': len(rows), 'linked': linked}


def set_candidate_status(kb: KnowledgeBase, cid: str, status: str, *, paper_id: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f'未知候选状态：{status}')
    with kb.tx() as con:
        row = con.execute('SELECT * FROM source_candidates WHERE id=?', (cid,)).fetchone()
        if row is None:
            raise ValueError('候选不存在。')
        con.execute('UPDATE source_candidates SET status=?, paper_id=?, updated_at=? WHERE id=?',
                    (status, paper_id or row['paper_id'], now(), cid))
    return dict(kb._con.execute('SELECT * FROM source_candidates WHERE id=?', (cid,)).fetchone())
