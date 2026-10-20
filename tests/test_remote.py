"""arXiv 取全文：标识解析、主机限制、失败不改状态。"""
import types

import pytest

from litprism import remote
from litprism.candidates import list_candidates, save_candidates
from litprism.config import Config
from litprism.db import KnowledgeBase


def kb_of(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    return cfg, KnowledgeBase(cfg)


def test_arxiv_id_parsed_from_every_common_form():
    assert remote.arxiv_id_of({'arxiv_id': '1607.01759'}) == '1607.01759'
    assert remote.arxiv_id_of({'arxiv_id': 'arXiv:1607.01759v2'}) == '1607.01759v2'
    assert remote.arxiv_id_of({'url': 'https://arxiv.org/abs/1607.01759'}) == '1607.01759'
    assert remote.arxiv_id_of({'url': 'https://arxiv.org/pdf/1607.01759v3.pdf'}) == '1607.01759v3'
    assert remote.arxiv_id_of({'doi': '10.48550/arXiv.1607.01759'}) == '1607.01759'
    assert remote.arxiv_id_of({'url': 'https://example.com/paper/1234'}) is None
    assert remote.arxiv_id_of({'title': 'no identifier'}) is None


def test_download_refuses_non_arxiv_hosts():
    with pytest.raises(ValueError):
        remote.download_pdf('https://evil.example.com/x.pdf', remote.Path('x.pdf'))


class _Resp:
    def __init__(self, body, status=200, url='https://arxiv.org/pdf/1607.01759'):
        self._body, self.status_code, self.url = body, status, url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield self._body


def test_download_rejects_non_pdf_body(tmp_path, monkeypatch):
    monkeypatch.setattr('requests.get', lambda *a, **k: _Resp(b'<html>not a pdf</html>'))
    with pytest.raises(ValueError, match='不是 PDF'):
        remote.download_pdf('https://arxiv.org/pdf/1607.01759', tmp_path / 'x.pdf')
    assert not (tmp_path / 'x.pdf').exists()
    assert not (tmp_path / 'x.part').exists()


def test_download_rejects_redirect_to_other_host(tmp_path, monkeypatch):
    monkeypatch.setattr('requests.get', lambda *a, **k: _Resp(b'%PDF-1.4 ...', url='https://evil.example.com/x'))
    with pytest.raises(ValueError, match='非 arXiv'):
        remote.download_pdf('https://arxiv.org/pdf/1607.01759', tmp_path / 'x.pdf')


class _Verifier:
    def __init__(self, level='verified'):
        self.level = level

    def verify(self, **kwargs):
        return types.SimpleNamespace(level=self.level, confidence=0.9,
                                     to_dict=lambda: {'level': self.level, 'matched': True})


def _stub_document(text='正文内容。模型得分为 71.8。'):
    from litprism.ingest import Document, Page
    return Document(title='Remote paper', authors=['A'], year=2024, abstract='摘要',
                    full_text=text * 40, pages=[Page(number=1, text=text)])


def test_fetch_candidate_imports_fulltext_and_links_candidate(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Remote paper', 'arxiv_id': '1607.01759'}], query='q')['added'][0]

        def fake_download(url, dest, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'%PDF-1.4 fake')
            return dest

        monkeypatch.setattr(remote, 'download_pdf', fake_download)
        monkeypatch.setattr('litprism.ingest.load_document', lambda p: _stub_document())
        monkeypatch.setattr('litprism.ingest.chunk_document',
                            lambda doc, size, overlap: [{'text': '正文内容', 'page': 1, 'section': '方法'}])

        result = remote.fetch_candidate_pdf(kb, cfg, _Verifier(), cid)
        assert result['paper_id'] and result['chunks'] == 1
        assert result['verify_level'] == 'verified' and result['material_level'] == '全文'

        paper = kb.get_paper(result['paper_id'])
        assert paper['material_level'] == 'fulltext' and paper['arxiv_id'] == '1607.01759'
        candidate = list_candidates(kb, kind='paper')[0]
        assert candidate['status'] == 'imported' and candidate['paper_id'] == result['paper_id']
    finally:
        kb.close()


def test_download_failure_leaves_candidate_untouched(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Broken', 'arxiv_id': '1607.00000'}], query='q')['added'][0]

        def boom(url, dest, **kwargs):
            raise ValueError('下载失败：HTTP 404')

        monkeypatch.setattr(remote, 'download_pdf', boom)
        with pytest.raises(ValueError):
            remote.fetch_candidate_pdf(kb, cfg, _Verifier(), cid)
        assert list_candidates(kb)[0]['status'] == 'new'
        assert kb._con.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0
    finally:
        kb.close()


def test_candidate_without_arxiv_id_is_rejected_before_network(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Web only', 'url': 'https://example.com/a'}], kind='web')['added'][0]
        called = []
        monkeypatch.setattr(remote, 'download_pdf', lambda *a, **k: called.append(1))
        with pytest.raises(ValueError, match='没有 arXiv 标识'):
            remote.fetch_candidate_pdf(kb, cfg, _Verifier(), cid)
        assert called == []
    finally:
        kb.close()


class _ImportVerifier:
    def __init__(self, exists=True, level='verified'):
        self.exists, self.level = exists, level
        self.message = 'ok' if exists else '未在权威数据库命中'

    def verify(self, **kwargs):
        from litprism.verify import SourceRecord
        best = SourceRecord(source='crossref', title='Deep Residual Learning',
                            doi='10.1109/CVPR.2016.90', year=2016) if self.exists else None
        return types.SimpleNamespace(exists=self.exists, best_match=best, manual=False,
                                     level=self.level, confidence=0.9, message=self.message,
                                     to_dict=lambda: {'level': self.level})


def test_import_non_arxiv_candidate_requires_verification(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Deep Residual Learning', 'doi': '10.1109/CVPR.2016.90',
                                    'year': 2016}], query='q')['added'][0]
        result = remote.import_candidate(kb, cfg, _ImportVerifier(), cid)
        assert result['paper_id'] and result['material_level'] == '仅题录与摘要'
        paper = kb.get_paper(result['paper_id'])
        assert paper['material_level'] in ('abstract', 'metadata')
        assert list_candidates(kb)[0]['status'] == 'imported'
    finally:
        kb.close()


def test_import_rejected_candidate_leaves_status_untouched(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Unverifiable', 'doi': '10.9999/none'}], query='q')['added'][0]
        with pytest.raises(ValueError, match='未通过核验'):
            remote.import_candidate(kb, cfg, _ImportVerifier(exists=False), cid)
        assert list_candidates(kb)[0]['status'] == 'new'
        assert kb._con.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0
    finally:
        kb.close()


def test_import_arxiv_candidate_delegates_to_fulltext_fetch(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Attention', 'arxiv_id': '1706.03762'}], query='q')['added'][0]
        called = []
        monkeypatch.setattr(remote, 'fetch_candidate_pdf', lambda *a, **k: called.append(a[3]) or {'paper_id': 'p_x'})
        assert remote.import_candidate(kb, cfg, _ImportVerifier(), cid) == {'paper_id': 'p_x'}
        assert called == [cid]
    finally:
        kb.close()


def test_import_rejects_web_candidate(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'A blog', 'url': 'https://example.com/x'}], kind='web')['added'][0]
        with pytest.raises(ValueError, match='只有论文类候选'):
            remote.import_candidate(kb, cfg, _ImportVerifier(), cid)
    finally:
        kb.close()


class _ImportVerifier:
    def __init__(self, exists=True, level='verified'):
        self.exists, self.level = exists, level
        self.message = 'ok' if exists else '未在权威数据库命中'

    def verify(self, **kwargs):
        from litprism.verify import SourceRecord
        best = SourceRecord(source='crossref', title='Deep Residual Learning',
                            doi='10.1109/CVPR.2016.90', year=2016) if self.exists else None
        return types.SimpleNamespace(exists=self.exists, best_match=best, manual=False,
                                     level=self.level, confidence=0.9, message=self.message,
                                     to_dict=lambda: {'level': self.level})


def test_import_non_arxiv_candidate_requires_verification(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Deep Residual Learning', 'doi': '10.1109/CVPR.2016.90',
                                    'year': 2016}], query='q')['added'][0]
        result = remote.import_candidate(kb, cfg, _ImportVerifier(), cid)
        assert result['paper_id'] and result['material_level'] == '仅题录与摘要'
        paper = kb.get_paper(result['paper_id'])
        assert paper['material_level'] in ('abstract', 'metadata')
        assert list_candidates(kb)[0]['status'] == 'imported'
    finally:
        kb.close()


