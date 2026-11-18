"""Paid acceptance must reject changed inputs before touching credentials."""
import importlib.util
from pathlib import Path
import types

import pytest

PATH = Path(__file__).resolve().parents[1] / 'tools/g04_quality_acceptance.py'
spec = importlib.util.spec_from_file_location('g04_acceptance_tested', PATH)
g04 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g04)


@pytest.mark.parametrize('changed', ['spec', 'implementation', 'input', 'document', 'pdf', 'scope'])
def test_changed_frozen_evidence_blocks_acceptance(tmp_path, monkeypatch, changed):
    source = tmp_path / 'source.pdf'; source.write_bytes(b'frozen-pdf')
    fixture = tmp_path / 'cases.json'
    g04.write(fixture, {'papers': {'short': {'sha256': g04.digest(source)}}})
    monkeypatch.setattr(g04, 'SPEC', fixture)
    monkeypatch.setattr(g04, 'implementation', lambda: {'code': 'original'})
    ids = ['S1', 'S2', 'S3', 'L1', 'L2', 'L3']
    inputs = {}
    for cid in ids:
        folder = tmp_path / cid; folder.mkdir()
        g04.write(folder / 'frozen-input.json', {'sources': ['unchanged']})
        inputs[cid] = g04.digest(folder / 'frozen-input.json')
    case = tmp_path / 'S1'
    paper = tmp_path / 'short'; paper.mkdir()
    g04.write(paper / 'document.json', {'pages': 5})
    manifest = {'spec_sha256': g04.digest(fixture), 'implementation': g04.implementation(),
        'endpoint': g04.ENDPOINT, 'model': g04.MODEL, 'planned_calls': 6, 'case_ids': ids,
        'frozen_inputs': inputs,
        'documents': {'short': g04.digest(paper / 'document.json')},
        'pdf_paths': {'short': str(source)}}
    g04.write(tmp_path / 'manifest.json', manifest)
    assert g04.verify(tmp_path) == manifest
    if changed == 'implementation':
        monkeypatch.setattr(g04, 'implementation', lambda: {'code': 'changed'})
    elif changed == 'scope':
        manifest.update(case_ids=['L1', 'L3'], planned_calls=2)
        g04.write(tmp_path / 'manifest.json', manifest)
    else:
        path = {'spec': fixture, 'input': case / 'frozen-input.json',
                'document': paper / 'document.json', 'pdf': source}[changed]
        path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='changed'):
        g04.verify(tmp_path)


def test_started_batch_cannot_resend_or_read_credentials(tmp_path, monkeypatch):
    g04.write(tmp_path / 'manifest.json', {'test': 'frozen'})
    g04.write(tmp_path / 'started.json', {'never_repeat': True})
    monkeypatch.setattr(g04, 'isolated', lambda path: path.resolve())
    monkeypatch.setattr(g04, 'verify', lambda _: {})

    def unexpected(*args, **kwargs):
        raise AssertionError('Repeated batch touched the credential database')

    monkeypatch.setattr(g04.sqlite3, 'connect', unexpected)
    with pytest.raises(FileExistsError):
        g04.live(tmp_path, tmp_path / 'credentials')


def test_scope_freeze_does_not_retrieve_other_papers(monkeypatch):
    from litprism import agents
    calls = []

    class Agent:
        def __init__(self, kb, cfg, capture):
            self.capture = capture

        def ask(self, question, **kwargs):
            calls.append(kwargs)
            try:
                self.capture.chat_json([{'content': '【片段】\nfixed\n\n【程序送达记录】'}], task='qa')
            except RuntimeError:
                pass
            return types.SimpleNamespace(excerpts=[{'chunk_id': 'one'}], retrieval={})

    monkeypatch.setattr(agents, 'QAAgent', Agent)
    real = types.SimpleNamespace(name='test', runtime_config=object())
    result = g04.freeze_question(None, None, real, 'question', ['one-paper'])
    assert calls == [{'paper_ids': ['one-paper'], 'strict_scope': True}]
    assert result['provider_calls'] == 0


def test_frozen_sources_match_actual_conversation_scope(tmp_path):
    from litprism.agents import QAAgent
    from litprism.db import KnowledgeBase
    from litprism.scope import chat_strict
    cfg = g04.config(tmp_path)
    cfg.llm.model_window_tokens = 1_000_000
    cfg.llm.model_max_output_tokens = 384_000
    cfg.llm.automatic_output = True
    kb = KnowledgeBase(cfg)
    try:
        pid = kb.add_paper(title='fastText control', verify_level='verified')
        kb.add_chunks(pid, [{'text': 'alphascope supervised text classifier. ' * 30, 'page': 1}])
        other = kb.add_paper(title='unrelated GPT paper', verify_level='verified')
        kb.add_chunks(other, [{'text': 'alphascope unrelated language model. ' * 30, 'page': 1}])
        real = types.SimpleNamespace(name='offline:g04', runtime_config=cfg)
        question = 'Explain alphascope architecture.'  # Does not contain an explicit only cue.
        frozen = g04.freeze_question(kb, cfg, real, question, [pid])
        scope = g04.case_scope(pid)
        actual = []

        class Capture:
            name = real.name
            runtime_config = cfg

            def chat_json(self, messages, *, task=None):
                actual.append(messages)
                raise RuntimeError('Offline capture')

        answer = QAAgent(kb, cfg, Capture()).ask(question, paper_ids=scope['paper_ids'],
                                               strict_scope=chat_strict(scope, question))
        assert {s['paper_id'] for s in answer.excerpts} == {pid}
        assert len(actual) == 1
        from answer_units_live_acceptance import material
        import hashlib
        assert hashlib.sha256(material(actual[0]).encode()).hexdigest() == frozen['material_sha256']
    finally:
        kb.close()
