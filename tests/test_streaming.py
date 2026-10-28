import json
from types import SimpleNamespace

import pytest

from litprism.config import LLMConfig
from litprism.llm import AnthropicClient, LLMError, OpenAICompatClient, OutputLimitError
from litprism.streaming import AnswerPreview, DraftStreams, provider_stream


class Response:
    status_code = 200
    def __init__(self, packets):
        self.packets, self.closed = packets, False
    def iter_lines(self, **kwargs):
        for packet in self.packets:
            payload = packet if isinstance(packet, str) else json.dumps(packet, ensure_ascii=False)
            yield ('data: ' + payload).encode('utf-8')
            yield b''
    def close(self):
        self.closed = True


@pytest.mark.parametrize('size', [1, 2, 7, 300])
def test_partial_json_only_previews_answer_text_including_escaped_unicode(size):
    value = {'question_items': [{'text': 'HIDDEN_QUESTION'}], 'answer_units': [
        {'text': '  正文 "引句" 😀 [P1]\n第二行  ', 'quotes': [{'text': 'HIDDEN_QUOTE'}]},
        {'text': '后续陈述', 'subject': 'HIDDEN_SUBJECT'}]}
    encoded = json.dumps(value, ensure_ascii=True)
    parser = AnswerPreview()
    seen = []
    for start in range(0, len(encoded), size):
        parser.feed(encoded[start:start+size]); seen.append(parser.text())
    assert seen[-1] == '正文 "引句" 😀 [P1]\n第二行\n\n后续陈述'
    assert not any('HIDDEN_' in text for text in seen)
    if size == 1:
        assert any(text == '正文' for text in seen[:-1])


def test_legacy_preview_and_duplicate_keys_never_leak_evidence():
    parser = AnswerPreview(); parser.feed('```json\n{"answer":"可见", "quotes":[{"text":"不可见"}]}\n```')
    assert parser.text() == '可见'
    parser = AnswerPreview(); parser.feed('{"answer":"先前", "answer":"后续"}')
    assert parser.text() == ''


def test_stream_reconnect_snapshot_resets_attempt_without_accumulating_old_draft():
    streams = DraftStreams(); streams.begin('a')
    streams.feed('a','content','{"answer":"第一个')
    streams.feed('a','reasoning','actual reasoning')
    first = streams.snapshot('a')
    assert first['text'] == '第一个'
    streams.begin('a'); streams.feed('a','content','{"answer":"新尝试"}')
    second = streams.snapshot('a')
    assert second['revision'] > first['revision'] and second['text'] == '新尝试' and not second['reasoning']


def openai_packets(text, stop='stop', done=True):
    parts = [{'choices':[{'index':0,'delta':{'reasoning_content':'服务思考'}}]}]
    parts += [{'choices':[{'index':0,'delta':{'content':text[:8]}}]},
              {'choices':[{'index':0,'delta':{'content':text[8:]},'finish_reason':stop}]},
              {'choices':[], 'usage':{'prompt_tokens':11,'completion_tokens':7}}]
    return parts + (['[DONE]'] if done else [])


def test_openai_stream_forwards_before_completion_and_preserves_usage(monkeypatch):
    response = Response(openai_packets('{"answer":"逐步正文"}'))
    calls, deltas = [], []
    def post(url, **kwargs):
        calls.append(kwargs)
        return response
    monkeypatch.setattr('litprism.llm.requests.post', post)
    client = OpenAICompatClient(LLMConfig(provider='openai',model='stream-test'))
    value = client.chat_json_stream([], task='qa', on_delta=lambda kind,text:deltas.append((kind,text)))
    assert value['answer'] == '逐步正文' and len(deltas) == 3
    assert calls[0]['stream'] is True and calls[0]['json']['stream'] is True
    assert calls[0]['json']['stream_options'] == {'include_usage':True}
    assert client.last_reasoning == '服务思考' and client.last_usage['completion_tokens'] == 7 and response.closed


@pytest.mark.parametrize('stop,done,error', [('stop',False,LLMError),('length',True,OutputLimitError)])
def test_early_end_or_truncation_never_succeeds(stop, done, error):
    client = SimpleNamespace(last_usage=None,last_reasoning='')
    with pytest.raises(error):
        provider_stream(Response(openai_packets('{"answer":"incomplete"}',stop,done)), 'openai', client, lambda *a:None)
    assert client.last_usage['completion_tokens'] == 7


def test_anthropic_stream_uses_cumulative_usage_and_thinking(monkeypatch):
    packets = [
        {'type':'message_start','message':{'usage':{'input_tokens':10,'output_tokens':1}}},
        {'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'真实思考'}},
        {'type':'content_block_delta','delta':{'type':'text_delta','text':'{"answer":"实时正文"}'}},
        {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':9}},
        {'type':'message_stop'}]
    response = Response(packets)
    monkeypatch.setattr('litprism.llm.requests.post',lambda *a,**k:response)
    client = AnthropicClient(LLMConfig(provider='anthropic',model='stream-test'))
    assert client.chat_json_stream([],task='qa',on_delta=lambda *a:None)['answer'] == '实时正文'
    assert client.last_usage == {'input_tokens':10,'output_tokens':9} and client.last_reasoning == '真实思考'
