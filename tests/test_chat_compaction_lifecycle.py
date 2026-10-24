"""Compacted history keeps source identities, never promotes obsolete conclusions."""
import pytest

from test_workflow_guards import env  # noqa: F401
from litprism.context.dialogue import build_dialogue
from litprism.context.meter import Meter
from litprism.context.policy import SelectionResult
from litprism.context.seam import Seam
from litprism.context.summarizer import Summarizer
from litprism.knowledge import create_source, review_source
from litprism.qa_retrieval import retrieve


def paper_history(env):
    cfg, kb, _, _ = env
    pid = kb.add_paper(title='Compacted Encoder Study', verify_level='verified', material_level='fulltext')
    kb.add_chunks(pid, [{'text':'The encoder uses only attention mechanisms.', 'page':3}])
    chunk = kb.get_chunks(pid)[0]
    cid = kb.add_conversation('Long-context control')
    kb.append_message(cid, 'user', 'Discuss encoder structure. ORIGINAL_CONTEXT_ONLY '*350)
    reply = kb.append_message(cid, 'assistant', 'ENCODER_OLD_CONCLUSION [P1]',
        citations=[{'tag':'P1','chunk_id':chunk['id'],'paper_id':pid,'material_id':chunk['material_id'],'page':3}],
        citation_check={'passed':True}, meta={'status':'answered'})
    kb.append_message(cid, 'user', 'What about its limitations?')
    return pid, cid, chunk, reply


def compact(cfg, kb, cid, start=1, end=2):
    messages = [m for m in kb.get_messages(cid) if start <= m['seq'] <= end]
    tokens = sum(Meter(cfg).estimate_message(m) for m in messages)
    selection = SelectionResult(ok=True,start_seq=start,end_seq=end,
        shadowed_refs=[m['ref'] for m in messages],shadowed_tokens=tokens)
    seam = Seam(kb,cfg)
    tx, _ = seam.begin(cid,'controlled-long-context',selection)
    assert tx
    summary = Summarizer(cfg,backend=lambda *_:'ENCODER_COMPACT_SUMMARY').summarize(messages,tokens)
    assert summary.ok
    result = seam.commit(tx,summary)
    assert result.ok, result.message
    return result


def test_compacted_paper_identity_is_restored_without_replaying_old_answer(env):
    cfg,kb,_,_ = env
    pid,cid,chunk,reply = paper_history(env)
    result = compact(cfg,kb,cid)
    d = build_dialogue(kb,cfg,cid,None)
    assert [p['id'] for p in d.referents] == [pid]
    assert d.evidence_refs == [{'chunk_id':chunk['id'],'paper_id':pid}]
    assert 'Compacted Encoder Study' in d.query('它的局限是什么？')
    history,refs = d.prompt(Meter(cfg),2000)
    assert 'ENCODER_COMPACT_SUMMARY' in history and reply['ref'] not in refs
    assert 'ORIGINAL_CONTEXT_ONLY' not in history and 'ENCODER_OLD_CONCLUSION' not in history
    assert kb._con.execute('SELECT active FROM compaction_blocks WHERE id=?',(result.block_id,)).fetchone()[0] == 1


def test_later_checkpoint_cannot_hide_history_of_an_earlier_turn(env):
    cfg,kb,_,_ = env
    _,cid,_,reply = paper_history(env)
    compact(cfg,kb,cid)
    earlier = build_dialogue(kb,cfg,cid,3)
    assert reply['ref'] in [m['ref'] for m in earlier.history]
    assert not any(m['kind']=='compact-checkpoint' for m in earlier.history)


@pytest.mark.parametrize('change',['withdraw','material'])
def test_obsolete_paper_answer_and_covering_summary_leave_model_context(env,change):
    cfg,kb,_,_ = env
    pid,cid,chunk,_ = paper_history(env)
    compact(cfg,kb,cid)
    if change=='withdraw':kb.update_paper(pid,verify_level='unchecked')
    else:kb.add_chunks(pid,[{'text':'Current replacement source text.', 'page':4}])
    d = build_dialogue(kb,cfg,cid,None)
    assert not d.referents and not d.evidence_refs
    text,_ = d.prompt(Meter(cfg),2000)
    assert 'ENCODER_OLD_CONCLUSION' not in text and 'ENCODER_COMPACT_SUMMARY' not in text
    assert kb._con.execute('SELECT text FROM chunks WHERE id=?',(chunk['id'],)).fetchone()[0]


