"""N05 人工补录（漏提）的测试。

核心口径：
1. 没有引用的补录必须拒绝 —— 否则分母不可信；
2. 召回在没有补录时是 ``None``（n/a），**不是 0**；
3. 补录按解读版本隔离，换一版分析不会把旧的算进新分母。
"""

from __future__ import annotations

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.manual_claims import (
    ClaimRejected,
    add,
    delete,
    list_claims,
    recall,
    validate,
)
from litprism.semantics import payload_hash

SOURCE = 'We train on eight P100 GPUs for 3.5 days. The model has 65 million parameters.'


def seed(kb, *, items=3):
    """造一篇论文 + 一份带陈述的解读，返回 (pid, chunk, digest)。"""
    pid = paper(kb, text=SOURCE)
    chunk = kb.get_chunks(pid)[0]
    report = {
        'status': 'not_run',
        'items': [
            {'id': f'cl_{i:016x}', 'field': 'methodology', 'text': f'机器陈述 {i}',
             'status': 'machine_supported', 'candidate_tags': ['P1']}
            for i in range(items)
        ],
    }
    payload = {
        '_semantic_review': report,
        '_citations': [{'tag': 'P1', 'chunk_id': chunk['id'], 'paper_id': pid, 'page': chunk['page']}],
    }
    kb.save_analysis(pid, payload, model='test')
    return pid, chunk, payload_hash(payload)


def claim(chunk, **over):
    base = {
        'field': 'key_conclusions',
        'text': '训练使用 8 块 P100，耗时 3.5 天。',
        'chunk_id': chunk['id'],
        'quote': 'We train on eight P100 GPUs for 3.5 days.',
        'note': '原文第一句',
    }
    base.update(over)
    return base


class TestValidate:
    def test_accepts_valid(self, env):
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        clean = validate(kb, pid, claim(chunk))
        assert clean['field'] == 'key_conclusions'
        assert clean['page'] == chunk['page']

    def test_quote_must_be_verbatim(self, env):
        """⭐ 没有原文依据的补录不能计入召回。"""
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        with pytest.raises(ClaimRejected) as exc:
            validate(kb, pid, claim(chunk, quote='我们训练用了 8 块 GPU'))
        assert '找不到' in exc.value.reason

    def test_quote_whitespace_tolerated(self, env):
        """PDF 抽取常把一句话断成多行，折叠空白后能匹配就应放行。"""
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        clean = validate(kb, pid, claim(chunk, quote='We train on eight P100 GPUs\n   for 3.5 days.'))
        assert clean['quote']

    def test_missing_quote_rejected(self, env):
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        with pytest.raises(ClaimRejected) as exc:
            validate(kb, pid, claim(chunk, quote=''))
        assert '依据' in exc.value.reason

    def test_chunk_must_belong_to_paper(self, env):
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        other = paper(kb, title='Other', text=SOURCE)
        other_chunk = kb.get_chunks(other)[0]
        with pytest.raises(ClaimRejected) as exc:
            validate(kb, pid, claim(chunk, chunk_id=other_chunk['id']))
        assert '不属于' in exc.value.reason

    def test_unknown_field_rejected(self, env):
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        with pytest.raises(ClaimRejected):
            validate(kb, pid, claim(chunk, field='whatever'))

    def test_empty_text_rejected(self, env):
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        with pytest.raises(ClaimRejected):
            validate(kb, pid, claim(chunk, text='   '))

    def test_text_length_capped(self, env):
        _, kb, _, _ = env
        pid, chunk, _ = seed(kb)
        with pytest.raises(ClaimRejected) as exc:
            validate(kb, pid, claim(chunk, text='长' * 2001))
        assert '过长' in exc.value.reason


class TestStore:
    def test_add_and_list(self, env):
        _, kb, _, _ = env
        pid, chunk, digest = seed(kb)
        got = add(kb, pid, digest, claim(chunk))
        assert got['origin'] == 'manual' and got['field_label'] == '关键结论'
        assert len(list_claims(kb, pid, digest)) == 1

    def test_duplicate_rejected(self, env):
        _, kb, _, _ = env
        pid, chunk, digest = seed(kb)
        add(kb, pid, digest, claim(chunk))
        with pytest.raises(ClaimRejected) as exc:
            add(kb, pid, digest, claim(chunk))
        assert exc.value.status == 409

    def test_version_isolated(self, env):
        """换一版分析后，旧补录不进入新分母。"""
        _, kb, _, _ = env
        pid, chunk, digest = seed(kb)
        add(kb, pid, digest, claim(chunk))
        assert len(list_claims(kb, pid, 'other-digest')) == 0

    def test_stale_hash_rejected(self, env):
        _, kb, _, _ = env
        pid, chunk, digest = seed(kb)
        with pytest.raises(ClaimRejected) as exc:
            add(kb, pid, digest, claim(chunk, analysis_hash='stale'))
        assert exc.value.status == 409

    def test_delete(self, env):
        _, kb, _, _ = env
        pid, chunk, digest = seed(kb)
        got = add(kb, pid, digest, claim(chunk))
        assert delete(kb, pid, digest, got['id'])
        assert not list_claims(kb, pid, digest)
        assert not delete(kb, pid, digest, got['id'])


class TestRecall:
    def test_none_when_no_manual(self):
        """⭐ 没补录 = 不可知，不是 0%。"""
        r = recall(machine=10, manual=0)
        assert r['ratio'] is None
        assert '不可用' in r['note']

    def test_none_when_no_machine_claims(self):
        """⭐ 机器没产出陈述时也不能算 100% —— 那是没有分母，不是"全漏了"。"""
        r = recall(machine=0, manual=1)
        assert r['ratio'] is None
        assert '没有机器陈述' in r['note']

    def test_ratio_with_both_sides(self):
        r = recall(machine=9, manual=1)
        assert r['ratio'] == pytest.approx(0.1)
        assert r['total'] == 10

    def test_reports_absolute_counts(self):
        """只报比例会诱导"少补 = 指标好看"，必须同时给绝对数。"""
        r = recall(machine=3, manual=2)
        assert r['manual'] == 2 and r['machine'] == 3
        assert '补录 2 条' in r['note']


class TestApi:
    def test_post_and_get(self, env):
        _, kb, _, client = env
        pid, chunk, digest = seed(kb)
        r = client.post(f'/api/paper/{pid}/semantic/claims',
                        json={**claim(chunk), 'analysis_hash': digest})
        assert r.status_code == 200, r.text
        assert r.json()['recall']['ratio'] == pytest.approx(1 / 4)  # 3 机器 + 1 人工
        body = client.get(f'/api/paper/{pid}/semantic').json()
        assert len(body['manual']) == 1
        assert body['recall']['manual'] == 1 and body['recall']['ratio'] > 0

    def test_post_rejects_bad_quote(self, env):
        _, kb, _, client = env
        pid, chunk, digest = seed(kb)
        r = client.post(f'/api/paper/{pid}/semantic/claims',
                        json={**claim(chunk, quote='编造的引用'), 'analysis_hash': digest})
        assert r.status_code == 400 and '找不到' in r.json()['detail']

    def test_delete_endpoint(self, env):
        _, kb, _, client = env
        pid, chunk, digest = seed(kb)
        cid = client.post(f'/api/paper/{pid}/semantic/claims',
                          json={**claim(chunk), 'analysis_hash': digest}).json()['claim']['id']
        assert client.delete(f'/api/paper/{pid}/semantic/claims/{cid}').status_code == 200
        assert client.get(f'/api/paper/{pid}/semantic').json()['manual'] == []
