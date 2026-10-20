"""N02 整篇长论文验收：取全文 → 真实模型解读 → 出指标与人工复核清单。

只做三件事，不替人下结论：
1. 用现有 arXiv 管道取回一篇**长文**并入库（材料完整度应为 fulltext）；
2. 用当前生效模型跑一次整篇解读，记录调用/token/缓存/失败与覆盖情况；
3. 输出 JSON 与 Markdown：指标 + 每个字段的陈述清单（供人工抽样核对）。

用法：
    $PY tools/fulltext_acceptance.py --arxiv 2312.10997 --home E:/litprism-live-paper/data --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--arxiv', required=True)
    ap.add_argument('--home', required=True)
    ap.add_argument('--yes', action='store_true', help='确认真实付费调用')
    ap.add_argument('--out', default=None)
    ap.add_argument('--no-review', action='store_true',
                    help='跳过机器语义复核（整篇复核在当前协议下会超时，先取提取层证据）')
    args = ap.parse_args()
    if not args.yes:
        raise SystemExit('真实解读会产生费用；确认后加 --yes。')

    os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    from litprism.llm import OpenAICompatClient
    from litprism.candidates import save_candidates
    from litprism.providers import ProviderStore, apply_active_window
    from litprism.remote import fetch_candidate_pdf
    from litprism.verify import PaperVerifier

    # 能力与凭据来自真实提供商库；论文写进隔离库，不碰用户主库
    provider_kb = KnowledgeBase(Config())
    try:
        store = ProviderStore(provider_kb)
        provider, model_id = store.get_active()
        if provider is None:
            raise SystemExit('未选择模型，无法做真实验收。')
        key = store.secrets.get(provider.api_key_ref)
    finally:
        provider_kb.close()

    cfg = Config()
    cfg.app.data_dir = args.home
    provider_cfg = Config()
    cap_kb = KnowledgeBase(provider_cfg)
    try:
        apply_active_window(provider_cfg, cap_kb, require_active=True)
    finally:
        cap_kb.close()
    cfg.llm = provider_cfg.llm
    cfg.context = provider_cfg.context
    cfg.llm.provider, cfg.llm.base_url, cfg.llm.api_key = provider.protocol, provider.base_url, key
    cfg.llm.model = model_id
    # 整篇复核单次读取远超默认 60s；显式放宽，否则会在复核阶段被 10s 掐断
    cfg.llm.timeout = max(int(cfg.llm.timeout or 0), 180)
    cfg.llm.review_read_timeout = 180
    os.environ['LITPRISM_REVIEW_READ_TIMEOUT'] = '180'
    os.environ.setdefault('LITPRISM_REVIEW_CONNECT_TIMEOUT', '60')

    kb = KnowledgeBase(cfg)
    try:
        before = {p['id'] for p in kb.list_papers()}
        pool = save_candidates(kb, [{'title': f'arXiv {args.arxiv}', 'arxiv_id': args.arxiv,
                                     'url': f'https://arxiv.org/abs/{args.arxiv}'}],
                               query='N02 整篇验收', kind='paper', source='manual')
        cid = (pool['added'] or pool['existing'])[0]
        fetched = fetch_candidate_pdf(kb, cfg, PaperVerifier(cfg), cid)
        pid = fetched['paper_id']
        paper = kb.get_paper(pid)
        print(json.dumps({'fetched': {k: fetched.get(k) for k in
                                      ('pages', 'chunks', 'title', 'verify_level')},
                          'material_level': paper['material_level']}, ensure_ascii=False), flush=True)

        from litprism.extract import analyze_paper

        class _ProgressClient(OpenAICompatClient):
            """每次调用打一行进度，避免整篇跑十几分钟时界面毫无输出。"""
            def __init__(self, llm_cfg):
                super().__init__(llm_cfg)
                self._n = 0

            def chat_json(self, messages, task='chat'):
                self._n += 1
                started = time.monotonic()
                print(f'[{self._n}] {task} …', end='', flush=True)
                try:
                    out = super().chat_json(messages, task=task)
                except Exception as exc:  # 偶发读超时：重试一次，仍失败才放弃
                    print(f' {time.monotonic() - started:.0f}s 失败：{str(exc)[:60]}，重试…', end='', flush=True)
                    time.sleep(3)
                    out = super().chat_json(messages, task=task)
                print(f' {time.monotonic() - started:.0f}s', flush=True)
                return out

        client = _ProgressClient(cfg.llm)
        started = time.monotonic()
        payload = analyze_paper(kb, cfg, client, pid, force=True, with_concepts=False,
                                semantic_review=not args.no_review)
        seconds = time.monotonic() - started

        coverage = payload.get('_coverage') or {}
        plan = coverage.get('plan') or {}
        usage = coverage.get('usage') or {}
        sem = payload.get('_semantic_review') or {}
        items = sem.get('items') or []
        counts = Counter(str(i.get('status') or '') for i in items)
        fields = ('research_question', 'methodology', 'experiment_design', 'key_conclusions',
                  'innovations', 'limitations')
        report = {
            'arxiv': args.arxiv, 'paper_id': pid, 'title': paper['title'],
            'material_level': paper['material_level'], 'pages': fetched.get('pages'),
            'chunks': fetched.get('chunks'), 'seconds': round(seconds, 1),
            'mode': plan.get('mode'), 'directives': len(plan.get('directives') or []),
            'batches': coverage.get('batches'), 'coverage_complete': coverage.get('complete'),
            'uncovered_units': len(coverage.get('uncovered_units') or []),
            'claims': len(items), 'claims_source': 'review_items' if items else 'candidates',
            'status_counts': dict(counts), 'semantic_status': sem.get('status'),
            'usage': usage, 'fields_nonempty': {f: bool((payload.get(f) or '').strip()) for f in fields},
            'field_claims': {f: [i.get('text') for i in items if i.get('field') == f] for f in fields},
            'field_notes': payload.get('field_notes'),
        }
        out = Path(args.out or Path(args.home) / 'n02-reports')
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        (out / f'n02-{stamp}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
        lines = [f'# N02 整篇验收 · arXiv {args.arxiv}', '',
                 f"- 论文：{report['title']}",
                 f"- 规模：{report['pages']} 页 / {report['chunks']} 块 / 材料 {report['material_level']}",
                 f"- 策略：{report['mode']}（{report['directives']} 条指令）",
                 f"- 覆盖：{'完整' if report['coverage_complete'] else '不完整'}，未覆盖 {report['uncovered_units']} 段",
                 f"- 陈述：{report['claims']} 条 · 机器支持 {counts.get('machine_supported', 0)} · "
                 f"待人工 {counts.get('needs_review', 0)} · 机器判不符 {counts.get('contradicted', 0)}",
                 f"- 用量：调用 {usage.get('calls', 0)} · 输入 {usage.get('input', 0)} · "
                 f"输出 {usage.get('output', 0)} · 缓存 {usage.get('cache_hit', 0)} · "
                 f"失败 {usage.get('failed_calls', 0)}",
                 f"- 耗时：{report['seconds']}s", '',
                 '## 字段覆盖', '',
                 '| 字段 | 有内容 | 陈述数 |', '|---|---|---|']
        for f in fields:
            lines.append(f"| {f} | {'是' if report['fields_nonempty'][f] else '否'} | "
                         f"{len(report['field_claims'][f] or [])} |")
        lines += ['', '## 待人工核对清单', '',
                  '> 下面是机器认为"有原文支撑"的陈述；**必须人工抽查**，机器支持不等于结论正确。', '']
        for f in fields:
            for text in (report['field_claims'][f] or [])[:8]:
                lines.append(f'- [{f}] {text}')
        (out / f'n02-{stamp}.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('pages', 'chunks', 'mode', 'claims', 'status_counts',
                                                 'coverage_complete', 'uncovered_units', 'usage',
                                                 'seconds')}, ensure_ascii=False), flush=True)
        print(f'报告：{out / f"n02-{stamp}.md"}')
        return 0
    finally:
        kb.close()


if __name__ == '__main__':
    raise SystemExit(main())