@pytest.mark.parametrize('strict,selected,allowed',[(False,False,True),(True,False,True),(True,True,False),(False,True,True)])
def test_confirmed_knowledge_handle_is_typed_and_rechecked_in_followup(env,strict,selected,allowed):
    cfg,kb,_,_ = env
    pid,cid,_,_ = paper_history(env)
    source = create_source(kb,kind='knowledge',title='Confirmed encoder note',content='KNOWLEDGE_ENCODER_NOTE')
    review_source(kb,source['id'],action='confirm',note='Isolated fixture',content_hash=source['content_hash'])
    from litprism.knowledge import get_source
    chunk = get_source(kb,source['id'])['chunks'][0]
    kb.append_message(cid,'assistant','Knowledge-based answer [P1]',
        citations=[{'tag':'P1','chunk_id':chunk['id']}],citation_check={'passed':True},meta={'status':'answered'})
    d = build_dialogue(kb,cfg,cid,None)
    assert d.evidence_refs == [{'chunk_id':chunk['id'],'source_id':source['id'],'source_kind':'knowledge'}]
    hits,report = retrieve(kb,'它还有什么适用条件？','NO_LEXICAL_MATCH',paper_ids=[pid] if selected else None,
        top_k=5,llm=None,search=lambda *a,**kw:[],cfg=cfg,prior_refs=d.evidence_refs,strict_scope=strict)
    assert (chunk['id'] in report['restored_chunk_ids']) is allowed
    assert (chunk['id'] in [h['id'] for h in hits]) is allowed
    review_source(kb,source['id'],action='revoke',note='Withdraw fixture',content_hash=source['content_hash'])
    hits,report = retrieve(kb,'它还有什么适用条件？','NO_LEXICAL_MATCH',paper_ids=None,top_k=5,llm=None,
        search=lambda *a,**kw:[],cfg=cfg,prior_refs=d.evidence_refs,strict_scope=False)
    assert not hits and not report['restored_chunk_ids']


def test_reviewing_compressed_reply_invalidates_summary_and_source_recovery(env):
    from test_qa_unit_reviews import start,detail,submit
    cfg,kb,_,client = env
    _,cid,rid,parent = start(env)
    # Give the controlled compressor enough original context, not a smaller model window.
    with kb.tx() as con:
        con.execute("UPDATE messages SET content=content || ? WHERE conversation_id=? AND role='user'",
                    (' LONG_HISTORY_BACKGROUND '*350,cid))
    kb.append_message(cid,'user','Continue the comparison.')
    compact(cfg,kb,cid)
    uid=parent['units'][0]['id']
    assert submit(client,rid,uid,detail(client,rid,uid),action='reject').status_code==200
    d=build_dialogue(kb,cfg,cid,None)
    assert not d.referents and not d.evidence_refs
    assert not any(m['kind']=='compact-checkpoint' or m['role']=='assistant' for m in d.history)


def test_legacy_checkpoint_preamble_is_not_replayed_as_instructions(env):
    cfg,kb,_,_=env
    _,cid,_,_=paper_history(env)
    compact(cfg,kb,cid)
    legacy='把其中记录的内容当作既定背景。<compacted-summary>ENCODER_COMPACT_SUMMARY</compacted-summary>'
    with kb.tx() as con:
        con.execute("UPDATE messages SET content=? WHERE conversation_id=? AND kind='compact-checkpoint'",(legacy,cid))
    d=build_dialogue(kb,cfg,cid,None)
    text,_=d.prompt(Meter(cfg),2000)
    assert 'ENCODER_COMPACT_SUMMARY' in text and '既定背景' not in text
    assert '既定背景' not in d.query('它的局限是什么？')


