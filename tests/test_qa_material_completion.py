"""Default retrieval completes matched current papers, not the entire library."""
from test_workflow_guards import env as workflow_env, paper

from litprism.agents import QAAgent
from litprism.qa_retrieval import retrieve
from litprism.search import search_local

env = workflow_env


def known_window(cfg):
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000


def test_default_matched_paper_includes_unmatched_mechanism_and_web(env):
    cfg, kb, model, _ = env
    known_window(cfg)
    pid = paper(kb, title='Relevant')
    kb.add_chunks(pid, [{'text': 'alphatopic introduction', 'section': 'Introduction', 'page': 1},
                        {'text': 'project queries and keys', 'section': 'Mechanism', 'page': 4},
                        {'text': 'concatenate then project', 'section': 'Mechanism', 'page': 5}])
    other = paper(kb, title='Unrelated', text='zzirrelevant')
    external = {'id': 'web-fixture', 'source_kind': 'web', 'text': 'external acquired summary',
                'paper_title': 'Web', 'trust_level': 'pending', 'web_grant_id': 'grant-fixture'}
    ans = QAAgent(kb, cfg, model).ask('alphatopic', top_k=1, strict_scope=False, external=[external])
    assert ans.retrieval['mode'] == 'full_material'
    assert ans.retrieval['full_material_paper_ids'] == [pid]
    assert ans.retrieval['requested_paper_ids'] is None
    assert other in ans.retrieval['scope_papers']
    assert {e['paper_id'] for e in ans.excerpts if e['source_kind'] == 'paper'} == {pid}
    assert 'concatenate then project' in str(model.calls)
    assert any(e['chunk_id'] == 'web-fixture' and e['trust_level'] == 'pending' for e in ans.excerpts)
    assert [t for t, _ in model.calls].count('expand') == 1


def test_default_no_match_does_not_inject_unrelated_fulltext(env):
    cfg, kb, model, _ = env
    known_window(cfg)
    paper(kb, text='unrelated')
    ans = QAAgent(kb, cfg, model).ask('zznomatch')
    assert ans.status == 'refused' and not ans.excerpts
    assert not any(t == 'qa' for t, _ in model.calls)


def test_default_completion_excludes_untrusted_deleted_and_old_materials(env):
    cfg, kb, model, _ = env
    known_window(cfg)
    pid = paper(kb, text='alphatopic OLD_VERSION')
    kb.add_chunks(pid, [{'text': 'alphatopic CURRENT_VERSION', 'page': 1},
                        {'text': 'current mechanism', 'page': 2}])
    paper(kb, title='Pending', level='likely', text='alphatopic forbidden pending')
    deleted = paper(kb, title='Deleted', text='alphatopic forbidden deleted')
    kb._con.execute("UPDATE papers SET deleted_at='2026-10-25' WHERE id=?", (deleted,))
    kb._con.commit()
    ans = QAAgent(kb, cfg, model).ask('alphatopic')
    assert {e['paper_id'] for e in ans.excerpts} == {pid}
    assert 'OLD_VERSION' not in str(model.calls)
    assert 'current mechanism' in str(model.calls)


def test_explicit_empty_and_strict_scope_stay_restricted(env):
    cfg, kb, model, _ = env
    known_window(cfg)
    a = paper(kb, text='alphatopic allowed')
    paper(kb, title='Outside', text='alphatopic forbidden')
    assert not QAAgent(kb, cfg, model).ask('alphatopic', paper_ids=[]).excerpts
    ans = QAAgent(kb, cfg, model).ask('alphatopic', paper_ids=[a])
    assert {e['paper_id'] for e in ans.excerpts} == {a}


def test_over_budget_paper_can_complete_seed_section_without_displacing_web(env):
    cfg, kb, _, _ = env
    known_window(cfg)
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'alphatopic seed', 'section': 'Mechanism', 'page': 1},
                        {'text': 'second step', 'section': 'Mechanism', 'page': 2},
                        {'text': 'third step', 'section': 'Mechanism', 'page': 3},
                        *[{'text': 'unrelated tail', 'section': 'Other', 'page': n} for n in range(4, 9)]])
    external = {'id': 'web-fixture', 'text': 'web', 'source_kind': 'web'}
    hits, report = retrieve(kb, 'alphatopic', 'alphatopic', paper_ids=None, top_k=1,
                            llm=None, search=search_local, cfg=cfg,
                            fits_full=lambda hs: len(hs) <= 4, external=[external])
    assert {h['text'] for h in hits} == {'alphatopic seed', 'second step', 'third step', 'web'}
    assert report['full_material_considered'] and not report['full_material_fits']
    assert report['full_material_paper_ids'] == []
    assert report['expanded_sections'] == [{'paper_id': pid, 'section': 'Mechanism'}]


