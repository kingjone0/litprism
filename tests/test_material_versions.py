# ruff: noqa: F811
"""N12: real SQLite migrations, immutable citations, version-isolated retrieval."""
from __future__ import annotations

import json
import sqlite3

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.evidence import stored_excerpts
from litprism.manual_claims import ClaimRejected, add, list_claims
from litprism.schema import MIGRATIONS, apply_migrations
from litprism.semantics import payload_hash


def old_analysis(kb, pid):
    chunk = kb.get_chunks(pid)[0]
    payload = {'research_question': 'old evidence [P1 p.1]',
               '_citations': [{'tag': 'P1', 'paper_id': pid, 'chunk_id': chunk['id'], 'page': 1}],
               '_semantic_review': {'items': [{'id': 'claim1', 'field': 'research_question',
                                               'text': 'old evidence [P1 p.1]', 'candidate_tags': ['P1']}]}}
    kb.save_analysis(pid, payload)
    return chunk, payload


@pytest.mark.parametrize('policy,status', [('replace', 'superseded'), ('keep_both', 'retained')])
def test_replace_isolates_default_reads_but_keeps_historical_citations(env, policy, status):
    _, kb, _, client = env
    pid = paper(kb, text='ancientmarker original evidence')
    old, payload = old_analysis(kb, pid)
    mid = old['material_id']
    kb.add_chunks(pid, [{'text': 'modernmarker new content', 'page': 3}], policy=policy)
    new = kb.get_chunks(pid)[0]
    assert new['material_id'] != mid
    assert kb.count_chunks(pid) == 1
    assert not kb.search_chunks('ancientmarker')
    assert not kb._search_like(['ancientmarker'], [pid], 8)
    assert kb.search_chunks('modernmarker')[0]['id'] == new['id']
    assert stored_excerpts(kb, payload['_citations'])[0]['text'] == old['text']
    assert kb.get_chunks(pid, material_id=mid)[0]['id'] == old['id']
    assert kb.list_materials(pid)[1]['status'] == status
    assert not kb.get_analysis(pid)['material_current']
    assert client.get(f'/api/paper/{pid}').json()['analysis_material'] == {'id': mid, 'current': False}
    assert client.get(f'/api/paper/{pid}/text').json()['pages'][0]['page'] == 3
    assert client.get(f'/api/paper/{pid}/text?material_id={mid}').json()['pages'][0]['page'] == 1
    detail = client.get('/api/evidence/'+old['id']).json()
    assert detail['citable'] and detail['material']['status'] == status
    # Existing human review is allowed on the actual historical sources.
    response = client.post(f'/api/paper/{pid}/semantic/claim1/review', json={
        'action': 'confirm', 'note': 'Checked old snapshot', 'analysis_hash': payload_hash(payload),
        'reviewed_text': 'Human corrected old evidence'})
    assert response.status_code == 200


def test_identical_import_and_historical_retry_do_not_duplicate_or_reactivate(env):
    _, kb, _, _ = env
    pid = paper(kb, text='same original')
    old = kb.get_chunks(pid)[0]
    kb.add_chunks(pid, [{'text': old['text'], 'page': old['page'], 'section': old['section']}])
    assert kb.get_chunks(pid)[0]['id'] == old['id']
    assert len(kb.list_materials(pid)) == 1
    kb.add_chunks(pid, [{'text': 'current version', 'page': 2}])
    current = kb.get_chunks(pid)
    kb.add_chunks(pid, [{'text': old['text'], 'page': old['page'], 'section': old['section']}])
    assert kb.get_chunks(pid) == current
    assert len(kb.list_materials(pid)) == 2


def test_lower_completeness_cannot_displace_fulltext(env):
    _, kb, _, _ = env
    pid = paper(kb, text='complete body')
    kb.add_chunks(pid, [{'text': 'version two full text', 'page': 4}], level='fulltext')
    current = kb.get_chunks(pid)
    kb.add_chunks(pid, [{'text': 'abstract only', 'section': '题录与摘要'}], level='abstract')
    assert kb.get_chunks(pid) == current
    assert kb.get_paper(pid)['material_level'] == 'fulltext'
    assert kb.list_materials(pid)[0]['status'] == 'retained'


