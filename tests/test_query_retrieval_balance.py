"""Long bilingual questions must not crowd out their translation branches."""
import pytest

from litprism.query import MAX_TERMS, expand_query, glossary_expand, keyword_terms, llm_expand


QUESTION = ('多头注意力怎样并行计算，并将各头输出拼接、投影为最终结果？'
            '请综合可用论文与网页来源，简要说明，逐步给出直接依据；不要扩展性能或无关参数。')


class Expansion:
    name = 'frozen-expansion'

    def __init__(self):
        self.calls = 0
        self.terms = ['multi-head attention', 'output projection', 'linear projections',
                      'concatenated heads', 'parallel attention']

    def chat_json(self, messages, task=None):
        assert task == 'expand'
        self.calls += 1
        return {'keywords': self.terms}


class SavedResponse:
    name = 'saved-response'

    def __init__(self, value):
        self.value, self.calls = value, 0

    def chat_json(self, messages, task=None):
        assert task == 'expand'
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@pytest.mark.parametrize('value', [
    None, [], 'RAG', 1,
    {}, {'answer': 'Do not use this as search evidence', 'sources': ['invented']},
    {'keywords': None}, {'keywords': 'RAG'}, {'keywords': {'RAG': 'Wikipedia'}},
    {'keywords': 123}, {'keywords': True},
    {'keywords': []}, {'keywords': ['', '  ']}, {'keywords': [None, {}, 123, False]},
])
def test_invalid_expansion_keeps_the_exact_deterministic_fallback(value):
    model = SavedResponse(value)
    assert expand_query(QUESTION, model) == expand_query(QUESTION)
    assert model.calls == 1


def test_mixed_keyword_list_keeps_only_nonempty_strings():
    model = SavedResponse({'keywords': [' RAG ', None, {'source': 'bad'}, 123, False, '', '  ', '维基百科']})
    assert llm_expand(model, QUESTION) == ['RAG', '维基百科']
    assert model.calls == 1


def test_service_exception_uses_fallback_without_retry():
    model = SavedResponse(RuntimeError('saved service failure'))
    assert expand_query(QUESTION, model) == expand_query(QUESTION)
    assert model.calls == 1


def test_keyword_limit_is_applied_after_ignoring_invalid_entries():
    model = SavedResponse({'keywords': [None, 123, ' ']+['valid'+str(i) for i in range(15)]})
    assert llm_expand(model, QUESTION) == ['valid'+str(i) for i in range(12)]
    assert model.calls == 1


def test_long_chinese_question_keeps_translation_and_model_branches():
    model = Expansion()
    assert len(keyword_terms(QUESTION)) > MAX_TERMS
    terms = expand_query(QUESTION, model)
    assert set(model.terms) <= set(terms)
    assert set(glossary_expand(QUESTION)) <= set(terms)
    assert model.calls == 1
    assert len(terms) <= 1 + MAX_TERMS * 2 + 12


def test_long_question_without_model_still_has_cross_language_terms():
    terms = expand_query(QUESTION)
    assert 'attention' in terms and 'output' in terms


def test_fallback_prefix_contains_more_than_one_query_branch():
    model = Expansion()
    terms = expand_query(QUESTION, model)
    # SQLite LIKE fallback only consumes the first six terms.
    assert any(t in model.terms for t in terms[:6])
    assert any(t in glossary_expand(QUESTION) for t in terms[:6])


def test_no_model_expansion_when_disabled_and_case_insensitive_deduplication():
    model = Expansion()
    model.terms = ['Attention', 'attention', ' output ', 'output']
    terms = expand_query('attention 输出', model)
    assert len(terms) == len({t.casefold() for t in terms})
    model.calls = 0
    expand_query(QUESTION, model, use_llm=False)
    assert model.calls == 0