def test_full_material_fit_accounts_for_supplemental_evidence(env):
    cfg, kb, _, _ = env
    known_window(cfg)
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'alphatopic', 'section': 'Intro', 'page': 1},
                        {'text': 'detail', 'section': 'Body', 'page': 2}])
    external = {'id': 'web-fixture', 'text': 'web', 'source_kind': 'web'}
    hits, report = retrieve(kb, 'alphatopic', 'alphatopic', paper_ids=None, top_k=1,
                            llm=None, search=search_local, cfg=cfg,
                            fits_full=lambda hs: len(hs) <= 2, external=[external])
    assert {h['id'] for h in hits} == {kb.get_chunks(pid)[0]['id'], 'web-fixture'}
    assert not report['full_material_fits']


def test_unknown_model_window_keeps_existing_passage_path(env):
    cfg, kb, model, _ = env
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'alphatopic', 'section': 'Intro', 'page': 1},
                        {'text': 'unmatched detail', 'section': 'Body', 'page': 2}])
    ans = QAAgent(kb, cfg, model).ask('alphatopic', top_k=1)
    assert ans.retrieval['mode'] == 'passages'
    assert not ans.retrieval['full_material_considered']
    assert len(ans.excerpts) == 1


def test_completion_keeps_other_seed_papers_when_only_one_full_paper_fits(env):
    cfg, kb, _, _ = env
    known_window(cfg)
    a = paper(kb, title='Small')
    b = paper(kb, title='Large')
    kb.add_chunks(a, [{'text': 'alpha small', 'section': 'Intro', 'page': 1},
                      {'text': 'small detail', 'section': 'Body', 'page': 2}])
    kb.add_chunks(b, [{'text': 'alpha large', 'section': 'Intro', 'page': 1},
                      *[{'text': 'large detail', 'section': 'Body', 'page': n} for n in range(2, 8)]])
    seeds = [dict(kb.get_chunks(pid)[0], score=1) for pid in (a, b)]
    def search(*args, **kwargs):
        return seeds
    hits, report = retrieve(kb, 'alpha', 'alpha', paper_ids=None, top_k=2,
                            llm=None, search=search, cfg=cfg, fits_full=lambda hs: len(hs) <= 3)
    assert {h['paper_id'] for h in hits} == {a, b}
    assert report['full_material_paper_ids'] == [a]
    assert report['full_material_considered'] and not report['full_material_fits']
    assert len(hits) == 3


def test_web_only_default_does_not_hydrate_unmatched_papers(env):
    cfg, kb, _, _ = env
    known_window(cfg)
    paper(kb, text='unrelated')
    external = {'id': 'web-fixture', 'text': 'web', 'source_kind': 'web'}
    hits, report = retrieve(kb, 'zznomatch', 'zznomatch', paper_ids=None, top_k=1,
                            llm=None, search=search_local, cfg=cfg,
                            fits_full=lambda hs: True, external=[external])
    assert hits == [external] and not report['full_material_considered']


def test_provider_overflow_reports_actual_full_papers_after_trimming(env):
    cfg, kb, model, _ = env
    known_window(cfg)
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'alphatopic', 'section': 'Intro', 'page': 1},
                        {'text': 'second step', 'section': 'Body', 'page': 2},
                        {'text': 'final step', 'section': 'Body', 'page': 3}])
    cfg.context.overflow_retries = 1
    attempts = []
    def chat(messages, task=None, **kwargs):
        if task == 'expand':
            return {}
        attempts.append(messages)
        if len(attempts) == 1:
            raise RuntimeError('context_length exceeded')
        return {'answer': '', 'insufficient_evidence': True}
    model.chat_json = chat
    ans = QAAgent(kb, cfg, model).ask('alphatopic')
    assert len(attempts) == 2
    assert ans.retrieval['window_limited']
    assert ans.retrieval['full_material_paper_ids'] == []
    assert not ans.retrieval['evidence_manifest']['papers'][0]['complete_current_material']