def test_new_analysis_archives_old_payload_and_manual_records(env):
    _, kb, _, client = env
    pid = paper(kb, text='old evidence')
    old, payload = old_analysis(kb, pid)
    digest = payload_hash(payload)
    add(kb, pid, digest, {'field': 'methodology', 'text': 'manual missing claim',
                         'chunk_id': old['id'], 'quote': old['text']})
    kb.add_chunks(pid, [{'text': 'fresh evidence', 'page': 2}])
    fresh = kb.get_chunks(pid)[0]
    # Cannot add evidence from new material to the old analysis.
    with pytest.raises(ClaimRejected, match='材料版本'):
        add(kb, pid, digest, {'field': 'methodology', 'text': 'new', 'chunk_id': fresh['id'], 'quote': fresh['text']})
    kb.save_analysis(pid, {'research_question': 'new evidence'})
    assert kb.get_analysis(pid)['material_current']
    history = client.get(f'/api/paper/{pid}/materials').json()['analyses']
    assert len(history) == 1 and history[0]['material_id'] == old['material_id']
    archived = kb.get_analysis(pid, version_id=history[0]['id'])
    assert archived['payload'] == payload
    assert list_claims(kb, pid, digest)[0]['chunk_id'] == old['id']
    history_review = client.get(f'/api/paper/{pid}/semantic?version_id='+history[0]['id']).json()
    assert history_review['manual'][0]['chunk_id'] == old['id']
    assert client.get(f'/api/paper/{pid}?version_id='+history[0]['id']).json()['analysis']['research_question'] == payload['research_question']


def test_cross_paper_material_and_tampered_citation_rejected(env):
    _, kb, _, client = env
    first, second = paper(kb), paper(kb, title='Other')
    old, payload = old_analysis(kb, first)
    mid = kb.get_chunks(second)[0]['material_id']
    assert client.get(f'/api/paper/{first}/text?material_id={mid}').status_code == 404
    with pytest.raises(ValueError, match='材料版本'):
        stored_excerpts(kb, [dict(payload['_citations'][0], material_id=mid)])
    with pytest.raises(ValueError, match='不属于'):
        kb.save_analysis(first, {}, material_id=mid)


def test_import_failure_rolls_back_snapshot_and_preserves_current(env):
    _, kb, _, _ = env
    pid = paper(kb)
    before = kb.get_chunks(pid)
    with pytest.raises(ValueError, match='ID 已存在'):
        kb.add_chunks(pid, [{'id': before[0]['id'], 'text': 'changed'}])
    with pytest.raises(ValueError):
        kb.add_chunks(pid, [])
    assert kb.get_chunks(pid) == before and len(kb.list_materials(pid)) == 1


@pytest.mark.parametrize('row_factory', [None, sqlite3.Row])
def test_v11_migration_preserves_ids_payload_and_fts(tmp_path, row_factory):
    con = sqlite3.connect(tmp_path/'legacy.sqlite')
    for migration in MIGRATIONS:
        if migration.version <= 11:
            migration.apply(con)
    con.execute('PRAGMA user_version=11')
    con.execute("INSERT INTO papers(id,title,verify_level,material_level) VALUES ('p1','Old','verified','fulltext')")
    con.execute("INSERT INTO chunks(id,paper_id,page,text) VALUES ('stable_id','p1',7,'legacy text')")
    rowid = con.execute('SELECT rowid FROM chunks').fetchone()[0]
    con.execute('INSERT INTO chunks_fts(rowid,seg_text) VALUES (?,?)', (rowid, 'legacy text'))
    payload = json.dumps({'_citations': [{'tag': 'P1', 'chunk_id': 'stable_id'}]})
    con.execute('INSERT INTO analyses(paper_id,payload) VALUES (?,?)', ('p1', payload))
    con.commit()
    con.row_factory = row_factory
    assert len(apply_migrations(con)) == 1
    assert con.row_factory is row_factory
    assert tuple(con.execute('SELECT id,rowid,text FROM chunks').fetchone()) == ('stable_id', rowid, 'legacy text')
    assert con.execute('SELECT payload FROM analyses').fetchone()[0] == payload
    assert con.execute('SELECT material_id FROM analyses').fetchone()[0]
    assert con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'legacy'").fetchone()[0] == 1
    assert not apply_migrations(con)
    assert not con.execute('PRAGMA foreign_key_check').fetchall()
    con.close()


