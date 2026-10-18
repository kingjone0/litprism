"""整篇实论文的真实模型重测：导入 → 解读 → 语义复核，输出陈述级结果。

用途：给「相邻/拆段证据补足」一个真实论文上的数字，而不是只有合成用例。
成本：5 页论文单次约数万 token（真实计费），所以必须显式传 --yes。

可比性：默认 PDF 是 fastText v3（SHA256 `6cbcf62…`），与
`docs/有界复核与短论文实测-2026-10-18.md` 记录的基线是同一份文件；
但模型有随机性，两次运行不能当成严格同分布比较。

用法：
    $PY tools/live_paper_run.py --pdf E:/litprism-live-paper/fasttext.pdf \
        --home E:/litprism-live-paper/data --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

# ProviderStore 的密钥存在默认数据根里。必须在 import litprism.config 之前设好，
# 否则 Config 会用 %LOCALAPPDATA%\LitPrism（那里没有 provider 记录）。
# 论文数据不受影响：下面会显式把 cfg.app.data_dir 指到 --home 的隔离目录。
os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')

BASE_URL = os.environ.get('LITPRISM_TEST_BASE_URL', 'https://api.commandcode.ai/provider/v1')
MODEL = os.environ.get('LITPRISM_TEST_MODEL', 'deepseek/deepseek-v4.1-flash')


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--pdf', required=True)
    p.add_argument('--home', required=True, help='隔离的数据根（不会被主库污染）')
    p.add_argument('--yes', action='store_true', help='确认会产生真实计费')
    p.add_argument('--concepts', action='store_true', help='同时抽取概念（更贵）')
    p.add_argument('--reconcile-only', action='store_true',
                   help='只重跑跨批比较（用上次落盘的陈述），成本约为整篇的 1/3')
    return p.parse_args()


def real_model():
    from litprism.config import Config, LLMConfig
    from litprism.db import KnowledgeBase
    from litprism.llm import OpenAICompatClient
    from litprism.providers import ProviderStore

    key = os.environ.get('LITPRISM_TEST_API_KEY')
    if not key:
        kb = KnowledgeBase(Config())
        try:
            store = ProviderStore(kb)
            key = store.secrets.get(next(p for p in store.list() if p.base_url == BASE_URL).api_key_ref)
        finally:
            kb.close()
    if not key:
        raise SystemExit('未找到该提供商的密钥；请先在应用里配置。')

    client = OpenAICompatClient(LLMConfig(provider='openai', base_url=BASE_URL, api_key=key,
                                          model=MODEL, timeout=180, max_tokens=8192))
    # 累计用量：单次调用的 last_usage 会被覆盖，需要自己加总。
    # 另外做一层**测试端**重试：这个端点偶发读超时（Codex 的记录里也有 ConnectTimeout），
    # 生产客户端的重试策略不动 —— 那是产品行为，不能为了跑通测试而放宽。
    usage = {'calls': 0, 'prompt': 0, 'completion': 0, 'total': 0}
    raw_chat = client.chat_json

    def counted(messages, **kwargs):
        last = None
        for attempt in range(3):
            try:
                out = raw_chat(messages, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                # 只重试**网络类**失败。输出上限、格式错误等是真实结果，重试只会多花钱。
                text = str(exc).lower()
                # 用类名判断，避免在模块级导入 litprism（该导入链要求先设好 LITPRISM_HOME）
                retryable = type(exc).__name__ != 'OutputLimitError' and any(
                    k in text for k in ('timed out', 'timeout', 'connection', 'temporarily'))
                if not retryable:
                    raise
                last = exc
                if attempt == 2:
                    raise
                time.sleep(2 + attempt * 3)
        else:  # pragma: no cover - 循环必然 break 或 raise
            raise last
        usage['calls'] += 1
        u = client.last_usage or {}
        usage['prompt'] += u.get('prompt_tokens') or 0
        usage['completion'] += u.get('completion_tokens') or 0
        usage['total'] += u.get('total_tokens') or 0
        return out

    client.chat_json = counted
    return client, usage


def main() -> int:
    args = parse_args()
    if not args.yes:
        raise SystemExit('这会真实调用付费模型；确认后加 --yes。')

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)

    from litprism.config import Config
    from litprism.db import KnowledgeBase
    from litprism.extract import analyze_paper
    from litprism.ingest import chunk_document, load_document

    cfg = Config()
    cfg.app.data_dir = str(home)
    for name in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
        getattr(cfg.sources, name).enabled = False

    kb = KnowledgeBase(cfg)
    try:
        if kb.list_papers():
            paper = kb.list_papers()[0]
            pid = paper['id']
            print(json.dumps({'step': 'reuse', 'paper': paper['title'][:60],
                              'chunks': len(kb.get_chunks(pid))}, ensure_ascii=False), flush=True)
        else:
            doc = load_document(Path(args.pdf))
            pid = kb.add_paper(title=doc.title, authors=doc.authors, year=doc.year,
                               abstract=doc.abstract, arxiv_id=doc.arxiv_id, source='local_pdf',
                               origin=str(args.pdf), full_text=doc.full_text,
                               verify_status='verified', verify_level='manual_confirmed',
                               verify_confidence=1.0, verify_evidence={'basis': 'live paper test'})
            chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
            n = kb.add_chunks(pid, chunks)
            print(json.dumps({'step': 'ingest', 'pages': doc.page_count, 'chunks': n,
                              'title': doc.title[:60]}, ensure_ascii=False), flush=True)

        if args.reconcile_only:
            from litprism.semantics import reconcile
            saved = json.loads((home / 'last_report.json').read_text(encoding='utf-8'))
            units = [dict(u, status='machine_supported') for u in saved['claims']]
            model, usage = real_model()
            start = time.monotonic()
            differences, coverage = reconcile(units, cfg, model)
            elapsed = round(time.monotonic() - start, 1)
            counts = {}
            for unit in units:
                counts[unit['status']] = counts.get(unit['status'], 0) + 1
            print(json.dumps({'step': 'reconcile-only', 'seconds': elapsed, 'claims': len(units),
                              'status_counts': counts, 'coverage': coverage,
                              'differences': len(differences), 'usage': usage}, ensure_ascii=False), flush=True)
            (home / 'last_reconcile.json').write_text(json.dumps(
                {'counts': counts, 'coverage': coverage, 'usage': usage,
                 'differences': differences}, ensure_ascii=False, indent=2), encoding='utf-8')
            return 0

        model, usage = real_model()
        start = time.monotonic()
        payload = analyze_paper(kb, cfg, model, pid, force=True, with_concepts=args.concepts)
        elapsed = round(time.monotonic() - start, 1)

        report = payload.get('_semantic_review') or {}
        items = report.get('items') or []
        counts = {}
        for item in items:
            counts[item['status']] = counts.get(item['status'], 0) + 1
        rows = [{'status': i['status'], 'field': i['field'],
                 'adjacent': i.get('adjacent_tags') or [],
                 'text': i['text'][:90],
                 'reason': (i.get('reason') or '')[:120]}
                for i in items if i['status'] != 'machine_supported']
        print(json.dumps({'step': 'analysis', 'seconds': elapsed,
                          'claims': len(items), 'status_counts': counts,
                          'passed': report.get('passed'), 'coverage': report.get('coverage', {}).get('complete'),
                          'differences': len(report.get('differences') or []),
                          'usage': usage}, ensure_ascii=False), flush=True)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        # 把 coverage / differences 也落盘：上一轮正是因为只存了 counts，
        # 「为什么全变待审」只能靠猜。
        (home / 'last_report.json').write_text(
            json.dumps({'counts': counts, 'usage': usage, 'seconds': elapsed,
                        'coverage': report.get('coverage'), 'differences': report.get('differences'),
                        'claims': items}, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0
    finally:
        kb.close()


if __name__ == '__main__':
    raise SystemExit(main())
