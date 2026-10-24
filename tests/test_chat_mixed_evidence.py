"""Mixed-source authorization, human corrections and upstream lifecycle, offline."""
import json
import re

import pytest
from fastapi import HTTPException
from test_workflow_guards import env, paper  # noqa: F401

from litprism.agents import QAAgent
from litprism.evidence import stored_excerpts
from litprism.knowledge import create_source, get_source, review_source, search_knowledge
from litprism.provenance import availability


def confirmed(kb, text='alphatopic correctedknowledge', dependencies=None):
    s = create_source(kb,kind='knowledge',title='人工知识',content=text,dependencies=dependencies)
    return review_source(kb,s['id'],action='confirm',note='逐项核对',content_hash=s['content_hash'])


def source(kb, pid):
    c = kb.get_chunks(pid)[0]
    return dict(c,chunk_id=c['id'],tag='P1')


def model_reply(model, phrases):
    def run(messages, *, task='', **kwargs):
        model.calls.append((task,messages))
        if task == 'expand':
            return {'terms':['alphatopic']}
        content = messages[-1]['content'].split('【片段】',1)[-1].split('【程序送达记录】')[0]
        blocks = re.findall(r'\[(P\d+)\] [^\n]+\n(.*?)(?=\n\n---|$)',content,re.S)
        tags = [tag for phrase in phrases for tag,text in blocks if phrase in text]
        assert tags, (phrases,content)
        return {'answer':'有来源的回答。'+''.join('['+t+']' for t in dict.fromkeys(tags))}
    model.chat_json = run


def web_fixture(kb, monkeypatch):
    from litprism.web import evidence_routes
    calls, items = [], []
    def discover(_kb,cfg,query):
        calls.append(query)
        item=create_source(kb,kind='web',title='本轮外部来源',content='alphatopic externalactualsummary',url='https://example.org/source')
        items.append(item)
        return {'items':[item]}
    monkeypatch.setattr(evidence_routes,'discover_web',discover)
    return calls,items


def ask(env, question='alphatopic', scope=None, **req):
    _, kb, _, client = env
    cid=kb.add_conversation('混合取证',scope=scope or {})
    response=client.post('/api/chat/'+cid,json={'question':question,**req})
    assert response.status_code==200,response.text
    return cid,response.json()


def test_focus_keeps_selected_full_material_and_adds_confirmed_knowledge_and_other_papers(env):
    cfg,kb,model,_=env
    a=paper(kb,text='alphatopic selectedmaterial')
    paper(kb,title='其他论文',text='alphatopic relatedmaterial')
    confirmed(kb)
    cfg.llm.model_window_tokens=cfg.context.window_tokens=1000000
    model_reply(model,['selectedmaterial','correctedknowledge','relatedmaterial'])
    _,r=ask(env,scope={'paper_ids':[a]})
    assert r['status']=='answered' and len(r['assistant']['source_links'])==3
    assert r['retrieval']['mode']=='full_material'
    assert r['assistant']['scope_snapshot']['strict'] is False


@pytest.mark.parametrize('scope_mode,question',[('strict','alphatopic'),('focus','仅依据这篇论文回答 alphatopic')])
def test_hard_scope_excludes_related_knowledge_and_never_searches_web(env,monkeypatch,scope_mode,question):
    _,kb,model,_=env
    pid=paper(kb,text='alphatopic selectedmaterial')
    confirmed(kb)
    paper(kb,title='其他论文',text='alphatopic relatedmaterial')
    calls,_=web_fixture(kb,monkeypatch)
    model_reply(model,['selectedmaterial'])
    _,r=ask(env,question,scope={'paper_ids':[pid],'mode':scope_mode},web_search=True)
    assert r['status']=='answered' and not calls
    request=next(m for task,m in model.calls if task=='qa')[-1]['content']
    assert 'correctedknowledge' not in request and 'relatedmaterial' not in request
    assert r['assistant']['scope_snapshot']['strict']


