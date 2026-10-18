"""Bounded live plan-to-review audit on a frozen real-paper excerpt.

Explicit --live required. No full-paper quality claim and no user DB migration.
Raw requests/results remain under ignored _selftest.* directories.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(ROOT / 'src'))

from audit_long_paper import RecordingClient, live_client, save  # noqa: E402

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.extract import analyze_paper  # noqa: E402
from litprism.llm import LLMError  # noqa: E402
from litprism.usage import UsageTracker  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=12, choices=range(1, 13))
    parser.add_argument('--provider-root', type=Path, default=Path('E:/codex-litprism-data'))
    args = parser.parse_args()
    folder = args.output.resolve()
    if folder.exists():
        parser.error('Output must be a new directory; do not overwrite evidence.')
    folder.mkdir(parents=True)
    fixture = json.loads((ROOT/'tools/fixtures/semantic-real-cases.json').read_text(encoding='utf-8'))
    excerpt = fixture['sources']['rmsnorm']
    source = {**excerpt, **fixture['papers']['rmsnorm'],
              'text_sha256': hashlib.sha256(excerpt['text'].encode()).hexdigest(),
              'scope': 'frozen first-page excerpt, not a full paper'}
    save(folder/'source.json', source)
    client = live_client(args.provider_root)

    class AuditBudgetExceeded(LLMError):
        pass

    class BudgetClient(RecordingClient):
        @property
        def remaining_calls(self):
            return max(0, args.max_calls-len(self.calls))

        def chat_json(self, messages, *, task=None):
            if len(self.calls) >= args.max_calls:
                raise AuditBudgetExceeded('Audit logical-call budget exhausted; no further network calls.')
            return super().chat_json(messages, task=task)

    recorder = BudgetClient(client, folder)
    cfg = Config()
    cfg.app.data_dir = str(folder/'data')
    cfg.context.window_tokens = 32768
    cfg.context.output_reserve_tokens = cfg.llm.max_tokens = 8192
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg)
    manifest = {'model': client.name, 'temperature': client.cfg.temperature,
                'max_calls': args.max_calls, 'max_output_per_call': 8192,
                'scope': source['scope'], 'source_text_sha256': source['text_sha256']}
    save(folder/'manifest.json', manifest)
    summary = dict(scope=source['scope'], completed=False)
    try:
        pid = kb.add_paper(title='RMSNorm · fixed first-page excerpt (not full paper)',
                           verify_level='manual_confirmed', url=source['url'])
        kb.add_chunks(pid, [{'page': source['page'], 'section': '摘要片段', 'text': source['text']}])
        payload = analyze_paper(kb, cfg, recorder, pid, force=True, with_concepts=False)
        save(folder/'analysis.json', payload)
        review = payload.get('_semantic_review') or {}
        summary.update(completed=True, mode=payload['_coverage']['reading_mode'],
                       extraction_complete=payload['_coverage']['complete'],
                       supported=review.get('supported'), pending=review.get('pending'),
                       semantic_passed=review.get('passed'),
                       cross_coverage=review.get('coverage'), usage=payload['_coverage']['usage'])
    except Exception as exc:
        # Never print arbitrary service error text, headers or credentials.
        summary.update(error_type=type(exc).__name__, http_status=getattr(exc, 'http_status', None))
        summary['stopped_at_budget'] = isinstance(exc, AuditBudgetExceeded)
    finally:
        usage = UsageTracker()
        for call in recorder.calls:
            usage.add(call.get('usage'), task=call['task'], failed=bool(call.get('error_type')))
        summary['actual_call_usage'] = usage.to_dict()
        summary['logical_calls'] = len(recorder.calls)
        summary['reported_total_tokens'] = sum((c.get('usage') or {}).get('total_tokens', 0) for c in recorder.calls)
        save(folder/'summary.json', summary)
        kb.close()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
