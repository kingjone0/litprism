"""真实样例的全流程验证：导入结果 → 解读（复核开/关）→ 人工确认入库。

与 ``live_paper_run.py`` 的区别：这里**走桌面同一条能力路径**（``apply_active_window``
把 API 读到的窗口/最大输出写进 cfg），而不是自建 LLMConfig 落到 legacy 4096 分支。

成本：整篇 5 页论文两次解读 + 一次人工入库，约几万 token，必须显式 --yes。

用法：
    $PY tools/live_full_pipeline.py --home E:/litprism-live-paper/data --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')

BASE_URL = os.environ.get('LITPRISM_TEST_BASE_URL', 'https://api.commandcode.ai/provider/v1')
MODEL = os.environ.get('LITPRISM_TEST_MODEL', 'deepseek/deepseek-v4.1-flash')


def real_client(cfg):
    from litprism.config import Config
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
        raise SystemExit('未找到该提供商的密钥。')
    # 必须复用 apply_active_window 解析后的 cfg.llm：自建 LLMConfig 会丢掉
    # automatic_output/窗口，退回 legacy 的 8192 —— 真实运行里已经因此截断过一次。
    cfg.llm.provider, cfg.llm.base_url, cfg.llm.api_key = 'openai', BASE_URL, key
    cfg.llm.model = MODEL
    cfg.llm.timeout = max(int(cfg.llm.timeout or 0), 300)
    client = OpenAICompatClient(cfg.llm)
    usage = {'calls': 0, 'prompt': 0, 'completion': 0, 'total': 0, 'cached': 0}
    raw = client.chat_json

    def counted(messages, **kwargs):
        for attempt in range(3):
            try:
                out = raw(messages, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                text = str(exc).lower()
                if type(exc).__name__ == 'OutputLimitError' or not any(
                        k in text for k in ('timed out', 'timeout', 'connection', 'temporarily')):
                    raise
                if attempt == 2:
                    raise
                time.sleep(2 + attempt * 3)
        usage['calls'] += 1
        u = client.last_usage or {}
        usage['prompt'] += u.get('prompt_tokens') or 0
        usage['completion'] += u.get('completion_tokens') or 0
        usage['total'] += u.get('total_tokens') or 0
        usage['cached'] += (u.get('prompt_tokens_details') or {}).get('cached_tokens') or 0
        return out

    client.chat_json = counted
    return client, usage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--home', required=True)
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--only', choices=['review_on', 'review_off'], default=None,
                    help='只跑其中一种情况，省一半费用')
    args = ap.parse_args()
    if not args.yes:
        raise SystemExit('这会真实调用付费模型；确认后加 --yes。')

    from litprism.config import Config
    from litprism.db import KnowledgeBase
    from litprism.extract import analyze_paper
    from litprism.providers import apply_active_window

    home = Path(args.home)
    cfg = Config()
    cfg.app.data_dir = str(home)
    kb = KnowledgeBase(cfg)
    report = {'steps': []}
    try:
        papers = kb.list_papers()
        if not papers:
            raise SystemExit('隔离库里没有论文，请先导入。')
        pid = papers[0]['id']
        report['paper'] = {'id': pid, 'title': papers[0]['title'], 'chunks': len(kb.get_chunks(pid))}

        # 与桌面一致：能力从提供商库解析后写进 cfg。
        # 前提是"当前生效模型"已选定；没选就回落到 32000/legacy，等于没接线。
        provider_kb = KnowledgeBase(Config())
        try:
            from litprism.providers import ProviderStore
            store = ProviderStore(provider_kb)
            provider, model_id = store.get_active()
            if provider is None:
                first = next((p for p in store.list() if p.models), None)
                if first is not None:
                    with provider_kb.tx() as con:
                        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('active_provider',?)",
                                    (first.id,))
                    store.set_active_model(first.id, first.models[0].model_id)
                    provider, model_id = store.get_active()
            limits = apply_active_window(cfg, provider_kb)
            report['active_model'] = {'provider': getattr(provider, 'id', None), 'model': model_id}
        finally:
            provider_kb.close()
        report['capability'] = {'window': cfg.context.window_tokens,
                                'max_output': cfg.llm.model_max_output_tokens,
                                'automatic_output': cfg.llm.automatic_output,
                                'source': limits.get('window_source') if isinstance(limits, dict) else None}

        cases = [('review_on', True), ('review_off', False)]
        if args.only:
            cases = [(c, v) for c, v in cases if c == args.only]
        for label, review_on in cases:
            client, usage = real_client(cfg)
            start = time.monotonic()
            payload = analyze_paper(kb, cfg, client, pid, force=True, with_concepts=False,
                                    semantic_review=review_on)
            sem = payload['_semantic_review']
            items = sem.get('items') or []
            cands = payload.get('_candidates') or []
            counts = {}
            for item in items:
                counts[item['status']] = counts.get(item['status'], 0) + 1
            report['steps'].append({
                'case': label,
                'seconds': round(time.monotonic() - start, 1),
                'semantic_status': sem.get('status'),
                'passed': sem.get('passed'),
                'claims': len(items),
                'candidates': len(cands),
                'ids_match': sorted(c['id'] for c in cands) == sorted(i['id'] for i in items) if items else None,
                'status_counts': counts,
                'coverage_complete': (sem.get('coverage') or {}).get('complete'),
                'review_calls': sem.get('review_calls'),
                'fields_nonempty': {f: bool(payload.get(f)) for f in
                                    ('research_question', 'methodology', 'experiment_design',
                                     'key_conclusions', 'innovations', 'limitations')},
                'usage': usage,
                'note': sem.get('note'),
            })
            print(json.dumps(report['steps'][-1], ensure_ascii=False), flush=True)

        # 人工确认一条：优先用机器人支持项，否则用候选
        from litprism.knowledge import create_source, review_source
        payload = kb.get_analysis(pid)['payload']
        pool = payload['_semantic_review'].get('items') or payload.get('_candidates') or []
        claim = next((c for c in pool if c.get('status') in (None, 'machine_supported')), pool[0])
        text = ('人工核对的知识陈述：\n' + claim['text'] + '\n\n核对依据：全流程真实样例验证\n\n'
                '解读版本：' + str(payload.get('_analysis_run_id') or 'live') + '\n陈述 ID：' + claim['id'])
        source = create_source(kb, kind='knowledge', title='真实样例全流程 · ' + papers[0]['title'],
                               content=text, url='')
        review_source(kb, source['id'], action='confirm', note='全流程真实样例验证',
                      content_hash=source['content_hash'])
        report['knowledge'] = {'claim_id': claim['id'], 'source_id': source['id'],
                               'status': source['status'] if isinstance(source, dict) else 'created'}
        print(json.dumps(report['knowledge'], ensure_ascii=False), flush=True)

        (home / 'last_full_pipeline.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                      encoding='utf-8')
        return 0
    finally:
        kb.close()


if __name__ == '__main__':
    raise SystemExit(main())
