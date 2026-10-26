"""Task routing must preserve scope, current material, and evidence breadth."""
from test_workflow_guards import env as workflow_env  # noqa: F401
from test_workflow_guards import paper
import pytest

from litprism.agents import QAAgent

env = workflow_env


@pytest.mark.parametrize('known_window', [False, True])
@pytest.mark.parametrize('question', ['请概述 RAG 原论文的方法。', '比较 RAG 论文的结论与网页描述。'])
def test_default_task_words_do_not_select_the_library(env, known_window, question):
    cfg, kb, model, _ = env
    if known_window:
        cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    related = paper(kb, title='RAG study', text='RAG retrieval method conclusion')
    other = paper(kb, title='Other study', text='unrelated method conclusion')
    answer = QAAgent(kb, cfg, model).ask(question, strict_scope=False)
    assert set(answer.retrieval['evidence_papers']) == {related}
    assert other not in {e['paper_id'] for e in answer.excerpts}
    assert len([t for t, _ in model.calls if t == 'expand']) == 1


def test_named_comparison_recovers_each_name_without_searching_every_paper(env):
    cfg, kb, model, _ = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    a = paper(kb, title='BERT', text='BERT attention')
    kb.add_chunks(a, [{'text': 'BERT attention '+('attention '*20), 'page': n} for n in range(1, 9)])
    b = paper(kb, title='Transformer', text='Transformer attention')
    unrelated = paper(kb, title='Other', text='unrelated attention method')
    answer = QAAgent(kb, cfg, model).ask('比较 BERT 与 Transformer 的注意力方法。', top_k=1, strict_scope=False)
    assert set(answer.retrieval['evidence_papers']) == {a, b}
    assert unrelated not in {e['paper_id'] for e in answer.excerpts}
    assert len([t for t, _ in model.calls if t == 'expand']) == 1


def test_overview_followup_uses_current_parent_source_without_selecting_library(env):
    cfg, kb, model, client = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    selected = paper(kb, title='Parent', text='alphatopic original evidence')
    paper(kb, title='Other', text='unrelated evidence')
    cid = kb.add_conversation('Actual parent')
    response = client.post('/api/chat/'+cid, json={'question': 'alphatopic'})
    assert response.json()['status'] == 'answered'
    old_chunk = kb.get_chunks(selected)[0]['id']
    answer = QAAgent(kb, cfg, model).ask('概述这篇的方法。', conversation_id=cid, strict_scope=False)
    assert set(answer.retrieval['evidence_papers']) == {selected}
    kb.add_chunks(selected, [{'text': 'replacement material', 'page': 2}])
    stale = QAAgent(kb, cfg, model).ask('概述这篇的方法。', conversation_id=cid, strict_scope=False)
    assert old_chunk not in set(stale.retrieval['restored_chunk_ids'])
    assert not any('original evidence' in e['text'] for e in stale.excerpts)


def test_default_overview_keeps_knowledge_and_current_web_beside_target_paper(env):
    from litprism.qa_retrieval import retrieve
    from litprism.search import search_local
    from litprism.knowledge import create_source, review_source
    cfg, kb, model, _ = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    selected = paper(kb, title='RAG', text='RAG retrieval evidence')
    paper(kb, title='Other', text='unrelated method evidence')
    knowledge = create_source(kb, kind='knowledge', title='RAG note', content='RAG checked knowledge')
    review_source(kb, knowledge['id'], action='confirm', note='Isolated source fixture', content_hash=knowledge['content_hash'])
    web = {'id': 'current-web', 'text': 'current search summary', 'source_kind': 'web', 'paper_id': None}
    hits, report = retrieve(kb, '概述 RAG 方法。', '概述 RAG 方法。', paper_ids=None, top_k=8,
                           llm=model, search=search_local, cfg=cfg, fits_full=lambda _: True,
                           strict_scope=False, external=[web])
    assert {h['paper_id'] for h in hits if h.get('paper_id')} == {selected}
    assert knowledge['id'] in {h.get('source_id') for h in hits}
    assert web in hits and report['requested_paper_ids'] is None


def test_overview_without_literal_match_keeps_normal_query_expansion(env):
    cfg, kb, model, _ = env
    selected = paper(kb, title='Residual study', text='residual evidence')
    paper(kb, title='Other', text='unrelated material')
    original = model.chat_json

    def expand(messages, task='', **kwargs):
        if task == 'expand':
            model.calls.append((task, messages))
            return {'keywords': ['residual']}
        return original(messages, task=task, **kwargs)

    model.chat_json = expand
    answer = QAAgent(kb, cfg, model).ask('概述残差连接的实现。', strict_scope=False)
    assert set(answer.retrieval['evidence_papers']) == {selected}
    assert len([t for t, _ in model.calls if t == 'expand']) == 1


