"""运行真实后端与无窗口浏览器的研究流程验收；仅使用临时库和离线模型桩。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(ROOT / 'src'))

from litprism.config import Config, config_to_dict  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.sidecar import harden  # noqa: E402
from litprism.web import server as web  # noqa: E402


class AcceptanceModel:
    name = 'offline:acceptance-fixture'
    supports_semantic_review = True

    def chat_json(self, messages, *, task=None):
        self.last_reasoning = '<script>thinking_ui_marker</script>' if task == 'qa' else ''
        self.last_request_settings = {'max_tokens': 384000, 'timeout': 300, 'output_mode': 'automatic'} if task == 'qa' else {}
        if task == 'semantic_cross_review':
            return {'differences': []}
        if task == 'semantic_review':
            request = json.loads(messages[-1]['content'])
            return {'reviews': [{'id': c['id'], 'verdict': 'needs_review' if '待核查' in c['text'] else 'supported',
                                 'reason': '离线流程桩：仅验收审核交互，不验证学术质量。',
                                 'quotes': [{'tag': s['tag'], 'quote': s['text']} for s in request['sources']], 'facts': []}
                                for c in request['claims']]}
        if task == 'qa' and 'delaymarker' in str(messages):
            from litprism.tasks import checkpoint
            for _ in range(60):
                checkpoint('取消操作验收：等待测试取消')
                time.sleep(.05)
        if task == 'expand':
            return {'terms': ['alphatopic']}
        if task == 'extract':
            return {'research_question': '待核查陈述：用于离线验收的原文描述 [P1 p.1]',
                    'field_evidence': {'research_question': ['P1 p.1']}}
        if task == 'concepts':
            return {'concepts': [{'term': 'alphatopic', 'definition': '用于离线验收的原文描述 [P1 p.1]', 'category': '方法'}]}
        if task == 'compare':
            return {'summary': '用于离线验收的原文描述 [P1 p.1]', 'dimensions': [],
                    'convergence': ['共同描述 [P1 p.1]'], 'divergence': [],
                    'evidence_map': [{'claim': '共同描述', 'sources': ['P1 p.1']}]}
        if 'reviewmarker' in str(messages):
            return {'answer': '知识快照的测试内容 [P1]'}
        return {'answer': '用于离线验收的原文描述 [P1 p.1]'}

    def chat(self, messages, **kwargs):
        if 'delaymarker' in str(messages):
            from litprism.tasks import checkpoint
            for _ in range(60):
                checkpoint('Skill 取消操作验收')
                time.sleep(.05)
        return '用于离线验收的原文描述 [P1 p.1]'


def main():
    import uvicorn
    home = Path(os.environ['LITPRISM_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workflow-acceptance-', dir=home) as folder:
        cfg = Config()
        cfg.app.data_dir = str(Path(folder) / 'data')
        for name in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
            getattr(cfg.sources, name).enabled = False
        import yaml
        cfg.config_path = str(Path(folder) / 'config.yaml')
        Path(cfg.config_path).write_text(yaml.safe_dump(config_to_dict(cfg), allow_unicode=True), encoding='utf-8')
        kb = KnowledgeBase(cfg)
        seed = kb.add_paper(title='验收文献 A', verify_level='verified')
        kb.add_chunks(seed, [{'text': 'alphatopic 用于离线验收的原文描述。' * 20, 'page': 1}])
        web.KnowledgeBase = lambda _: kb
        web.build_llm = lambda _: AcceptanceModel()
        app = web.create_app(cfg)
        harden(app, 'acceptance-only')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        backend = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        worker = threading.Thread(target=backend.run, kwargs={'sockets': [sock]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not backend.started:
                if time.monotonic() > deadline:
                    raise RuntimeError('验收后端启动超时')
                time.sleep(.05)
            env = dict(os.environ, LITPRISM_ACCEPTANCE_URL=f'http://127.0.0.1:{port}', LITPRISM_ACCEPTANCE_SEED=seed)
            result = subprocess.run(['node', str(ROOT / 'tools/check_workflows.cjs')], env=env,
                                    capture_output=True, text=True, encoding='utf-8',
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            print(result.stdout, end='')
            if result.stderr:
                print(result.stderr, file=sys.stderr, end='')
            return result.returncode
        finally:
            backend.should_exit = True
            worker.join(timeout=10)
            sock.close()
            kb.close()


if __name__ == '__main__':
    raise SystemExit(main())
