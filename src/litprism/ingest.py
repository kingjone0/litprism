"""文档导入：PDF / DOCX / HTML / TXT / MD / TeX → 统一 Document 结构。

流程：``load_document()`` → 元数据抽取 → 章节识别 → ``chunk_document()`` 分块。
"""

from __future__ import annotations

import html as html_mod
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .textutil import clean_text, extract_arxiv_id, extract_doi, normalize_doi, truncate

SUPPORTED_EXT = {".pdf", ".docx", ".doc", ".html", ".htm", ".txt", ".md", ".markdown", ".tex", ".latex"}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Page:
    number: int
    text: str
    # Raw text remains in full_text; only recovered layouts enter reading chunks.
    reading_text: str | None = None
    tables: list[dict] = field(default_factory=list)


@dataclass
class Section:
    name: str
    level: int = 1
    text: str = ""
    start_page: int | None = None
    # (页码, 行文本) —— 保留逐行页码，分块时才能给出准确的引用页码
    lines: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Document:
    path: Path | None = None
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    pages: list[Page] = field(default_factory=list)
    full_text: str = ""
    sections: list[Section] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)


# ---------------------------------------------------------------------------
# 章节识别
# ---------------------------------------------------------------------------
SECTION_ALIASES: dict[str, str] = {
    # 英文
    "abstract": "摘要", "summary": "摘要",
    "introduction": "引言", "intro": "引言",
    "related work": "相关工作", "related works": "相关工作", "background": "背景",
    "literature review": "相关工作", "prior work": "相关工作",
    "method": "方法", "methods": "方法", "methodology": "方法", "approach": "方法",
    "materials and methods": "方法", "proposed method": "方法", "our approach": "方法",
    "model": "方法", "framework": "方法", "system design": "方法",
    "experiment": "实验", "experiments": "实验", "experimental setup": "实验",
    "experimental results": "实验", "evaluation": "实验", "results": "结果",
    "results and discussion": "结果", "ablation study": "消融实验",
    "discussion": "讨论", "analysis": "分析",
    "conclusion": "结论", "conclusions": "结论", "conclusion and future work": "结论",
    "limitations": "局限", "future work": "未来工作",
    "references": "参考文献", "bibliography": "参考文献",
    "appendix": "附录", "supplementary material": "附录", "acknowledgments": "致谢",
    # 中文
    "摘要": "摘要", "引言": "引言", "绪论": "引言", "前言": "引言",
    "相关工作": "相关工作", "文献综述": "相关工作", "研究现状": "相关工作", "背景": "背景",
    "方法": "方法", "方法论": "方法", "研究方法": "方法", "模型": "方法", "算法": "方法",
    "实验": "实验", "实验设计": "实验", "实验设置": "实验", "数据集": "实验",
    "结果": "结果", "结果与分析": "结果", "实验结果": "结果",
    "讨论": "讨论", "分析": "分析", "消融实验": "消融实验",
    "结论": "结论", "总结": "结论", "结论与展望": "结论",
    "局限": "局限", "局限性": "局限", "未来工作": "未来工作", "展望": "未来工作",
    "参考文献": "参考文献", "附录": "附录", "致谢": "致谢",
}

# 常见复合标题（精确匹配，避免 "Intro text." 这类正文被误判成 "引言"）
EXTRA_HEADINGS: dict[str, str] = {
    "model architecture": "方法", "proposed method": "方法", "proposed approach": "方法",
    "our approach": "方法", "our method": "方法", "materials and methods": "方法",
    "methodology": "方法", "the proposed method": "方法", "system overview": "方法",
    "experimental setup": "实验", "experimental settings": "实验", "experimental results": "结果",
    "results and discussion": "结果", "main results": "结果", "ablation study": "消融实验",
    "ablation studies": "消融实验", "conclusion and future work": "结论",
    "conclusions and future work": "结论", "related works": "相关工作", "prior work": "相关工作",
    "problem statement": "研究问题", "problem formulation": "研究问题", "task formulation": "研究问题",
    "training details": "实验", "implementation details": "实验", "evaluation setup": "实验",
    "datasets and setup": "实验", "dataset and setup": "实验", "results analysis": "分析",
    "discussion and conclusion": "讨论", "limitations and future work": "局限",
    "future directions": "未来工作", "ethics statement": "伦理声明", "broader impact": "影响",
    "case study": "分析", "error analysis": "分析", "complexity analysis": "分析",
}

