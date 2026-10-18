"""提取与核对质量的评测聚合。

从**已有的**审核记录算出可复现的指标，不新增任何数据采集：

| 指标 | 定义 | 说明 |
|---|---|---|
| 复核覆盖 | 已审核陈述 / 全部陈述 | 人到底看了多少 |
| 逐字精度 | 确认且未改动 / 已审核 | 机器陈述被原样接受的比例 |
| 修正率 | 确认但改过正文 / 已审核 | 方向对但表述需纠正 |
| 拒绝率 | 拒绝 / 已审核 | 机器说错了 |
| 可用率 | (逐字 + 修正) / 已审核 | 人工处理后能用的比例 |

**召回（漏提）当前无法测量** —— 审核界面没有"补一条机器没提到的陈述"的入口。
这里如实显示 `n/a`，不用任何代理指标冒充。

用法::

    python tools/eval_extraction.py            # 读主库
    python tools/eval_extraction.py --demo     # 合成数据自检（不碰主库）
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FIELD_LABELS = {
    "research_question": "研究问题", "methodology": "方法", "experiment_design": "实验设计",
    "key_conclusions": "主要结论", "innovations": "创新点", "limitations": "局限",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def collect(kb) -> list[dict]:
    """从 analyses 与 semantic_runs 收集 (报告, 审核记录)。"""
    rows = []

    # 1) 挂在论文解读上的核查
    for r in kb._con.execute("SELECT paper_id, payload FROM analyses").fetchall():
        pid = r["paper_id"]
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
        report = payload.get("_semantic_review") or {}
        if not report.get("items"):
            continue
        digest = _payload_hash(payload)
        reviews = [dict(x) for x in kb._con.execute(
            "SELECT * FROM semantic_reviews WHERE paper_id=? AND analysis_hash=? ORDER BY rowid",
            (pid, digest)).fetchall()]
        paper = kb.get_paper(pid) or {}
        rows.append({
            "source": "paper", "id": pid,
            "title": paper.get("display_title") or paper.get("title") or pid,
            "report": report, "reviews": reviews,
        })

    # 2) 独立的核查运行
    for r in kb._con.execute("SELECT id, title, payload FROM semantic_runs").fetchall():
        payload = json.loads(r["payload"])
        report = payload.get("_semantic_review") or {}
        if not report.get("items"):
            continue
        reviews = [dict(x) for x in kb._con.execute(
            "SELECT * FROM semantic_reviews WHERE run_id=? ORDER BY rowid", (r["id"],)).fetchall()]
        rows.append({"source": "run", "id": r["id"], "title": r["title"], "report": report, "reviews": reviews})

    return rows


def _payload_hash(payload: dict) -> str:
    from litprism.semantics import payload_hash

    return payload_hash(payload)


def aggregate(row: dict) -> dict:
    items = {i["id"]: i for i in row["report"].get("items", [])}

    # 同一陈述可能有多条记录（确认→撤销→再确认），以**最后一条**为准
    last: dict[str, dict] = {}
    for rec in row["reviews"]:
        last[rec["claim_id"]] = rec

    per_field = defaultdict(lambda: {
        "total": 0, "reviewed": 0, "verbatim": 0, "corrected": 0, "rejected": 0,
        "machine_supported": 0,
    })
    for cid, item in items.items():
        f = per_field[item.get("field") or "正文"]
        f["total"] += 1
        if item.get("status") == "machine_supported":
            f["machine_supported"] += 1

    for cid, rec in last.items():
        item = items.get(cid)
        if item is None:
            continue  # 陈述来自旧版本，已不在当前报告里
        f = per_field[item.get("field") or "正文"]
        if rec["action"] == "revoke":
            continue  # 撤销是回退动作，不计入审核样本
        f["reviewed"] += 1
        if rec["action"] == "reject":
            f["rejected"] += 1
        elif rec["action"] == "confirm":
            if _norm(rec.get("reviewed_text")) and _norm(rec.get("reviewed_text")) != _norm(item.get("text")):
                f["corrected"] += 1
            else:
                f["verbatim"] += 1

    return per_field


def _pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%" if b else "—"


def report_row(row: dict) -> str:
    per_field = aggregate(row)
    lines = [f"\n■ {row['title']}  ({row['source']}:{row['id']})"]

    hdr = f"  {'字段':<10}{'陈述':>5}{'已审':>5}{'逐字':>5}{'修正':>5}{'拒绝':>5}{'逐字精度':>9}{'可用率':>8}{'复核覆盖':>9}"
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))

    tot = {"total": 0, "reviewed": 0, "verbatim": 0, "corrected": 0, "rejected": 0}
    for field, f in sorted(per_field.items(), key=lambda kv: -kv[1]["total"]):
        for k in tot:
            tot[k] += f[k]
        lines.append(
            f"  {FIELD_LABELS.get(field, field):<10}{f['total']:>5}{f['reviewed']:>5}"
            f"{f['verbatim']:>5}{f['corrected']:>5}{f['rejected']:>5}"
            f"{_pct(f['verbatim'], f['reviewed']):>9}"
            f"{_pct(f['verbatim'] + f['corrected'], f['reviewed']):>8}"
            f"{_pct(f['reviewed'], f['total']):>9}"
        )
    lines.append(
        f"  {'合计':<10}{tot['total']:>5}{tot['reviewed']:>5}{tot['verbatim']:>5}"
        f"{tot['corrected']:>5}{tot['rejected']:>5}"
        f"{_pct(tot['verbatim'], tot['reviewed']):>9}"
        f"{_pct(tot['verbatim'] + tot['corrected'], tot['reviewed']):>8}"
        f"{_pct(tot['reviewed'], tot['total']):>9}"
    )
    lines.append("  召回（漏提）：n/a —— 审核界面没有「补一条机器未提及的陈述」入口，无法测量")
    if tot["reviewed"] == 0:
        lines.append("  ⚠️ 尚无人工审核记录：上面的覆盖为 0，指标不可用。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def demo() -> None:
    """合成一份带审核记录的数据，验证指标算法。"""
    from litprism.config import Config
    from litprism.db import KnowledgeBase

    tmp = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.app.data_dir = str(tmp)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp / "demo.db")

    claims = [
        # (field, text, status, action, reviewed_text)
        ("key_conclusions", "GPT-3 在 PTB 上困惑度为 20.50。", "machine_supported", "confirm", None),
        ("key_conclusions", "SuperGLUE Average 为 71.8。", "machine_supported", "confirm", "SuperGLUE Average 为 71.8，RTE 为 69.0。"),
        ("key_conclusions", "fastText 在所有任务都超过深度学习分类器。", "machine_supported", "reject", None),
        ("methodology", "使用 Adam 优化器。", "machine_supported", "confirm", None),
        ("methodology", "训练 100 万步。", "needs_review", None, None),
        ("limitations", "未讨论低资源语言。", "needs_review", None, None),
    ]
    report = {"items": [
        {"id": f"cl_{i}", "field": f, "text": t, "status": s,
         "reason": "", "quotes": [], "candidate_tags": [], "batch": 1}
        for i, (f, t, s, _, _) in enumerate(claims)
    ], "coverage": {"complete": True}}
    payload = {"_semantic_review": report}
    digest = _payload_hash(payload)

    with kb.tx() as con:
        con.execute("INSERT INTO papers (id, title, created_at) VALUES (?,?,?)",
                    ("p1", "演示论文", _now()))
        con.execute("INSERT INTO analyses (paper_id, payload, created_at) VALUES (?,?,?)",
                    ("p1", json.dumps(payload, ensure_ascii=False), _now()))
        for i, (f, t, s, action, reviewed) in enumerate(claims):
            if not action:
                continue
            con.execute(
                "INSERT INTO semantic_reviews (id, paper_id, run_id, analysis_hash, claim_id,"
                " action, note, reviewed_text, knowledge_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"r{i}", "p1", None, digest, f"cl_{i}", action, "人工核对依据",
                 reviewed if reviewed is not None else t, None, _now()),
            )

    rows = collect(kb)
    for row in rows:
        print(report_row(row))

    print("\n期望：逐字 2（20.50 / Adam）、修正 1（SuperGLUE）、拒绝 1（all tasks）")
    print("      逐字精度 = 2/4 = 50%，可用率 = 3/4 = 75%，复核覆盖 = 4/6 = 67%")
    kb.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="提取与核对质量评测")
    ap.add_argument("--demo", action="store_true", help="用合成数据自检，不碰主库")
    args = ap.parse_args()

    if args.demo:
        demo()
        return 0

    from litprism.config import load_config
    from litprism.db import KnowledgeBase

    kb = KnowledgeBase(load_config())
    rows = collect(kb)
    if not rows:
        print("主库里没有语义核查报告，无指标可算。")
        print("先跑一次解读（会产生 _semantic_review），再到界面里做人工审核。")
        kb.close()
        return 0
    for row in rows:
        print(report_row(row))
    kb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
