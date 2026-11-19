"""G08 · N08 真实库恢复演练（只读源库，绝不激活/切换）。

流程：只读 URI 打开真实库 → sqlite 在线 backup API 取一致快照 → 复制文件树到
全新演练目录 → 用真实 KnowledgeBase 打开副本（迁移应幂等为 0）→ 以真实
backup._database_content 校验链核对快照与副本（schema 版本/integrity/FK/逐表
rowid 逻辑 sha256/材料归属）→ 输出 JSON 报告。

硬约束：
- 源库以 ``file:...?mode=ro`` 只读打开，全程不写源库、不迁移真实主库；
- 演练目录必须位于数据根之外且不存在（绝不覆盖任何现有目录）；
- 绝不调用 activate_directory / activate / 重启切换。
默认 dry-run 只打印计划；加 ``--execute`` 才真正执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

TABLE_TREES = ('notes', 'skills', 'papers', 'attachments')


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, help='真实数据根（含 litprism.db），全程只读')
    parser.add_argument('--execute', action='store_true', help='真正执行演练；默认只打印计划')
    parser.add_argument('--output', default=None, help='证据输出目录（默认 _selftest.g08-drill-<时间戳>）')
    args = parser.parse_args()

    from litprism import backup
    from litprism.schema import SCHEMA_VERSION

    home = Path(args.home).expanduser().resolve()
    db_path = home / 'litprism.db'
    if not db_path.is_file():
        print(f'错误：{db_path} 不存在。', file=sys.stderr)
        return 2
    stamp = time.strftime('%Y%m%d-%H%M%S')
    evidence = Path(args.output or ROOT / f'_selftest.g08-drill-{stamp}').absolute()
    drill_root = evidence / 'restored-copy'
    print(f'源库（只读）：{db_path}')
    print(f'证据目录  ：{evidence}')
    print(f'演练副本  ：{drill_root}（必须不存在）')
    print(f'数据库 schema 版本预期：v{SCHEMA_VERSION}')
    if home.resolve() in {evidence.resolve(), drill_root.resolve().parent} or evidence.is_relative_to(home):
        print('错误：证据目录不得位于数据根内。', file=sys.stderr)
        return 2
    if drill_root.exists():
        print('错误：演练副本目录已存在，拒绝覆盖。', file=sys.stderr)
        return 2
    if not args.execute:
        print('\n这是 dry-run。确认无误后加 --execute 执行。')
        return 0

    started = time.time()
    report: dict = {'home': str(db_path), 'schema_version_expected': SCHEMA_VERSION,
                    'source_readonly': True, 'activated': False}
    evidence.mkdir(parents=True, exist_ok=True)
    staging = evidence / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    passed = True

    # 1) 在线备份：只读源连接 → 一致快照（含 WAL 已提交内容），绝不写源库
    src = sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True)
    try:
        src.row_factory = sqlite3.Row
        report['source_schema_version'] = src.execute('PRAGMA user_version').fetchone()[0]
        report['source_integrity'] = src.execute('PRAGMA integrity_check').fetchone()[0]
        report['source_fk_violations'] = len(src.execute('PRAGMA foreign_key_check').fetchall())
        dest = sqlite3.connect(staging / 'litprism.db')
        try:
            src.backup(dest, pages=256, sleep=0.0)
            dest.execute('PRAGMA journal_mode=DELETE')
        finally:
            dest.close()
    finally:
        src.close()
    report['source_integrity_ok'] = report['source_integrity'] == 'ok'
    passed &= report['source_integrity_ok'] and report['source_fk_violations'] == 0

    # 2) 真实校验链核对快照（schema 版本/integrity/FK/未知表/逐表逻辑 sha256/材料归属）。
    #    旧版源库（尚未运行当前版本应用）会被 schema gate 如实拒绝：记录并继续，
    #    改由第 4 步在演练副本上走真实迁移，报告迁移数与迁移后校验结果。
    snapshot = None
    try:
        snapshot = backup._database_content(staging / 'litprism.db')
        report['snapshot'] = {'schema_version': snapshot['schema_version'],
                              'schema_hash': snapshot['schema_hash'][:16],
                              'tables': snapshot['tables']}
        passed &= snapshot['schema_version'] == SCHEMA_VERSION
    except backup.BackupError as exc:
        # 旧版源库被 schema gate 如实拒绝属预期，迁移能力由第 4 步在副本上验证
        report['snapshot_gate'] = str(exc)

    # 3) 组装全新演练目录：快照库 + 文件树副本（源侧只读复制）
    drill_root.mkdir(parents=True)
    shutil.copyfile(staging / 'litprism.db', drill_root / 'litprism.db')
    copied_files = 0
    file_mismatches: list[str] = []
    for tree in TABLE_TREES:
        source_tree = home / tree
        if not source_tree.is_dir():
            report.setdefault('file_trees_absent', []).append(tree)
            continue
        target_tree = drill_root / tree
        shutil.copytree(source_tree, target_tree)
        for path in sorted(target_tree.rglob('*')):
            if path.is_file():
                copied_files += 1
                if _sha(path) != _sha(source_tree / path.relative_to(target_tree)):
                    file_mismatches.append(path.relative_to(target_tree).as_posix())
    report['copied_files'] = copied_files
    report['file_mismatches'] = file_mismatches
    passed &= not file_mismatches

    # 4) 用真实 KnowledgeBase 打开副本：当前版源库迁移应为 0；旧版源库则走真实迁移升级，
    #    迁移数必须恰为版本差。KB 关闭（WAL 自动 checkpoint 进主库）后再走完整校验链，
    #    因为 backup._database_content 以 immutable 只读打开主文件、不读 -wal。
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    cfg = Config()
    cfg.app.data_dir = str(drill_root)
    kb = KnowledgeBase(cfg)
    try:
        applied = kb.migrations_applied
        applied = len(applied) if isinstance(applied, list) else applied
        report['migrations_applied_on_restore'] = applied
        expected_migrations = SCHEMA_VERSION - report['source_schema_version']
        passed &= applied == expected_migrations
        report['restored_stats'] = {k: v for k, v in kb.stats().items()
                                    if isinstance(v, (int, str, float))}
    except Exception as exc:  # noqa: BLE001
        report['restored_open_error'] = str(exc)
        passed = False
    finally:
        kb.close()
    try:
        restored = backup._database_content(kb.db_path)
        report['restored'] = {'schema_version': restored['schema_version'],
                              'schema_hash': restored['schema_hash'][:16],
                              'tables': restored['tables']}
        if snapshot is not None:
            report['tables_equal'] = (report['snapshot']['tables'] == report['restored']['tables']
                                      and report['snapshot']['schema_hash'] == report['restored']['schema_hash'])
            passed &= bool(report['tables_equal'])
        else:
            report['tables_equal'] = None  # 旧版源库：快照被 gate 拒绝，逐表对照以迁移后校验为准
    except backup.BackupError as exc:
        report['restored_error'] = str(exc)
        passed = False

    report['passed'] = bool(passed)
    report['duration_s'] = round(time.time() - started, 1)
    (evidence / 'drill-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: report.get(k) for k in
                      ('source_integrity', 'source_fk_violations', 'snapshot', 'copied_files',
                       'file_mismatches', 'migrations_applied_on_restore', 'restored',
                       'tables_equal', 'restored_stats', 'passed', 'duration_s')},
                     ensure_ascii=False, indent=2, default=str))
    print(f"报告已写入 {evidence / 'drill-report.json'}")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