# 形如 "1 Introduction" / "2.1 Related Work" / "三、实验" / "一 引言"
_HEADING_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*)|(?:[一二三四五六七八九十]+))\s*[.、,，:：]?\s*(.{2,40}?)\s*$"
)
_SENTENCE_END = re.compile(r"[.!?。！？；;]\s*$")
_LEAD_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


def _canonical_section(line: str) -> str | None:
    """把一行文本映射到规范章节名；不是标题则返回 None。

    只接受**精确匹配**（含少量显式列出的复合标题），不做前缀模糊匹配——
    否则 "Intro text." 会因为以 "intro " 开头而被误判成"引言"章节。
    """
    raw = line.strip().strip(".:：。 ")
    if not raw or len(raw) > 48:
        return None
    # 去掉编号前缀
    m = _HEADING_RE.match(raw)
    if m:
        raw = m.group(1).strip()
    key = re.sub(r"\s+", " ", raw).lower().strip(" .:：")
    if not key:
        return None
    if key in SECTION_ALIASES:
        return SECTION_ALIASES[key]
    if key in EXTRA_HEADINGS:
        return EXTRA_HEADINGS[key]
    stripped = _LEAD_ARTICLE.sub("", key)
    if stripped in SECTION_ALIASES:
        return SECTION_ALIASES[stripped]
    if stripped in EXTRA_HEADINGS:
        return EXTRA_HEADINGS[stripped]
    return None


def _is_heading_line(line: str) -> str | None:
    s = line.strip()
    if not s or len(s) > 60:
        return None
    # 以句末标点结尾的行是正文句子，不是标题
    if _SENTENCE_END.search(s):
        return None
    canon = _canonical_section(s)
    if canon:
        return canon
    # 全大写短行（英文论文常见，如 "METHODS AND MATERIALS"）
    if re.fullmatch(r"[A-Z][A-Z \-\d]{3,40}", s):
        return _canonical_section(s.title())
    return None


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------
def _load_pdf(path: Path) -> tuple[list[Page], dict]:
    meta: dict = {}
    pages: list[Page] = []
    try:
        import pymupdf as fitz  # 新版 API
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]  # 旧版 API
        except ImportError:
            try:
                from pypdf import PdfReader  # type: ignore
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "解析 PDF 需要 pymupdf 或 pypdf，请执行：python -m pip install pymupdf"
                ) from e
            reader = PdfReader(str(path))
            meta = dict(reader.metadata or {})
            for i, pg in enumerate(reader.pages, 1):
                pages.append(Page(i, clean_text(pg.extract_text() or "")))
            return pages, meta

    doc = fitz.open(str(path))
    meta = dict(doc.metadata or {})
    for i, pg in enumerate(doc, 1):
        from .pdf_layout import reading_layout
        reading, tables = reading_layout(pg)
        pages.append(Page(i, clean_text(pg.get_text("text") or ""), reading, tables))
    if doc.page_count:
        meta["_font_title"] = _title_by_font(doc[0])
    doc.close()
    return pages, meta


# 标题噪声：版权声明、arXiv 页眉、期刊模板套话、页脚信息
_NOISE_TITLE = re.compile(
    r"(provided proper attribution|copyright|©|\(c\)\s*\d{4}|all rights reserved|"
    r"permission to|preprint|under review|submitted to|arxiv:|doi:|journal homepage|"
    r"www\.|https?://|proceedings of|conference on|volume \d|vol\.\s*\d|pp\.\s*\d|"
    r"issn|isbn|license|cc[- ]by|correspondence|equal contribution|work was done|"
    r"google hereby grants|to appear in|accepted at|published as|"
    r"第\s*\d+\s*卷|第\s*\d+\s*期|收稿日期|基金项目|作者简介|中图分类号|文献标识码)",
    re.I,
)


