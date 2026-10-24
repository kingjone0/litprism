"""Fixed first-attempt live Web acceptance; raw data stays in a new _selftest root.

Uses real search and the normal answer protocol. Review actions require a separate
operator-authored JSON file after inspecting the actual reply and delivered text.
They apply only to the copied database and are not user knowledge approvals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
from chat_mixed_acceptance import write, sha, inspect_result, wait  # noqa: E402

CASES = [
    ('mixed', 'Transformer 原论文提出的架构是否依赖循环网络或卷积？请综合知识库和可取得的网页来源，简洁回答并标注出处。'),
    ('compare', '比较 Transformer 与 BERT：两者的编码器是否都使用自注意力？BERT 的预训练与 Transformer 原论文的翻译任务有什么区别？请区分直接陈述与概括，标明来源。'),
    ('confirmed', '检索刚保存的“Transformer 架构结论”，用一句话说明它是否依赖循环或卷积，并标出这条人工知识的来源。'),
    ('revised', '结合刚修订的“Transformer 架构结论”和其他可用来源，说明该结论的适用范围；不要把它扩大成所有后续模型都不含卷积。'),
    ('revoked', '我已撤销那条“Transformer 架构结论”。请重新检索论文与网页来源，回答原论文的模型结构是否依赖循环或卷积，并给出当前可用出处。'),
    ('restricted', '仅根据所选的 Transformer 原论文，简要说明多头注意力的作用，不使用其他文献或网页。'),
]


def fingerprints():
    files = [p for p in (ROOT/'src').rglob('*') if p.suffix in ('.py', '.js', '.css')]
    return {str(p.relative_to(ROOT)): sha(p) for p in files}


def require_home(home):
    home = home.resolve()
    if not home.name.startswith('_selftest.') or ROOT not in home.parents:
        raise ValueError('Use an independent _selftest root inside the workspace')
    return home


def prepare(home, source):
    import yaml
    from anchor_live_acceptance import clone
    from litprism.config import _from_dict, config_to_dict
    from litprism.secrets import SecretStore
    home, source = require_home(home), require_home(source)
    if home.exists():
        raise ValueError('Output exists; do not replace or repeat acceptance')
    cfg = _from_dict(yaml.safe_load((source/'config/config.yaml').read_text(encoding='utf-8')))
    cfg, kb = clone(home, source/'data/litprism.db', cfg, source)
    try:
        cfg.qa.evidence_anchor_trial = False
        cfg.llm.timeout = cfg.llm.review_read_timeout = 900
        cfg.context.overflow_retries = 1
        raw = yaml.safe_load((Path('E:/aicoding/paper-research-agent/config/config.yaml')).read_text(encoding='utf-8'))
        data = Path(raw['app']['data_dir'])
        if not data.is_absolute():
            data = Path('E:/aicoding/paper-research-agent')/data
        if not (data/'secrets.json').is_file():
            raise ValueError('Existing search secret store not found')
        key = SecretStore(data).get(raw['websearch']['api_key_ref'])
        target = SecretStore(home/'data')
        if not key or target.backend == 'weak':
            raise ValueError('Secure stored search key unavailable')
        cfg.websearch.provider = 'tavily'
        cfg.websearch.api_key_ref = target.set('default_chat_acceptance_search', key)
        del key
        for name in ('crossref','openalex','arxiv','semantic_scholar','unpaywall'):
            getattr(cfg.sources, name).enabled = False
        papers = [dict(r) for r in kb._con.execute('SELECT id,title,verify_level,current_material_id FROM papers WHERE deleted_at IS NULL')]
        if any(p['verify_level'] != 'verified' for p in papers):
            raise ValueError('Use the original verified four-paper corpus; do not relabel sources')
        if kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]:
            raise ValueError('Seed must not contain earlier operator knowledge')
        cid = kb.add_conversation('默认全库与联网固定验收', scope={})
        (home/'config').mkdir(exist_ok=True)
        (home/'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(cfg), allow_unicode=True), encoding='utf-8')
        write(home/'manifest.json', {'source':str(source), 'source_manifest_sha256':sha(source/'manifest.json'),
            'conversation_id':cid, 'papers':papers, 'cases':[{'id':a,'question':b} for a,b in CASES],
            'purpose':'Six fixed first-attempt turns: five real mixed searches plus one restricted-source turn; confirm/revise/revoke controls.',
            'authorization':'User requested direct acceptance after commit; prior fixed-acceptance-to-completion budget retained; no manual token cap.',
            'policy':'One first QA per case; at most one normal expansion per case; no paid retries or reruns. Unknown usage and failures retained.',
            'quality':'Useful supported main conclusions; harmless paraphrase, translation and auxiliary omissions are not semantic failures. Wrong sources, numbers, negation or attribution remain failures.',
            'scripted_operator_not_user_approval':True, 'daily_database_opened':False})
        print(json.dumps({'prepared':str(home),'cases':6,'papers':len(papers),'secure_search_key':True},ensure_ascii=True))
    finally:
        kb.close()


def environment(home):
    import yaml
    from litprism.config import _from_dict
    from litprism.db import KnowledgeBase
    from litprism.web import server
    from fastapi.testclient import TestClient
    home = require_home(home)
    cfg = _from_dict(yaml.safe_load((home/'config/config.yaml').read_text(encoding='utf-8')))
    if cfg.data_path.resolve() != (home/'data').resolve():
        raise ValueError('Acceptance configuration must use its own copied data directory')
    kb = KnowledgeBase(cfg)
    original = server.KnowledgeBase
    server.KnowledgeBase = lambda _: kb
    try:
        app = server.create_app(cfg)
    finally:
        server.KnowledgeBase = original
    return kb, app, TestClient(app)


def turn(home, case_id):
    from litprism.llm import OpenAICompatClient
    home = require_home(home)
    manifest = json.loads((home/'manifest.json').read_text(encoding='utf-8'))
    case = next(c for c in manifest['cases'] if c['id'] == case_id)
    frozen = home/'code-sha256.json'
    if frozen.exists() and json.loads(frozen.read_text(encoding='utf-8')) != fingerprints():
        raise ValueError('Implementation changed; do not mix acceptance versions')
    if not frozen.exists():
        write(frozen, fingerprints())
    folder = home/case_id
    folder.mkdir(exist_ok=False)
    write(folder/'started.json', {'case':case,'time':time.time()})
    kb, app, client = environment(home)
    requests, original = [], OpenAICompatClient.chat
    def capture(self, messages, **kwargs):
        task = kwargs.get('task')
        if task not in ('qa','expand') or sum(r['task']==task for r in requests) >= 1:
            raise RuntimeError('Extra model dispatch prevented by fixed first-attempt protocol')
        record = {'task':task,'messages':messages,'kwargs':kwargs}
        requests.append(record); write(folder/'requests.json', requests)
        response = original(self, messages, **kwargs)
        (folder/('raw-'+task+'.txt')).write_text(response, encoding='utf-8')
        record['response_sha256'] = sha(folder/('raw-'+task+'.txt'))
        write(folder/'requests.json', requests)
        return response
    OpenAICompatClient.chat = capture
    try:
        cid = manifest['conversation_id']
        if case_id == 'restricted':
            pid = next(p['id'] for p in manifest['papers'] if p['title']=='Attention Is All You Need')
            r=client.patch('/api/conversations/'+cid,json={'scope':{'paper_ids':[pid],'mode':'strict'}}); r.raise_for_status()
        stamp=time.monotonic()
        response=client.post('/api/conversations/'+cid+'/chat-runs',json={
            'question':case['question'],'request_id':'default-fixed-'+case_id,'web_search':case_id!='restricted'})
        response.raise_for_status()
        rid=response.json()['run_id']
        write(folder/'run.json', {'run_id':rid})
        state=wait(client,rid,stamp)
        row=inspect_result(kb,client,cid,rid,dict(case,expect='Useful grounded response; optional knowledge controls and actual source lifecycle'),state,time.monotonic()-stamp)
        if row['review']:
            for unit in row['review']['units']:
                unit['detail']=client.get('/api/chat-reviews/'+row['review']['id']+'/units/'+unit['id']).json()
        row['request_count']=len(requests)
        row['implementation_unchanged']=fingerprints()==json.loads(frozen.read_text(encoding='utf-8'))
        write(folder/'result.json', row)
        print(json.dumps({'case':case_id,'run_status':row['run_status'],'status':row['status'],
            'seconds':row['elapsed_seconds'],'usage':row['usage'], 'citations':len(row['citation_audit']),
            'valid_citations':sum(c['valid'] for c in row['citation_audit']),
            'cited_kinds':sorted({c['kind'] for c in row['citation_audit'] if c['kind']}),
            'source_warning':row.get('source_warning')},ensure_ascii=True),flush=True)
    finally:
        OpenAICompatClient.chat=original
        app.state.tasks.pool.shutdown(wait=True)
        kb.close()


def decide(home, decision_file):
    home=require_home(home)
    request=json.loads(decision_file.read_text(encoding='utf-8'))
    kb, app, client=environment(home)
    try:
        endpoint='/api/chat-reviews/'+request['review_id']+'/units/'+request['unit_id']
        detail=client.get(endpoint); detail.raise_for_status(); d=detail.json()
        if request.get('show_context'):
            c=client.get(endpoint+'/sources/'+request['show_context']); c.raise_for_status()
            write(home/'operator-context.json',c.json())
            print('Actual source context saved locally; no model call.')
            return
        body={**request['decision'],'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']}
        response=client.post(endpoint+'/review',json=body); response.raise_for_status()
        events=home/'operator-events.json'
        rows=json.loads(events.read_text(encoding='utf-8')) if events.exists() else []
        rows.append({'request':request,'response':response.json(),'scripted_operator_not_user_approval':True})
        write(events,rows)
        print(json.dumps(response.json(),ensure_ascii=True))
    finally:
        app.state.tasks.pool.shutdown(wait=True);kb.close()


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','turn','decide'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--case',choices=[c[0] for c in CASES])
    parser.add_argument('--decision',type=Path)
    args=parser.parse_args()
    if args.action=='prepare':prepare(args.output,args.source)
    elif args.action=='turn':turn(args.output,args.case)
    else:decide(args.output,args.decision)
