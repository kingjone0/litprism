"""N08: consistent portable snapshots, verified restoration to a new directory.

Never opens the destination with KnowledgeBase (which would migrate it). Secrets,
caches and process locks are excluded. Original databases are never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import schema
from .config import Config, _from_dict, _validate
from .tasks import checkpoint

FORMAT = 'litprism-backup-v1'
MAX_FILES = 10000
MAX_BYTES = 20 * 1024**3
MARKER = '.litprism-restored.json'
SECRET_NAMES = {'secrets.json', '.secrets.key'}


class BackupError(ValueError):
    """A backup failed validation; safe to show the explanation to the user."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            checkpoint()
            digest.update(block)
    return digest.hexdigest()


def _portable_config(cfg):
    def clean(value):
        if isinstance(value, dict):
            private = {'api_key', 'api_key_ref', 'token', 'password', 'secret', 'client_secret',
                       'access_token', 'refresh_token', 'authorization'}
            return {k: clean(v) for k, v in value.items() if k.lower() not in private}
        return [clean(v) for v in value] if isinstance(value, list) else value
    raw = asdict(cfg)
    for key in ('config_path', 'config_warnings'):
        raw.pop(key, None)
    raw = clean(raw)
    raw['app']['data_dir'] = '.'
    raw['export']['notes_dir'] = 'notes'
    return raw


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _read_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)


