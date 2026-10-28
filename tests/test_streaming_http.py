"""Real localhost SSE sockets across provider, durable journal, API and reconnect."""
from contextlib import contextmanager
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from litprism.config import Config, LLMConfig
from litprism.db import KnowledgeBase
from litprism.llm import OpenAICompatClient
from litprism.sidecar import harden
from litprism.web import server


@contextmanager
def serve(app):
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    backend=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    worker=threading.Thread(target=backend.run,kwargs={'sockets':[sock]},daemon=True);worker.start()
    try:
        for _ in range(200):
            if backend.started:break
            time.sleep(.01)
        assert backend.started
        yield 'http://127.0.0.1:'+str(port)
    finally:
        backend.should_exit=True;worker.join(5);sock.close()


@pytest.fixture
def wire(tmp_path,monkeypatch):
    release=threading.Event();calls=[]
    mode={'value':'normal'}
    provider=FastAPI()
    def packet(value):return 'data: '+(value if isinstance(value,str) else json.dumps(value))+'\n\n'
    @provider.post('/v1/chat/completions')
    def completion(req:dict):
        calls.append(req)
        if not req.get('stream'):
            return JSONResponse({'choices':[{'message':{'content':'{"terms":["streamingfixture"]}'}}]})
        async def generate():
            yield packet({'choices':[{'index':0,'delta':{'reasoning_content':'服务返回的思考'}}]})
            yield packet({'choices':[{'index':0,'delta':{'content':'{"answer":"streamingfixture 第一段 '}}]})
            while not release.is_set():
                yield ': ping\n\n'
                await asyncio.sleep(.03)
            if mode['value']=='disconnect':return
            tag='P99' if mode['value']=='bad_citation' else 'P1'
            yield packet({'choices':[{'index':0,'delta':{'content':f'已完成 [{tag}]"}}'},
                                      'finish_reason':'length' if mode['value']=='length' else 'stop'}]})
            yield packet({'choices':[],'usage':{'prompt_tokens':21,'completion_tokens':9}})
            yield packet('[DONE]')
        return StreamingResponse(generate(),media_type='text/event-stream')
    cfg=Config();cfg.app.data_dir=str(tmp_path)
    kb=KnowledgeBase(cfg)
    pid=kb.add_paper(title='streamingfixture paper',verify_level='verified')
    kb.add_chunks(pid,[{'text':'streamingfixture known evidence. '*30,'page':1}])
    cid=kb.add_conversation('Streaming acceptance',scope={'paper_ids':[pid],'mode':'strict'})
    with serve(provider) as provider_url:
        model=OpenAICompatClient(LLMConfig(provider='openai',model='wire-test',base_url=provider_url+'/v1',timeout=10))
        monkeypatch.setattr(server,'KnowledgeBase',lambda _:kb)
        monkeypatch.setattr(server,'build_llm',lambda _:model)
        app=server.create_app(cfg);harden(app,'stream-test')
        with serve(app) as url, httpx.Client(base_url=url,headers={'X-LitPrism-Token':'stream-test'},trust_env=False,timeout=8) as api:
            try:yield api,kb,cid,release,calls,mode
            finally:
                release.set()
                app.state.tasks.pool.shutdown(wait=True,cancel_futures=True)
    kb.close()


def start(api,cid):
    response=api.post('/api/conversations/'+cid+'/chat-runs',json={
        'question':'streamingfixture 的原文是什么？','web_search':False,'request_id':'wire-request'})
    assert response.status_code==200,response.text
    return response.json()['run_id']


def draft_events(response):
    for line in response.iter_lines():
        if line.startswith('data: '):yield json.loads(line[6:])


def finished(api,rid):
    for _ in range(300):
        record=api.get('/api/chat-runs/'+rid).json()
        if not record['active'] and record['status'] not in ('queued','running'):return record
        time.sleep(.02)
    pytest.fail('streaming task did not finish')


def test_preview_arrives_before_complete_and_reconnect_never_resends(wire):
    api,kb,cid,release,calls,_=wire
    rid=start(api,cid)
    assert api.get('/api/chat-runs/'+rid+'/events',headers={'X-LitPrism-Token':''}).status_code==401
    with api.stream('GET','/api/chat-runs/'+rid+'/events') as response:
        assert response.headers['cache-control']=='no-store'
        for event in draft_events(response):
            if '第一段' in event.get('text','')+event.get('text_delta',''):break
        assert not release.is_set()
        assert not any(m['role']=='assistant' for m in kb.get_messages(cid))
        assert api.get('/api/chat-runs/'+rid+'/result').status_code==409
    with api.stream('GET','/api/chat-runs/'+rid+'/events') as response:
        snapshot=next(draft_events(response))
        assert snapshot['reset'] and '第一段' in snapshot['text'] and snapshot['reasoning']=='服务返回的思考'
        assert sum(bool(c.get('stream')) for c in calls)==1
        release.set()
    assert finished(api,rid)['status']=='completed'
    result=api.get('/api/chat-runs/'+rid+'/result').json()
    assert '已完成' in result['assistant']['content'] and sum(bool(c.get('stream')) for c in calls)==1
    assert len([m for m in kb.get_messages(cid) if m['role']=='assistant'])==1


@pytest.mark.parametrize('mode', ['length','disconnect','bad_citation'])
def test_bad_end_or_citation_cannot_promote_preview_to_trusted_answer(wire,mode):
    api,kb,cid,release,calls,setting=wire
    setting['value']=mode;rid=start(api,cid);release.set()
    record=finished(api,rid)
    assert sum(bool(c.get('stream')) for c in calls)==1
    if mode=='bad_citation':
        assert record['status']=='completed'
        assert api.get('/api/chat-runs/'+rid+'/result').json()['assistant']['status']!='answered'
        with api.stream('GET','/api/chat-runs/'+rid+'/events') as response:
            snapshot=next(draft_events(response))
            assert snapshot['terminal'] and snapshot.get('text','')==''
    else:
        assert record['status']=='failed' and record['retry_required']
        assert not any(m['role']=='assistant' for m in kb.get_messages(cid))
        assert api.get('/api/chat-runs/'+rid+'/result').status_code==409
        if mode=='length':assert record['usage']['output']==9


def test_stream_cancel_keeps_unknown_attempt_and_never_auto_retries(wire):
    api,kb,cid,release,calls,_=wire;rid=start(api,cid)
    with api.stream('GET','/api/chat-runs/'+rid+'/events') as response:
        for event in draft_events(response):
            if '第一段' in event.get('text','')+event.get('text_delta',''):
                assert api.post('/api/tasks/'+event['task_id']+'/cancel').status_code==200
                break
    record=finished(api,rid)
    assert record['status']=='cancelled' and sum(bool(c.get('stream')) for c in calls)==1
    assert api.post('/api/chat-runs/'+rid+'/resume',json={}).status_code==409
    assert not any(m['role']=='assistant' for m in kb.get_messages(cid))
