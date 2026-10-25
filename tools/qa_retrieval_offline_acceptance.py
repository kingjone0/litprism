"""Replay captured retrieval without models, search services, credentials or approvals.

Only an isolated _selftest corpus is accepted. The original answer is never
retagged against new sources or marked semantically correct by this tool.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def isolated(path):
    path = Path(path).resolve()
    if not any(p.startswith('_selftest.') for p in path.parts):
        raise ValueError('Only an isolated _selftest path is accepted')
    return path


class FrozenInput:
    name = 'offline:captured-query-expansion'

    def __init__(self, expansion):
        self.expansion = expansion
        self.calls = []
        self.messages = None

    def chat_json(self, messages, *, task=None):
        if task in self.calls:
            raise AssertionError('Unexpected duplicate offline task')
        self.calls.append(task)
        if task == 'expand':
            return deepcopy(self.expansion)
        if task == 'qa':
            self.messages = deepcopy(messages)
            # Assemble the production prompt, without synthesising an answer.
            return {'answer': '', 'insufficient_evidence': True, 'confidence': 'none'}
        raise AssertionError('Unplanned task')


def legacy_terms(question, expansion):
    """Frozen pre-fix merge, solely to demonstrate the truncation defect."""
    from litprism.query import MAX_TERMS, glossary_expand, keyword_terms
    terms = keyword_terms(question) + glossary_expand(question) + expansion.get('keywords', [])[:12]
    if question.strip() and len(question) <= 80:
        terms.insert(0, question.strip())
    out, seen = [], set()
    for term in terms:
        key = term.lower()
        if key.strip() and key not in seen:
            seen.add(key)
            out.append(term)
        if len(out) >= MAX_TERMS:
            break
    return out


def run(source, output):
    import yaml
    from litprism.agents import QAAgent
    from litprism.config import _from_dict
    from litprism.db import KnowledgeBase
    from litprism.query import build_or_fts_query, expand_query

    source, output = isolated(source), isolated(output)
    if output.exists() or source == output or source in output.parents or output in source.parents:
        raise ValueError('Output must be a new, independent isolated directory')
    case = source / 'mixed-mechanism'
    paths = [case / 'result.json', case / 'raw-expand.txt', case / 'raw-qa.txt',
             source / 'retrieval-diagnostic.json', source / 'operator-assessment.json',
             source / 'config/config.yaml']
    before = {str(p): sha(p) for p in paths}
    original, expansion, diagnostic = read(paths[0]), read(paths[1]), read(paths[3])
    cfg = _from_dict(yaml.safe_load(paths[-1].read_text(encoding='utf-8')))
    # Stored YAML precedes active-provider capability resolution. Reproduce the
    # effective limits from the actual paid request receipt, without credentials.
    settings = next(c['settings'] for c in original['calls'] if c['task'] == 'qa')
    window, reserve = settings['model_window_tokens'], settings['max_tokens']
    if type(window) is not int or type(reserve) is not int or not 0 < reserve < window:
        raise ValueError('Frozen request lacks usable runtime limits')
    cfg.llm.model_window_tokens = cfg.context.window_tokens = window
    cfg.llm.model_max_output_tokens = settings['model_max_output_tokens']
    cfg.llm.max_tokens = cfg.context.output_reserve_tokens = reserve
    cfg.llm.automatic_output = settings['output_mode'] == 'automatic'
    cfg.context.overflow_retries = 0
    cfg.app.data_dir, cfg.export.notes_dir = str(output / 'data'), str(output / 'notes')
    # Never instantiate ProviderStore/SecretStore or open the daily data root.
    output.mkdir(parents=True)
    target = output / 'data/litprism.db'
    target.parent.mkdir()
    src = sqlite3.connect((source / 'data/litprism.db').as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()

    def no_network(*args, **kwargs):
        raise AssertionError('Network access forbidden in offline acceptance')

    with patch.object(socket.socket, 'connect', no_network):
        kb = KnowledgeBase(cfg)
        try:
            question = original['question']
            old_terms = legacy_terms(question, expansion)
            new_terms = expand_query(question, FrozenInput(expansion))
            # Preserve the exact previously acquired web snapshots and grant
            # metadata; IDs must not be replaced by a fresh search or scrape.
            from litprism.chat_evidence import excerpts as web_excerpts
            captured = original['review']['sources']
            grant_ids = list(dict.fromkeys(e['web_grant_id'] for e in captured if e.get('web_grant_id')))
            external = web_excerpts(kb, grant_ids)
            frozen_web = {e['chunk_id']: e['text'] for e in captured if e.get('web_grant_id')}
            assert {e['id']: e['text'] for e in external} == frozen_web
            model = FrozenInput(expansion)
            result = QAAgent(kb, cfg, model).ask(question, strict_scope=False, external=external)
            allowed = [r['id'] for r in kb._con.execute(
                "SELECT id FROM papers WHERE deleted_at IS NULL AND verify_level IN ('verified','manual_confirmed')")]
            def lexical(terms):
                return kb.search_match(build_or_fts_query(terms), paper_ids=allowed,
                                       top_k=cfg.retrieval.top_k, fallback_terms=terms)
            old_hits, new_hits = lexical(old_terms), lexical(new_terms)
            delivered = set(result.retrieval['delivered_chunk_ids'])
            witnesses = []
            for witness in diagnostic['known_relevant_witnesses']:
                chunk = kb._con.execute('SELECT text FROM chunks WHERE id=?', (witness['chunk_id'],)).fetchone()
                intact = bool(chunk and hashlib.sha256(chunk['text'].encode()).hexdigest() == witness['text_sha256'])
                witnesses.append({**witness, 'current_source_intact': intact,
                                  'delivered_before': witness['chunk_id'] in original['retrieval']['delivered_chunk_ids'],
                                  'delivered_after': witness['chunk_id'] in delivered})
            report = {'real_model_calls': 0, 'real_search_calls': 0, 'credential_access': False,
                      'offline_stub_tasks': model.calls,
                      'query_terms_before': old_terms, 'query_terms_after': new_terms,
                      'model_terms_retained_before': [t for t in expansion['keywords'] if t in old_terms],
                      'model_terms_retained_after': [t for t in expansion['keywords'] if t in new_terms],
                      'lexical_chunk_ids_before': [h['id'] for h in old_hits],
                      'lexical_chunk_ids_after': [h['id'] for h in new_hits],
                      'retrieval_before': original['retrieval'], 'retrieval_after': result.retrieval,
                      'witnesses': witnesses,
                      'external_source_ids_preserved': {e['id'] for e in external} <= delivered,
                      'original_stage_attribution_counterexample_unchanged': sha(paths[2]) == before[str(paths[2])],
                      'semantic_passed': None, 'answer_generation_performed': False,
                      'runtime_limits_from_captured_request': settings,
                      'input_sha256': before, 'inputs_unchanged': all(sha(p) == before[str(p)] for p in paths)}
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            (output / 'assembled-request.json').write_text(json.dumps(model.messages, ensure_ascii=False, indent=2), encoding='utf-8')
            assert report['inputs_unchanged'] and report['external_source_ids_preserved']
            assert all(w['current_source_intact'] and w['delivered_after'] for w in witnesses)
            assert report['model_terms_retained_after'] == expansion['keywords']
            return report
        finally:
            kb.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    summary = run(args.source, args.output)
    print(json.dumps({'output': str(Path(args.output).resolve()), 'real_model_calls': 0,
                      'witnesses_delivered': sum(w['delivered_after'] for w in summary['witnesses']),
                      'delivered_chunks': summary['retrieval_after']['delivered_chunks'],
                      'semantic_passed': None}))
