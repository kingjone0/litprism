"""G08 演练脚本自动化：合成库通过、旧版源库迁移路径、comparison 标注与残留重跑。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _make_library(home: Path) -> None:
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    home.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.app.data_dir = str(home)
    kb = KnowledgeBase(cfg)
    try:
        pid = kb.add_paper(title='演练自动化样本', verify_level='verified', material_level='fulltext')
        kb.add_chunks(pid, [{'text': 'alphatopic drill automated evidence', 'page': 1}], level='fulltext')
    finally:
        kb.close()


def _run_drill(home: Path, output: Path):
    env = {k: v for k, v in os.environ.items() if k != 'LITPRISM_HOME'}
    return subprocess.run(
        [sys.executable, str(ROOT / 'tools' / 'backup_drill.py'),
         '--home', str(home), '--execute', '--output', str(output)],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)


def _report(output: Path) -> dict:
    return json.loads((output / 'drill-report.json').read_text(encoding='utf-8'))


def test_drill_passes_on_current_schema(tmp_path):
    from litprism.schema import SCHEMA_VERSION
    home = tmp_path / 'home'
    _make_library(home)
    out = tmp_path / 'evidence'
    result = _run_drill(home, out)
    assert result.returncode == 0, result.stdout + result.stderr
    report = _report(out)
    assert report['passed'] and report['comparison'] == 'full'
    assert report['tables_equal'] and report['migrations_applied_on_restore'] == 0
    assert report['snapshot']['schema_version'] == SCHEMA_VERSION


def test_drill_migrates_old_version_copy(tmp_path):
    from litprism.schema import SCHEMA_VERSION
    home = tmp_path / 'home'
    _make_library(home)
    # 模拟旧版源库：表结构保持当前版本、user_version 降为 v15——
    # 预期 gate 如实拒绝直恢，副本经真实迁移 v16+v17 后全链通过
    con = sqlite3.connect(home / 'litprism.db')
    con.execute('PRAGMA user_version = 15')
    con.commit()
    con.close()
    out = tmp_path / 'evidence'
    result = _run_drill(home, out)
    assert result.returncode == 0, result.stdout + result.stderr
    report = _report(out)
    assert report['passed'] and report['comparison'] == 'skipped_old_schema'
    assert 'v15' in report['snapshot_gate']
    assert report['migrations_applied_on_restore'] == SCHEMA_VERSION - 15
    assert report['restored']['schema_version'] == SCHEMA_VERSION
    assert report['tables_equal'] is None


def test_drill_rerun_after_leftover(tmp_path):
    """上次运行残留 drill_root 后：同 --output 重跑有明确指引而非静默覆盖；
    staging 残留由脚本自身清理（rmtree 后重建），整个证据目录删除后可重跑。"""
    home = tmp_path / 'home'
    _make_library(home)
    out = tmp_path / 'evidence'
    first = _run_drill(home, out)
    assert first.returncode == 0
    # drill_root 已存在 → 拒绝覆盖并给清理指引，退出码 2
    blocked = _run_drill(home, out)
    assert blocked.returncode == 2
    assert '清理指引' in blocked.stderr
    # 清理证据目录后可重跑
    shutil.rmtree(out)
    rerun = _run_drill(home, out)
    assert rerun.returncode == 0 and _report(out)['passed']
