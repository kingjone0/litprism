"""Explicit GPT-3 arXiv v4 audit. Never saves credentials or changes the user's library.

Prepare: python tools/audit_long_paper.py PAPER.pdf --output _selftest.long-paper/baseline
Live: add --live (reads the existing provider via read-only SQLite and SecretStore).
Two independent full-text partitions; raw responses are retained before validation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from litprism.config import Config, LLMConfig  # noqa: E402
from litprism.context.meter import Meter  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.extract import analyze_paper  # noqa: E402
from litprism.ingest import chunk_document, load_document  # noqa: E402
from litprism.llm import OpenAICompatClient  # noqa: E402
from litprism.prompts import CONCEPTS_SYSTEM, EXTRACT_SYSTEM  # noqa: E402
from litprism.reading import reading_batches  # noqa: E402
from litprism.secrets import SecretStore  # noqa: E402


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def inspect_parser(doc, chunks):
    """Conservative physical-page check using unique verbatim long lines, not an LLM judge."""
    lookup = {}
    for page in doc.pages:
        for line in page.text.splitlines():
            if len(line) > 45:
                lookup.setdefault(line, set()).add(page.number)
    mismatches = []
    for index, chunk in enumerate(chunks, 1):
        other = [{'line': line, 'physical_page': next(iter(lookup[line]))}
                 for line in chunk['text'].splitlines()
                 if line in lookup and len(lookup[line]) == 1 and chunk['page'] not in lookup[line]]
        if other:
            mismatches.append({'tag': f'P{index}', 'label_page': chunk['page'], 'other_page_lines': other})
    lines = [(page, text) for section in doc.sections if section.name not in ('参考文献', '目录')
             for page, text in section.lines if text.strip()]
    corpus = '\n'.join(c['text'] for c in chunks)
    return {'chunk_count': len(chunks), 'mismatched_chunks': mismatches,
            'page_order_reversals': sum(chunks[i]['page'] < chunks[i-1]['page'] for i in range(1, len(chunks))),
            'non_reference_source_lines': len(lines),
            'missing_source_lines': [{'page': page, 'text': text} for page, text in lines if text not in corpus],
            'tables': [{'page': page.number, **table} for page in doc.pages for table in page.tables],
            'warnings': doc.warnings,
            'note': 'Verbatim text presence does not verify table layout, chart values, or scientific meaning.'}


def live_client(provider_root):
    # Do not open the user's DB through KnowledgeBase: that could apply migrations.
    db = Path(provider_root) / 'data' / 'litprism.db'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
        row = con.execute('SELECT base_url, api_key_ref, chat_model FROM providers WHERE id=?',
                          ('pv_7b80a37754',)).fetchone()
    if not row:
        raise ValueError('Saved test provider is missing')
    key = SecretStore(db.parent).get(row[1])
    if not key:
        raise ValueError('Saved test credential is unavailable')
    return OpenAICompatClient(LLMConfig(provider='openai', base_url=row[0], api_key=key,
                                       model=row[2], temperature=0, max_tokens=8192, timeout=180))


class RecordingClient:
    supports_semantic_review = True
    def __init__(self, client, folder, replay=None, replay_tasks=None, cross_cache=None):
        self.client, self.folder = client, folder
        self.name = client.name
        self.calls = []
        self.replay = replay
        self.replay_tasks = replay_tasks or {'extract'}
        if replay:
            manifest = json.loads((replay/'manifest.json').read_text(encoding='utf-8'))
            if manifest.get('model') != self.name or manifest.get('temperature') != client.cfg.temperature:
                raise ValueError('Replay model/temperature changed')
        self.cross_cache = []
        for root in cross_cache or []:
            manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            if manifest.get('model') != self.name or manifest.get('temperature') != client.cfg.temperature:
                raise ValueError('Cross replay model/temperature changed')
            self.cross_cache.extend((p,json.loads(p.read_text(encoding='utf-8'))) for p in sorted(root.glob('call-*.json')))

    def chat_json(self, messages, *, task=None):
        number = len(self.calls) + 1
        path = self.folder / f'call-{number:02}.json'
        record = {'task': task, 'messages': messages}
        save(path, record)
        started = time.monotonic()
        try:
            if task == 'semantic_cross_review':
                cached = next(((p,c) for p,c in self.cross_cache if c.get('task') == task and c.get('messages') == messages and 'response' in c),None)
                if cached:
                    record['replayed_from'] = str(cached[0])
                    record['response'] = copy.deepcopy(cached[1]['response'])
                    return record['response']
            if task in self.replay_tasks and self.replay:
                candidates = [(p, json.loads(p.read_text(encoding='utf-8'))) for p in sorted(self.replay.glob('call-*.json'))]
                match = next(((p,c) for p,c in candidates if c.get('task') == task and c.get('messages') == messages and 'response' in c), None)
                if match is None:
                    raise ValueError('Replay extraction request does not exactly match frozen messages')
                record['replayed_from'] = str(match[0])
                record['response'] = copy.deepcopy(match[1]['response'])
                return record['response']
            data = self.client.chat_json(messages, task=task)
            record['response'] = copy.deepcopy(data)
            return data
        except Exception as exc:
            # Never persist arbitrary provider exception text (could contain secrets).
            record['error_type'] = type(exc).__name__
            record['error_http_status'] = getattr(exc, 'http_status', None)
            record['error_cause'] = type(exc.__cause__).__name__ if exc.__cause__ else None
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 2)
            record['usage'] = None if record.get('replayed_from') else getattr(self.client, 'last_usage', None)
            record['request_settings'] = None if record.get('replayed_from') else getattr(self.client, 'last_request_settings', None)
            self.calls.append(record)
            save(path, record)
            print(json.dumps({'call': number, 'seconds': record['seconds'],
                              'usage': record['usage'], 'error': record.get('error_type')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--windows', type=int, nargs='+', default=[32768, 28672])
    parser.add_argument('--reserve', type=int, default=16384)
    parser.add_argument('--provider-root', type=Path, default=Path('E:/codex-litprism-data'))
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--replay-extractions', type=Path, help='Reuse identical frozen extraction requests; only review calls use API')
    args = parser.parse_args()
    if args.reserve < 1 or any(w <= args.reserve for w in args.windows):
        parser.error('Each window must exceed the positive output reserve')
    sha256 = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    if sha256 != '97fd272f1fdfc18677462d0292f5fbf26ca86b4d1b485c2dba03269b643a0e83':
        parser.error('This frozen audit preset requires the exact arXiv 2005.14165v4 PDF')
    args.output.mkdir(parents=True, exist_ok=False)
    doc = load_document(args.pdf)
    chunks = chunk_document(doc)
    save(args.output / 'parser-check.json', inspect_parser(doc, chunks))
    save(args.output / 'document.json', {
        'pdf': str(args.pdf.resolve()), 'sha256': sha256,
        'source_url': 'https://arxiv.org/pdf/2005.14165v4',
        'code_sha256': {name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
                        for name in ['src/litprism/ingest.py', 'src/litprism/pdf_layout.py', 'src/litprism/reading.py',
                                     'src/litprism/extract.py', 'src/litprism/prompts.py',
                                     'src/litprism/evidence.py', 'src/litprism/llm.py', 'src/litprism/semantics.py']},
        'page_count': doc.page_count, 'characters': len(doc.full_text),
        'warnings': doc.warnings, 'sections': [dict(name=s.name, page=s.start_page) for s in doc.sections],
        'pages': [dict(page=p.number, text=p.text) for p in doc.pages], 'chunks': chunks,
    })
    client = live_client(args.provider_root) if args.live else None
    if client:
        client.cfg.max_tokens = args.reserve
    failed = False
    summary = {'schemes': [], 'all_live_calls': 0,
               'all_usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
               'note': 'A completed run is not a semantic acceptance result; compare the frozen anchors manually.'}
    for window in args.windows:
        folder = args.output / f'window-{window}'
        folder.mkdir()
        cfg = Config()
        cfg.app.data_dir = str(folder / 'data')
        cfg.context.window_tokens = window
        cfg.context.output_reserve_tokens = args.reserve
        kb = KnowledgeBase(cfg)
        try:
            pid = kb.add_paper(title='Language Models are Few-Shot Learners', year=2020,
                               arxiv_id='2005.14165v4', verify_level='manual_confirmed')
            kb.add_chunks(pid, chunks)
            if args.replay_extractions:
                frozen = json.loads((args.replay_extractions / f'window-{window}' / 'manifest.json').read_text(encoding='utf-8'))
                stored = kb.get_chunks(pid)
                if len(stored) != len(frozen['excerpts']):
                    raise ValueError('Replay source chunk count changed')
                with kb.tx() as con:
                    for current, old in zip(stored, frozen['excerpts'], strict=True):
                        if (current['text'],current['page']) != (old['text'],old['page']):
                            raise ValueError('Replay source content or physical page changed')
                        con.execute('UPDATE chunks SET id=? WHERE id=?', (old['id'],current['id']))
            paper = kb.get_paper(pid)
            extract_system = EXTRACT_SYSTEM + '\n每个非空字段必须在 field_evidence 提供原文标记列表；无依据留空。'
            extract_system += '\n每个独立句子就地附其直接依据，不把多个分号连接的结论统一在段末附一串引用；每句最多 3 个直接来源，复杂内容分成多句。'
            concept_system = CONCEPTS_SYSTEM + '\n每条 definition 必须含本批原文引用 [P# p.N]；无页码不写页码，无依据不生成。'
            batches, excerpts, coverage = reading_batches(kb, cfg, [paper],
                max([extract_system, concept_system], key=len), '仅分析本批原文。未读取部分不能称为原文未提及。')
            manifest = {'window': window, 'reserve': args.reserve, 'temperature': 0,
                        'model': client.name if client else None, 'coverage': coverage,
                        'database_path': str(cfg.db_path),
                        'batches': batches, 'excerpts': excerpts}
            save(folder / 'manifest.json', manifest)
            print(json.dumps({'window': window, 'batches': len(batches), 'chunks': len(excerpts)}), flush=True)
            if client:
                recorder = RecordingClient(client, folder, args.replay_extractions / f'window-{window}' if args.replay_extractions else None)
                result = None
                try:
                    result = analyze_paper(kb, cfg, recorder, pid, force=True, with_concepts=False)
                    save(folder / 'analysis.json', result)
                    failed |= not result['_citation_check']['passed']
                    print(json.dumps({'window': window, 'citation_check': result['_citation_check']}), flush=True)
                except Exception as exc:
                    failed = True
                    save(folder / 'failure.json', {'error_type': type(exc).__name__,
                                                  'analysis_saved': kb.get_analysis(pid) is not None})
                    print(json.dumps({'window': window, 'error_type': type(exc).__name__}), flush=True)
                finally:
                    extraction = [c for c in recorder.calls if c['task'] == 'extract']
                    submitted = [e for batch in batches[:len(extraction)] for e in batch]
                    costs = [sum(Meter(cfg).estimate_message(m) for m in call['messages'])
                             for call in recorder.calls]
                    save(folder / 'transport-check.json', {
                        'planned_batches': len(batches), 'attempted_batches': len(extraction),
                        'successful_batches': sum('response' in c for c in extraction),
                        'calls_by_task': {t: sum(c['task'] == t for c in recorder.calls) for t in {c['task'] for c in recorder.calls}},
                        'all_batches_attempted': len(submitted) == sum(map(len, batches)),
                        'estimated_input_tokens': costs,
                        'actual_input_tokens': [(c.get('usage') or {}).get('prompt_tokens') for c in recorder.calls],
                        'note': 'Submission and citation syntax checks do not verify semantic entailment.',
                    })
                    records = [{'file': str(folder / f'call-{i:02}.json'), 'seconds': c['seconds'],
                                'task': c['task'], 'replayed_from': c.get('replayed_from'),
                                'error_type': c.get('error_type'), 'usage': c.get('usage'),
                                'estimated_input': costs[i-1]} for i, c in enumerate(recorder.calls, 1)]
                    summary['schemes'].append({'window': window, 'reserve': args.reserve,
                                               'planned_batches': len(batches), 'records': records,
                                               'citation_check': (result or {}).get('_citation_check'),
                                               'semantic_summary': {k:v for k,v in (result or {}).get('_semantic_review', {}).items() if k != 'items'},
                                               'analysis_saved': kb.get_analysis(pid) is not None})
                    summary['all_live_calls'] += sum(not c.get('replayed_from') for c in recorder.calls)
                    for call in recorder.calls:
                        for key in summary['all_usage']:
                            summary['all_usage'][key] += (call.get('usage') or {}).get(key, 0)
                    save(args.output / 'run-summary.json', summary)
        finally:
            kb.close()
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