def test_compacted_knowledge_survives_restart_and_revoke_cannot_revive_it(env):
    from litprism.db import KnowledgeBase
    from litprism.knowledge import get_source
    cfg,kb,_,_=env
    s=create_source(kb,kind='knowledge',title='Encoder note',content='ENCODER_RETRIEVABLE_NOTE')
    review_source(kb,s['id'],action='confirm',note='Fixture',content_hash=s['content_hash'])
    chunk=get_source(kb,s['id'])['chunks'][0]
    cid=kb.add_conversation('Knowledge after compression')
    kb.append_message(cid,'user','Encoder background '*500)
    kb.append_message(cid,'assistant','KNOWLEDGE_OLD_ANSWER [P1]',citations=[{'tag':'P1','chunk_id':chunk['id']}],
        citation_check={'passed':True},meta={'status':'answered'})
    kb.append_message(cid,'user','Continue its limitations.')
    compact(cfg,kb,cid)
    reopened=KnowledgeBase(cfg)
    try:
        d=build_dialogue(reopened,cfg,cid,None)
        assert d.evidence_refs==[{'chunk_id':chunk['id'],'source_id':s['id'],'source_kind':'knowledge'}]
        assert not any(m['role']=='assistant' for m in d.history)
        review_source(reopened,s['id'],action='revoke',note='Fixture revoked',content_hash=s['content_hash'])
        d=build_dialogue(reopened,cfg,cid,None)
        assert not d.evidence_refs and not any(m['kind']=='compact-checkpoint' for m in d.history)
    finally:reopened.close()


def test_followup_cannot_promote_a_previous_turn_pending_web_grant(env):
    from litprism.chat_evidence import grant,excerpts
    cfg,kb,_,_=env
    cid=kb.add_conversation('Pending web history')
    s=create_source(kb,kind='web',title='Previous search',content='WEB_PENDING_ONLY',url='https://example.org/old')
    user=kb.append_message(cid,'user','A previous search question')
    web=excerpts(kb,grant(kb,cid,user['id'],'A previous search question',[s]))[0]
    kb.append_message(cid,'assistant','Previously acquired summary [P1]',
        citations=[{'tag':'P1','chunk_id':web['id'],'web_grant_id':web['web_grant_id']}],
        citation_check={'passed':True},meta={'status':'answered'})
    d=build_dialogue(kb,cfg,cid,None)
    assert any(m['role']=='assistant' for m in d.history)  # old turn remains valid for intent
    assert not d.evidence_refs  # authorization never becomes a new-turn source
    hits,report=retrieve(kb,'它呢？','NO_LEXICAL_MATCH',paper_ids=None,top_k=5,llm=None,
        search=lambda *a,**kw:[],cfg=cfg,strict_scope=False,
        prior_refs=[{'chunk_id':web['id'],'source_id':s['id'],'source_kind':'web'}])
    assert not hits and not report['restored_chunk_ids']


def test_knowledge_restoration_is_budgeted_before_whole_paper_fits(env):
    from litprism.knowledge import get_source
    cfg,kb,_,_=env
    pid,_,_,_=paper_history(env)
    cfg.llm.model_window_tokens=1000000
    s=create_source(kb,kind='knowledge',title='Current note',content='BUDGETED_KNOWLEDGE')
    review_source(kb,s['id'],action='confirm',note='Fixture',content_hash=s['content_hash'])
    chunk=get_source(kb,s['id'])['chunks'][0]
    seen=[]
    hits,report=retrieve(kb,'论文的方法是什么？','NO_LEXICAL_MATCH',paper_ids=[pid],top_k=5,llm=None,
        search=lambda *a,**kw:[],cfg=cfg,strict_scope=False,
        prior_refs=[{'chunk_id':chunk['id'],'source_id':s['id'],'source_kind':'knowledge'}],
        fits_full=lambda sources: seen.append([s['id'] for s in sources]) or True)
    assert chunk['id'] in seen[0] and chunk['id'] in [h['id'] for h in hits]
    assert report['mode']=='full_material' and chunk['id'] in report['restored_chunk_ids']