def test_negative_only_instruction_does_not_accidentally_enforce_strict_scope(env):
    _,kb,model,_=env
    pid=paper(kb,text='alphatopic selectedmaterial')
    confirmed(kb)
    model_reply(model,['correctedknowledge'])
    _,r=ask(env,'不要只依据这篇论文回答 alphatopic',scope={'paper_ids':[pid]})
    assert r['status']=='answered' and not r['assistant']['scope_snapshot']['strict']


def test_kernel_explicit_paper_scope_remains_strict_for_legacy_callers(env):
    cfg,kb,model,_=env
    pid=paper(kb,text='alphatopic selectedmaterial')
    confirmed(kb)
    model_reply(model,['selectedmaterial'])
    answer=QAAgent(kb,cfg,model).ask('alphatopic',paper_ids=[pid])
    assert all(e['paper_id']==pid for e in answer.excerpts)


def test_web_only_answer_has_request_bound_sources_reopens_without_global_admission(env,monkeypatch):
    cfg,kb,model,client=env
    calls,items=web_fixture(kb,monkeypatch)
    model_reply(model,['externalactualsummary'])
    cid,r=ask(env,web_search=True)
    assert r['status']=='answered' and len(calls)==1
    assert r['assistant']['source_links'][0]['session_only']
    assert get_source(kb,items[0]['id'])['status']=='pending'
    assert not search_knowledge(kb,'alphatopic',10)
    reopened=client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert reopened['status']=='answered' and reopened['source_links'][0]['session_only']
    assert QAAgent(kb,cfg,model).ask('alphatopic').status=='refused'
    grant=kb._con.execute('SELECT * FROM chat_evidence_grants').fetchone()
    assert grant['conversation_id']==cid and grant['query']=='alphatopic'
    assert client.get('/api/evidence/'+r['assistant']['source_links'][0]['ref']).json()['trust_level']=='pending'


def test_request_payload_cannot_inject_external_source_ids(env):
    _,kb,_,_=env
    s=create_source(kb,kind='web',title='待审',content='alphatopic externalactualsummary',url='https://example.org')
    _,r=ask(env,external=[s],web_source_ids=[s['id']])
    assert r['status']=='refused'
    assert not kb._con.execute('SELECT * FROM chat_evidence_grants').fetchall()


def test_pending_web_can_be_human_knowledge_without_globally_confirming_the_web(env,monkeypatch):
    _,kb,model,client=env
    _,items=web_fixture(kb,monkeypatch)
    model_reply(model,['externalactualsummary'])
    _,r=ask(env,web_search=True)
    rid=r['assistant']['qa_review_id'];d=client.get('/api/chat-reviews/'+rid).json()
    response=client.post('/api/chat-reviews/'+rid+'/review',json={
        'action':'confirm','note':'核对本轮网页快照的适用范围','reviewed_text':'alphatopic correctedknowledge [P1]',
        'source_tags':['P1'],'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']})
    assert response.status_code==200,response.text
    sid=response.json()['knowledge_id']
    assert get_source(kb,sid)['citable'] and get_source(kb,items[0]['id'])['status']=='pending'
    assert availability(kb,sid)['refs'][0]['accepted_pending']
    model_reply(model,['correctedknowledge'])
    _,next_answer=ask(env)
    assert next_answer['status']=='answered'
    review_source(kb,items[0]['id'],action='reject',note='原文有误',content_hash=items[0]['content_hash'])
    knowledge=get_source(kb,sid)
    assert knowledge['status']=='manual_confirmed' and not knowledge['citable']
    assert knowledge['effective_status']=='needs_review' and not search_knowledge(kb,'correctedknowledge',10)
    with pytest.raises(ValueError,match='依据失效'):
        review_source(kb,sid,action='confirm',note='无法忽略撤销',content_hash=knowledge['content_hash'])


