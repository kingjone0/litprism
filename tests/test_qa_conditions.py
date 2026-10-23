import pytest

from litprism.qa_conditions import conditional_risks


SOURCE = ('The system scores 71% using the 2020 index for\n2020 world leaders '
          'and 67% using the 2024 index for 2024 world leaders. '
          'With mismatched indices it scores 11% with the 2024 index and 2020 leaders, '
          '3% with the 2020 index and 2024 leaders.')


@pytest.mark.parametrize('answer,blocked', [
    ('对2020年领导人，2020索引得71%，2024索引得67%。[P1 p.8]', True),
    ('2020索引对2020年领导人得71%；2024索引对2024年领导人得67%。[P1 p.8]', False),
    ('2024索引对2020年领导人得3%。[P1 p.8]', True),
    ('2024索引对2020年领导人得11%；2020索引对2024年领导人得3%。[P1 p.8]', False),
    ('2024年领导人使用2024索引得67％。[P1 p.8]', False),
    ('The 2024 index scores 67% for 2024 leaders. [P1 p.8]', False),
    ('The 2024 index scores 67% for 2020 leaders. [P1 p.8]', True),
    ('The 2020 index scores 71% for 2020 leaders and the 2024 index scores 67% for 2024 leaders. [P1 p.8]', False),
    ('The 2020 index scores 71% for 2020 leaders and the 2024 index scores 67% for 2020 leaders. [P1 p.8]', True),
    ('比较2020索引与2024年索引，用2020索引回答2020年领导人得71%，用2024索引回答2024年领导人得67%。[P1 p.8]', False),
    ('比较2020索引与2024年索引，对2020年领导人，2020索引得71%，2024索引得67%。[P1 p.8]', True),
    ('不能说2024索引在2020领导人上得67%。[P1 p.8]', False),
    ('原文报告71%的准确率。[P1 p.8]', False),
    ('2020索引答对71%（对应2020年在任者）、2024索引答对67%（对应2024年在任者），2024索引配2020领袖为11%，2020索引配2024领袖为3%。[P1]', False),
    ('2020索引答对71%（对应2024年在任者）、2024索引答对67%（对应2020年在任者）。[P1]', True),
    ('2020索引答对71%（对应2020年在任者）、2024索引答对67%（对应2024年在任者），2024索引配2024领袖为11%。[P1]', True),
    ('2024索引对2020年领袖得67%。[P1]', True),
])
def test_condition_pairs_not_just_numeric_presence(answer, blocked):
    assert bool(conditional_risks(answer, [{'tag': 'P1', 'text': SOURCE}])) == blocked


def test_uncited_source_cannot_supply_or_contradict_conditions():
    answer = '2024索引对2020年领导人得67%。[P1 p.8]'
    assert not conditional_risks(answer, [{'tag': 'P1', 'text': 'Unrelated source.'},
                                           {'tag': 'P2', 'text': SOURCE}])


def test_matching_conditions_from_another_actually_cited_source_remain_valid():
    answer = '2024索引对2020年领导人得67%。[P1 p.8][P2 p.9]'
    assert not conditional_risks(answer, [{'tag': 'P1', 'text': SOURCE},
        {'tag': 'P2', 'text': 'Accuracy is 67% using the 2024 index for 2020 leaders.'}])


def test_shared_conditions_are_not_carried_across_unrelated_sentences():
    answer = '2020索引对2020领导人得71%。[P1 p.8]\n2024索引得67%。[P1 p.8]'
    assert conditional_risks(answer, [{'tag': 'P1', 'text': SOURCE}])


@pytest.mark.parametrize('target,value,blocked', [('2024','67',False),('2020','67',True),('2020','11',False)])
def test_labelled_markdown_rows_cannot_bypass_condition_pairing(target, value, blocked):
    answer = ('| 索引年份 | 评测对象年份 | 准确率 | 来源 |\n|---|---|---|---|\n'
              f'| 2024 | {target} | {value}% | [P1 p.8] |')
    assert bool(conditional_risks(answer, [{'tag': 'P1', 'text': SOURCE}])) == blocked
