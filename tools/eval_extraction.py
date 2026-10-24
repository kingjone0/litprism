"""提取与核对质量的评测聚合。

从**已有的**审核记录算出可复现的指标，不新增任何数据采集：

| 指标 | 定义 | 说明 |
|---|---|---|
| 复核覆盖 | 已审核陈述 / 全部陈述 | 人到底看了多少 |
| 逐字精度 | 确认且未改动 / 已审核 | 机器陈述被原样接受的比例 |
| 修正率 | 确认但改过正文 / 已审核 | 方向对但表述需纠正 |
| 拒绝率 | 拒绝 / 已审核 | 机器说错了 |
| 可用率 | (逐字 + 修正) / 已审核 | 人工处理后能用的比例 |

**真实召回率尚不可测量**：人工补录只记录已经发现的遗漏，不能证明全部遗漏已采集。
已采集漏提占比用于描述样本，不冒充真实召回率；接口旧键 recall 保留兼容。

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


def collect(kb) -> list[dict]:
    """从 analyses 与 semantic_runs 收集 (报告, 审核记录, 人工补录)。"""
    rows = []

    # 1) 挂在论文解读上的核查
    for r in kb._con.execute("SELECT paper_id, payload FROM analyses").fetchall():
        pid = r["paper_id"]
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
        report = payload.get("_semantic_review") or {}
        digest = _payload_hash(payload)
        manual = _manual(kb, pid, digest)
        if not report.get('items') and not manual:
            continue
        reviews = [dict(x) for x in kb._con.execute(
            "SELECT * FROM semantic_reviews WHERE paper_id=? AND analysis_hash=? ORDER BY rowid",
            (pid, digest)).fetchall()]
        paper = kb.get_paper(pid) or {}
        rows.append({
            "source": "paper", "id": pid,
            "title": paper.get("display_title") or paper.get("title") or pid,
            "report": report, "reviews": reviews,
            "manual": manual,
        })

    # 2) 独立的核查运行
    for r in kb._con.execute("SELECT id, title, payload FROM semantic_runs").fetchall():
        payload = json.loads(r["payload"])
        report = payload.get("_semantic_review") or {}
        if not report.get("items"):
            continue
        reviews = [dict(x) for x in kb._con.execute(
            "SELECT * FROM semantic_reviews WHERE run_id=? ORDER BY rowid", (r["id"],)).fetchall()]
        rows.append({"source": "run", "id": r["id"], "title": r["title"],
                     "report": report, "reviews": reviews, "manual": []})

    return rows


def _payload_hash(payload: dict) -> str:
    from litprism.semantics import payload_hash

    return payload_hash(payload)


def _manual(kb, paper_id: str, digest: str) -> list[dict]:
    """人工补录（N05）。没有补录时返回空列表，已采集漏提占比仍显示 n/a。"""
    try:
        from litprism.manual_claims import list_claims

        return list_claims(kb, paper_id, digest)
    except Exception:  # noqa: BLE001 - 旧库没有该表时按未采集处理
        return []


def aggregate(row: dict) -> dict:
    """委托给 litprism.quality —— 界面面板与命令行必须同一套口径。"""
    from litprism.quality import aggregate as _aggregate

    return _aggregate(row["report"], row["reviews"], row.get("manual"))


def _pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%" if b else "—"


def report_row(row: dict) -> str:
    from litprism.quality import summarize
    summary = summarize(row['report'], row['reviews'], row.get('manual'))
    per_field = {f['field']: f for f in summary['fields']}
    lines = [f"\n■ {row['title']}  ({row['source']}:{row['id']})"]

    hdr = f"  {'字段':<10}{'陈述':>5}{'已审':>5}{'逐字':>5}{'修正':>5}{'拒绝':>5}{'逐字精度':>9}{'可用率':>8}{'复核覆盖':>9}"
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))

    tot = {"total": 0, "reviewed": 0, "verbatim": 0, "corrected": 0, "rejected": 0}
    for field, f in sorted(per_field.items(), key=lambda kv: -kv[1]["total"]):
        for k in tot:
            tot[k] += f[k]
        state = {'not_analyzed':'未解读', 'not_extracted':'未提取', 'manually_supplemented':'机器未提取/人工已补录',
                 'unreviewed':'未审核', 'partly_reviewed':'部分已审核', 'reviewed':'已审核'}.get(f['status'],'')
        lines.append(
            f"  {FIELD_LABELS.get(field, field):<10}{f['total']:>5}{f['reviewed']:>5}"
            f"{f['verbatim']:>5}{f['corrected']:>5}{f['rejected']:>5}"
            f"{_pct(f['verbatim'], f['reviewed']):>9}"
            f"{_pct(f['verbatim'] + f['corrected'], f['reviewed']):>8}"
            f"{_pct(f['reviewed'], f['total']):>9}  {state}"
        )
    lines.append(
        f"  {'合计':<10}{tot['total']:>5}{tot['reviewed']:>5}{tot['verbatim']:>5}"
        f"{tot['corrected']:>5}{tot['rejected']:>5}"
        f"{_pct(tot['verbatim'], tot['reviewed']):>9}"
        f"{_pct(tot['verbatim'] + tot['corrected'], tot['reviewed']):>8}"
        f"{_pct(tot['reviewed'], tot['total']):>9}"
    )
    manual = row.get("manual") or []
    machine = summary['totals']['total']
    if summary['totals']['recall'] is not None:
        total = machine + len(manual)
        lines.append(
            f"  已采集漏提占比（漏提）：人工补录 {len(manual)} 条 / 机器 {machine} 条 → "
            f"{summary['totals']['recall'] * 100:.0f}%（分母 {total}）"
        )
    else:
        lines.append('  已采集漏提占比：n/a —— ' + summary['note'])
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