def test_corrected_knowledge_is_retrieved_in_same_focused_conversation(env):
    _,kb,model,client=env
    pid=paper(kb,text='alphatopic selectedmaterial')
    model_reply(model,['selectedmaterial'])
    cid,r=ask(env,scope={'paper_ids':[pid]})
    rid=r['assistant']['qa_review_id'];d=client.get('/api/chat-reviews/'+rid).json()
    response=client.post('/api/chat-reviews/'+rid+'/review',json={
        'action':'confirm','note':'修订限定条件','reviewed_text':'alphatopic newlycorrected [P1]',
        'source_tags':['P1'],'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']})
    assert response.status_code==200,response.text
    model_reply(model,['newlycorrected'])
    result=client.post('/api/chat/'+cid,json={'question':'alphatopic 继续解释'}).json()
    assert result['status']=='answered'
    assert any(s['kind']=='knowledge' for s in result['assistant']['source_links'])


@pytest.mark.parametrize('change',['trust','delete','text','page'])
def test_upstream_paper_loss_blocks_derived_knowledge_without_erasing_confirmation(env,change):
    _,kb,_,_=env
    pid=paper(kb);original=source(kb,pid)
    s=confirmed(kb,dependencies=[original])
    if change=='trust':kb.update_paper(pid,verify_level='likely')
    elif change=='delete':kb.delete_paper(pid)
    else:
        with kb.tx() as con:con.execute('UPDATE chunks SET '+change+'=? WHERE id=?',
            ('changed' if change=='text' else 99,original['id']))
    current=get_source(kb,s['id'])
    assert current['status']=='manual_confirmed' and current['reviews']==s['reviews']
    assert not current['citable'] and not search_knowledge(kb,'correctedknowledge',10)
    with pytest.raises(ValueError):stored_excerpts(kb,[{'tag':'P1','chunk_id':s['chunks'][0]['id']}])


def test_transitive_dependencies_and_unaffected_knowledge_are_separate(env):
    _,kb,_,_=env
    pid=paper(kb)
    first=confirmed(kb,dependencies=[source(kb,pid)])
    parent=stored_excerpts(kb,[{'tag':'P1','chunk_id':first['chunks'][0]['id']}])
    second=confirmed(kb,text='alphatopic second',dependencies=parent)
    independent=confirmed(kb,text='alphatopic independent')
    kb.update_paper(pid,verify_level='unchecked')
    assert not get_source(kb,first['id'])['citable'] and not get_source(kb,second['id'])['citable']
    assert get_source(kb,independent['id'])['citable']


def test_new_material_marks_historical_basis_and_never_rewrites_knowledge(env):
    _,kb,model,_=env
    pid=paper(kb,text='alphatopic oldmaterial')
    s=confirmed(kb,dependencies=[source(kb,pid)])
    kb.add_chunks(pid,[{'text':'alphatopic newmaterial','page':2}],level='fulltext')
    d=get_source(kb,s['id'])
    assert d['citable'] and d['effective_status']=='historical' and d['content']==s['content']
    model_reply(model,['correctedknowledge'])
    _,r=ask(env,scope={'paper_ids':[pid]})
    assert r['status']=='answered'
    assert '历史版本依据' in next(m for task,m in model.calls if task=='qa')[-1]['content']


def test_search_failure_preserves_local_answer_and_distinct_warning(env,monkeypatch):
    from litprism.web import evidence_routes
    _,kb,model,_=env
    paper(kb,text='alphatopic selectedmaterial')
    def failed(*a):raise HTTPException(502,'搜索暂不可用')
    monkeypatch.setattr(evidence_routes,'discover_web',failed)
    model_reply(model,['selectedmaterial'])
    _,r=ask(env,web_search=True)
    assert r['status']=='answered' and '搜索暂不可用' in r['assistant']['source_warning']
    assert not r['assistant']['candidate_links']


