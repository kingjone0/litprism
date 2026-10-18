"""离线复查未闭环边界；只写临时库，不使用真实提供商或密钥。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastapi.testclient import TestClient  # noqa: E402

from litprism.agents import CompareAgent  # noqa: E402
from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.extract import paper_context  # noqa: E402
from litprism.skills.runner import SkillRunner  # noqa: E402
from litprism.web import server  # noqa: E402


class AuditModel:
    name = 'offline:audit-boundary'

    def __init__(self):
        self.requests = []

    def chat_json(self, messages, task='', **kwargs):
        self.requests.append((task, messages))
        if task == 'expand':
            return {'terms': ['auditmarker']}
        if task == 'compare':
            return {'summary': '测试结论 [P1 p.1]', 'dimensions': [
                {'dimension': '方法', 'values': {'D1': '甲方法 [P1 p.1]', 'D2': '乙方法 [P1 p.1]'}}]}
        return {'answer': '测试结论 [P1 p.1]'}


def main():
    with tempfile.TemporaryDirectory(prefix='litprism-gap-audit-') as folder:
        cfg = Config()
        cfg.app.data_dir = folder
        cfg.retrieval.top_k = 1
        for source in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
            getattr(cfg.sources, source).enabled = False
        kb = KnowledgeBase(cfg)
        try:
            model = AuditModel()
            a = kb.add_paper(title='Synthetic Audit A', verify_level='verified')
            kb.add_chunks(a, [{'page': i, 'section': '正文', 'text': 'auditmarker 原文 ' * 100 + f' PAGE_{i}_ONLY'} for i in range(1, 8)])
            b = kb.add_paper(title='Synthetic Audit B', verify_level='verified')
            kb.add_chunks(b, [{'page': 1, 'text': 'auditmarker 乙文唯一原文。' * 20}])
            kb.add_concepts(a, [{'term': 'RetainedTerm', 'definition': '测试旧术语'}])
            server.KnowledgeBase = lambda _: kb
            server.build_llm = lambda _: model
            client = TestClient(server.create_app(cfg))
            cite = {'tag': 'P1', 'paper_id': a, 'chunk_id': kb.get_chunks(a)[0]['id']}
            created = client.post('/api/deliverables', json={'type': 'notes', 'title': 'Audit',
                                  'content': '这是一条没有正文引用、且原文未陈述的测试结论。', 'citations': [cite]})
            did = created.json().get('id')
            results = {'delivery_without_inline_citation': {'create_http': created.status_code,
                       'download_http': client.get('/api/deliverables/'+did+'/download').status_code if did else None}}
            compared = CompareAgent(kb, cfg, model).compare([a, b])
            request = '\n'.join(m['content'] for task, msgs in model.requests if task == 'compare' for m in msgs)
            results['compare_wrong_paper_source_passed'] = compared['_citation_check']['passed']
            results['compare_last_page_sent'] = 'PAGE_7_ONLY' in request
            skill_hits = SkillRunner(kb, cfg, llm=model)._dispatch(
                'search_passages', {'query': 'auditmarker', 'paper_ids': [b]}, {})
            results['skill_prefetch_ignores_paper_scope'] = any(h['paper_id'] != b for h in skill_hits)
            conv = client.post('/api/conversations', json={'scope': {'paper_ids': [a]}}).json()['id']
            chat = client.post('/api/chat/'+conv, json={'question': 'auditmarker'})
            results['configured_top_k_one'] = {'http': chat.status_code,
                                               'actual_sources': len(chat.json().get('sources', [])[0]['component']['items'])}
            client.delete('/api/paper/'+a)
            results['trash_concepts_still_listed'] = any(c['paper_id'] == a for c in client.get('/api/concepts').json()['items'])
            missing = client.get('/api/concepts?paper=nonexistent')
            results['missing_paper_concepts_http'] = missing.status_code
            results['missing_paper_concepts_broaden_to_all'] = bool(missing.json().get('items'))
            long = kb.add_paper(title='Synthetic Long', verify_level='verified')
            kb.add_chunks(long, [{'page': 1, 'text': '前部内容。' * 6000}, {'page': 9, 'text': 'TAIL_LIMITATION_ONLY 最后局限'}])
            results['analysis_last_chunk_sent'] = 'TAIL_LIMITATION_ONLY' in paper_context(kb, kb.get_paper(long))
            print(json.dumps(results, ensure_ascii=False, indent=2))
        finally:
            kb.close()


if __name__ == '__main__':
    main()
