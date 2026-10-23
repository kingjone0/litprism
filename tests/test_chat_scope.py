"""对话文献范围的可见可改（M04 缺口 1）。

三条底线：
1. 严格限定时，改范围后证据只来自新范围；默认重点模式允许补充；
2. 范围含不存在/已删除文献时明确报错，**不得静默扩为全库**；
3. ``paper_ids: []`` 表示"一篇都不选"，不是"全部"。
"""

from __future__ import annotations

import json

import pytest
from test_workflow_guards import env, paper  # noqa: F401


def cited_papers(client, conv):
    """读最后一条回答的引用归属论文。"""
    items = client.get(f'/api/conversations/{conv}/messages').json()['items']
    asst = [m for m in items if m.get('role') == 'assistant']
    if not asst:
        return set()
    raw = asst[-1].get('citations') or []
    if isinstance(raw, str):
        raw = json.loads(raw or '[]')
    return {c['paper_id'] for c in raw if c.get('paper_id')}


def make(kb, client, scope=None):
    return client.post('/api/conversations', json={'title': 'T', 'scope': scope}).json()['id']


class TestPatch:
    def test_scope_changes_evidence(self, env):
        """⭐ 改范围后，同一问题的证据只来自新范围。"""
        _, kb, _, client = env
        a = paper(kb, title='A', text='aaa evidence')
        b = paper(kb, title='B', text='bbb evidence')
        conv = make(kb, client, {'paper_ids': [a], 'mode':'strict'})

        first = client.post(f'/api/chat/{conv}', json={'question': 'bbb'}).json()
        assert first['insufficient_evidence'] and first['sources'] == []

        r = client.patch(f'/api/conversations/{conv}', json={'scope': {'paper_ids': [b], 'mode':'strict'}})
        assert r.status_code == 200 and r.json()['paper_count'] == 1

        second = client.post(f'/api/chat/{conv}', json={'question': 'bbb'}).json()
        assert not second['insufficient_evidence']
        assert cited_papers(client, conv) == {b}

    def test_clear_scope_returns_to_all(self, env):
        _, kb, _, client = env
        a = paper(kb, title='A', text='aaa evidence')
        b = paper(kb, title='B', text='bbb evidence')
        conv = make(kb, client, {'paper_ids': [a]})
        r = client.patch(f'/api/conversations/{conv}', json={'scope': None})
        assert r.status_code == 200 and r.json()['scope'] is None
        result = client.post(f'/api/chat/{conv}', json={'question': 'bbb'}).json()
        assert cited_papers(client, conv) == {b}

    def test_history_preserved(self, env):
        """改范围不动历史消息 —— 否则用户等于被迫重建会话。"""
        _, kb, _, client = env
        a = paper(kb, title='A', text='aaa evidence')
        conv = make(kb, client, {'paper_ids': [a]})
        client.post(f'/api/chat/{conv}', json={'question': 'aaa'})
        before = client.get(f'/api/conversations/{conv}/messages').json()['items']
        client.patch(f'/api/conversations/{conv}', json={'scope': None})
        after = client.get(f'/api/conversations/{conv}/messages').json()['items']
        assert len(after) == len(before) and len(before) >= 2

    def test_unknown_paper_rejected(self, env):
        """⭐ 未知文献不得被静默忽略成"全部"。"""
        _, kb, _, client = env
        conv = make(kb, client)
        r = client.patch(f'/api/conversations/{conv}', json={'scope': {'paper_ids': ['p_nope']}})
        assert r.status_code == 409
        assert '不存在' in r.json()['detail']

    def test_deleted_paper_rejected(self, env):
        _, kb, _, client = env
        pid = paper(kb, title='Gone')
        kb.delete_paper(pid)
        conv = make(kb, client)
        r = client.patch(f'/api/conversations/{conv}', json={'scope': {'paper_ids': [pid]}})
        assert r.status_code == 409

    def test_empty_list_is_not_all(self, env):
        """⭐ 空列表 = 一篇都不选，不是"全部"。"""
        _, kb, _, client = env
        paper(kb, title='A', text='aaa evidence')
        conv = make(kb, client)
        r = client.patch(f'/api/conversations/{conv}', json={'scope': {'paper_ids': []}})
        assert r.status_code == 200
        assert r.json()['scope'] == {'paper_ids': []}
        assert r.json()['paper_count'] == 0
        result = client.post(f'/api/chat/{conv}', json={'question': 'aaa'}).json()
        assert result['insufficient_evidence'] and result['sources'] == []

    def test_collection_scope(self, env):
        _, kb, _, client = env
        a = paper(kb, title='A', text='aaa evidence')
        gid = client.post('/api/collections', json={'name': '组'}).json()['id']
        client.patch(f'/api/paper/{a}', json={'collection_id': gid})
        conv = make(kb, client)
        r = client.patch(f'/api/conversations/{conv}', json={'scope': {'collection_id': gid}})
        assert r.status_code == 200 and r.json()['paper_count'] == 1

    def test_bad_shapes(self, env):
        _, kb, _, client = env
        conv = make(kb, client)
        # 不带 scope 是合法空操作（同一接口还管改名/置顶）
        assert client.patch(f'/api/conversations/{conv}', json={}).status_code == 200
        assert client.patch(f'/api/conversations/{conv}', json={'scope': 'nope'}).status_code == 400
        assert client.patch(f'/api/conversations/{conv}',
                            json={'scope': {'paper_ids': 'p1'}}).status_code == 400

    def test_title_and_scope_together(self, env):
        """同一接口既管改名也管范围，互不覆盖。"""
        _, kb, _, client = env
        a = paper(kb, title='A', text='aaa evidence')
        conv = make(kb, client)
        r = client.patch(f'/api/conversations/{conv}',
                         json={'title': '改名', 'scope': {'paper_ids': [a]}})
        assert r.status_code == 200
        got = [c for c in client.get('/api/conversations').json()['items'] if c['id'] == conv][0]
        assert got['title'] == '改名' and json.loads(got['scope'])['paper_ids'] == [a]

    def test_unknown_conversation(self, env):
        _, _kb, _, client = env
        assert client.patch('/api/conversations/conv_nope', json={'scope': None}).status_code == 404
