"""显式运行的真实模型冒烟；凭据只读环境变量，使用临时合成资料库。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastapi.testclient import TestClient  # noqa: E402

from litprism.config import Config, LLMConfig  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.llm import OpenAICompatClient  # noqa: E402
from litprism.providers import ProviderStore  # noqa: E402
from litprism.web import server  # noqa: E402


def main():
    model_id = os.environ.get('LITPRISM_TEST_MODEL', 'deepseek/deepseek-v4.1-flash')
    key = os.environ.get('LITPRISM_TEST_API_KEY')
    base_url = os.environ.get('LITPRISM_TEST_BASE_URL', 'https://api.commandcode.ai/provider/v1')
    if not key:
        saved = KnowledgeBase(Config())
        try:
            store = ProviderStore(saved)
            provider = next(p for p in store.list() if p.base_url == base_url)
            key = store.secrets.get(provider.api_key_ref)
        finally:
            saved.close()
    if not key:
        raise ValueError('测试提供商尚未配置密钥')
    model = OpenAICompatClient(LLMConfig(provider='openai', base_url=base_url,
                                      api_key=key, model=model_id, timeout=55, max_tokens=8192))
    # 不保存请求头、密钥或提供方错误原文；只输出合成材料的业务结果。
    with tempfile.TemporaryDirectory(prefix='litprism-live-') as folder:
        cfg = Config()
        cfg.app.data_dir = folder
        for name in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
            getattr(cfg.sources, name).enabled = False
        kb = KnowledgeBase(cfg)
        ids = []
        for title, score in [('Synthetic Alpha', 82), ('Synthetic Beta', 76)]:
            pid = kb.add_paper(title=title, verify_level='manual_confirmed')
            kb.add_chunks(pid, [{'page': 3, 'section': '方法', 'text': (
                f'本资料为合成验收材料，不是真实论文。{title} 研究 alphatopic 检索准确率。'
                f'作者使用固定词表匹配方法，在合成数据集 Sample100 的 100 条记录上评估。'
                f'准确率为 {score}%。作者声称贡献是固定词表对照实验。'
                '局限是样本仅有 100 条，未报告其他数据集实验。固定词表定义为实验前冻结的词项集合。'
            ) * 2}])
            ids.append(pid)
        server.KnowledgeBase = lambda _: kb
        server.build_llm = lambda _: model
        client = TestClient(server.create_app(cfg))
        passed = []
        def run(label, path, **kwargs):
            start = time.monotonic()
            try:
                r = client.post(path, **kwargs)
                payload = r.json()
                check = (payload.get('assistant') or {}).get('citation_check') or (payload.get('analysis') or payload.get('data') or {}).get('_citation_check') or {}
                passed.append(r.status_code == 200 and check.get('passed') is True)
                print(json.dumps({'step': label, 'http': r.status_code, 'seconds': round(time.monotonic()-start, 2),
                                  'citation_check': check, 'passed': passed[-1]}, ensure_ascii=False), flush=True)
                return payload
            except Exception as exc:
                passed.append(False)
                print(json.dumps({'step': label, 'error_type': type(exc).__name__}, ensure_ascii=False), flush=True)
                return {}
        conv = client.post('/api/conversations', json={'title': '真实模型合成验收', 'scope': {'paper_ids': [ids[0]]}}).json()['id']
        if not os.environ.get('LITPRISM_TEST_COMPARE_ONLY'):
            run('qa', '/api/chat/'+conv, json={'question': 'alphatopic 的准确率是多少？请引用原文。'})
            run('followup', '/api/chat/'+conv, json={'question': '它的实验局限是什么？'})
            run('analysis', '/api/analyze/'+ids[0]+'?force=true')
        run('compare', '/api/compare', json={'refs': ids})
        print(json.dumps({'step': 'usage', 'usage': getattr(model, 'last_usage', None)}, ensure_ascii=False), flush=True)
        kb.close()
        return 0 if passed and all(passed) else 1


if __name__ == '__main__':
    raise SystemExit(main())