@pytest.mark.parametrize('withdraw',[False,True])
def test_web_response_replay_does_not_repeat_search_or_model_after_publication_failure(env,monkeypatch,withdraw):
    from litprism import qa_reviews
    from test_chat_runs import wait
    _,kb,model,client=env
    searches,items=web_fixture(kb,monkeypatch)
    model_reply(model,['externalactualsummary'])
    cid=kb.add_conversation('可恢复混合取证')
    saved=qa_reviews.save
    def fail(*a,**kw):raise RuntimeError('publication interrupted')
    monkeypatch.setattr(qa_reviews,'save',fail)
    response=client.post('/api/conversations/'+cid+'/chat-runs',json={
        'question':'alphatopic','request_id':'mixed-0001','web_search':True})
    assert response.status_code==200,response.text
    rid=response.json()['run_id'];assert wait(client,rid)['status']=='failed'
    calls=len(model.calls)
    monkeypatch.setattr(qa_reviews,'save',saved)
    if withdraw:
        review_source(kb,items[0]['id'],action='reject',note='来源撤回',content_hash=items[0]['content_hash'])
    assert client.post('/api/chat-runs/'+rid+'/resume',json={}).status_code==200
    assert wait(client,rid)['status']==('stale' if withdraw else 'completed')
    assert len(searches)==1 and len(model.calls)==calls
    assert kb._con.execute('SELECT count(*) FROM chat_evidence_grants').fetchone()[0]==1
    if not withdraw:
        assert client.get('/api/chat-runs/'+rid+'/result').json()['status']=='answered'


def test_grant_cannot_authorize_different_web_snapshot(env):
    from litprism.chat_evidence import grant
    _,kb,_,_=env
    a=create_source(kb,kind='web',title='A',content='alphatopic A',url='https://example.org/a')
    b=create_source(kb,kind='web',title='B',content='alphatopic B',url='https://example.org/b')
    gid=grant(kb,'conversation','user','alphatopic',[a])[0]
    own={'tag':'P1','chunk_id':a['chunks'][0]['id'],'web_grant_id':gid}
    with pytest.raises(ValueError):stored_excerpts(kb,[own])
    assert stored_excerpts(kb,[own],allow_session=True)[0]['session_only']
    with pytest.raises(ValueError):
        stored_excerpts(kb,[{'tag':'P1','chunk_id':b['chunks'][0]['id'],'web_grant_id':gid}],allow_session=True)


def test_legacy_derived_knowledge_without_structured_dependencies_is_explicitly_unreviewed(env):
    _,kb,model,client=env
    pid=paper(kb,text='alphatopic selectedmaterial');model_reply(model,['selectedmaterial'])
    _,r=ask(env,scope={'paper_ids':[pid]});rid=r['assistant']['qa_review_id']
    d=client.get('/api/chat-reviews/'+rid).json()
    sid=client.post('/api/chat-reviews/'+rid+'/review',json={
        'action':'confirm','note':'核对','reviewed_text':'alphatopic correctedknowledge [P1]',
        'source_tags':['P1'],'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']}).json()['knowledge_id']
    with kb.tx() as con:con.execute('DELETE FROM evidence_lineage WHERE source_id=?',(sid,))
    d=get_source(kb,sid)
    assert d['effective_status']=='needs_review' and '旧派生知识' in d['dependency_status']['issues'][0]


def test_dependency_failure_excludes_old_reply_and_its_checkpoint_from_context(env):
    from litprism.context.dialogue import build_dialogue
    cfg,kb,model,_=env
    pid=paper(kb)
    confirmed(kb,dependencies=[source(kb,pid)])
    model_reply(model,['correctedknowledge'])
    cid,r=ask(env)
    reply=kb.get_messages(cid)[-1]
    assert any(m['role']=='assistant' for m in build_dialogue(kb,cfg,cid,None).history)
    with kb.tx() as con:
        refs=json.dumps([reply['ref']])
        con.execute('INSERT INTO compaction_blocks (id,conversation_id,summary,direct_refs,effective_refs,active) VALUES (?,?,?,?,?,1)',
                    ('dependency-block',cid,'OLD_DEPENDENCY_SUMMARY',refs,refs))
        msg=kb.append_message(cid,'user','OLD_DEPENDENCY_SUMMARY',kind='compact-checkpoint')
        con.execute('UPDATE messages SET block_id=? WHERE id=?',('dependency-block',msg['id']))
    kb.update_paper(pid,verify_level='unchecked')
    context=build_dialogue(kb,cfg,cid,None)
    assert not any(m['role']=='assistant' or 'OLD_DEPENDENCY_SUMMARY' in m['content'] for m in context.history)


