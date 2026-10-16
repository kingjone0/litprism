"""文本工具：标题归一化、CJK 分词、FTS5 查询构造、相似度计算、引用标注解析。

这里集中了"学术文本比对"所需的基础能力，是真实性核验与防幻觉机制的地基。
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from typing import Iterable

# ---------------------------------------------------------------------------
# 字符集
# ---------------------------------------------------------------------------
CJK_RANGES = (
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xF900, 0xFAFF),    # 兼容表意文字
    (0x3040, 0x30FF),    # 日文假名
    (0xAC00, 0xD7AF),    # 韩文音节
)
_CJK_SET = set()
for _lo, _hi in CJK_RANGES:
    _CJK_SET.update(range(_lo, _hi + 1))

_CJK_CHAR = re.compile(
    "["
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\u3040-\u30ff"
    "\uac00-\ud7af"
    "]"
)

# FTS5 表达式里只允许字母、数字与 CJK —— 用白名单剔除 ? ! . , ' 等会引发语法错误的字符
_FTS_KEEP = re.compile(
    r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+"
)


def _sanitize_token(tok: str) -> str:
    return _FTS_KEEP.sub("", tok.lower())


def build_fts_query(query: str) -> str:
    """把自然语言查询转成 FTS5 MATCH 表达式（词间 AND）。

    - 连续汉字合成一个短语（保证字序，如 "方法论" 不会匹配到 "法论方"）
    - 拉丁词使用前缀匹配，便于 "transformer" 命中 "transformers"
    - 标点等非法字符一律剔除，避免 ``fts5: syntax error``
    """
    if not query or not query.strip():
        return ""
    seg = cjk_segment(query).split()
    groups: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            groups.append(" ".join(buf))
            buf.clear()

    for tok in seg:
        if len(tok) == 1 and is_cjk_char(tok):
            buf.append(tok)
        else:
            flush()
            cleaned = _sanitize_token(tok)
            if cleaned:
                groups.append(cleaned)
    flush()

    terms: list[str] = []
    for g in groups:
        if not g:
            continue
        if " " in g:                      # 汉字短语
            terms.append(f'"{g}"')
        elif len(g) >= 4:                 # 长拉丁词 → 前缀匹配
            terms.append(f"{g}*")
        else:
            terms.append(f'"{g}"')
    return " AND ".join(terms)


def contains_cjk(text: str) -> bool:
    return bool(_CJK_CHAR.search(text or ""))


def cjk_segment(text: str) -> str:
    """在 CJK 字符两侧插入空格，使 SQLite FTS5 的 unicode61 分词器
    能把每个汉字切成独立 token，从而支持中文检索。

    ``"论文解读"`` → ``"论 文 解 读"``
    """
    if not text:
        return ""
    return _CJK_CHAR.sub(lambda m: f" {m.group(0)} ", text)


def is_cjk_char(ch: str) -> bool:
    return len(ch) == 1 and ord(ch) in _CJK_SET


# ---------------------------------------------------------------------------
# 标题归一化与相似度（核验核心）
# ---------------------------------------------------------------------------
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\s*")
_NON_ALNUM = re.compile(r"[^0-9a-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MULTISPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """把标题归一化成可稳定比对的字符串。

    处理：HTML 实体/标签、LaTeX 命令、大小写、标点、空白、全角半角。
    """
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title)
    t = _HTML_TAG.sub(" ", t)
    t = t.replace("&amp;", " and ").replace("&", " and ")
    t = _LATEX_CMD.sub(" ", t)
    t = t.replace("{", " ").replace("}", " ").replace("$", " ")
    t = t.lower()
    # 统一各种破折号/连字符
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)
    t = _NON_ALNUM.sub(" ", t)
    return _MULTISPACE.sub(" ", t).strip()


def title_similarity(a: str, b: str) -> float:
    """标题相似度（0~1），对词序敏感。

    三个信号加权融合，避免"词集合相同但语序不同"被判为同一篇：

    * 字符级序列相似度（顺序敏感，权重最高）
    * 词序列相似度（顺序敏感，处理缩写/标点差异）
    * 词集合 Jaccard（顺序不敏感，权重最低，仅作稳健性修正）

    另外对"副标题"场景（一篇标题是另一篇的真子串且长度占比足够）给予较高分。
    因此 ``"Attention Is All You Need"`` 与 ``"Is Attention All You Need?"``
    会落在 0.90 附近（判为"疑似匹配、需人工确认"），而不是 1.00。
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    char_ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    word_seq = difflib.SequenceMatcher(None, na.split(), nb.split()).ratio()

    sa, sb = set(na.split()), set(nb.split())
    jac = len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0

    score = 0.75 * max(char_ratio, word_seq) + 0.25 * jac

    # 副标题/子串场景：短标题是长标题的连续子串，且占比足够
    if len(na) >= 15 and len(nb) >= 15:
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        if shorter in longer and len(shorter) / len(longer) >= 0.6:
            score = max(score, 0.94)

    return round(min(score, 1.0), 4)


def author_overlap(a: Iterable[str], b: Iterable[str]) -> float:
    """作者集合重合度（按姓氏 token 比对，容忍 "J. Smith" vs "John Smith"）。"""
    def keys(names: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for n in names or []:
            n = (n or "").strip()
            if not n:
                continue
            parts = re.split(r"[\s,\.]+", n)
            for p in parts:
                if len(p) >= 3 and p.isalpha():
                    out.add(p.lower())
        return out

    ka, kb = keys(a), keys(b)
    if not ka or not kb:
        return 0.0
    return round(len(ka & kb) / min(len(ka), len(kb)), 4)


# ---------------------------------------------------------------------------
# 标识符
# ---------------------------------------------------------------------------
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)\b")
_ARXIV_RE = re.compile(r"\barXiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?", re.I)


def extract_doi(text: str) -> str | None:
    """从正文中提取 DOI（会自动去掉尾部标点）。"""
    if not text:
        return None
    m = _DOI_RE.search(text)
    if not m:
        return None
    doi = m.group(1).rstrip(".,;)]}")
    return doi.lower()


def extract_arxiv_id(text: str) -> str | None:
    if not text:
        return None
    m = _ARXIV_RE.search(text)
    return m.group(1) if m else None


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip() or None


def paper_fingerprint(title: str, year: int | None = None) -> str:
    """标题+年份的稳定指纹，用于去重。"""
    key = f"{normalize_title(title)}|{year or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 引用标注解析（防幻觉：校验回答里的引用是否真的存在）
# ---------------------------------------------------------------------------
CITATION_RE = re.compile(
    r"\[([Pp]\d+)"                       # 片段标签，如 P3
    r"(?:\s*[,，]?\s*"                    # 可选分隔符（逗号可有可无）
    r"(?:p|pp|page|页|§|sec)\.?\s*"       # 页码前缀
    r"([0-9]+(?:[-–~][0-9]+)?))?"         # 页码，如 12 / 12-14
    r"\]"
)


def parse_citations(text: str) -> list[tuple[str, str | None]]:
    """从模型回答中解析出 ``[P3 p.12]`` 形式的引用标记。"""
    if not text:
        return []
    return [(m.group(1).upper(), (m.group(2) or None)) for m in CITATION_RE.finditer(text)]


def clean_text(text: str) -> str:
    """通用清洗：去掉多余空白、连字符换行、控制字符。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)      # 跨行连字符
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    if not text or len(text) <= limit:
        return text or ""
    return text[: limit - len(ellipsis)] + ellipsis
