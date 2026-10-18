"""真实模型跑一次完整解读，输出可核对的指标。

用途：给 P-1~P-5b 的实现一个真实数字，而不是只有桩模型的结果。
成本：Attention（8.5k token）一次约 4 万 token，必须显式传 --yes。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import load_config
from litprism.db import KnowledgeBase
from litprism.extract import analyze_paper
from litprism.llm import build_llm_for
from litprism.providers import ProviderStore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True, help="论文标题片段")
    ap.add_argument("--window", type=int, default=0, help="覆盖模型窗口（默认读配置）")
    ap.add_argument("--max-tokens", type=int, default=0, help="覆盖输出预算（推理模型需要更大）")
    ap.add_argument("--skip-review", action="store_true", help="跳过语义复核，只跑提取与闸门")
    ap.add_argument("--yes", action="store_true", help="确认产生真实费用")
    ap.add_argument("--out", default="", help="把结果写到这个 JSON 路径")
    args = ap.parse_args()

    if not args.yes:
        print("会产生真实调用与费用，确认请加 --yes")
        return 2

    cfg = load_config()
    kb = KnowledgeBase(cfg)
    store = ProviderStore(kb, cfg.data_path)
    provider, model = store.get_active()
    if provider is None:
        print("没有生效的提供商")
        return 1

    if args.window:
        store.set_models(provider.id, [
            {"model_id": m.model_id, "display_name": m.display_name,
             "enabled": m.enabled, "window_tokens": args.window if m.model_id == model else m.window_tokens}
            for m in provider.models
        ])

    key = store.secrets.get(provider.api_key_ref)
    llm = build_llm_for(cfg, base_url=provider.base_url, api_key=key,
                        model=model, protocol=provider.protocol,
                        max_tokens=args.max_tokens or None)
    if args.skip_review:
        llm.supports_semantic_review = False
    paper = next((p for p in kb.list_papers() if args.paper.lower() in (p.get("title") or "").lower()), None)
    if paper is None:
        print(f"没找到匹配 {args.paper!r} 的论文")
        return 1

    print(f"模型 {model} | 论文 {(paper.get('title') or '')[:40]}")
    t0 = time.time()
    try:
        payload = analyze_paper(kb, cfg, llm, paper["id"], force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{type(exc).__name__}: {exc}")
        return 1
    elapsed = time.time() - t0

    cov = payload.get("_coverage") or {}
    plan = cov.get("plan") or {}
    usage = cov.get("usage") or {}
    review = payload.get("_semantic_review") or {}

    print(f"\n耗时 {elapsed:.0f}s")
    print(f"读取模式 {cov.get('reading_mode')} | 指令 {len(plan.get('directives') or [])} 条 | "
          f"上下文 {plan.get('context_tokens'):,} token" if plan.get('context_tokens') else "")
    print(f"\n用量：{usage.get('calls')} 次调用 | 入 {usage.get('input', 0):,} 出 {usage.get('output', 0):,}")
    print(f"缓存：{usage.get('note')}")

    print("\n闸门：")
    for g in cov.get("gates") or []:
        fails = [r for r in g["results"] if not r["ok"]]
        detail = ' | '.join(f'{r["name"]}: {r["detail"]}' for r in fails) or '全部通过'
        print(f"  {g['unit']}: {detail}")

    if cov.get("overlaps"):
        print("\n重复告警：")
        for o in cov["overlaps"]:
            print(f"  {o['detail']}")

    print(f"\n语义复核：{review.get('status')} | 支持 {review.get('supported', 0)} / "
          f"待审 {review.get('pending', 0)} | 差异 {len(review.get('differences') or [])} 组")
    covr = review.get("coverage") or {}
    if covr:
        print(f"  覆盖：complete={covr.get('complete')} 调用 {covr.get('calls')}/{covr.get('max_calls')} "
              f"字段已核 {covr.get('fields_done')} 未核 {covr.get('fields_pending')}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "paper": paper.get("title"), "model": model, "elapsed_s": round(elapsed, 1),
            "coverage": cov, "semantic": review,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.out}")
    kb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
