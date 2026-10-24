"""Scripted operator controls on copies of the frozen three real replies.

Not a new model sample or user approval. Original model choices and results stay
unchanged. No model client or service is constructed; only local audit APIs run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
from chat_mixed_acceptance import backup_readonly, write  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(source, output):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from litprism.config import Config
    from litprism.context.dialogue import build_dialogue
    from litprism.db import KnowledgeBase
    from litprism.knowledge import get_source, search_knowledge
    from litprism.qa_reviews import register_qa_reviews
    source, output = source.resolve(), output.resolve()
    if not source.name.startswith('_selftest.') or not output.name.startswith('_selftest.') or source == output:
        raise ValueError('Use a frozen isolated source and a separate new output root')
    frozen = {name: sha(source/name) for name in ('manifest.json', 'results.json', 'development-review.json')}
    original = json.loads((source/'results.json').read_text(encoding='utf-8'))
    output.mkdir(parents=True, exist_ok=False)
    controls = []
    for row in original['rows']:
        folder = output/row['id']
        backup_readonly(source/row['id']/'data/litprism.db', folder/'data/litprism.db')
        cfg = Config()
        cfg.app.data_dir, cfg.export.notes_dir = str(folder/'data'), str(folder/'notes')
        kb = KnowledgeBase(cfg)
        try:
            app = FastAPI()
            register_qa_reviews(app, kb)
            client = TestClient(app)
            rid = row['review']['id']
            parent_endpoint = '/api/chat-reviews/'+rid
            parent = client.get(parent_endpoint).json()
            raw_parent = kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0]
            item = parent['units'][0] if row['id'] == 'simple' else parent['units'][-1]
            uid = item['id']
            endpoint = parent_endpoint+'/units/'+uid
            events = []
            def act(action, text=None, evidence=None):
                d = client.get(endpoint).json()
                response = client.post(endpoint+'/review', json={'action': action,
                    'note': '隔离脚本操作者控制，核对原文和章节；不是用户人工确认。', 'reviewed_text': text,
                    'evidence': evidence or [], 'snapshot_hash': d['snapshot_hash'], 'review_token': d['review_token']})
                assert response.status_code == 200, response.text
                events.append(dict(action=action, response=response.json()))
                return response.json().get('knowledge_id')
            def selected(tag, cue):
                c = client.get(endpoint+'/sources/'+tag)
                c.raise_for_status()
                a = next(a for a in c.json()['anchors'] if cue in a['quote'])
                return {'source': tag, 'anchor_id': a['id']}
            if row['id'] == 'simple':
                evidence = [{'source': q['source'], 'anchor_id': q['anchor_id']} for q in item['original_evidence']]
                sid = act('confirm', 'unitcontrolsimplemarker '+item['text'], evidence)
                assert search_knowledge(kb, 'unitcontrolsimplemarker', 10)
            elif row['id'] == 'cross_sentence':
                original_selection = [{'source': q['source'], 'anchor_id': q['anchor_id']} for q in item['original_evidence']]
                later = selected('P19', 'This allows every')
                evidence = [*original_selection, later]
                sid = act('confirm', 'unitcontrolcrossmarker '+item['text'], evidence)
                d = client.get(endpoint).json()
                assert len(d['original_evidence']) == 1 and len(d['reviews'][-1]['evidence_snapshot']) == 2
                old = sid
                sid = act('revise', 'unitcontrolcrossupdated 查询来自解码器、键值来自编码器，每个解码器位置可关注全部输入位置。[P19]', evidence)
                assert get_source(kb, old)['status'] == 'revoked'
                assert not search_knowledge(kb, 'unitcontrolcrossmarker', 10)
                assert search_knowledge(kb, 'unitcontrolcrossupdated', 10)
                act('revoke')
                assert not search_knowledge(kb, 'unitcontrolcrossupdated', 10)
            else:
                act('reject')
                assert not any(u['knowledge_id'] for u in client.get(parent_endpoint).json()['units'])
                evidence = [selected('P72', '11 NLP tasks'), selected('P91', 'Feature-based Approach with BERT'), selected('P92', 'CoNLL')]
                text = ('BERT第4节报告11个NLP微调任务[P72]；第5.3节另比较特征提取与微调[P91]，'
                        '并使用CoNLL-2003 NER任务比较这两种方式[P92]。不把后者自动并入前述11任务集合。unitcontrolx1marker')
                sid = act('confirm', text, evidence)
                hits = search_knowledge(kb, 'unitcontrolx1marker', 10)
                assert hits and all(item['text'] not in h['text'] for h in hits)
            after = client.get(parent_endpoint).json()
            assert all(u['decision'] == 'unreviewed' for u in after['units'] if u['id'] != uid)
            assert not after['reviews'] and after['review_mode'] == 'units'
            assert kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0] == raw_parent
            assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, row['conversation_id'], None).history)
            controls.append({'case': row['id'], 'unit_id': uid, 'events': events,
                'parent_payload_unchanged': True, 'siblings_unreviewed': True,
                'original_status': row['status'], 'scripted_operator_not_user_approval': True,
                'unit_detail': client.get(endpoint).json()})
        finally:
            kb.close()
    assert all(sha(source/p) == h for p,h in frozen.items())
    result = {'model_calls': 0, 'new_model_quality_sample': False, 'semantic_passed': None,
        'original_results_unchanged': True, 'daily_database_opened': False,
        'scripted_operator_not_user_approval': True, 'source_sha256': frozen,
        'controls': controls, 'code_sha256': {str(p.relative_to(ROOT)): sha(p) for p in [
            *sorted((ROOT/'src/litprism').rglob('*.py')), Path(__file__)]}}
    write(output/'controls.json', result)
    return {'cases': len(controls), 'model_calls': 0, 'original_results_unchanged': True,
            'siblings_unreviewed': True, 'new_model_quality_sample': False}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.input, args.output)))
