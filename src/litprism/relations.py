"""跨论文关联分析：TF-IDF 余弦相似度 + 共享术语/方法线索。

用于回答"这几篇论文之间是什么关系""这个方向上有哪些聚类"。
纯词法统计，不做语义臆测；相似度只是线索，最终判断仍以内容对比为准。
"""

from __future__ import annotations

import math
import re
from collections import Counter

from .db import KnowledgeBase
from .textutil import cjk_segment, contains_cjk, is_cjk_char

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "was",
    "were", "be", "been", "this", "that", "these", "those", "we", "our", "it", "its", "as",
    "by", "at", "from", "can", "may", "also", "such", "which", "have", "has", "had", "not",
    "but", "however", "therefore", "thus", "using", "used", "use", "based", "show", "shows",
    "result", "results", "method", "methods", "paper", "propose", "proposed", "however",
    "的", "了", "是", "在", "和", "与", "对", "为", "以", "及", "等", "我们", "本文", "该",
    "这", "那", "有", "被", "从", "到", "中", "上", "下", "个", "方法", "研究", "结果",
}
_WORD_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")


def tokenize(text: str) -> list[str]:
    """英文取词干化后的词；中文取字符 bigram（比单字更能表达术语）。"""
    if not text:
        return []
    toks: list[str] = []
    low = text.lower()
    for m in _WORD_RE.finditer(low):
        w = m.group(0)
        if w in _STOPWORDS or len(w) < 3:
            continue
        toks.append(w)
    # 中文 bigram
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    for run in cjk_runs:
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg in _STOPWORDS:
                continue
            toks.append(bg)
    return toks


def build_vectors(kb: KnowledgeBase, paper_ids: list[str] | None = None) -> tuple[list[str], list[Counter], dict[str, float]]:
    papers = kb.list_papers(limit=1000)
    if paper_ids:
        papers = [p for p in papers if p["id"] in set(paper_ids)]
    ids: list[str] = []
    counters: list[Counter] = []
    for p in papers:
        chunks = kb.get_chunks(p["id"])
        text = "\n".join(c["text"] for c in chunks)
        if not text.strip():
            text = f"{p['title']}\n{p.get('abstract') or ''}"
        if not text.strip():
            continue
        ids.append(p["id"])
        counters.append(Counter(tokenize(text)))

    df: Counter = Counter()
    for c in counters:
        for t in c:
            df[t] += 1
    n = max(1, len(counters))
    idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items()}
    return ids, counters, idf


def _tfidf_vector(counter: Counter, idf: dict[str, float]) -> dict[str, float]:
    total = sum(counter.values()) or 1
    return {t: (c / total) * idf.get(t, 1.0) for t, c in counter.items() if t in idf}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def top_shared_terms(a: Counter, b: Counter, idf: dict[str, float], k: int = 8) -> list[str]:
    shared = set(a) & set(b)
    ranked = sorted(shared, key=lambda t: -(idf.get(t, 1.0) * min(a[t], b[t])))
    return [t for t in ranked[:k]]


def compute_relations(
    kb: KnowledgeBase, *, threshold: float = 0.18, paper_ids: list[str] | None = None, persist: bool = True
) -> list[dict]:
    """计算论文两两相似度，返回并按需写入 relations 表。"""
    ids, counters, idf = build_vectors(kb, paper_ids)
    vecs = [_tfidf_vector(c, idf) for c in counters]
    titles = {p["id"]: p["title"] for p in kb.list_papers(limit=1000)}

    out: list[dict] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sim = cosine(vecs[i], vecs[j])
            if sim < threshold:
                continue
            terms = top_shared_terms(counters[i], counters[j], idf, k=8)
            evidence = "共享高频术语：" + "、".join(terms)
            rec = {
                "paper_a": ids[i],
                "paper_b": ids[j],
                "title_a": titles.get(ids[i], ids[i]),
                "title_b": titles.get(ids[j], ids[j]),
                "rel_type": "similar",
                "score": round(sim, 4),
                "evidence": evidence,
                "shared_terms": terms,
            }
            out.append(rec)
            if persist:
                kb.upsert_relation(ids[i], ids[j], "similar", sim, evidence)
    out.sort(key=lambda x: -x["score"])
    return out


def cluster(kb: KnowledgeBase, *, threshold: float = 0.2, paper_ids: list[str] | None = None) -> list[list[str]]:
    """基于相似度阈值的简单连通分量聚类，给出"研究主题簇"。"""
    ids, counters, idf = build_vectors(kb, paper_ids)
    vecs = [_tfidf_vector(c, idf) for c in counters]
    parent = {i: i for i in range(len(ids))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if cosine(vecs[i], vecs[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[str]] = {}
    for i, pid in enumerate(ids):
        groups.setdefault(find(i), []).append(pid)
    return [g for g in groups.values() if len(g) >= 1]


def field_map(kb: KnowledgeBase) -> dict:
    """生成领域知识图谱的原始数据：节点（论文）+ 边（相似度）+ 术语共现。"""
    papers = kb.list_papers(limit=1000)
    rels = compute_relations(kb, persist=False)
    concepts: Counter = Counter()
    for c in kb.list_concepts(limit=5000):
        concepts[c["term"]] += 1
    return {
        "nodes": [
            {
                "id": p["id"],
                "title": p["title"],
                "year": p.get("year"),
                "verify_level": p.get("verify_level"),
                "degree": sum(1 for r in rels if p["id"] in (r["paper_a"], r["paper_b"])),
            }
            for p in papers
        ],
        "edges": [
            {"source": r["paper_a"], "target": r["paper_b"], "weight": r["score"], "evidence": r["evidence"]}
            for r in rels
        ],
        "top_terms": concepts.most_common(30),
    }
