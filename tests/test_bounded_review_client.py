"""Bounded review transport is strict and does not silently spend retries."""
import pytest

from litprism.config import LLMConfig
from litprism.llm import AnthropicClient, LLMError, OpenAICompatClient


@pytest.mark.parametrize('client_type',[OpenAICompatClient,AnthropicClient])
def test_review_caps_output_and_timeout_without_overriding_temperature(monkeypatch,client_type):
    calls=[]
    class Response:
        status_code=200
        def json(self):
            return {'choices':[{'finish_reason':'stop','message':{'content':'{"reviews":[]}'}}],
                    'content':[{'type':'text','text':'{"reviews":[]}'}],'usage':{}}
    def post(*a,**kw):
        calls.append(kw)
        return Response()
    monkeypatch.setattr('litprism.llm.requests.post',post)
    client=client_type(LLMConfig(max_tokens=16000,timeout=180,temperature=.2))
    assert client.chat_json([{'role':'user','content':'test'}],task='semantic_review')=={'reviews':[]}
    assert calls[0]['json']['max_tokens']==4096 and calls[0]['timeout']==(10,60)
    assert calls[0]['json']['temperature']==.2
    assert client.cfg.max_tokens==16000 and client.cfg.timeout==180


@pytest.mark.parametrize('client_type',[OpenAICompatClient,AnthropicClient])
def test_review_timeout_does_not_retry(monkeypatch,client_type):
    calls=[]
    def post(*a,**kw):
        calls.append(kw)
        raise TimeoutError('controlled')
    monkeypatch.setattr('litprism.llm.requests.post',post)
    with pytest.raises(LLMError):
        client_type(LLMConfig()).chat_json([{'role':'user','content':'test'}],task='semantic_review')
    assert len(calls)==1


def test_review_rejects_loose_json_repairs_but_other_tasks_remain_compatible(monkeypatch):
    client=OpenAICompatClient(LLMConfig())
    monkeypatch.setattr(client,'chat',lambda *a,**kw:'```json\n{"reviews":[],}\n```')
    with pytest.raises(LLMError,match='严格 JSON'):
        client.chat_json([],task='semantic_review')
    assert client.chat_json([],task='extract')=={'reviews':[]}


@pytest.mark.parametrize('client_type',[OpenAICompatClient,AnthropicClient])
def test_connect_timeout_has_at_most_one_safe_retry(monkeypatch,client_type):
    import requests
    calls=[]
    def post(*a,**kw):
        calls.append(kw)
        raise requests.exceptions.ConnectTimeout('controlled')
    monkeypatch.setattr('litprism.llm.requests.post',post)
    monkeypatch.setattr('litprism.llm.time.sleep',lambda _:None)
    client=client_type(LLMConfig())
    with pytest.raises(LLMError) as error:
        client.chat_json([{'role':'user','content':'test'}],task='semantic_review')
    assert len(calls)==2 and client.last_request_settings['attempts']==2
    assert isinstance(error.value.__cause__,requests.exceptions.ConnectTimeout)


def test_http_failure_keeps_safe_status_without_blind_retry(monkeypatch):
    class Response:
        status_code=503
        text='service unavailable'
    calls=[]
    def post(*a,**kw):
        calls.append(kw)
        return Response()
    monkeypatch.setattr('litprism.llm.requests.post',post)
    with pytest.raises(LLMError) as error:
        OpenAICompatClient(LLMConfig()).chat_json([],task='semantic_review')
    assert len(calls)==1 and error.value.http_status==503


@pytest.mark.parametrize('configured,expected',[(16000,8192),(4096,4096)])
def test_cross_budget_uses_configured_reserve_without_silently_increasing_cap(monkeypatch,configured,expected):
    class Response:
        status_code=200
        def json(self):
            return {'choices':[{'finish_reason':'stop','message':{'content':'{"differences":[]}'}}]}
    calls=[]
    def post(*a,**kw):
        calls.append(kw)
        return Response()
    monkeypatch.setattr('litprism.llm.requests.post',post)
    OpenAICompatClient(LLMConfig(max_tokens=configured)).chat_json([],task='semantic_cross_review')
    assert calls[0]['json']['max_tokens']==expected
