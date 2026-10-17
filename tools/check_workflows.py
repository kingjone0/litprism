"""运行真实后端与无窗口浏览器的研究流程验收；仅使用临时库和离线模型桩。"""
from __future__ import annotations

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

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.sidecar import harden  # noqa: E402
from litprism.web import server as web  # noqa: E402


class AcceptanceModel:
    name = 'offline:acceptance-fixture'

    def chat_json(self, messages, *, task=None):
        if task == 'expand':
            return {'terms': ['alphatopic']}
        if task == 'extract':
            return {'research_question': '用于离线验收的原文描述 [P1 p.1]',
                    'field_evidence': {'research_question': ['P1 p.1']}}
        if task == 'concepts':
            return {'concepts': [{'term': 'alphatopic', 'definition': '用于离线验收的原文描述 [P1 p.1]', 'category': '方法'}]}
        if task == 'compare':
            return {'summary': '用于离线验收的原文描述 [P1 p.1]', 'dimensions': []}
        return {'answer': '用于离线验收的原文描述 [P1 p.1]'}

    def chat(self, messages, **kwargs):
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