def test_import_rejected_candidate_leaves_status_untouched(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Unverifiable', 'doi': '10.9999/none'}], query='q')['added'][0]
        with pytest.raises(ValueError, match='未通过核验'):
            remote.import_candidate(kb, cfg, _ImportVerifier(exists=False), cid)
        assert list_candidates(kb)[0]['status'] == 'new'
        assert kb._con.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0
    finally:
        kb.close()


def test_import_arxiv_candidate_delegates_to_fulltext_fetch(tmp_path, monkeypatch):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'Attention', 'arxiv_id': '1706.03762'}], query='q')['added'][0]
        called = []
        monkeypatch.setattr(remote, 'fetch_candidate_pdf', lambda *a, **k: called.append(a[3]) or {'paper_id': 'p_x'})
        assert remote.import_candidate(kb, cfg, _ImportVerifier(), cid) == {'paper_id': 'p_x'}
        assert called == [cid]
    finally:
        kb.close()


def test_import_rejects_web_candidate(tmp_path):
    cfg, kb = kb_of(tmp_path)
    try:
        cid = save_candidates(kb, [{'title': 'A blog', 'url': 'https://example.com/x'}], kind='web')['added'][0]
        with pytest.raises(ValueError, match='只有论文类候选'):
            remote.import_candidate(kb, cfg, _ImportVerifier(), cid)
    finally:
        kb.close()
