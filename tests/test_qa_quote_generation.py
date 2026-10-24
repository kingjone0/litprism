"""Generation inputs expose literal per-source boundaries; guards stay strict."""
import re

import pytest

from test_workflow_guards import env  # noqa: F401
from litprism.agents import QAAgent
from litprism.prompts import QA_QUOTE_CHECK
from litprism.qa_retrieval import material_text


def test_per_source_body_boundaries_preserve_all_original_characters_and_types():
    sources = [
        {'tag':'P1','title':'Paper','source_kind':'paper','page':2,'text':'First line.\nSecond line.  ﬁxed features.'},
        {'tag':'P2','title':'Human note','source_kind':'knowledge','text':'人工正文含旧编号[P9]，不应改写。'},
        {'tag':'P3','title':'Search','source_kind':'web','web_grant_id':'grant','trust_level':'pending',
         'url':'https://example.org/','text':'Retrieved summary, not a full article.'},
    ]
    before = [dict(s) for s in sources]
    rendered = material_text(sources)
    bodies = re.findall(r'【来源正文 (P\d+) 开始】\n(.*?)\n【来源正文 \1 结束】',rendered,re.S)
    assert bodies == [(s['tag'],s['text']) for s in sources]
    assert sources == before
    assert '本轮搜索摘要 · 未人工审核' in rendered and '人工确认的知识条目' in rendered


@pytest.mark.parametrize('anchors',[False,True])
def test_real_qa_generation_preserves_full_prefix_budget_and_protocol(env,anchors):
    cfg,kb,model,_ = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    cfg.llm.model_max_output_tokens = 384000
    cfg.qa.evidence_anchor_trial = anchors
    pid = kb.add_paper(title='Generation contract',verify_level='verified',material_level='fulltext')
    texts = ['Queries are projected separately.\nParallel attention produces values.',
             'These outputs are concatenated and projected again.']
    kb.add_chunks(pid,[{'text':text,'page':i} for i,text in enumerate(texts,1)])
    def respond(messages,*,task='',**kw):
        assert task=='qa'
        model.calls.append((task,messages))
        return {'answer_units':[{'text':'Outputs are concatenated.[P2]','subject':'Outputs','basis':'direct',
            'sources':['P2'],'quotes':[{'source':'P2','quote':texts[1]}]}]}
    model.chat_json = respond
    first = QAAgent(kb,cfg,model).ask('如何处理输出？',paper_ids=[pid])
    prompt = model.calls[-1][1][-1]['content']
    second = QAAgent(kb,cfg,model).ask('请继续解释这个步骤。',paper_ids=[pid])
    following = model.calls[-1][1][-1]['content']
    assert first.status == second.status == 'answered'
    assert first.retrieval['mode'] == 'full_material' and first.retrieval['delivered_chunks']==2
    assert prompt.split('【问题】')[0] == following.split('【问题】')[0]
    for n,text in enumerate(texts,1):
        assert f'【来源正文 P{n} 开始】\n{text}\n【来源正文 P{n} 结束】' in prompt
    assert (QA_QUOTE_CHECK in prompt) is not anchors
    assert ('【试验定位锚点' in prompt) is anchors
    located = first.evidence_audit['items'][0]['quotes'][0]
    assert texts[1][located['start']:located['end']] == located['quote'] == texts[1]
    assert first.evidence_audit['semantic_status']=='unreviewed'
    assert cfg.llm.model_window_tokens==1000000 and cfg.llm.model_max_output_tokens==384000
