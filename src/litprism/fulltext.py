"""为候选文献查找**合法开放**的全文副本。

只问开放获取索引（Unpaywall / OpenAlex / Semantic Scholar / Europe PMC），
命中才返回直链；付费墙内容一律返回 ``paywalled``，不尝试绕过，也不使用任何账户凭据。

设计取舍：
- DOI 是唯一可靠的入口，没有 DOI 直接判定 ``no_doi``，不猜 URL；
- 只认直链 PDF：只有落地页时判 ``no_oa``，不抓 HTML 再猜链接；
- 网络失败与"没有开放副本"必须分开，前者不能记成后者；
- 只返回 URL，不在这里下载；下载校验（%PDF、大小、公网主机）在 remote 中统一做。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .textutil import normalize_doi

UNPAYWALL = 'https://api.unpaywall.org/v2/{doi}'
OPENALEX = 'https://api.openalex.org/works/doi:{doi}'
SEMANTIC_SCHOLAR = 'https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}'
EUROPEPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
PLACEHOLDER_EMAILS = {'', 'your_email@example.com', 'you@example.com'}


@dataclass(frozen=True)
class OpenCopy:
    url: str
    provider: str
    license: str | None = None


@dataclass(frozen=True)
class Lookup:
    status: str  # ok | no_oa | paywalled | network_error | no_doi
    copy: OpenCopy | None = None
    detail: str = ''


def doi_of(record: dict) -> str | None:
    return normalize_doi((record or {}).get('doi') or (record or {}).get('url'))


def _usable_mailto(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in PLACEHOLDER_EMAILS and '@' in value


def _fetch_json(url: str, params: dict, headers: dict | None = None, *, timeout: int = 15):
    import requests

    merged = {'User-Agent': 'LitPrism/1.0 (local research tool)'}
    if headers:
        merged.update(headers)
    resp = requests.get(url, params=params, timeout=timeout, headers=merged)
    if resp.status_code >= 400:
        raise ValueError(f'HTTP {resp.status_code}')
    return resp.json()


def _from_unpaywall(doi: str, cfg: Config, fetch_json) -> tuple[OpenCopy | None, dict]:
    email = cfg.sources.unpaywall.mailto
    if not _usable_mailto(email):
        return None, {}
    data = fetch_json(UNPAYWALL.format(doi=doi), {'email': email})
    best = data.get('best_oa_location') or {}
    url = best.get('url_for_pdf')
    if not url:
        return None, data
    return OpenCopy(url=url, provider='unpaywall', license=best.get('license')), data


def _from_openalex(doi: str, cfg: Config, fetch_json) -> tuple[OpenCopy | None, dict]:
    params = {}
    if _usable_mailto(cfg.sources.openalex.mailto):
        params['mailto'] = cfg.sources.openalex.mailto
    data = fetch_json(OPENALEX.format(doi=doi), params)
    best = data.get('best_oa_location') or {}
    url = best.get('pdf_url')
    if not url:
        for loc in data.get('locations') or []:
            if (loc or {}).get('pdf_url'):
                url = loc['pdf_url']
                break
    if not url:
        return None, data
    return OpenCopy(url=url, provider='openalex', license=best.get('license')), data


def _from_semantic_scholar(doi: str, cfg: Config, fetch_json) -> tuple[OpenCopy | None, dict]:
    headers = {}
    key = (cfg.sources.semantic_scholar.api_key or '').strip()
    if key:
        headers['x-api-key'] = key
    data = fetch_json(SEMANTIC_SCHOLAR.format(doi=doi),
                      {'fields': 'openAccessPdf,isOpenAccess'}, headers)
    pdf = (data.get('openAccessPdf') or {}).get('url')
    if not pdf:
        return None, data
    return OpenCopy(url=pdf, provider='semantic_scholar'), data


def _from_europepmc(doi: str, cfg: Config, fetch_json) -> tuple[OpenCopy | None, dict]:
    data = fetch_json(EUROPEPMC, {'query': f'DOI:"{doi}"', 'format': 'json', 'resultType': 'core'})
    results = ((data.get('resultList') or {}).get('result')) or []
    if not results:
        return None, data
    record = results[0] or {}
    for entry in ((record.get('fullTextUrlList') or {}).get('fullTextUrl')) or []:
        url = entry.get('url')
        availability = (entry.get('availability') or '').lower()
        style = (entry.get('documentStyle') or '').lower()
        if url and style == 'pdf' and availability in ('open access', 'free'):
            return OpenCopy(url=url, provider='europepmc'), data
    pmcid = record.get('pmcid')
    if record.get('hasPDF') == 'Y' and pmcid:
        return OpenCopy(url=f'https://europepmc.org/articles/{pmcid}?pdf=render',
                        provider='europepmc'), data
    return None, data


def _looks_paywalled(data: dict) -> bool:
    """Unpaywall 把 is_oa 放顶层，OpenAlex 放在 open_access 里，两种都认。"""
    is_oa = data.get('is_oa')
    if is_oa is None:
        is_oa = (data.get('open_access') or {}).get('is_oa')
    return is_oa is False and data.get('journal_is_oa') is False


def _providers(cfg: Config):
    chain = []
    if cfg.sources.unpaywall.enabled:
        chain.append(('unpaywall', _from_unpaywall))
    if cfg.sources.openalex.enabled:
        chain.append(('openalex', _from_openalex))
    if cfg.sources.semantic_scholar.enabled:
        chain.append(('semantic_scholar', _from_semantic_scholar))
    europepmc = getattr(cfg.sources, 'europepmc', None)
    if europepmc is not None and europepmc.enabled:
        chain.append(('europepmc', _from_europepmc))
    return chain


def resolve_open_copy(record: dict, cfg: Config, *, fetch_json=None) -> Lookup:
    """按已启用的来源顺序查开放副本；任一来源命中即返回。"""
    fetch_json = fetch_json or _fetch_json
    doi = doi_of(record)
    if not doi:
        return Lookup('no_doi', detail='该候选没有 DOI，无法查询开放副本。')

    providers = _providers(cfg)
    if not providers:
        return Lookup('no_oa', detail='未启用任何开放全文来源（sources.unpaywall / openalex / semantic_scholar / europepmc）。')

    errors = []
    answered = False
    paywalled = False
    for name, fn in providers:
        try:
            copy, payload = fn(doi, cfg, fetch_json)
            answered = True
        except Exception as exc:  # 网络/服务异常：继续问下一个来源
            errors.append(f'{name}: {exc}')
            continue
        if copy:
            return Lookup('ok', copy=copy)
        if name == 'unpaywall':
            paywalled = _looks_paywalled(payload)
    if not answered:
        return Lookup('network_error', detail='; '.join(errors) or '开放全文来源不可用。')
    if paywalled:
        return Lookup('paywalled', detail='该文献为订阅内容，没有可自动获取的开放副本。')
    return Lookup('no_oa', detail='没有找到开放副本。')