def _title_by_font(page) -> str:
    """用字号推断标题：论文首页字号最大的文本行几乎总是标题。

    比"取第一行"稳健得多——能跳过版权声明、arXiv 页眉、期刊模板信息。
    """
    try:
        d = page.get_text("dict")
    except Exception:
        return ""
    spans: list[tuple[float, float, float, str]] = []
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            for sp in line.get("spans", []):
                t = (sp.get("text") or "").strip()
                size = float(sp.get("size") or 0)
                if len(t) < 2 or size <= 0:
                    continue
                if _NOISE_TITLE.search(t):
                    continue
                bbox = line.get("bbox") or sp.get("bbox") or (0, 0, 0, 0)
                spans.append((size, float(bbox[1]), float(bbox[0]), t))
    if not spans:
        return ""
    max_size = max(s[0] for s in spans)
    cands = [s for s in spans if s[0] >= max_size - 0.8]
    cands.sort(key=lambda s: (round(s[1]), s[2]))
    text = " ".join(s[3] for s in cands)
    text = re.sub(r"\s+", " ", text).strip()
    # 标题一般 12~250 字符；过短或过长都不可信
    if not (10 <= len(text) <= 250) or _NOISE_TITLE.search(text):
        return ""
    return text


def _load_docx(path: Path) -> tuple[list[Page], dict]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = xml.replace("</w:p>", "\n")
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = html_mod.unescape(text)
    return [Page(1, clean_text(text))], {}


def _load_html(path: Path) -> tuple[list[Page], dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = html_mod.unescape(m.group(1)).strip()
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", raw)
    return [Page(1, clean_text(html_mod.unescape(text)))], {"title": title}


def _load_tex(path: Path) -> tuple[list[Page], dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    raw = re.sub(r"(?<!\\)%.*", "", raw)                       # 注释
    raw = re.sub(r"\\begin\{(equation|align|figure|table)\*?\}.*?\\end\{\1\*?\}", " ", raw, flags=re.S)
    raw = re.sub(r"\\(section|subsection|subsubsection)\*?\{([^}]*)\}", r"\n\n\2\n", raw)
    raw = re.sub(r"\\(title|author)\{([^}]*)\}", r"\n\2\n", raw)
    raw = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^}]*\})?", " ", raw)
    raw = raw.replace("{", " ").replace("}", " ")
    return [Page(1, clean_text(raw))], {}


def _load_plain(path: Path) -> tuple[list[Page], dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # 按分页符切分（若有）
    parts = re.split(r"\f|\n-{3,}\s*第\s*\d+\s*页\s*-{3,}\n", text)
    parts = [p for p in parts if p.strip()]
    if len(parts) > 1:
        return [Page(i, clean_text(p)) for i, p in enumerate(parts, 1)], {}
    return [Page(1, clean_text(text))], {}


_LOADERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".doc": _load_docx,
    ".html": _load_html,
    ".htm": _load_html,
    ".tex": _load_tex,
    ".latex": _load_tex,
}


# ---------------------------------------------------------------------------
# 元数据抽取
# ---------------------------------------------------------------------------
_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_ARXIV_HINT = re.compile(r"arxiv", re.I)