def test_backup_roundtrip_preserves_web_grants_and_transitive_dependencies(env,monkeypatch,tmp_path):
    from litprism import backup
    from litprism.config import load_config
    from litprism.db import KnowledgeBase
    from litprism.web import server
    from fastapi.testclient import TestClient
    cfg,kb,model,client=env
    _,items=web_fixture(kb,monkeypatch);model_reply(model,['externalactualsummary'])
    cid,r=ask(env,web_search=True)
    rid=r['assistant']['qa_review_id'];d=client.get('/api/chat-reviews/'+rid).json()
    sid=client.post('/api/chat-reviews/'+rid+'/review',json={
        'action':'confirm','note':'核对快照','reviewed_text':'alphatopic correctedknowledge [P1]',
        'source_tags':['P1'],'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']}).json()['knowledge_id']
    archive=tmp_path/'mixed.zip';backup.create_backup(kb,cfg,archive)
    restored=backup.restore_backup(archive,tmp_path.parent/(tmp_path.name+'-restored'),current_root=cfg.data_path)
    other=KnowledgeBase(load_config(restored['config_path']))
    try:
        monkeypatch.setattr(server,'KnowledgeBase',lambda _:other)
        restored_client=TestClient(server.create_app(other.cfg))
        assert other._con.execute('PRAGMA user_version').fetchone()[0]==15
        assert other._con.execute('SELECT count(*) FROM chat_evidence_grants').fetchone()[0]==1
        assert get_source(other,sid)['dependency_status']['refs'][0]['accepted_pending']
        assert restored_client.get('/api/chat-reviews/'+rid).json()['sources'][0]['citable']
        review_source(other,items[0]['id'],action='reject',note='撤销恢复副本的上游',content_hash=items[0]['content_hash'])
        assert not get_source(other,sid)['citable'] and get_source(kb,sid)['citable']
    finally:other.close()


@pytest.mark.parametrize('durable',[False,True])
def test_empty_selection_retrieves_library_knowledge_and_enabled_web(env,monkeypatch,durable):
    _,kb,model,client=env
    paper(kb,text='alphatopic selectedmaterial');confirmed(kb)
    calls,_=web_fixture(kb,monkeypatch)
    model_reply(model,['selectedmaterial','correctedknowledge','externalactualsummary'])
    cid=kb.add_conversation('空选择不限定',scope={'paper_ids':[]})
    if durable:
        from test_chat_runs import wait
        request=client.post('/api/conversations/'+cid+'/chat-runs',json={
            'request_id':'unrestricted-empty','question':'alphatopic','web_search':True})
        rid=request.json()['run_id'];assert wait(client,rid)['status']=='completed'
        r=client.get('/api/chat-runs/'+rid+'/result').json()
    else:r=client.post('/api/chat/'+cid,json={'question':'alphatopic','web_search':True}).json()
    assert r['status']=='answered' and len(calls)==1
    assert {s['kind'] for s in r['assistant']['source_links']}=={'paper','knowledge','web'}
    assert r['assistant']['scope_snapshot']['paper_ids'] is None
    assert not r['assistant']['scope_snapshot']['strict']


def test_unrestricted_selection_still_honors_network_switch_and_explicit_local_mode(env,monkeypatch):
    _,kb,model,_=env
    paper(kb,text='alphatopic selectedmaterial');calls,_=web_fixture(kb,monkeypatch)
    model_reply(model,['selectedmaterial'])
    _,local=ask(env,scope={'paper_ids':[]},web_search=False)
    _,strict=ask(env,scope={'paper_ids':[],'mode':'strict'},web_search=True)
    assert local['status']==strict['status']=='answered' and not calls
    assert strict['assistant']['scope_snapshot']['paper_ids'] is None
    assert strict['assistant']['scope_snapshot']['strict']