def _expected_tables():
    con = sqlite3.connect(':memory:')
    try:
        schema.init_schema(con)
        return {r[0]: {c[1] for c in con.execute('PRAGMA table_info('+_quote(r[0])+')')}
                for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    finally:
        con.close()


def _database_content(path):
    """Logical checksums preserve rowid ordering used by audit histories.

    Physical SQLite checksums can change merely by switching WAL mode; logical
    checksums are the invariant for export -> restore -> export comparisons.
    """
    con = _read_db(path)
    try:
        version = con.execute('PRAGMA user_version').fetchone()[0]
        if version != schema.SCHEMA_VERSION:
            raise BackupError(f'备份 schema v{version} 与当前 v{schema.SCHEMA_VERSION} 不同，请使用匹配版本恢复。')
        if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise BackupError('数据库完整性检查失败。')
        if con.execute('PRAGMA foreign_key_check').fetchone():
            raise BackupError('数据库存在无效的关联记录。')
        objects = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        if any(r[0] in ('trigger', 'view') for r in objects):
            raise BackupError('备份含当前应用不支持的视图或触发器。')
        tables = {r[1] for r in objects if r[0] == 'table' and not r[1].startswith('sqlite_')}
        expected = _expected_tables()
        if tables != set(expected):
            raise BackupError('数据库必需表不完整或含未知表。')
        stats = {}
        for table in sorted(tables | {r[1] for r in objects if r[1] == 'sqlite_sequence'}):
            checkpoint('校验数据库表：' + table)
            columns = [r[1] for r in con.execute('PRAGMA table_info('+_quote(table)+')')]
            if table in expected and not expected[table].issubset(columns):
                raise BackupError('数据库缺少必需字段：' + table)
            try:
                cursor = con.execute('SELECT rowid,* FROM '+_quote(table)+' ORDER BY rowid')
            except sqlite3.OperationalError:
                cursor = con.execute('SELECT * FROM '+_quote(table)+' ORDER BY '+','.join(map(_quote, columns)))
            digest, count = hashlib.sha256(), 0
            for row in cursor:
                values = [{'blob': v.hex()} if isinstance(v, bytes) else v for v in row]
                digest.update(_json(values) + b'\n')
                count += 1
            stats[table] = {'rows': count, 'sha256': digest.hexdigest()}
        # Validate material ownership as well as FKs (a valid ID can belong to another paper).
        if con.execute('''SELECT 1 FROM chunks c LEFT JOIN materials m ON m.id=c.material_id
                          WHERE m.id IS NULL OR m.paper_id<>c.paper_id LIMIT 1''').fetchone():
            raise BackupError('原文块材料绑定无效。')
        if con.execute('''SELECT 1 FROM papers p LEFT JOIN materials m ON m.id=p.current_material_id
                          WHERE p.current_material_id IS NOT NULL AND (m.id IS NULL OR m.paper_id<>p.id) LIMIT 1''').fetchone():
            raise BackupError('当前材料归属无效。')
        return {'schema_version': version, 'schema_hash': hashlib.sha256(_json(objects)).hexdigest(), 'tables': stats}
    except sqlite3.DatabaseError as exc:
        raise BackupError('不是完整有效的 LitPrism 数据库。') from exc
    finally:
        con.close()


def _safe_source(path, root):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise BackupError('备份范围内包含链接或目录联接，请先使用实际文件。')
    if not path.resolve().is_relative_to(root.resolve()):
        raise BackupError('备份文件超出了配置的数据范围。')


def _copy_tree(source, dest):
    source = Path(source)
    if not source.exists():
        return
    if dest.resolve().is_relative_to(source.resolve()):
        raise BackupError('备份暂存目录位于文件来源内部，不能递归备份自身。')
    _safe_source(source, source)
    for path in sorted(source.rglob('*')):
        _safe_source(path, source)
        if path.name in SECRET_NAMES:
            continue
        if path.is_file():
            checkpoint('复制备份文件：' + path.name)
            before = path.stat()
            target = dest/path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise BackupError('备份期间文件发生变化，请稍后重试。')


def _logical_content(folder, include_originals):
    files = {}
    for path in sorted(folder.rglob('*')):
        _safe_source(path, folder)
        if path.is_file() and path.relative_to(folder).as_posix() not in ('manifest.json', MARKER, 'restored-config.yaml'):
            name = path.relative_to(folder).as_posix()
            if name != 'litprism.db':
                files[name] = {'size': path.stat().st_size, 'sha256': _sha(path)}
    return {'database': _database_content(folder/'litprism.db'), 'files': files,
            'include_originals': include_originals, 'secrets_included': False}


def create_backup(kb, cfg: Config, output, *, include_originals=False):
    output = Path(output).absolute()
    if output.exists():
        raise BackupError('备份文件已存在，请换一个文件名。')
    if cfg.data_path.resolve().is_relative_to(cfg.notes_path.resolve()):
        raise BackupError('笔记目录不能包含整个数据根，请先为笔记指定独立子目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    for root in (cfg.notes_path, cfg.data_path/'skills', cfg.data_path/'papers'):
        if output.resolve().is_relative_to(root.resolve()):
            raise BackupError('备份文件不能放入即将备份的笔记、技能或原文目录。')
    with tempfile.TemporaryDirectory(prefix='litprism-backup-') as temp:
        folder = Path(temp)
        with kb._tx_lock:
            if kb._con.in_transaction:
                raise BackupError('请在当前写入事务完成后备份。')
            dest = sqlite3.connect(folder/'litprism.db')
            try:
                kb._con.backup(dest, pages=256, progress=lambda *_: checkpoint('创建一致数据库快照'), sleep=.01)
                dest.execute('PRAGMA journal_mode=DELETE')
            finally:
                dest.close()
            (folder/'settings.json').write_bytes(_json(_portable_config(cfg)))
            _copy_tree(cfg.notes_path, folder/'notes')
            _copy_tree(cfg.data_path/'skills', folder/'skills')
            if include_originals:
                _copy_tree(cfg.data_path/'papers', folder/'papers')
        content = _logical_content(folder, bool(include_originals))
        manifest = {'format': FORMAT, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'content': content, 'content_hash': hashlib.sha256(_json(content)).hexdigest(), 'files': {}}
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                manifest['files'][path.relative_to(folder).as_posix()] = {'size': path.stat().st_size, 'sha256': _sha(path)}
        if len(manifest['files']) > MAX_FILES or sum(f['size'] for f in manifest['files'].values()) > MAX_BYTES:
            raise BackupError('备份超过当前文件数或总大小限制。')
        (folder/'manifest.json').write_bytes(_json(manifest))
        # Publish without overwriting an existing file, on the output filesystem.
        fd, tmp = tempfile.mkstemp(prefix='.litprism-backup-', suffix='.part', dir=output.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(folder.rglob('*')):
                    if path.is_file():
                        checkpoint('写入备份归档')
                        archive.write(path, path.relative_to(folder).as_posix())
            checkpoint()
            os.link(tmp, output)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return manifest


def _valid_name(info):
    name = info.filename
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute() or
            any(p in ('', '.', '..') for p in name.split('/')) or info.is_dir() or
            stat.S_ISLNK(info.external_attr >> 16)):
        raise BackupError('备份含不安全的文件路径或链接。')
    if name not in ('manifest.json', 'litprism.db', 'settings.json') and path.parts[0] not in ('notes', 'skills', 'papers'):
        raise BackupError('备份含不支持的文件。')
    if any(p in SECRET_NAMES for p in path.parts):
        raise BackupError('备份不得包含本机密钥文件。')
    # Windows aliases, reserved device paths, trailing dots/spaces are ambiguous.
    if any(p.endswith(('.', ' ')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', p) for p in path.parts):
        raise BackupError('备份含不兼容的 Windows 路径。')
    return name


def _unpack_verified(archive_path, folder):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1 or sum(i.file_size for i in infos) > MAX_BYTES:
                raise BackupError('备份超过当前文件数或总大小限制。')
            names = [_valid_name(info) for info in infos]
            if len(set(n.casefold() for n in names)) != len(names):
                raise BackupError('备份有重复或大小写冲突的文件路径。')
            if not {'manifest.json', 'litprism.db', 'settings.json'}.issubset(names):
                raise BackupError('备份缺少数据库、设置或清单。')
            if archive.getinfo('manifest.json').file_size > 8*1024**2:
                raise BackupError('备份清单过大。')
            if archive.getinfo('settings.json').file_size > 2*1024**2:
                raise BackupError('备份设置文件过大。')
            manifest = json.loads(archive.read('manifest.json'))
            if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or not isinstance(manifest.get('files'), dict):
                raise BackupError('不支持的备份格式。')
            if set(manifest['files']) != set(names)-{'manifest.json'}:
                raise BackupError('备份清单与文件列表不一致。')
            for name in names:
                if name == 'manifest.json':
                    continue
                entry = manifest['files'][name]
                if not isinstance(entry, dict) or entry.get('size') != archive.getinfo(name).file_size:
                    raise BackupError('备份文件大小校验失败。')
                target = folder/name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open('xb') as dest:
                    shutil.copyfileobj(source, dest, length=1024**2)
                checkpoint('验证备份文件：' + name)
                if _sha(target) != entry.get('sha256'):
                    raise BackupError('备份文件校验和不匹配。')
            content = manifest.get('content')
            if not isinstance(content, dict) or type(content.get('include_originals')) is not bool:
                raise BackupError('备份内容清单无效。')
            actual = _logical_content(folder, content['include_originals'])
            if content != actual or manifest.get('content_hash') != hashlib.sha256(_json(actual)).hexdigest():
                raise BackupError('备份逻辑内容校验失败。')
            settings = json.loads((folder/'settings.json').read_text(encoding='utf-8'))
            if _portable_config(_from_dict(settings)) != settings:
                raise BackupError('备份设置含密钥、机器路径或不支持的字段。')
            _validate(_from_dict(settings))
            (folder/'manifest.json').write_bytes(_json(manifest))
            return manifest
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        raise BackupError('备份损坏或清单格式无效。') from exc


def inspect_backup(archive_path):
    with tempfile.TemporaryDirectory(prefix='litprism-inspect-') as temp:
        return _unpack_verified(archive_path, Path(temp))


def _new_target(target, current_root=None):
    target = Path(target).expanduser()
    if not target.is_absolute() or not target.parent.is_dir():
        raise BackupError('请填写父目录已存在的新目录绝对路径。')
    if target.exists() or target.is_symlink():
        raise BackupError('目标目录已存在；恢复只允许写入新目录。')
    target = target.resolve()
    if current_root:
        current = Path(current_root).resolve()
        if target.is_relative_to(current) or current.is_relative_to(target):
            raise BackupError('目标不能位于当前数据目录内或覆盖其上级目录。')
    return target


def restore_backup(archive_path, target, *, current_root=None, expected_hash=None):
    target = _new_target(target, current_root)
    # Staging and destination share a filesystem; only the final rename is visible.
    with tempfile.TemporaryDirectory(prefix='.litprism-restore-', dir=target.parent) as temp:
        stage = Path(temp)/'data'
        stage.mkdir()
        manifest = _unpack_verified(archive_path, stage)
        if expected_hash and manifest['content_hash'] != expected_hash:
            raise BackupError('备份在预检后已变化，请重新预检。')
        settings = json.loads((stage/'settings.json').read_text(encoding='utf-8'))
        settings['app']['data_dir'] = str(target)
        (stage/'restored-config.yaml').write_bytes(_json(settings))  # JSON is valid YAML.
        (stage/MARKER).write_bytes(_json({'content_hash': manifest['content_hash'], 'credentials_excluded': True}))
        checkpoint('恢复校验完成，发布新目录')
        if target.exists():
            raise BackupError('目标目录已被创建，请换一个新目录。')
        stage.rename(target)
    return {'data_dir': str(target), 'content_hash': manifest['content_hash'],
            'config_path': str(target/'restored-config.yaml'), 'requires_restart': True,
            'note': '恢复已完成，原目录未修改；密钥未迁移，需要重新配置。'}


def migrate_data(kb, cfg, target, *, include_originals=True):
    target = _new_target(target, cfg.data_path)
    with tempfile.TemporaryDirectory(prefix='litprism-migrate-') as temp:
        archive = Path(temp)/'snapshot.zip'
        create_backup(kb, cfg, archive, include_originals=include_originals)
        return restore_backup(archive, target, current_root=cfg.data_path)


def _atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix='.litprism-switch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def activate_directory(cfg, target):
    """Only change the config pointer; callers keep the old live connection until restart."""
    import yaml
    if os.environ.get('LITPRISM_DATA_DIR'):
        raise BackupError('数据目录由 LITPRISM_DATA_DIR 覆盖，请先调整该环境变量再切换。')
    if not cfg.config_path or Path(cfg.config_path).name == 'config.example.yaml':
        raise BackupError('没有可写的用户配置文件，不能保存切换；可用恢复目录中的配置启动。')
    target = Path(target).resolve()
    if not (target/MARKER).is_file():
        raise BackupError('目标不是已校验的恢复目录。')
    manifest = json.loads((target/'manifest.json').read_text(encoding='utf-8'))
    actual = _logical_content(target, manifest['content']['include_originals'])
    if actual != manifest['content'] or hashlib.sha256(_json(actual)).hexdigest() != manifest['content_hash']:
        raise BackupError('恢复目录在预检后发生变化，请重新恢复后再切换。')
    path = Path(cfg.config_path).resolve()
    previous = path.read_bytes()
    # Use the hash-verified portable settings, never a separately edited helper file.
    raw = json.loads((target/'settings.json').read_text(encoding='utf-8'))
    raw['app']['data_dir'] = str(target)
    raw['export']['notes_dir'] = 'notes'
    _validate(_from_dict(raw))
    changed = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode('utf-8')
    _atomic_write(path, changed)
    return {'previous': previous, 'expected': hashlib.sha256(changed).hexdigest(), 'config_path': path,
            'data_dir': str(target)}


def cancel_activation(switch):
    path = switch['config_path']
    if _sha(path) != switch['expected']:
        raise BackupError('配置已被其他操作修改，不能覆盖；请手工核对数据目录。')
    _atomic_write(path, switch['previous'])


def require_restored_credentials(cfg):
    if ((cfg.data_path/MARKER).is_file() and
            ((cfg.llm.provider or '').lower() == 'mock' or
             (not cfg.llm.api_key and cfg.llm.provider != 'ollama'))):
        raise BackupError('此目录由备份恢复，密钥未迁移。请重新配置提供商密钥并选择模型；不会自动使用 mock。')
