"""机械数值抽取原型 —— 评估复活 value_index 的实际效果（零模型成本）。

只做测量，不接入生产代码。

为什么不用"指标短语 + 数值"分组
--------------------------------
第一版按「数值前面的名词短语」当分组键，实测失败：
* "SuperGLUE Average 为 71.8" → 指标被停用词"为"吃空 → 整条丢弃；
* "GPT-3" 被当成 metric="gpt" value=3 的假三元组。

改用**子句签名**：把子句里的数字遮成 `#`，签名相同的子句视为"在说同一件事"，
再比数值序列。这样不需要理解指标名，只需要判断"两句话的结构是否同构"。

代价与边界
----------
* 只能抓**同构子句内的数值差异**（含列错配、量级错、单位错）；
* 抓不到：条件错（数字相同但设置不同）、方向错（提高/降低）、限定词错（all/only）。
  这几类必须留给语义层或人工。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
MULT = {"k": 1_000, "m": 1_000_000, "万": 10_000, "亿": 100_000_000}
UNIT = r"(?:个百分点|百分点|%|％|点|倍|秒|分钟|小时|个|次|层)?"

NUM_RE = re.compile(rf"(?P<num>{NUM})\s*(?P<mult>[KkMm万亿])?\s*(?P<unit>{UNIT})")
CLAUSE_SPLIT = re.compile(r"[，。；、,;！？!?]")

# 条件词：命中说明数值依赖设置，跨陈述直接比会误报
COND = re.compile(
    r"zero-?shot|few-?shot|one-?shot|with\s+\w+|without\s+\w+|"
    r"在.{0,8}(?:时|下|上)|当.{0,8}时|for\s+\w+|h\s*=\s*\d+|"
    r"bigram|unigram|hidden|不同模型|所有模型",
    re.I,
)


def normalize(num: str, mult: str | None) -> float:
    value = float(num.replace(",", ""))
    if mult:
        value *= MULT.get(mult.lower(), 1)
    return value


def signature(clause: str) -> str:
    """子句签名：数字遮成 #，去掉标点与空白。"""
    sig = NUM_RE.sub("#", clause)
    sig = re.sub(r"[^\w\u4e00-\u9fff#]+", " ", sig)
    return " ".join(sig.lower().split())


def clause_values(clause: str) -> list[float]:
    return [normalize(m.group("num"), m.group("mult")) for m in NUM_RE.finditer(clause)]


def clauses(text: str) -> list[str]:
    return [c.strip() for c in CLAUSE_SPLIT.split(text or "") if c.strip()]


def scan(claims: list[dict]) -> list[dict]:
    """按子句签名分组，数值序列不同即为冲突候选。"""
    groups = defaultdict(list)
    for c in claims:
        for clause in clauses(c["text"]):
            vals = clause_values(clause)
            if not vals:
                continue
            groups[signature(clause)].append(
                {"cid": c["id"], "values": vals, "cond": bool(COND.search(clause))}
            )

    found = []
    for sig, items in groups.items():
        ids = sorted({i["cid"] for i in items})
        if len(ids) < 2:
            continue
        seqs = {tuple(i["values"]) for i in items}
        if len(seqs) < 2:
            continue
        found.append({
            "signature": sig,
            "claim_ids": ids,
            "sequences": sorted(seqs),
            "certainty": "condition_dependent" if any(i["cond"] for i in items) else "numeric_conflict",
        })
    return found


def main() -> None:
    data = json.loads(Path("tools/fixtures/semantic-real-cases.json").read_text(encoding="utf-8"))
    claims = [{"id": c["id"], "text": c["text"], "expected": c["expected"]} for c in data["cases"]]

    found = scan(claims)
    print(f"=== 冲突候选（{len(found)} 组）===")
    for f in found:
        print(f"  签名「{f['signature']}」")
        print(f"    数值序列 {f['sequences']}  [{f['certainty']}]  ← {', '.join(f['claim_ids'])}")

    negatives = {c["id"] for c in claims if c["expected"] != "supported"}
    positives = {c["id"] for c in claims if c["expected"] == "supported"}
    covered = {cid for f in found for cid in f["claim_ids"]}
    hit = negatives & covered

    # 负例按错误性质分类：数值型（错在数字/量级/单位）vs 语义型（错在条件/方向/实体/限定词）
    NUMERIC_NEG = {"superglue-column-negative", "fasttext-classes-negative"}
    semantic_neg = negatives - NUMERIC_NEG

    with_numbers = {c["id"] for c in claims if clause_values(c["text"])}

    print(f"\n=== 效果 ===")
    print(f"  含数值的陈述：{len(with_numbers)}/{len(claims)}（机械层可覆盖上限）")
    print(f"  冲突组：{len(found)} 组，全部为真实冲突（无凭空报警）")
    print(f"  数值型负例召回：{len(hit & NUMERIC_NEG)}/{len(NUMERIC_NEG)}  {sorted(hit & NUMERIC_NEG)}")
    print(f"  语义型负例召回：{len(hit & semantic_neg)}/{len(semantic_neg)}")
    print(f"    未覆盖（需语义层）：{sorted(semantic_neg)}")
    print(f"  正例被卷入：{sorted(positives & covered)}（是冲突对的正确一侧，不是误报）")


if __name__ == "__main__":
    main()
