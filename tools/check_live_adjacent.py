"""真实模型的「相邻/拆段证据补足」定向实测。

为什么不用整篇论文跑：fastText 那篇一次要 ~10 万 token、58 次调用，
而本轮要回答的问题只有两个——**补足后边界情形还会不会误判**、
**护栏有没有被放宽**。用合成片段定向复现即可，单次几百 token。

四个用例：
  split   —— 同一 chunk 被预算拆成两段（同 tag），证据横跨两段   → 期望 supported
  adjacent—— 引用片段在表格边界被截断，后一段补齐条件            → 期望 supported
  contra  —— 与原文相反（护栏对照）                              → 期望 contradicted
  absent  —— 原文完全没有的信息（护栏对照）                      → 期望 needs_review

凭据只从 ProviderStore 读，不落日志、不打印。
用法：``$PY tools/check_live_adjacent.py``
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from litprism.config import Config, LLMConfig  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.llm import OpenAICompatClient  # noqa: E402
from litprism.providers import ProviderStore  # noqa: E402
from litprism.semantics import make_units, review_units  # noqa: E402

BASE_URL = os.environ.get('LITPRISM_TEST_BASE_URL', 'https://api.commandcode.ai/provider/v1')
MODEL = os.environ.get('LITPRISM_TEST_MODEL', 'deepseek/deepseek-v4.1-flash')


def real_model(cfg: Config):
    key = os.environ.get('LITPRISM_TEST_API_KEY')
    if not key:
        kb = KnowledgeBase(Config())
        try:
            store = ProviderStore(kb)
            provider = next(p for p in store.list() if p.base_url == BASE_URL)
            key = store.secrets.get(provider.api_key_ref)
        finally:
            kb.close()
    if not key:
        raise SystemExit('未找到该提供商的密钥；请先在应用里配置。')
    return OpenAICompatClient(LLMConfig(provider='openai', base_url=BASE_URL, api_key=key,
                                        model=MODEL, timeout=60, max_tokens=8192))


def excerpts(*texts, paper='paper1', page=4):
    return [{'tag': f'P{i}', 'id': f'chunk{i}', 'paper_id': paper, 'page': page,
             'paper_title': 'Synthetic Study', 'text': t} for i, t in enumerate(texts, 1)]


def run(cfg, model, label, texts, claim, expect):
    rows = excerpts(*texts)
    units = make_units([('key_conclusions', claim, ['P1 p.4'])], rows, 1)
    start = time.monotonic()
    units = review_units(units, rows, cfg, model)
    unit = units[0]
    used = [q['tag'] for q in unit.get('quotes') or []]
    record = {
        'case': label,
        'expect': expect,
        'status': unit['status'],
        'adjacent_tags': unit.get('adjacent_tags'),
        'evidence_tags': used,
        'seconds': round(time.monotonic() - start, 1),
        'reason': (unit.get('reason') or '')[:160],
        'ok': unit['status'] == expect,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record['ok']


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='litprism-live-adj-') as folder:
        cfg = Config()
        cfg.app.data_dir = folder
        model = real_model(cfg)
        results = [
            # 证据横跨被拆开的两段：前半给条件、后半给数值
            run(cfg, model, 'split',
                ['With hidden size 256, the average SuperGLUE score drops to ',
                 '71.8 points, down from 76.0 at hidden size 512.'],
                'Reducing hidden size from 512 to 256 lowers the average SuperGLUE score from 76.0 to 71.8 [P1 p.4]。',
                'machine_supported'),
            # 引用片段在表格边界被截断，条件在下一段
            run(cfg, model, 'adjacent',
                ['Table 3 lists sentiment analysis results. Only the',
                 'second column reuses the hidden variable across tasks.'],
                'The sentiment analysis setup reuses a hidden variable [P1 p.4]。',
                'machine_supported'),
            # 护栏对照：与原文方向相反
            run(cfg, model, 'contra',
                ['With hidden size 256, the average SuperGLUE score drops to ',
                 '71.8 points, down from 76.0 at hidden size 512.'],
                'Reducing hidden size from 512 to 256 raises the average SuperGLUE score [P1 p.4]。',
                'contradicted'),
            # 护栏对照：原文完全没有的信息
            run(cfg, model, 'absent',
                ['Table 3 lists sentiment analysis results. Only the',
                 'second column reuses the hidden variable across tasks.'],
                'The authors also report Chinese word segmentation accuracy [P1 p.4]。',
                'needs_review'),
        ]
        usage = getattr(model, 'last_usage', None)
        print(json.dumps({'case': 'usage', 'usage': usage}, ensure_ascii=False), flush=True)
        return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
