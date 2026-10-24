"""Fixed long-chat acceptance using copied real history and controlled compaction.

No fake smaller model window, automatic retries, daily-library writes, or secret
output. Each live case runs once through the persistent Web entry point. Operator
decisions are separate files passed to default_chat_acceptance.py, never automatic
approvals inferred from model output.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tools')]
from default_chat_acceptance import require_home, environment, turn, fingerprints  # noqa: E402
from chat_mixed_acceptance import write, sha  # noqa: E402

CASES = [
    ('compacted', '刚才那篇 Transformer 论文的多头注意力有哪些作用？请简要说明并给出当前来源。'),
    ('confirmed', '结合刚保存的“Transformer 多头注意力结论”与当前来源，简要说明它为什么使用多个注意力头，并标注来源。'),
    ('revised', '结合刚修订的“Transformer 多头注意力结论”，说明适用范围；是否意味着所有注意力头必然学到不同功能？请区分原文和推断。'),
    ('revoked', '我已撤销那条“Transformer 多头注意力结论”。请重新查论文与网页，简要回答原论文对多头注意力的动机，不沿用已撤销知识。'),
    ('restricted', '仅根据所选 Transformer 原论文，说明它是否依赖循环或卷积；不使用其他论文、人工知识或网页。'),
]


def prepare(home, source):
    import yaml
    from anchor_live_acceptance import clone
    from litprism.config import _from_dict, config_to_dict
    from litprism.secrets import SecretStore
    home, source = require_home(home), require_home(source)
    if home.exists():
        raise ValueError('Existing acceptance is immutable; use a new root')
    seed = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    base = _from_dict(yaml.safe_load((source/'config/config.yaml').read_text(encoding='utf-8')))
    cfg, kb = clone(home, source/'data/litprism.db', base, source)
    try:
        key = SecretStore(source/'data').get(base.websearch.api_key_ref)
        target = SecretStore(home/'data')
        if not key or target.backend == 'weak':
            raise ValueError('Secure search credential unavailable')
        cfg.websearch.api_key_ref = target.set('long_chat_acceptance_search',key)
        del key
        if cfg.qa.evidence_anchor_trial or cfg.websearch.provider != 'tavily':
            raise ValueError('Use the active quotes protocol and real Tavily search')
        cid = seed['conversation_id']
        if not kb.resolve_conversation(cid):
            raise ValueError('Seed conversation missing')
        with kb.tx() as con:
            con.execute('UPDATE conversations SET scope=? WHERE id=?', ('{}',cid))
        (home/'config').mkdir(exist_ok=True)
        (home/'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(cfg),allow_unicode=True),encoding='utf-8')
        write(home/'manifest.json', {'source':str(source),'source_manifest_sha256':sha(source/'manifest.json'),
            'conversation_id':cid,'papers':seed['papers'],'cases':[{'id':a,'question':b} for a,b in CASES],
            'controlled_compaction_range':[9,12], 'planned_qa_calls':5,'maximum_normal_expansions':5,
            'request_namespace':'long-context-fixed-',
            'authorization':'User requested next planned task; previously authorized fixed acceptance to completion.',
            'policy':'One first attempt per case; no paid retries, no token cap override, no source relabeling.',
            'scope':'Real existing history compressed with production extractive backend; five real followups; operator confirm/revise/revoke controls in this copy only.',
            'limitations':'Controlled trigger, not natural 1M-window exhaustion; operator controls are not human approval or general semantic certification.',
            'scripted_operator_not_user_approval':True,'daily_database_opened':False})
        write(home/'code-sha256.json',fingerprints())
        print(json.dumps({'prepared':str(home),'fixed_turns':len(CASES)},ensure_ascii=True))
    finally:
        kb.close()


def audit(home, label):
    from litprism.context.dialogue import build_dialogue
    from litprism.context.meter import Meter
    home = require_home(home)
    kb, app, _ = environment(home)
    try:
        cid = json.loads((home/'manifest.json').read_text(encoding='utf-8'))['conversation_id']
        d = build_dialogue(kb,kb.cfg,cid,None)
        text, refs = d.prompt(Meter(kb.cfg),20000)
        row = {'referents':d.referents,'evidence_refs':d.evidence_refs,'history_refs':refs,'prompt':text,
            'history':[{'seq':m['seq'],'ref':m['ref'],'role':m['role'],'kind':m['kind']} for m in d.history]}
        write(home/('context-'+label+'.json'),row)
        print(json.dumps({k:v for k,v in row.items() if k not in ('prompt','history')},ensure_ascii=True))
    finally:
        app.state.tasks.pool.shutdown(wait=True);kb.close()


def compact(home):
    from litprism.context.meter import Meter
    from litprism.context.policy import SelectionResult
    from litprism.context.seam import Seam
    from litprism.context.summarizer import Summarizer, ExtractiveSummarizer
    home = require_home(home)
    marker = home/'compaction-started.json'
    with marker.open('x',encoding='utf-8') as f:
        json.dump({'no_repeat':True},f)
    manifest = json.loads((home/'manifest.json').read_text(encoding='utf-8'))
    kb, app, _ = environment(home)
    try:
        cfg = kb.cfg
        cid = manifest['conversation_id']
        start,end = manifest['controlled_compaction_range']
        messages = [m for m in kb.get_messages(cid) if start <= m['seq'] <= end]
        if [m['role'] for m in messages] != ['user','assistant','user','assistant']:
            raise ValueError('Frozen paired seed range changed')
        tokens = sum(Meter(cfg).estimate_message(m) for m in messages)
        selection = SelectionResult(ok=True,start_seq=start,end_seq=end,
            shadowed_refs=[m['ref'] for m in messages],shadowed_tokens=tokens)
        seam = Seam(kb,cfg)
        tx,_ = seam.begin(cid,'controlled-live-long-context',selection)
        if not tx:raise ValueError('Compaction begin refused')
        summary = Summarizer(cfg,backend=ExtractiveSummarizer()).summarize(messages,tokens)
        if not summary.ok:
            seam.fail(tx,'summary',summary.error)
            raise ValueError(summary.error)
        result = seam.commit(tx,summary)
        if not result.ok:raise ValueError(result.message)
        record = {'block_id':result.block_id,'range':[start,end],'original_tokens':tokens,
            'summary_tokens':summary.framed_tokens,'attempts':summary.attempts,
            'summary':summary.framed,'controlled_trigger':True,'model_summary_calls':0}
        write(home/'compaction.json',record)
        print(json.dumps({k:v for k,v in record.items() if k!='summary'},ensure_ascii=True))
    finally:
        app.state.tasks.pool.shutdown(wait=True);kb.close()
    audit(home,'compacted-reopened')


def controls(home, label='verified'):
    """Zero-model negative controls on separate copies of the actual history."""
    import yaml
    from chat_mixed_acceptance import backup_readonly
    from litprism.config import _from_dict, config_to_dict
    from litprism.context.dialogue import build_dialogue
    from litprism.context.restore import Restorer
    home = require_home(home)
    manifest = json.loads((home/'manifest.json').read_text(encoding='utf-8'))
    block = json.loads((home/'compaction.json').read_text(encoding='utf-8'))['block_id']
    cfg = _from_dict(yaml.safe_load((home/'config/config.yaml').read_text(encoding='utf-8')))
    pid = next(p['id'] for p in manifest['papers'] if p['title']=='Attention Is All You Need')
    rows = []
    for change in ('withdraw','material','review'):
        branch = home/('_selftest.control-'+change+'-'+label)
        branch.mkdir(exist_ok=False)
        child = _from_dict(config_to_dict(cfg))
        child.app.data_dir, child.export.notes_dir = str(branch/'data'),str(branch/'notes')
        backup_readonly(home/'data/litprism.db',branch/'data/litprism.db')
        (branch/'config').mkdir()
        (branch/'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(child),allow_unicode=True),encoding='utf-8')
        kb, app, client = environment(branch)
        try:
            cid = manifest['conversation_id']
            before = build_dialogue(kb,child,cid,None)
            checkpoint = next(m for m in before.history if m['kind']=='compact-checkpoint' and m['block_id']==block)
            original = kb.get_paper(pid)['current_material_id']
            if change=='withdraw':
                kb.update_paper(pid,verify_level='unchecked')
            elif change=='material':
                chunks = kb.get_chunks(pid)
                # Simulate a parser-version change using identical real text;
                # only chapter metadata changes, never scientific contents.
                kb.add_chunks(pid,[{'text':c['text'],'page':c['page'],
                    'section':(c.get('section') or '')+'\n','section_role':c.get('section_role')}
                    for c in chunks],level='fulltext')
                assert kb.get_paper(pid)['current_material_id'] != original
            else:
                target = next(m for m in kb.get_messages(cid) if m['seq']==12)
                rid = json.loads(target['meta'])['qa_review_id']
                parent = client.get('/api/chat-reviews/'+rid);parent.raise_for_status()
                uid = parent.json()['units'][0]['id']
                endpoint = '/api/chat-reviews/'+rid+'/units/'+uid
                detail = client.get(endpoint);detail.raise_for_status();d=detail.json()
                response = client.post(endpoint+'/review',json={'action':'reject','note':'Offline copied-history control; not user approval.',
                    'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']})
                response.raise_for_status()
            after = build_dialogue(kb,child,cid,None)
            assert checkpoint['ref'] not in [m['ref'] for m in after.history]
            assert not any(m['seq']==12 for m in after.history)
            restored = Restorer(kb,child).restore_block(block)
            assert restored.ok and len(restored.messages)==4 and restored.citations
            row = {'change':change,'covering_summary_excluded':True,'covered_reply_excluded':True,
                'original_material_id':original,'current_material_id':kb.get_paper(pid)['current_material_id'],
                'audit_originals_preserved':len(restored.messages),'paper_sources_reopen':len(restored.citations),
                'after_history_refs':[m['ref'] for m in after.history],'new_model_calls':0}
            write(branch/'result.json',row);rows.append(row)
        finally:
            app.state.tasks.pool.shutdown(wait=True);kb.close()
    write(home/'offline-controls.json',rows)
    print(json.dumps(rows,ensure_ascii=True))


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','compact','audit','turn','controls'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--case',choices=[c[0] for c in CASES])
    parser.add_argument('--label',default='current')
    args=parser.parse_args()
    if args.action=='prepare':prepare(args.output,args.source)
    elif args.action=='compact':compact(args.output)
    elif args.action=='audit':audit(args.output,args.label)
    elif args.action=='controls':controls(args.output,args.label)
    else:turn(args.output,args.case)
