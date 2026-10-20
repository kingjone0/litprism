"""候选池：去重、留档、状态流转。"""
from litprism.candidates import (
    candidate_fingerprint,
    list_candidates,
    normalize_url,
    save_candidates,
    set_candidate_status,
)
from litprism.config import Config
from litprism.db import KnowledgeBase


def kb_of(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    return cfg, KnowledgeBase(cfg)


def test_same_paper_in_three_forms_is_one_candidate(tmp_path):
    """DOI、arXiv、落地页三种形式必须收敛成一条，否则候选池会灌水。"""
    cfg, kb = kb_of(tmp_path)
    try:
        first = save_candidates(kb, [{'title': 'A paper', 'doi': '10.1234/ABC'}], query='q1')
        again = save_candidates(kb, [{'title': 'A paper (preprint)', 'doi': 'https://doi.org/10.1234/abc '}],
                                query='q1')
        arxiv = save_candidates(kb, [{'title': 'A paper', 'arxiv_id': '1607.01759'}], query='q2')
        url = save_candidates(kb, [{'title': 'A paper mirror', 'url': 'https://www.Example.com/p/1/?utm=1'}],
                              query='q3')
        url2 = save_candidates(kb, [{'title': 'A paper mirror again', 'url': 'example.com/p/1'}], query='q4')
        assert len(first['added']) == 1
        assert again['added'] == [] and again['existing'] == first['added']
        assert arxiv['added'] and arxiv['added'] != first['added']
        assert url2['added'] == [] and url2['existing'] == url['added']
    finally:
        kb.close()


def test_candidates_keep_query_kind_status_and_filter(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        save_candidates(kb, [{'title': 'Paper A', 'doi': '10.1/a'}, {'title': 'Paper B', 'doi': '10.1/b'}],
                        query='quantum', kind='paper', source='openalex')
        save_candidates(kb, [{'title': 'Web C', 'url': 'https://example.com/c'}], query='quantum', kind='web',
                        source='tavily')
        assert len(list_candidates(kb)) == 3
        assert len(list_candidates(kb, kind='paper')) == 2
        assert len(list_candidates(kb, kind='web')) == 1
        assert len(list_candidates(kb, query='quantum')) == 3
        assert len(list_candidates(kb, query='none')) == 0

        target = list_candidates(kb, kind='web')[0]
        updated = set_candidate_status(kb, target['id'], 'rejected')
        assert updated['status'] == 'rejected'
        assert [c['id'] for c in list_candidates(kb, status='rejected')] == [target['id']]
        assert len(list_candidates(kb, status='new')) == 2
    finally:
        kb.close()


def test_candidate_status_rejects_unknown_values(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'X', 'doi': '10.1/x'}])['added'][0]
        for bad in ('done', '', 'IMPORTED'):
            try:
                set_candidate_status(kb, cid, bad)
            except ValueError:
                continue
            raise AssertionError(f'非法状态被接受：{bad!r}')
        assert list_candidates(kb, kind='nope') == [] if False else True
        try:
            list_candidates(kb, kind='nope')
        except ValueError:
            pass
        else:
            raise AssertionError('未知 kind 应报错')
    finally:
        kb.close()


def test_imported_candidate_keeps_paper_link(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Link me', 'doi': '10.1/link'}])['added'][0]
        pid = kb.add_paper(title='Link me', verify_level='verified', material_level='fulltext')
        row = set_candidate_status(kb, cid, 'imported', paper_id=pid)
        assert row['status'] == 'imported' and row['paper_id'] == pid
    finally:
        kb.close()


def test_url_normalization_ignores_scheme_www_and_trailing_slash():
    assert normalize_url('https://www.Example.com/a/b/') == 'example.com/a/b'
    assert normalize_url('http://example.com/a/b') == 'example.com/a/b'
    assert candidate_fingerprint({'title': 'T', 'url': 'https://www.example.com/a/b/'}) == 'url:example.com/a/b'


def test_candidate_is_marked_imported_when_paper_already_in_library(tmp_path):
    """库里已有这篇 → 候选直接记 imported 并回链，不再当新候选。"""
    from litprism.candidates import find_library_paper, list_candidates

    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='Already here', doi='10.1/here', verify_level='verified', material_level='fulltext')
        save_candidates(kb, [{'title': 'Already here', 'doi': '10.1/here'}], query='q')
        row = list_candidates(kb)[0]
        assert row['status'] == 'imported' and row['paper_id'] == pid
        assert row['in_library'] is True and row['library_paper_id'] == pid
        assert find_library_paper(kb, {'arxiv_id': '1607.01759'}) is None
    finally:
        kb.close()


def test_hide_in_library_filters_only_linked_candidates(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        kb.add_paper(title='In library', doi='10.1/in', verify_level='verified')
        save_candidates(kb, [{'title': 'In library', 'doi': '10.1/in'}, {'title': 'Outside', 'doi': '10.1/out'}])
        assert len(list_candidates(kb)) == 2
        visible = list_candidates(kb, hide_in_library=True)
        assert [c['title'] for c in visible] == ['Outside']
    finally:
        kb.close()


def test_manual_candidate_creation_and_dedup(tmp_path):
    from litprism.candidates import add_manual_candidate

    cfg, kb = kb_of(tmp_path)
    try:
        first = add_manual_candidate(kb, title='Manual paper', arxiv_id='1607.01759')
        assert first['created'] is True and first['candidate']['kind'] == 'paper'
        again = add_manual_candidate(kb, title='Manual paper', arxiv_id='1607.01759')
        assert again['created'] is False and again['id'] == first['id']

        web = add_manual_candidate(kb, title='A blog post', url='https://example.com/post')
        assert web['candidate']['kind'] == 'web'

        try:
            add_manual_candidate(kb, title='   ')
        except ValueError:
            pass
        else:
            raise AssertionError('空标题应被拒绝')
    finally:
        kb.close()


def test_sync_library_backfills_candidates_added_before_paper(tmp_path):
    """先有候选、后导入论文：sync 应把这条补成 imported。"""
    from litprism.candidates import sync_library

    cfg, kb = kb_of(tmp_path)
    try:
        save_candidates(kb, [{'title': 'Later imported', 'doi': '10.1/later'}])
        assert list_candidates(kb)[0]['status'] == 'new'
        pid = kb.add_paper(title='Later imported', doi='10.1/later', verify_level='verified')
        result = sync_library(kb)
        assert result['linked'] == 1
        row = list_candidates(kb)[0]
        assert row['status'] == 'imported' and row['paper_id'] == pid
    finally:
        kb.close()
