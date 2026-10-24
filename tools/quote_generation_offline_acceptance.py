"""Replay frozen quotes; operator-authored unit controls are not new generation.

Reads JSON artifacts only. Does not open databases, access credentials, dispatch
models, fix production responses, or infer a replacement source by similarity.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from litprism.context.meter import Meter  # noqa: E402
from litprism.prompts import QA_QUOTE_CHECK, QA_SYSTEM  # noqa: E402
from litprism.qa_answer import compile_answer  # noqa: E402
from litprism.qa_claims import audit_map  # noqa: E402
from litprism.qa_retrieval import source_block  # noqa: E402


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def replay(value,sources):
    answer,mapping,bindings,protocol = compile_answer(value)
    return audit_map(answer,mapping,sources,answer_bindings=bindings,answer_protocol=protocol)


def controlled_unit(value,control):
    """Explicit copied-fixture edit, hash-bound to one original answer unit."""
    index = control['unit_index']
    if type(index) is not int or index < 0 or index >= len(value['answer_units']):
        raise ValueError('Control must select an existing original unit')
    original = value['answer_units'][index]
    if digest(original) != control['original_unit_sha256']:
        raise ValueError('Original unit changed; do not reuse this control')
    replacement = control['replacement']
    if not isinstance(replacement,dict) or not replacement or set(replacement)-{'text','sources','quotes'}:
        raise ValueError('Control only changes explicit text, source tags and literal quotations')
    result = deepcopy(value)
    result['answer_units'][index].update(deepcopy(replacement))
    return result


def frame_frozen_sources(prefix,sources):
    """Retain original delivered headers; review snapshots omit some metadata."""
    cursor,blocks = 0,[]
    separator = '\n\n---\n\n'
    for n,s in enumerate(sources):
        end = prefix.index('\n',cursor)
        head = prefix[cursor:end]
        if not head.startswith('['+s['tag']+'] '):
            raise ValueError('Frozen source header does not match inventory order')
        body_end = end+1+len(s['text'])
        if prefix[end+1:body_end] != s['text']:
            raise ValueError('Frozen source body differs from delivered text')
        blocks.append(source_block(head,s['tag'],s['text']))
        cursor = body_end
        if n < len(sources)-1:
            if prefix[cursor:cursor+len(separator)] != separator:
                raise ValueError('Unexpected frozen source boundary')
            cursor += len(separator)
    if cursor != len(prefix):
        raise ValueError('Extra text outside frozen source inventory')
    return separator.join(blocks)


def run(source,output,controls_file):
    source,output = source.resolve(),output.resolve()
    if (ROOT not in source.parents or ROOT not in output.parents or source==output
            or not source.name.startswith('_selftest.') or not output.name.startswith('_selftest.')):
        raise ValueError('Use distinct _selftest roots inside this workspace')
    spec = read(controls_file)
    cases = read(source/'manifest.json')['cases']
    files = [source/'manifest.json',controls_file]
    for case in cases:
        files.extend(source/case['id']/f for f in ('raw-qa.txt','requests.json','result.json'))
    frozen = {str(p):sha(p) for p in files}
    output.mkdir(parents=True,exist_ok=False)
    rows,controls = [],[]
    for case in cases:
        name = case['id']
        row = read(source/name/'result.json')
        value = read(source/name/'raw-qa.txt')
        sources = row['review']['sources']
        old = replay(value,sources)
        if old['blocking_issues'] != row['review']['evidence_audit']['blocking_issues']:
            raise ValueError('Historical quote outcome changed')
        req = next(r for r in read(source/name/'requests.json') if r['task']=='qa')
        old_prefix = req['messages'][-1]['content'].split('【片段】\n',1)[1].split('\n\n【程序送达记录】',1)[0]
        new_prefix = frame_frozen_sources(old_prefix,sources)
        folder = output/name;folder.mkdir()
        (folder/'rendered-sources.txt').write_text(new_prefix,encoding='utf-8')
        write(folder/'original-replay.json',old)
        rows.append({'case':name,'original_status':row['status'],'blockers':old['blocking_issues'],
            'source_count':len(sources),'raw_source_prefix_preserved':True,
            'original_prefix_tokens_estimated':Meter().estimate_text(old_prefix),
            'new_prefix_tokens_estimated':Meter().estimate_text(new_prefix)})
        for c in spec['controls']:
            if c['case'] != name:continue
            variant = controlled_unit(value,c)
            audit = replay(variant,sources)
            if audit['blocking_issues']:
                raise ValueError('Authored control still contains invalid quotes')
            assert audit['semantic_status']=='unreviewed'
            write(folder/'scripted-control.json',{'value':variant,'audit':audit,'operator_note':c['note'],
                'new_generation':False,'semantic_certification':False})
            controls.append({'case':name,'unit_index':c['unit_index'],'quote_location_passed':True,
                'scripted_operator_control':True,'semantic_status':'unreviewed'})
    assert {c['case'] for c in controls} == {c['case'] for c in spec['controls']}
    assert all(sha(Path(p))==value for p,value in frozen.items())
    result = {'model_calls':0,'new_generation':False,'original_files_unchanged':True,
        'source_files_sha256':frozen,'semantic_passed':None,'original_rows':rows,'scripted_controls':controls,
        'original_blockers_retained':sum(len(r['blockers']) for r in rows),
        'quality_limit':'Prompt and boundary contract verified offline; no evidence yet of new model adherence.',
        'implementation_sha256':{str(p.relative_to(ROOT)):sha(p) for p in [
            ROOT/'src/litprism/prompts.py',ROOT/'src/litprism/qa_retrieval.py',ROOT/'src/litprism/agents.py',Path(__file__).resolve()]}}
    write(output/'summary.json',result)
    write(output/'generation-contract.json',{'system':QA_SYSTEM,'final_check':QA_QUOTE_CHECK,
        'partial_contract_not_a_dispatched_request':True})
    return {k:v for k,v in result.items() if k not in ('source_files_sha256','implementation_sha256')}


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--controls',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.source,args.output,args.controls),ensure_ascii=True))