@pytest.mark.parametrize('question', ['概述所有论文的方法。', 'Summarize all papers.'])
def test_explicit_library_overview_keeps_all_current_papers(env, question):
    cfg, kb, model, _ = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    a = paper(kb, title='A', text='alpha evidence')
    b = paper(kb, title='B', text='completely different vocabulary')
    paper(kb, title='Unverified', level='unchecked', text='forbidden material')
    answer = QAAgent(kb, cfg, model).ask(question, strict_scope=False)
    assert set(answer.retrieval['evidence_papers']) == {a, b}
    assert not any(t == 'expand' for t, _ in model.calls)


@pytest.mark.parametrize('known_window', [False, True])
def test_separate_source_types_do_not_expand_to_every_paper(env, monkeypatch, known_window):
    cfg, kb, model, _ = env
    if known_window:
        cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    related = paper(kb, title='RAG study', text='alphatopic knowledge index')
    other = paper(kb, title='Unrelated study', text='knowledge background only')
    from litprism.search import search_local
    calls = []

    def observed_search(*args, **kwargs):
        calls.append(kwargs.get('paper_ids'))
        return search_local(*args, **kwargs)

    monkeypatch.setattr('litprism.agents.search_local', observed_search)
    result = QAAgent(kb, cfg, model).ask(
        'alphatopic knowledge：请分别说明论文依据和网页做法。', top_k=1, strict_scope=False)
    assert result.retrieval['evidence_papers'] == [related]
    assert other not in {e['paper_id'] for e in result.excerpts}
    assert calls == [None]


def test_global_overview_delivers_each_current_paper_without_lexical_bias(env):
    cfg, kb, model, _ = env
    a = paper(kb, title='A', text='alpha research')
    b = paper(kb, title='B', text='unrelated vocabulary')
    kb.add_chunks(a, [{'text': 'current alpha', 'page': 2}])
    ans = QAAgent(kb, cfg, model).ask('这些论文分别研究了什么问题？')
    assert ans.retrieval['mode'] == 'overview'
    assert set(ans.retrieval['evidence_papers']) == {a, b}
    assert 'alpha research' not in str(model.calls)
    assert not any(task == 'expand' for task, _ in model.calls)


def test_cross_paper_search_recovers_paper_missing_from_global_top_k(env):
    cfg, kb, model, _ = env
    a = paper(kb, title='A', text='alpha evidence')
    kb.add_chunks(a, [{'text': 'alpha '+('evidence '*20), 'page': n} for n in range(1, 8)])
    b = paper(kb, title='B', text='alpha alternative evidence')
    ans = QAAgent(kb, cfg, model).ask('比较这些论文的 alpha 方法', top_k=2)
    assert ans.retrieval['mode'] == 'cross_paper'
    assert set(ans.retrieval['evidence_papers']) == {a, b}
    assert len([task for task, _ in model.calls if task == 'expand']) == 1


def test_section_reads_full_selected_chapter_without_300_character_cutoff(env):
    cfg, kb, model, _ = env
    a = paper(kb, title='A')
    kb.add_chunks(a, [{'text': 'Earlier chapter', 'section': '方法', 'page': 1},
                      {'text': 'x '*350+'TAIL_LIMITATION', 'section': '局限与未来工作', 'page': 2},
                      {'text': 'Second limitation', 'section': '局限与未来工作', 'page': 3}])
    ans = QAAgent(kb, cfg, model).ask('这篇的局限是什么？', paper_ids=[a], top_k=1)
    assert ans.retrieval['mode'] == 'section'
    assert ans.retrieval['delivered_chunks'] == 2 and 'TAIL_LIMITATION' in str(model.calls)
    assert 'Earlier chapter' not in str([m for t, m in model.calls if t == 'qa'])


def test_passage_expansion_stays_in_same_material_section_and_paper(env):
    cfg, kb, model, _ = env
    a = paper(kb, title='A')
    kb.add_chunks(a, [{'text': 'neighbor detail', 'section': '方法', 'page': 1},
                      {'text': 'alpha seed', 'section': '方法', 'page': 2},
                      {'text': 'other chapter', 'section': '结论', 'page': 3}])
    paper(kb, title='B', text='alpha forbidden')
    ans = QAAgent(kb, cfg, model).ask('alpha', paper_ids=[a], top_k=1)
    texts = [e['text'] for e in ans.excerpts]
    assert 'neighbor detail' in texts and 'other chapter' not in texts
    assert 'alpha forbidden' not in texts


