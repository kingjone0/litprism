"""Three first-attempt generations against frozen historical QA requests.

Only the current QA system contract, source framing and final copy check change.
No retrieval/search/expansion, source relabeling, response repair or publication.
The source snapshots describe historical delivery, not current trust certification.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tools')]
from quote_generation_offline_acceptance import read, write, sha, digest, frame_frozen_sources, replay  # noqa: E402
from litprism.context.dialogue import HISTORY_GUARD  # noqa: E402
from litprism.context.meter import Meter  # noqa: E402
from litprism.prompts import QA_SYSTEM, QA_QUOTE_CHECK  # noqa: E402
from litprism.qa_answer import compile_answer  # noqa: E402
from litprism.llm import parse_json_loose, safe_failure, output_allocation  # noqa: E402
from litprism.textutil import parse_citations  # noqa: E402

CASES = ('confirmed', 'revised', 'restricted')


def make_request(messages, sources):
    if [m.get('role') for m in messages] != ['system', 'user']:
        raise ValueError('Expected exactly one frozen system and user message')
    separator = '\n\n'+HISTORY_GUARD+'\n\n'
    system = messages[0]['content']
    if system.count(separator) != 1:
        raise ValueError('Historical guard boundary changed')
    content = messages[1]['content']
    before, marker, rest = content.partition('【片段】\n')
    prefix, delivery, after = rest.partition('\n\n【程序送达记录】')
    if not marker or not delivery or content.count('【问题】') != 1 or QA_QUOTE_CHECK in content:
        raise ValueError('Unexpected original request framing; do not guess')
    result = deepcopy(messages)
    result[0]['content'] = QA_SYSTEM+separator+system.split(separator, 1)[1]
    result[1]['content'] = before+marker+frame_frozen_sources(prefix, sources)+delivery+after+'\n'+QA_QUOTE_CHECK
    return result


def implementation():
    files = list((ROOT/'src').rglob('*.py'))+[Path(__file__).resolve(), ROOT/'tools/quote_generation_offline_acceptance.py']
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}


def safe_home(path):
    path = path.resolve()
    if ROOT not in path.parents or not path.name.startswith('_selftest.'):
        raise ValueError('Use a _selftest root inside this workspace')
    return path


def inspect_generation(value, sources):
    if not isinstance(value, dict):
        raise ValueError('Model did not return a JSON object')
    answer, _, bindings, protocol = compile_answer(value)
    if not answer.strip():
        raise ValueError('Empty answer')
    audit = replay(value, sources)
    tags = {s['tag'] for s in sources}
    cited = [tag for tag, _ in parse_citations(answer)]
    units = value.get('answer_units', [])
    units = units if isinstance(units, list) else []
    quotes = [q for u in units if isinstance(u, dict) and isinstance(u.get('quotes'), list)
              for q in u['quotes'] if isinstance(q, dict)]
    by_tag = {s['tag']: s['text'] for s in sources}
    return {'answer': answer, 'answer_protocol': protocol, 'bindings': bindings, 'audit': audit,
            'citation_count': len(cited), 'unknown_citation_tags': [tag for tag in cited if tag not in tags],
            'answer_unit_count': len(units),
            'proposed_quotes': len(quotes), 'located_quotes': sum(len(i['quotes']) for i in audit['items']),
            'byte_exact_quotes': sum(isinstance(q.get('quote'), str) and bool(q['quote'])
                                    and q['quote'] in by_tag.get(q.get('source'), '') for q in quotes),
            'semantic_judgment': None, 'historical_sources_not_current_trust_check': True}


def prepare(home, source):
    home, source = safe_home(home), safe_home(source)
    if home.exists():
        raise ValueError('Existing acceptance is immutable')
    files = [source/'manifest.json']
    rows = []
    for case in CASES:
        folder = source/case
        requests, result = read(folder/'requests.json'), read(folder/'result.json')
        request = [r for r in requests if r['task'] == 'qa']
        if len(request) != 1 or request[0]['kwargs'] != {'json_mode': True, 'task': 'qa'}:
            raise ValueError('Unrecognized original dispatch contract')
        sources = result['review']['sources']
        generated = make_request(request[0]['messages'], sources)
        original = read(folder/'raw-qa.txt')
        audit = replay(original, sources)
        if audit['blocking_issues'] != result['review']['evidence_audit']['blocking_issues']:
            raise ValueError('Original failure baseline changed')
        rows.append({'id': case, 'question': result['question'], 'messages': generated, 'sources': sources,
                     'old_status': result['status'], 'old_blockers': audit['blocking_issues'],
                     'sources_sha256': digest(sources), 'request_sha256': digest(generated),
                     'estimated_input_tokens': sum(Meter().estimate_message(m) for m in generated)+8})
        files += [folder/'requests.json', folder/'result.json', folder/'raw-qa.txt']
    home.mkdir()
    for row in rows:
        folder = home/row['id']; folder.mkdir()
        write(folder/'frozen-request.json', {'messages': row.pop('messages'), 'sources': row.pop('sources')})
    plan = {'version': 1, 'source': str(source), 'source_files_sha256': {str(p): sha(p) for p in files},
            'implementation_sha256': implementation(), 'cases': rows,
            'planned_qa_calls': 3, 'planned_search_calls': 0, 'planned_expand_calls': 0,
            'authorization': 'User requested the next planned fixed three-case verification; prior acceptance-to-completion budget retained.',
            'budget_policy': 'Exactly one QA HTTP attempt per case; automatic production output allocation, no manual token cap or paid retries.',
            'read_timeout_seconds': 900, 'semantic_passed': None,
            'scope': 'New real generations from historical frozen delivery; generation-constraint test, not persistent-Web or current-source lifecycle acceptance.',
            'history_and_sources_unchanged': True, 'knowledge_publication': False}
    write(home/'manifest.json', plan)
    return {'prepared': str(home), 'qa_calls': 3, 'estimated_input_tokens': sum(r['estimated_input_tokens'] for r in rows)}


def check_frozen(home):
    plan = read(home/'manifest.json')
    if implementation() != plan['implementation_sha256']:
        raise ValueError('Implementation changed after freeze')
    if any(sha(Path(p)) != expected for p, expected in plan['source_files_sha256'].items()):
        raise ValueError('Original evidence changed')
    for row in plan['cases']:
        frozen = read(home/row['id']/'frozen-request.json')
        if digest(frozen['sources']) != row['sources_sha256'] or digest(frozen['messages']) != row['request_sha256']:
            raise ValueError('Frozen request changed')
    return plan


def run(home):
    import yaml
    from anchor_live_acceptance import clone
    from litprism.config import _from_dict
    from litprism.llm import build_active_llm
    from urllib.parse import urlparse
    from unittest.mock import patch
    home = safe_home(home)
    plan = check_frozen(home)
    # Exclusive marker survives interrupts and unknown charges; never auto-resume.
    with (home/'started.json').open('x', encoding='utf-8') as stream:
        json.dump({'time': time.time(), 'first_attempt_only': True}, stream)
    source = Path(plan['source'])
    base = _from_dict(yaml.safe_load((source/'config/config.yaml').read_text(encoding='utf-8')))
    cfg, kb = clone(home, source/'data/litprism.db', base, source)
    results = []
    try:
        cfg.llm.timeout = 900
        client = build_active_llm(kb, cfg, allow_mock=False)
        if client.model != 'deepseek/deepseek-v4.1-flash':
            raise ValueError('Frozen target model differs; do not dispatch')
        if client.cfg.model_window_tokens != 1000000 or client.cfg.model_max_output_tokens != 384000:
            raise ValueError('Model capability differs from approved 1M/384K baseline')
        if type(client).__name__ != 'OpenAICompatClient' or urlparse(client.base_url).hostname != 'api.commandcode.ai':
            raise ValueError('Provider/protocol differs from authorized endpoint')
        client.durable_single_attempt = True
        # Product client protocol/parameters remain unchanged; only internal retries are disabled.
        settings = {'model': client.model, 'protocol': type(client).__name__,
                    'context_tokens': client.cfg.model_window_tokens, 'max_output_tokens': client.cfg.model_max_output_tokens,
                    'timeout': client.cfg.timeout, 'automatic_output': client.cfg.automatic_output}
        write(home/'runtime.json', settings)
        for row in plan['cases']:
            check_frozen(home)
            folder = home/row['id']; frozen = read(folder/'frozen-request.json')
            stamp = time.monotonic()
            write(folder/'dispatch-started.json', {'time': time.time(), 'unknown_usage_until_response': True,
                'request_sha256': row['request_sha256'], 'allocation': output_allocation(client.cfg, frozen['messages'])})
            result = {'id': row['id'], 'request_sha256': row['request_sha256'], 'usage': None,
                      'request_settings': None, 'service_status': 'unknown', 'semantic_judgment': None}
            from litprism import llm
            original_post = llm.requests.post
            attempts = 0
            def dispatch(url, **kwargs):
                nonlocal attempts
                if attempts or url != client.base_url+'/chat/completions':
                    raise RuntimeError('Unplanned or repeated HTTP dispatch prevented')
                attempts += 1
                write(folder/'http-request.json', kwargs['json'])  # Never headers or credentials.
                response = original_post(url, **kwargs)
                body = response.text.replace(client.cfg.api_key, '[redacted]') if client.cfg.api_key else response.text
                (folder/'http-response.txt').write_text(body, encoding='utf-8')
                return response
            try:
                with patch.object(llm.requests, 'post', dispatch):
                    raw = client.chat(frozen['messages'], json_mode=True, task='qa')
                (folder/'raw-qa.txt').write_text(raw, encoding='utf-8')
                (folder/'reasoning.txt').write_text(client.last_reasoning, encoding='utf-8')
                result.update(service_status='returned', raw_sha256=sha(folder/'raw-qa.txt'))
                value = parse_json_loose(raw)
                write(folder/'parsed.json', value)
                result['inspection'] = inspect_generation(value, frozen['sources'])
            except Exception as exc:
                result['failure'] = safe_failure(exc)
                if result['service_status'] == 'returned': result['inspection_failed'] = True
                else: result['service_status'] = 'failed_or_unknown'
            finally:
                result.update(elapsed_seconds=round(time.monotonic()-stamp, 3), usage=client.last_usage,
                              request_settings=client.last_request_settings, http_attempts=attempts)
                write(folder/'result.json', result)
                results.append(result)
                write(home/'results.json', results)
                print(json.dumps({k: v for k, v in result.items() if k != 'inspection'}, ensure_ascii=True), flush=True)
    finally:
        kb.close()
    check_frozen(home)
    summary = {'completed_cases': len(results), 'planned_qa_calls': 3, 'search_calls': 0, 'expand_calls': 0,
               'responses': sum(r['service_status'] == 'returned' for r in results),
               'unknown_usage_cases': sum(r['usage'] is None for r in results), 'semantic_passed': None,
               'original_files_unchanged': True, 'product_source_changed': False, 'results': results}
    write(home/'summary.json', summary)
    return {'completed_cases': len(results), 'responses': summary['responses']}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.source) if args.action == 'prepare' else run(args.output), ensure_ascii=True))
