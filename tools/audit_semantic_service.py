"""Explicit bounded service acceptance: frozen cases, then real short-paper pipeline.

No credentials or reasoning text are stored. Provider DB is opened read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from audit_long_paper import RecordingClient, live_client, save  # noqa: E402

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.extract import analyze_paper  # noqa: E402
from litprism.ingest import chunk_document, load_document  # noqa: E402
from litprism.semantics import make_units, reconcile, review_units  # noqa: E402


def grade(rows, planned):
    positives = [r for r in rows if r['expected'] == 'supported']
    negatives = [r for r in rows if r['expected'] == 'blocked']
    false_accepts = [r['id'] for r in negatives if r['raw_verdict'] == 'supported' or r['released']]
    correct_positive = sum(r['released'] for r in positives)
    transport_ok = all(r['complete'] for r in rows) and len(rows) == planned
    return {'passed': bool(positives and negatives) and transport_ok and not false_accepts
            and correct_positive / len(positives) >= .8,
            'planned': planned, 'completed': len(rows), 'transport_complete': transport_ok,
            'positive_count': len(positives), 'positive_released': correct_positive,
            'false_accept_ids': false_accepts,
            'note': 'Acceptance is limited to this frozen sample; not a general accuracy guarantee.'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--fixture', type=Path, default=Path('tools/fixtures/semantic-real-cases.json'))
    ap.add_argument('--split', choices=['development', 'holdout', 'all'], default='development')
    ap.add_argument('--limit', type=int, default=48)
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--paper', choices=['fasttext', 'rmsnorm'])
    ap.add_argument('--cross',action='store_true',help='Run four manually specified cross-claim controls')
    ap.add_argument('--replay-run',type=Path,help='Reuse exactly matching successful extraction/local review requests; cross checks stay live')
    ap.add_argument('--cross-cache',type=Path,action='append',default=[],help='Reuse identical completed cross requests; independent case repetitions never use this cache')
    ap.add_argument('--live', action='store_true', required=True)
    args = ap.parse_args()
    if not 1 <= args.limit <= 48 or not 1 <= args.repeat <= 3:
        ap.error('limit 1..48, repeat 1..3')
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(args.fixture.read_text(encoding='utf-8'))
    client = live_client(Path('E:/codex-litprism-data'))
    client.cfg.temperature, client.cfg.max_tokens, client.cfg.timeout = .1, 8192, 60
    recorder = RecordingClient(client, args.output,args.replay_run,{'extract','semantic_review'},args.cross_cache if args.paper else [])
    cfg = Config()
    cfg.context.window_tokens, cfg.context.output_reserve_tokens = 16384, 8192
    metadata = {'fixture_sha256': hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
                'model': client.name, 'temperature': .1, 'paper': args.paper,
                'code_sha256': {f: hashlib.sha256((Path('src/litprism')/f).read_bytes()).hexdigest()
                                for f in ('semantics.py','llm.py','extract.py')}, 'split':args.split}
    save(args.output/'manifest.json', metadata)
    if args.cross:
        path=Path('tools/fixtures/semantic-cross-cases.json')
        data=json.loads(path.read_text(encoding='utf-8'))
        metadata['cross_fixture_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        save(args.output/'manifest.json',metadata)
        rows=[]
        for case in data['cases']:
            items=[{'id':f'cl{i}','text':text,'batch':i+1,'field':'summary','facts':[],
                    'status':'needs_review','reason':'test candidate'} for i,text in enumerate(case['claims'])]
            differences,coverage=reconcile(items,cfg,recorder)
            row={'id':case['id'],'expected_conflict':case['expected_conflict'],
                 'found':bool(differences),'coverage':coverage,'differences':differences}
            rows.append(row)
            save(args.output/'cross-results.json',{'passed':all(r['coverage']['complete'] and r['found']==r['expected_conflict'] for r in rows) and len(rows)==len(data['cases']),'rows':rows})
            if not coverage['complete']:
                return 1
        return int(any(r['found']!=r['expected_conflict'] for r in rows))
    if args.paper:
        path = Path('_selftest.semantic-short')/(args.paper+'.pdf')
        if hashlib.sha256(path.read_bytes()).hexdigest() != fixture['papers'][args.paper]['sha256']:
            raise ValueError('Short paper SHA256 changed')
        doc = load_document(path)
        chunks = chunk_document(doc)
        cfg.app.data_dir = str(args.output/'data')
        cfg.context.window_tokens = 65536
        kb = KnowledgeBase(cfg)
        try:
            pid = kb.add_paper(title=doc.title or args.paper, verify_level='manual_confirmed')
            kb.add_chunks(pid,chunks)
            if args.replay_run:
                old=json.loads((args.replay_run/'analysis.json').read_text(encoding='utf-8'))['_citations']
                current=kb.get_chunks(pid)
                if len(old)!=len(current):
                    raise ValueError('Replay chunk count changed')
                with kb.tx() as con:
                    for src,ref in zip(current,old,strict=True):
                        if src['page']!=ref['page']:
                            raise ValueError('Replay physical page changed')
                        con.execute('UPDATE chunks SET id=? WHERE id=?',(ref['chunk_id'],src['id']))
            data = analyze_paper(kb,cfg,recorder,pid,force=True,with_concepts=False)
            save(args.output/'analysis.json',data)
            save(args.output/'pipeline-summary.json',{'pages':doc.page_count,'chunks':len(chunks),
                 'warnings':doc.warnings,'citation_check':data['_citation_check'],
                 'transport':{'logical_calls':len(recorder.calls),'live_calls':sum(not c.get('replayed_from') for c in recorder.calls),
                    'replayed_calls':sum(bool(c.get('replayed_from')) for c in recorder.calls),
                    'live_total_tokens':sum((c.get('usage') or {}).get('total_tokens',0) for c in recorder.calls)},
                 'semantic':{k:v for k,v in data['_semantic_review'].items() if k!='items'}})
        finally:
            kb.close()
        return 0
    cases = [c for c in fixture['cases'] if args.split == 'all' or c['split'] == args.split][:args.limit]
    if len(cases)*args.repeat > 48:
        ap.error('At most 48 paid case calls per run')
    rows = []
    planned = len(cases)*args.repeat
    for rep in range(args.repeat):
        for case in cases:
            excerpts = [{**fixture['sources'][sid], 'id':sid, 'paper_id':fixture['sources'][sid]['paper'],
                         'paper_title':fixture['sources'][sid]['paper'], 'tag':f'P{i}'}
                        for i,sid in enumerate(case['source_ids'],1)]
            refs = [e['tag'] for e in excerpts]
            items = make_units([('key_conclusions',case['text'],refs)],excerpts,1)
            if len(items) != 1:
                raise ValueError('Fixture case must be a single claim')
            error = None
            try:
                review_units(items,excerpts,cfg,recorder)
            except Exception as exc:
                error = type(exc).__name__
            response = recorder.calls[-1].get('response') or {}
            raw = next((r for r in response.get('reviews',[]) if r.get('id')==items[0]['id']),{})
            complete = not error and not items[0].get('review_error') and raw.get('verdict') in ('supported','contradicted','needs_review')
            rows.append({'id':case['id'],'repeat':rep,'expected':case['expected'],
                         'raw_verdict':raw.get('verdict'),'released':items[0]['status']=='machine_supported',
                         'complete':complete,'error_type':error,'item':items[0]})
            summary = grade(rows,planned)
            save(args.output/'results.json',{'summary':summary,'rows':rows})
            print(json.dumps({k:v for k,v in rows[-1].items() if k!='item'},ensure_ascii=False),flush=True)
            if not complete:
                return 1  # Do not expand calls after transport/format failure.
            if summary['false_accept_ids']:
                return 1
    return int(not grade(rows,planned)['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