def test_empty_scope_cannot_become_overview_of_all(env):
    cfg, kb, model, _ = env
    paper(kb)
    ans = QAAgent(kb, cfg, model).ask('请概述全文', paper_ids=[])
    assert ans.status == 'refused' and not ans.excerpts


def test_window_limited_overview_is_explicit(env):
    cfg, kb, model, _ = env
    a = paper(kb)
    kb.add_chunks(a, [{'text': 'alpha '*400, 'page': n} for n in range(1, 12)])
    cfg.context.window_tokens = 4500
    cfg.context.output_reserve_tokens = 1000
    ans = QAAgent(kb, cfg, model).ask('概述全文', paper_ids=[a])
    assert ans.retrieval['window_limited']
    assert ans.retrieval['delivered_chunks'] < ans.retrieval['candidate_chunks']


def test_known_window_scoped_paper_delivers_full_material_with_stable_prefix(env):
    cfg, kb, model, _ = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'Introduction evidence', 'page': 1},
                        {'text': 'SIN_COS_DEFINITION not in lexical question', 'page': 6},
                        {'text': 'TRAINING_SCHEDULE', 'page': 7}])
    first = QAAgent(kb, cfg, model).ask('位置编码使用哪些函数？', paper_ids=[pid], top_k=1)
    prompt1 = model.calls[-1][1][-1]['content']
    second = QAAgent(kb, cfg, model).ask('训练多久？', paper_ids=[pid], top_k=1)
    prompt2 = model.calls[-1][1][-1]['content']
    assert first.retrieval['mode'] == second.retrieval['mode'] == 'full_material'
    assert first.retrieval['delivered_chunks'] == 3
    assert prompt1.split('【问题】')[0] == prompt2.split('【问题】')[0]
    assert 'SIN_COS_DEFINITION' in prompt1 and 'TRAINING_SCHEDULE' in prompt1
    assert not any(task == 'expand' for task, _ in model.calls)


def test_full_material_that_does_not_fit_uses_retrieval_without_losing_scope(env):
    cfg, kb, model, _ = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 4500
    cfg.context.output_reserve_tokens = 1000
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'alpha '+('large '*500), 'page': n} for n in range(1, 20)])
    paper(kb, title='Forbidden', text='alpha forbidden')
    result = QAAgent(kb, cfg, model).ask('alpha', paper_ids=[pid], top_k=1)
    assert result.retrieval['full_material_considered'] and not result.retrieval['full_material_fits']
    assert result.retrieval['mode'] == 'passages'
    assert {e['paper_id'] for e in result.excerpts} == {pid}


def test_actual_overview_wording_cannot_fall_back_to_two_chunks_per_paper(env):
    cfg, kb, model, _ = env
    a = paper(kb, title='A', text='alpha facts')
    b = paper(kb, title='B', text='OTHER_ABSTRACT')
    result = QAAgent(kb, cfg, model).ask('这些论文分别研究什么问题？每篇一句。', paper_ids=[a, b])
    assert result.retrieval['mode'] == 'overview'
    assert 'OTHER_ABSTRACT' in str(model.calls)


def test_short_followup_restores_actual_source_and_rejects_stale_or_out_of_scope(env, monkeypatch):
    cfg, kb, model, client = env
    pid = paper(kb, text='alphatopic MASK selected 15 percent; conditional 80 percent.')
    other = paper(kb, title='Other')
    cid = kb.add_conversation('Percentages', scope={'paper_ids': [pid]})
    assert client.post('/api/chat/'+cid, json={'question': 'alphatopic masking?'}).json()['status'] == 'answered'
    monkeypatch.setattr('litprism.agents.search_local', lambda *a, **kw: [])
    restored = QAAgent(kb, cfg, model).ask('这里的比例分母是什么？', paper_ids=[pid], conversation_id=cid)
    assert restored.retrieval['restored_chunk_ids']
    assert 'MASK selected' in str(model.calls[-1])
    switched = QAAgent(kb, cfg, model).ask('它是什么？', paper_ids=[other], conversation_id=cid)
    assert switched.status == 'refused'
    kb.add_chunks(pid, [{'text': 'New current material', 'page': 2}])
    stale = QAAgent(kb, cfg, model).ask('这里的比例呢？', paper_ids=[pid], conversation_id=cid)
    assert stale.status == 'refused'