@pytest.mark.parametrize('question,strict',[
    ('BERT 是只用左侧上下文还是同时使用左右上下文？',False),
    ('模型 only uses attention 吗？',False),
    ('only use attention mechanisms?',False),
    ('请仅依据指定论文回答。',True),
    ('only use the selected papers',True),
    ('不要只依据这篇论文，请补充网页。',False),
])
def test_content_direction_and_source_instructions_are_distinct(question,strict):
    from litprism.scope import chat_strict
    assert chat_strict({'mode':'focus'},question)==strict


def test_empty_chat_selection_does_not_expand_batch_operations_or_missing_group(env):
    from litprism.scope import chat_paper_scope,paper_scope
    _,kb,_,client=env
    pid=paper(kb)
    assert chat_paper_scope(kb,{'paper_ids':[]}) is None
    assert paper_scope(kb,{'paper_ids':[]})==[]
    assert client.post('/api/library/batch',json={'paper_ids':[],'operation':'delete'}).status_code==400
    assert kb.get_paper(pid)
    with pytest.raises(ValueError):chat_paper_scope(kb,{'paper_ids':[],'collection_id':'missing'})


def test_scope_mode_is_persisted_and_invalid_update_is_atomic(env):
    _,kb,_,client=env
    pid=paper(kb)
    cid=client.post('/api/conversations',json={'scope':{'paper_ids':[pid],'mode':'focus'}}).json()['id']
    changed=client.patch('/api/conversations/'+cid,json={'scope':{'paper_ids':[pid],'mode':'strict'}})
    assert changed.status_code==200 and changed.json()['scope']['mode']=='strict'
    before=kb.resolve_conversation(cid)
    bad=client.patch('/api/conversations/'+cid,json={'title':'must not persist','scope':{'mode':'invalid'}})
    assert bad.status_code==400
    assert kb.resolve_conversation(cid)==before


@pytest.mark.parametrize('change',['chunk','source','url'])
def test_mutated_web_snapshot_cannot_reuse_a_grant(env,change):
    from litprism.chat_evidence import grant
    _,kb,_,_=env
    s=create_source(kb,kind='web',title='A',content='alphatopic A',url='https://example.org/a')
    gid=grant(kb,'conversation','user','alphatopic',[s])[0]
    with kb.tx() as con:
        if change=='chunk':con.execute('UPDATE evidence_chunks SET text=? WHERE source_id=?',('changed',s['id']))
        else:con.execute('UPDATE evidence_sources SET '+('url' if change=='url' else 'content')+'=? WHERE id=?',('changed',s['id']))
    with pytest.raises(ValueError):
        stored_excerpts(kb,[{'tag':'P1','chunk_id':s['chunks'][0]['id'],'web_grant_id':gid}],allow_session=True)


def test_reviewed_knowledge_indexes_only_human_text_not_unreviewed_provenance_or_note(env,monkeypatch):
    from litprism.web import evidence_routes
    _,kb,model,client=env
    upstream=create_source(kb,kind='web',title='网页',content='alphatopic externalactualsummary unreviewedprovenanceonly',url='https://example.org/page')
    monkeypatch.setattr(evidence_routes,'discover_web',lambda *a:{'items':[upstream]})
    model_reply(model,['externalactualsummary'])
    _,r=ask(env,web_search=True)
    rid=r['assistant']['qa_review_id'];d=client.get('/api/chat-reviews/'+rid).json()
    response=client.post('/api/chat-reviews/'+rid+'/review',json={
        'action':'confirm','note':'auditnoteonly','reviewed_text':'alphatopic curatedknowledgeonly [P1]',
        'source_tags':['P1'],'snapshot_hash':d['snapshot_hash'],'review_token':d['review_token']})
    assert response.status_code==200,response.text
    s=get_source(kb,response.json()['knowledge_id'])
    assert 'unreviewedprovenanceonly' in s['content'] and 'auditnoteonly' in s['content']
    assert ''.join(c['text'] for c in s['chunks'])=='alphatopic curatedknowledgeonly [P1]'
    assert search_knowledge(kb,'curatedknowledgeonly',10)
    assert not search_knowledge(kb,'unreviewedprovenanceonly',10)
    assert not search_knowledge(kb,'auditnoteonly',10)