def test_same_content_can_upgrade_completeness_without_new_chunk_ids(env):
    _, kb, _, _ = env
    pid = paper(kb)
    chunk = kb.get_chunks(pid)[0]
    kb.add_chunks(pid, [{k: chunk[k] for k in ('text', 'page', 'section')}], level='fulltext')
    assert kb.get_chunks(pid)[0]['id'] == chunk['id']
    assert kb.list_materials(pid)[0]['level'] == 'fulltext'


def test_concurrent_reads_never_observe_half_written_material(env):
    from concurrent.futures import ThreadPoolExecutor
    _, kb, _, _ = env
    pid = paper(kb)
    def write():
        for n in range(20):
            kb.add_chunks(pid, [{'text': f'v{n}', 'page': n}, {'text': f'v{n}', 'page': n}])
    def read():
        for _ in range(40):
            chunks = kb.get_chunks(pid)
            assert len({c['material_id'] for c in chunks}) == 1
            assert len({c['text'] for c in chunks}) == 1
            kb.get_analysis(pid)
            kb.stats()
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(write), pool.submit(read), pool.submit(read)]
        for job in jobs:
            job.result()


def test_stale_analysis_is_not_reused_or_exported_as_current(env):
    from litprism.extract import analyze_paper, export_notes
    from litprism.genui import _validated_analysis
    from litprism.mcp.tools import LitPrismTools
    cfg, kb, model, _ = env
    pid = paper(kb)
    _old, payload = old_analysis(kb, pid)
    payload['_citation_check'] = {'passed': True}
    kb.save_analysis(pid, payload)
    kb.add_chunks(pid, [{'text': 'new current material ' * 40, 'page': 1}])
    assert not _validated_analysis(kb, pid)
    tools = LitPrismTools(kb, cfg)
    assert not tools.get_paper(pid)['analysis']['material_current']
    assert '不代表当前原文' in tools.resource_paper(pid)
    second = paper(kb, title='Other current material')
    assert not tools.compare_papers([pid, second])['ok']
    with pytest.raises(ValueError, match='旧材料'):
        export_notes(kb, cfg, pid)
    analyze_paper(kb, cfg, model, pid, with_concepts=False)
    assert any(task == 'extract' for task, _ in model.calls)
    assert kb.get_analysis(pid)['material_current']


def test_cli_and_ui_share_missing_share_when_machine_is_empty():
    import runpy
    from pathlib import Path

    from litprism.quality import summarize
    script = runpy.run_path(str(Path(__file__).resolve().parents[1]/'tools/eval_extraction.py'))
    row = {'title': 'test', 'source': 'paper', 'id': 'p1', 'report': {'items': []},
           'reviews': [], 'manual': [{'field': 'methodology'}]}
    assert summarize(row['report'], [], row['manual'])['totals']['recall'] is None
    rendered = script['report_row'](row)
    assert 'n/a' in rendered and '100%' not in rendered


def test_v12_migration_failure_is_atomic(tmp_path):
    con = sqlite3.connect(tmp_path/'broken.sqlite')
    for migration in MIGRATIONS:
        if migration.version <= 11:
            migration.apply(con)
    con.execute('PRAGMA user_version=11')
    con.execute("INSERT INTO papers(id,title) VALUES ('p1','Broken payload')")
    con.execute("INSERT INTO chunks(id,paper_id,text) VALUES ('stable','p1','text')")
    con.execute("INSERT INTO analyses(paper_id,payload) VALUES ('p1','{bad json')")
    con.commit()
    with pytest.raises(RuntimeError):
        apply_migrations(con)
    assert con.execute('PRAGMA user_version').fetchone()[0] == 11
    assert 'material_id' not in {r[1] for r in con.execute('PRAGMA table_info(chunks)')}
    assert con.execute('SELECT text FROM chunks').fetchone()[0] == 'text'
    con.close()