def _extract_metadata(pages: list[Page], raw_meta: dict) -> dict:
    first = pages[0].text if pages else ""
    head = "\n".join(p.text for p in pages[:2])

    # ---- 标题：优先用字号推断，其次 PDF 元数据，最后退回首行启发式 ----
    title = (raw_meta.get("_font_title") or "").strip()
    if not title:
        cand = (raw_meta.get("title") or "").strip()
        if cand and len(cand) >= 6 and cand.lower() not in {"untitled", "microsoft word"} \
                and not _NOISE_TITLE.search(cand):
            title = cand
    if not title:
        for line in first.split("\n")[:18]:
            s = line.strip()
            if (15 <= len(s) <= 220 and not _NOISE_TITLE.search(s)
                    and not s.lower().startswith(("abstract", "摘要", "keywords", "关键词"))
                    and not re.match(r"^\s*(?:\d+\.?\s*)?(introduction|引言)", s, re.I)):
                title = s
                break

    # ---- 年份：arXiv 编号 > 首页日期戳 > PDF 元数据 ----
    arxiv_id = extract_arxiv_id(head)
    year = None
    if arxiv_id and re.match(r"^\d{2}", arxiv_id):
        yy = int(arxiv_id[:2])
        year = 1900 + yy if yy >= 90 else 2000 + yy
    if year is None:
        # "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023" 这类戳记里的年份不是首发年份，故只做兜底
        for line in head.split("\n")[:30]:
            if re.search(r"(published|accepted|received|copyright|©)", line, re.I):
                ym = _YEAR_RE.search(line)
                if ym:
                    year = int(ym.group(1))
                    break
    if year is None:
        mdate = str(raw_meta.get("creationDate") or raw_meta.get("CreationDate") or "")
        m = re.search(r"(19|20)\d{2}", mdate)
        if m:
            year = int(m.group(0))
    if year is None:
        ym = _YEAR_RE.search(head)
        if ym:
            year = int(ym.group(1))

    # ---- 作者 ----
    authors: list[str] = []
    raw_author = (raw_meta.get("author") or "").strip()
    if raw_author and not _NOISE_TITLE.search(raw_author):
        authors = [a.strip() for a in re.split(r"[;,]|\band\b", raw_author) if a.strip() and len(a.strip()) > 2]
    if not authors:
        lines = [line.strip() for line in first.split("\n")[:18]]
        for ln in lines:
            if _NOISE_TITLE.search(ln):
                continue
            if re.search(r"[A-Z][a-z]+ [A-Z]\.?\s*[A-Z]?\.?\s*[A-Z][a-z]+", ln) and 6 < len(ln) < 200:
                cand = [a.strip() for a in re.split(r",|\band\b|、", ln) if a.strip()]
                cand = [c for c in cand if not re.search(
                    r"@|university|institute|department|college|大学|学院|研究所|实验室|google|brain", c, re.I)]
                if 1 <= len(cand) <= 12 and sum(len(c) for c in cand) > 8:
                    authors = cand
                    break

    # ---- 摘要 ----
    abstract = ""
    m = re.search(
        r"(?:^|\n)\s*(?:Abstract|ABSTRACT|摘要)\s*[:：]?\s*\n?(.{80,3000}?)(?:\n\s*(?:\d+\.?\s*)?(?:Introduction|Keywords|关键词|1\s)|$)",
        head, re.S,
    )
    if m:
        abstract = " ".join(m.group(1).split())[:3000]

    doi = normalize_doi(extract_doi(head))
    if not doi:
        doi = normalize_doi(str(raw_meta.get("doi") or "") or None)
    if not arxiv_id and _ARXIV_HINT.search(head):
        m = re.search(r"(\d{4}\.\d{4,5})", head)
        arxiv_id = m.group(1) if m else None

    return {
        "title": clean_text(title),
        "authors": authors,
        "year": year,
        "abstract": abstract,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "raw_meta": {k: v for k, v in raw_meta.items() if isinstance(v, (str, int, float))},
    }


