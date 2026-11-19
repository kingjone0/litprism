"""阅读标注：增删改查与边界。"""
import pytest

from litprism import annotations
from litprism.config import Config
from litprism.db import KnowledgeBase


def kb_of(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    return cfg, KnowledgeBase(cfg)


def test_add_list_update_delete(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='Annotated paper')
        a = annotations.add(kb, pid, quote='原文的一段话', note='这是我的理解', page=3)
        assert a['paper_id'] == pid and a['page'] == 3
        assert annotations.count(kb, pid) == 1

        items = annotations.list_for(kb, pid)
        assert [x['quote'] for x in items] == ['原文的一段话']

        updated = annotations.update(kb, a['id'], note='改过的批注')
        assert updated['note'] == '改过的批注'

        annotations.delete(kb, a['id'])
        assert annotations.list_for(kb, pid) == []
        assert annotations.count(kb, pid) == 0
        assert len(annotations.list_for(kb, pid, include_deleted=True)) == 1
    finally:
        kb.close()


def test_quote_is_required(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='P')
        with pytest.raises(ValueError, match='不能为空'):
            annotations.add(kb, pid, quote='   ')
    finally:
        kb.close()


def test_missing_paper_is_rejected(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        with pytest.raises(ValueError, match='论文不存在'):
            annotations.add(kb, 'p_missing', quote='x')
    finally:
        kb.close()


def test_quote_is_truncated_not_silently_dropped(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='P')
        a = annotations.add(kb, pid, quote='字' * (annotations.MAX_QUOTE + 50))
        assert len(a['quote']) == annotations.MAX_QUOTE
    finally:
        kb.close()


def test_ui_and_routes_are_wired():
    root = __import__('pathlib').Path(__file__).resolve().parents[1]
    app_js = (root / 'src/litprism/web/static/app.js').read_text(encoding='utf-8')
    assert 'data-ptab="annotations"' in app_js, '缺少标注标签页'
    assert 'annotationFab' in app_js, '缺少选中文字后的标注入口'
    assert '/annotations`' in app_js, '前端没有调用标注接口'
    server = (root / 'src/litprism/web/server.py').read_text(encoding='utf-8')
    assert 'register_annotations' in server, '标注路由未注册'


def test_markdown_export_keeps_quote_note_and_page(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='Export paper', doi='10.1234/exp')
        annotations.add(kb, pid, quote='第一行\n第二行', note='我的批注', page=7)
        md = annotations.to_markdown(kb, pid)
        assert '# Export paper' in md
        assert '> 第一行' in md and '> 第二行' in md
        assert '我的批注' in md
        assert '第 7 页' in md
    finally:
        kb.close()


def test_csv_export_escapes_commas(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='CSV paper')
        annotations.add(kb, pid, quote='a, b', note='带,逗号')
        out = annotations.to_csv(kb, pid)
        lines = out.strip().splitlines()
        assert lines[0].startswith('page,chunk_id,quote,note,created_at')
        assert '"a, b"' in out and '"带,逗号"' in out
    finally:
        kb.close()


def test_markdown_export_keeps_quote_note_and_page(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='Export paper', doi='10.1234/exp')
        annotations.add(kb, pid, quote='第一行\n第二行', note='我的批注', page=7)
        md = annotations.to_markdown(kb, pid)
        assert '# Export paper' in md
        assert '> 第一行' in md and '> 第二行' in md
        assert '我的批注' in md
        assert '第 7 页' in md
    finally:
        kb.close()


def test_csv_export_escapes_commas(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='CSV paper')
        annotations.add(kb, pid, quote='a, b', note='带,逗号')
        out = annotations.to_csv(kb, pid)
        lines = out.strip().splitlines()
        assert lines[0].startswith('page,chunk_id,quote,note,created_at')
        assert '"a, b"' in out and '"带,逗号"' in out
    finally:
        kb.close()


def test_export_endpoint_serves_markdown_and_csv(tmp_path):
    from fastapi.testclient import TestClient

    from litprism.web import server

    cfg, kb = kb_of(tmp_path)
    pid = kb.add_paper(title='API paper')
    kb.close()
    api = TestClient(server.create_app(cfg))
    saved = api.post(f'/api/paper/{pid}/annotations', json={'quote': '引用一句话', 'note': '批注', 'page': 2})
    assert saved.status_code == 200, saved.text

    md = api.get(f'/api/paper/{pid}/annotations/export?format=md')
    assert md.status_code == 200
    assert '# API paper' in md.text and '> 引用一句话' in md.text and '批注' in md.text
    assert 'attachment' in md.headers.get('content-disposition', '')

    csv_resp = api.get(f'/api/paper/{pid}/annotations/export?format=csv')
    assert csv_resp.status_code == 200
    assert csv_resp.text.splitlines()[0].startswith('page,chunk_id,quote,note,created_at')


def test_list_all_is_cross_paper_with_titles(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        p1 = kb.add_paper(title='First paper')
        p2 = kb.add_paper(title='Second paper')
        annotations.add(kb, p1, quote='q1')
        annotations.add(kb, p2, quote='q2')
        items = annotations.list_all(kb)
        assert [x['quote'] for x in items] == ['q2', 'q1']  # 新的在前
        assert {x['paper_title'] for x in items} == {'First paper', 'Second paper'}
        only = annotations.list_all(kb, paper_id=p1)
        assert [x['quote'] for x in only] == ['q1']
    finally:
        kb.close()


def test_list_all_excludes_deleted(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        pid = kb.add_paper(title='P')
        a = annotations.add(kb, pid, quote='will delete')
        annotations.delete(kb, a['id'])
        assert annotations.list_all(kb) == []
    finally:
        kb.close()


def test_global_endpoint_lists_annotations(tmp_path):
    from fastapi.testclient import TestClient

    from litprism.web import server

    cfg, kb = kb_of(tmp_path)
    pid = kb.add_paper(title='Global paper')
    kb.close()
    api = TestClient(server.create_app(cfg))
    api.post(f'/api/paper/{pid}/annotations', json={'quote': '全局可见', 'note': 'note'})
    resp = api.get('/api/annotations?limit=10')
    assert resp.status_code == 200
    items = resp.json()['items']
    assert items and items[0]['quote'] == '全局可见'
    assert items[0]['paper_title'] == 'Global paper'
