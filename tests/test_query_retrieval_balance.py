"""Long bilingual questions must not crowd out their translation branches."""
from litprism.query import MAX_TERMS, expand_query, glossary_expand, keyword_terms


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