# ---------------------------------------------------------------------------
# 章节切分
# ---------------------------------------------------------------------------
def detect_sections(pages: list[Page]) -> list[Section]:
    sections: list[Section] = []
    current: Section | None = None
    buf: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal current, buf
        if current is not None:
            current.lines = buf
            current.text = clean_text("\n".join(t for _, t in buf))
            if current.start_page is None and buf:
                current.start_page = buf[0][0]
            if current.text:
                sections.append(current)
        buf = []

    for pg in pages:
        text = pg.reading_text if pg.reading_text is not None else pg.text
        lines = text.split('\n')
        # TOC entries are locators, not body sections or scientific evidence.
        toc = any(re.fullmatch(r'\s*(?:contents|table of contents|目录)\s*', ln, re.I) for ln in lines[:8])
        toc |= sum(bool(re.search(r'\.{4,}', ln)) for ln in lines) >= 3
        if toc:
            flush()
            sections.append(Section(name='目录', text=text, start_page=pg.number,
                                    lines=[(pg.number, ln) for ln in lines]))
            current = None
            continue
        for line in lines:
            canon = _is_heading_line(line)
            if canon:
                flush()
                current = Section(name=canon, start_page=pg.number)
            else:
                if current is None:
                    current = Section(name="正文", start_page=pg.number)
                buf.append((pg.number, line))
    flush()

    if not sections:
        all_lines = [(p.number, ln) for p in pages for ln in p.text.split("\n")]
        sections = [
            Section(
                name="正文",
                text=clean_text("\n".join(t for _, t in all_lines)),
                start_page=1,
                lines=all_lines,
            )
        ]

    # Only adjacent sections may merge; distant names must never reorder the source.
    merged: list[Section] = []
    for s in sections:
        if merged and merged[-1].name == s.name:
            tgt = merged[-1]
            tgt.lines = tgt.lines + s.lines
            tgt.text = clean_text(tgt.text + "\n\n" + s.text)
            continue
        merged.append(s)

    # 丢掉只是标题/作者/版权块的"正文"段（太短且已有其它章节）
    if len(merged) > 1:
        merged = [s for s in merged if not (s.name == "正文" and len(s.text) < 400)]
    return merged


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------
def chunk_document(doc: Document, chunk_size: int = 900, overlap: int = 150) -> list[dict]:
    """按章节 → 段落聚合分块。

    每块严格限于一个物理页，重叠尾部也不跨页；不混合远距离同名章节。
    """
    chunks: list[dict] = []
    chunk_size = max(300, chunk_size)
    overlap = max(0, min(overlap, chunk_size // 2))

    for sec in doc.sections:
        if sec.name in ('参考文献', '目录'):
            continue
        src: list[tuple[int | None, str]] = sec.lines or [(sec.start_page, sec.text)]
        substantial = sum(len(t) for _, t in src) >= 60
        buf: list[tuple[int | None, str]] = []
        buf_len = 0

        def flush(section_name=sec.name, keep_short=substantial) -> None:
            nonlocal buf, buf_len
            if buf:
                text = "\n".join(t for _, t in buf).strip()
                if text and (len(text) >= 60 or keep_short and any(c.isalpha() for c in text)):
                    chunks.append({"text": text, "section": section_name, "page": buf[0][0]})
            buf, buf_len = [], 0

        for pgno, line in src:
            if buf and pgno != buf[0][0]:
                flush()
            if not line.strip():
                # 段落边界：块已足够大就断开，否则保留空行维持可读性
                if buf_len >= chunk_size * 0.6:
                    flush()
                elif buf:
                    buf.append((pgno, ""))
                continue

            if buf_len + len(line) + 1 > chunk_size and buf:
                # 计算 overlap 尾巴（保留尾部若干行），新块的页码由尾巴首行决定
                tail: list[tuple[int | None, str]] = []
                acc = 0
                for item in reversed(buf):
                    if not item[1]:
                        continue
                    if acc + len(item[1]) > overlap:
                        break
                    tail.insert(0, item)
                    acc += len(item[1]) + 1
                flush()
                buf = tail
                buf_len = sum(len(t) + 1 for _, t in tail)

            buf.append((pgno, line))
            buf_len += len(line) + 1
        flush()

    return chunks


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def load_document(path: str | Path) -> Document:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"暂不支持的格式 {ext}；支持：{', '.join(sorted(SUPPORTED_EXT))}")

    loader = _LOADERS.get(ext, _load_plain)
    pages, raw_meta = loader(p)

    doc = Document(path=p)
    doc.pages = pages
    doc.full_text = "\n\n".join(f"[第 {pg.number} 页]\n{pg.text}" for pg in pages)
    meta = _extract_metadata(pages, raw_meta)
    doc.title = meta["title"] or p.stem
    doc.authors = meta["authors"]
    doc.year = meta["year"]
    doc.abstract = meta["abstract"]
    doc.doi = meta["doi"]
    doc.arxiv_id = meta["arxiv_id"]
    doc.meta = meta["raw_meta"]
    doc.sections = detect_sections(pages)
    for page in pages:
        pending = sum(t['status'] == 'needs_review' for t in page.tables)
        if pending:
            doc.warnings.append(f'第 {page.number} 页有 {pending} 个表格区域未可靠恢复行列；原始文本仍保留，该区域数值未纳入可引用片段。')

    if not doc.title or len(doc.title) < 8:
        doc.warnings.append("未能可靠识别标题，已使用文件名代替，建议人工确认。")
    if doc.page_count and sum(len(p.text) for p in pages) < 500:
        doc.warnings.append("提取到的文本很少，可能是扫描版 PDF（需要 OCR）或加密文档。")
    return doc


def summarize_document(doc: Document) -> str:
    secs = "、".join(s.name for s in doc.sections[:12])
    return (
        f"文件：{doc.path.name if doc.path else '-'}\n"
        f"标题：{truncate(doc.title, 100)}\n"
        f"作者：{', '.join(doc.authors[:5]) or '未识别'}\n"
        f"年份：{doc.year or '未识别'}   DOI：{doc.doi or '未识别'}   arXiv：{doc.arxiv_id or '无'}\n"
        f"页数：{doc.page_count}   章节：{secs}"
    )
