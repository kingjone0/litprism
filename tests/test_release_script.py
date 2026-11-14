"""发布脚本的校验逻辑：宁可打不出来，也不要打出"看起来新"的旧产物。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('litprism_release', ROOT / 'packaging' / 'release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_version_matches_source():
    release = _load()
    sys.path.insert(0, str(ROOT / 'src'))
    from litprism.schema import SCHEMA_VERSION
    assert release.code_schema_version() == SCHEMA_VERSION


def test_sha256_reads_whole_file(tmp_path):
    release = _load()
    target = tmp_path / 'a.bin'
    target.write_bytes(b'litprism' * 1000)
    import hashlib
    assert release.sha256(target) == hashlib.sha256(b'litprism' * 1000).hexdigest()


def test_verify_rejects_installer_with_old_sidecar(tmp_path, monkeypatch):
    """安装包里嵌的 sidecar 与 dist 不同 → 必须失败，不能只比时间。"""
    release = _load()
    dist_exe = tmp_path / 'dist' / 'litprism-sidecar.exe'
    dist_exe.parent.mkdir()
    dist_exe.write_bytes(b'NEW' * 1000)
    unpacked = tmp_path / 'installer' / 'win-unpacked' / 'resources' / 'litprism-sidecar.exe'
    unpacked.parent.mkdir(parents=True)
    unpacked.write_bytes(b'OLD' * 1000)
    (tmp_path / 'installer' / 'LitPrism-Setup-0.1.0.exe').write_bytes(b'setup')

    monkeypatch.setattr(release, 'SIDECAR', dist_exe)
    monkeypatch.setattr(release, 'UNPACKED_SIDECAR', unpacked)
    monkeypatch.setattr(release, 'INSTALLER_DIR', tmp_path / 'installer')
    monkeypatch.setattr(release, 'MANIFEST', tmp_path / 'manifest.json')

    try:
        release.verify(15)
    except SystemExit as exc:
        assert '体积' in str(exc) or '内容' in str(exc)
    else:
        raise AssertionError('旧 sidecar 应被拒绝')


def test_verify_writes_manifest_when_hashes_match(tmp_path, monkeypatch):
    release = _load()
    payload = b'SAME' * 1000
    dist_exe = tmp_path / 'dist' / 'sidecar.exe'
    dist_exe.parent.mkdir()
    dist_exe.write_bytes(payload)
    unpacked = tmp_path / 'installer' / 'win-unpacked' / 'resources' / 'litprism-sidecar.exe'
    unpacked.parent.mkdir(parents=True)
    unpacked.write_bytes(payload)
    setup = tmp_path / 'installer' / 'LitPrism-Setup-0.1.0.exe'
    setup.write_bytes(b'setup')
    import os
    os.utime(setup, (dist_exe.stat().st_atime + 5, dist_exe.stat().st_mtime + 5))

    monkeypatch.setattr(release, 'SIDECAR', dist_exe)
    monkeypatch.setattr(release, 'UNPACKED_SIDECAR', unpacked)
    monkeypatch.setattr(release, 'INSTALLER_DIR', tmp_path / 'installer')
    monkeypatch.setattr(release, 'MANIFEST', tmp_path / 'manifest.json')

    manifest = release.verify(15)
    assert manifest['schema'] == 15
    assert (tmp_path / 'manifest.json').exists()
    assert manifest['sidecar']['bytes'] == len(payload)
