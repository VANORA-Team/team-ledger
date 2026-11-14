"""Versioned full backups, validated restore and a persisted in-app schedule."""
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import zipfile
import zlib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import config
from .backup_storage import StorageError, remote, valid_name
from .db import SCHEMA, db, init_db
from .locks import data_guard, file_lock

log = logging.getLogger(__name__)
TARGETS = ('local', 'webdav', 's3')
DEFAULT = {
    'webdav': {'url': '', 'username': '', 'password': '', 'prefix': 'team-ledger'},
    's3': {'endpoint': '', 'region': 'us-east-1', 'bucket': '', 'access_key_id': '',
           'secret_access_key': '', 'prefix': 'team-ledger', 'path_style': True},
    'schedule': {'enabled': False, 'frequency': 'daily', 'time': '03:00',
                 'weekday': 0, 'targets': ['local']},
    'keep': 30,
}
RECEIPT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}')
# Pending actions must be confirmed afresh after restoring a historical ledger.
EPHEMERAL = ('sessions', 'payment_quotes', 'link_codes', 'feishu_link_codes',
             'ai_plans', 'tg_drafts', 'tg_outbox', 'tg_prompts', 'fsui_drafts', 'fsui_outbox',
             'fsui_prompts', 'fsui_actions', 'feishu_pending', 'feishu_outbox')


class BackupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def max_bytes():
    return int(os.getenv('BACKUP_MAX_BYTES', str(256 * 1024 * 1024)))


def utcnow():
    return datetime.now(timezone.utc)


def _read_control():
    path = config.DATA / 'backup-control.json'
    if not path.exists():
        return {'config': copy.deepcopy(DEFAULT), 'status': {}}
    return json.loads(path.read_text(encoding='utf-8'))


def _write_control(value):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config.DATA,
                                     prefix='.backup-config-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if os.name != 'nt':
            temporary.chmod(0o600)
        os.replace(temporary, config.DATA / 'backup-control.json')
    finally:
        temporary.unlink(missing_ok=True)


def read_control():
    with file_lock('.backup-config.lock'):
        return _read_control()


def _status(**values):
    with file_lock('.backup-config.lock'):
        control = _read_control()
        control['status'].update(values)
        _write_control(control)


def public_config():
    control = read_control()
    conf = control['config']
    conf['webdav']['has_password'] = bool(conf['webdav'].pop('password', ''))
    conf['s3']['has_secret_access_key'] = bool(conf['s3'].pop('secret_access_key', ''))
    return {**conf, 'status': control['status'], 'timezone': config.TIMEZONE,
            'max_upload_bytes': max_bytes()}


def _text(value, label, maximum=500):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise BackupError(label + '格式不正确')
    return value.strip()


def _url(value, label, optional=False):
    value = _text(value, label)
    if not value and optional:
        return value
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed ports before persisting a URL.
    except ValueError:
        raise BackupError(label + '格式不正确') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise BackupError(label + '需为不含账号、查询参数的 HTTP / HTTPS 地址')
    return value.rstrip('/')


def _prefix(value):
    value = _text(value, '备份目录', 200).strip('/')
    if any(p in ('.', '..', '') or '\\' in p for p in value.split('/')) and value:
        raise BackupError('备份目录不能包含空层级、反斜线或相对路径')
    return value


def require_target(target, conf):
    if target not in TARGETS:
        raise BackupError('备份存储方式不正确')
    if target == 'webdav' and not all(conf['webdav'][k] for k in ('url', 'username', 'password')):
        raise BackupError('请先填写并保存 WebDAV 地址、用户名和密码')
    if target == 's3' and not all(conf['s3'][k] for k in ('bucket', 'access_key_id', 'secret_access_key')):
        raise BackupError('请先填写并保存 S3 存储桶和访问密钥')


def next_run(schedule, after=None):
    after = after or utcnow()
    local = after.astimezone(ZoneInfo(config.TIMEZONE))
    hour, minute = map(int, schedule['time'].split(':'))
    if schedule['frequency'] == 'hourly':
        candidate = (local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if schedule['frequency'] == 'weekly':
            candidate += timedelta(days=(schedule['weekday'] - local.weekday()) % 7)
            if candidate <= local:
                candidate += timedelta(days=7)
        elif candidate <= local:
            candidate += timedelta(days=1)
    result = candidate.astimezone(timezone.utc)
    # In a DST fold the next local hour can map back to an elapsed instant.
    if result <= after:
        result = after + timedelta(hours=1)
    return result.isoformat(timespec='seconds')


def save_config(body):
    if not isinstance(body, dict):
        raise BackupError('备份配置格式不正确')
    with file_lock('.backup-config.lock'):
        control = _read_control()
        conf = control['config']
        previous = copy.deepcopy(conf['schedule'])
        for section in ('webdav', 's3'):
            incoming = body.get(section, {})
            if not isinstance(incoming, dict):
                raise BackupError('存储配置格式不正确')
            for key in DEFAULT[section]:
                if key not in incoming:
                    continue
                value = incoming[key]
                if key in ('password', 'secret_access_key'):
                    if not isinstance(value, str) or len(value) > 2000 or '\r' in value or '\n' in value:
                        raise BackupError('密码或密钥格式不正确')
                    if value:
                        conf[section][key] = value
                elif key == 'path_style':
                    if type(value) is not bool:
                        raise BackupError('S3 路径风格格式不正确')
                    conf[section][key] = value
                elif key in ('url', 'endpoint'):
                    conf[section][key] = _url(value, '服务地址', optional=True)
                elif key == 'prefix':
                    conf[section][key] = _prefix(value)
                else:
                    conf[section][key] = _text(value, key, 200)
        if conf['s3']['bucket'] and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,254}', conf['s3']['bucket']):
            raise BackupError('S3 存储桶名称不正确')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', conf['s3']['region']):
            raise BackupError('S3 区域格式不正确')
        if 'keep' in body:
            if type(body['keep']) is not int or not 1 <= body['keep'] <= 1000:
                raise BackupError('备份保留份数需为 1–1000')
            conf['keep'] = body['keep']
        if 'schedule' in body:
            incoming = body['schedule']
            if not isinstance(incoming, dict):
                raise BackupError('自动备份配置格式不正确')
            schedule = {**conf['schedule'], **{k: v for k, v in incoming.items() if k in DEFAULT['schedule']}}
            if type(schedule['enabled']) is not bool or schedule['frequency'] not in ('hourly', 'daily', 'weekly'):
                raise BackupError('自动备份频率不正确')
            if not isinstance(schedule['time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule['time']):
                raise BackupError('备份时间需为 HH:mm')
            if type(schedule['weekday']) is not int or not 0 <= schedule['weekday'] <= 6:
                raise BackupError('备份星期不正确')
            targets = schedule['targets']
            if not isinstance(targets, list) or not targets or any(t not in TARGETS for t in targets) or len(set(targets)) != len(targets):
                raise BackupError('请至少选择一个有效的自动备份位置')
            if schedule['enabled']:
                for target in targets:
                    require_target(target, conf)
            conf['schedule'] = schedule
        if conf['schedule'] != previous or not control['status'].get('next_run'):
            control['status']['next_run'] = next_run(conf['schedule']) if conf['schedule']['enabled'] else None
        _write_control(control)
    return public_config()


def local_folder():
    folder = config.DATA / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def local_path(name):
    if not valid_name(name):
        raise BackupError('备份文件名不正确')
    path = local_folder() / name
    if not path.is_file() or path.is_symlink():
        raise BackupError('备份文件不存在', 404)
    return path


def list_backups(target='local'):
    conf = read_control()['config']
    require_target(target, conf)
    if target == 'local':
        return sorted([{'name': p.name, 'size': p.stat().st_size,
                        'modified_at': datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
                       for p in local_folder().iterdir() if p.is_file() and not p.is_symlink() and valid_name(p.name)],
                      key=lambda p: p['modified_at'], reverse=True)
    try:
        return remote(target, conf).list()
    except StorageError as exc:
        raise BackupError(str(exc), 502) from None


def _name(prefix='team-ledger'):
    return f'{prefix}-{utcnow():%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}.zip'


def _receipt_name(name):
    return isinstance(name, str) and RECEIPT.fullmatch(name) is not None and '..' not in name


def _snapshot(output, work):
    """Hold the data lock through snapshot + receipts to keep their references consistent."""
    with data_guard(exclusive=True):
        database = work / 'ledger.sqlite3'
        with db() as source:
            with closing(sqlite3.connect(database)) as destination:
                source.backup(destination)
        files = {'ledger.sqlite3': database}
        with closing(sqlite3.connect(database)) as conn:
            receipts = [r[0] for r in conn.execute("SELECT DISTINCT receipt FROM entries WHERE receipt IS NOT NULL AND receipt<>''")]
        for name in receipts:
            path = config.DATA / 'receipts' / name
            if not _receipt_name(name) or not path.is_file() or path.is_symlink():
                raise BackupError('账本引用的凭证文件缺失或无效，备份已中止', 409)
            files['receipts/' + name] = path
        manifest = {'format': 'team-ledger-backup', 'version': 1,
                    'created_at': utcnow().isoformat(timespec='seconds'), 'files': {}}
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, path in files.items():
                digest = hashlib.sha256()
                size = 0
                with path.open('rb') as stream, archive.open(name, 'w', force_zip64=True) as member:
                    while chunk := stream.read(1024 * 1024):
                        member.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                manifest['files'][name] = {'size': size, 'sha256': digest.hexdigest()}
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        if output.stat().st_size > max_bytes():
            raise BackupError('备份文件超过服务器允许的大小，请调整 BACKUP_MAX_BYTES', 413)


def _prune(target, conf, preserve=None):
    items = list_backups(target) if target == 'local' else remote(target, conf).list()
    regular = sorted([i for i in items if i['name'].startswith('team-ledger-')],
                     key=lambda i: i['name'], reverse=True)
    regular.sort(key=lambda i: i['name'] != preserve)
    for item in regular[conf['keep']:]:
        if target == 'local':
            local_path(item['name']).unlink(missing_ok=True)
        else:
            remote(target, conf).delete(item['name'])
    # Keep a separate bounded set of automatic safety copies.
    if target == 'local':
        safety = sorted([i for i in items if i['name'].startswith('before-restore-')],
                        key=lambda i: i['name'], reverse=True)
        safety.sort(key=lambda i: i['name'] != preserve)
        for item in safety[conf['keep']:]:
            local_path(item['name']).unlink(missing_ok=True)


def _perform_backup(targets, conf, automatic=False):
    name = _name()
    _status(last_attempt=utcnow().isoformat(timespec='seconds'), last_error=None, last_results={})
    results, errors = {}, []
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=config.DATA) as directory:
        work = Path(directory)
        output = work / name
        _snapshot(output, work)
        os.replace(output, local_folder() / name)
    results['local'] = 'ok'
    for target in targets:
        if target == 'local':
            continue
        try:
            remote(target, conf).upload(name, local_path(name))
            results[target] = 'ok'
        except StorageError as exc:
            results[target] = str(exc)
            errors.append(str(exc))
    for target, result in list(results.items()):
        if result == 'ok':
            try:
                _prune(target, conf, preserve=name)
            except (BackupError, StorageError, OSError):
                errors.append(f'{target} 备份已保存，但旧备份清理失败')
    values = {'last_name': name, 'last_results': results, 'last_automatic': automatic,
              'last_error': '；'.join(errors) or None}
    if not errors:
        values['last_success'] = utcnow().isoformat(timespec='seconds')
    _status(**values)
    if errors:
        raise BackupError('本地备份已保存；' + '；'.join(errors), 502)
    return {'name': name, 'targets': list(results), 'size': local_path(name).stat().st_size}


def create_backup(targets):
    conf = read_control()['config']
    if not isinstance(targets, list) or not targets or any(t not in TARGETS for t in targets):
        raise BackupError('请指定有效的备份位置')
    for target in targets:
        require_target(target, conf)
    try:
        with file_lock('.backup-operation.lock', timeout=0):
            return _perform_backup(list(dict.fromkeys(targets)), conf)
    except TimeoutError:
        raise BackupError('已有备份或恢复任务正在执行，请稍后重试', 409) from None
    except (OSError, sqlite3.Error):
        _status(last_error='创建备份失败，请检查服务器存储空间和目录权限')
        raise BackupError('创建备份失败，请检查服务器存储空间和目录权限', 500) from None
    except BackupError as exc:
        _status(last_error=str(exc))
        raise


def test_storage(target):
    conf = read_control()['config']
    require_target(target, conf)
    try:
        with file_lock('.backup-operation.lock', timeout=0):
            # A tiny invalid-as-backup probe verifies read, write, list and delete.
            name = _name()
            with tempfile.TemporaryDirectory(prefix='.backup-test-', dir=config.DATA) as directory:
                path = Path(directory) / name
                path.write_bytes(b'team-ledger storage connection test')
                if target == 'local':
                    probe = local_folder() / ('.probe-' + secrets.token_hex(8))
                    try:
                        probe.write_bytes(path.read_bytes())
                        if probe.read_bytes() != path.read_bytes():
                            raise BackupError('本地备份目录读写校验失败')
                    finally:
                        probe.unlink(missing_ok=True)
                else:
                    store = remote(target, conf)
                    try:
                        store.upload(name, path)
                        if name not in {i['name'] for i in store.list()}:
                            raise StorageError('连接成功，但无法列出刚写入的测试文件')
                        downloaded = Path(directory) / 'probe'
                        store.download(name, downloaded, 1024)
                        if downloaded.read_bytes() != path.read_bytes():
                            raise StorageError('远程文件读写校验失败')
                    finally:
                        store.delete(name)
    except TimeoutError:
        raise BackupError('已有备份或恢复任务正在执行', 409) from None
    except StorageError as exc:
        raise BackupError(str(exc), 502) from None
    return {'ok': True}


def _validate_database(path, receipt_names):
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.execute('PRAGMA trusted_schema=OFF')
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise BackupError('备份数据库完整性校验失败')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') OR upper(sql) LIKE '%CREATE VIRTUAL%'").fetchone():
                raise BackupError('备份数据库包含不支持的结构')
            with closing(sqlite3.connect(':memory:')) as expected:
                expected.executescript(SCHEMA)
                expected.execute('ALTER TABLE entries ADD COLUMN reimbursed_minor INTEGER NOT NULL DEFAULT 0')
                for (table,) in expected.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    columns = {r[1] for r in expected.execute(f'PRAGMA table_info("{table}")')}
                    actual = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                    if not columns.issubset(actual):
                        raise BackupError('备份数据库结构不受支持，请使用兼容版本的备份')
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise BackupError('备份数据库包含缺失的关联记录')
            if not conn.execute("SELECT 1 FROM users WHERE active=1 AND role='owner'").fetchone():
                raise BackupError('备份中没有可登录的团队成员')
            for (value,) in conn.execute('SELECT value FROM settings'):
                json.loads(value)
            for (name,) in conn.execute("SELECT DISTINCT receipt FROM entries WHERE receipt IS NOT NULL AND receipt<>''"):
                if not _receipt_name(name) or 'receipts/' + name not in receipt_names:
                    raise BackupError('备份缺少账目引用的凭证文件')
    except (sqlite3.Error, ValueError, TypeError):
        raise BackupError('备份数据库无效或已损坏') from None


def _unpack(path, destination):
    if path.stat().st_size > max_bytes():
        raise BackupError('备份文件超过服务器允许的大小', 413)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(names) > 50000 or len(set(names)) != len(names) or 'manifest.json' not in names:
                raise BackupError('备份清单缺失、重复或文件数量过多')
            if sum(i.file_size for i in infos) > max_bytes() * 4 or any(i.flag_bits & 1 for i in infos):
                raise BackupError('备份解压大小超出限制或使用了不支持的加密格式')
            if archive.getinfo('manifest.json').file_size > 8 * 1024 * 1024:
                raise BackupError('备份清单过大')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('format') != 'team-ledger-backup' or type(manifest.get('version')) is not int or manifest['version'] != 1:
                raise BackupError('不是受支持的一起记备份文件')
            files = manifest['files']
            if not isinstance(files, dict) or set(files) != set(names) - {'manifest.json'} or 'ledger.sqlite3' not in files:
                raise BackupError('备份文件与清单不一致')
            for name, metadata in files.items():
                if name != 'ledger.sqlite3' and not (name.startswith('receipts/') and _receipt_name(name[9:])):
                    raise BackupError('备份包含不允许的文件路径')
                if type(metadata.get('size')) is not int or metadata['size'] != archive.getinfo(name).file_size:
                    raise BackupError('备份文件大小与清单不一致')
                output = destination / name
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.open(name) as stream, output.open('wb') as handle:
                    while chunk := stream.read(1024 * 1024):
                        handle.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != metadata.get('sha256'):
                    raise BackupError('备份文件校验失败，文件可能损坏或被修改')
        _validate_database(destination / 'ledger.sqlite3', set(files))
        return manifest
    except (zipfile.BadZipFile, zlib.error, json.JSONDecodeError, KeyError, TypeError, AttributeError, RuntimeError, NotImplementedError):
        raise BackupError('备份格式无效、损坏或不受支持') from None


def _install(database, destination):
    with closing(sqlite3.connect(database)) as source:
        with closing(sqlite3.connect(destination, timeout=15)) as target:
            source.backup(target)


def _restore(path, work):
    restored = work / 'restored'
    restored.mkdir()
    manifest = _unpack(path, restored)
    (restored / 'receipts').mkdir(exist_ok=True)
    with closing(sqlite3.connect(restored / 'ledger.sqlite3')) as conn:
        with conn:
            for table in EPHEMERAL:
                conn.execute(f'DELETE FROM "{table}"')
            conn.execute('DELETE FROM feishu_inbox WHERE done=0')
    with data_guard(exclusive=True):
        safety = _name('before-restore')
        safety_work = work / 'safety'
        safety_work.mkdir()
        safety_path = local_folder() / safety
        pending = work / safety
        _snapshot(pending, safety_work)
        os.replace(pending, safety_path)
        receipts = config.DATA / 'receipts'
        previous = work / 'previous-receipts'
        had_receipts = receipts.exists()
        if had_receipts:
            os.replace(receipts, previous)
        try:
            os.replace(restored / 'receipts', receipts)
            # Restore into the existing SQLite file. Replacing/unlinking the live
            # file would strand WAL connections in other web or bot processes.
            _install(restored / 'ledger.sqlite3', config.DB_PATH)
            init_db()
        except Exception:
            _install(safety_work / 'ledger.sqlite3', config.DB_PATH)
            if receipts.exists():
                os.replace(receipts, work / 'failed-receipts')
            if had_receipts:
                os.replace(previous, receipts)
            raise
    try:
        _status(last_restore=utcnow().isoformat(timespec='seconds'), restore_safety_copy=safety)
    except OSError:
        # The ledger has already been installed successfully; a status write must
        # not report it as a failed restore or leave the UI signed in.
        log.exception('Could not persist restore status')
    try:
        _prune('local', read_control()['config'], preserve=safety)
    except (BackupError, OSError):
        log.exception('Could not prune old safety copies after restore')
    return {'ok': True, 'created_at': manifest.get('created_at'), 'safety_copy': safety,
            'requires_login': True}


def restore_backup(target=None, name=None, uploaded=None):
    try:
        with file_lock('.backup-operation.lock', timeout=0):
            with tempfile.TemporaryDirectory(prefix='.restore-', dir=config.DATA) as directory:
                work = Path(directory)
                if uploaded is not None:
                    path = uploaded
                else:
                    conf = read_control()['config']
                    require_target(target, conf)
                    if not valid_name(name):
                        raise BackupError('备份文件名不正确')
                    if target == 'local':
                        path = local_path(name)
                    else:
                        path = work / 'download.zip'
                        remote(target, conf).download(name, path, max_bytes())
                return _restore(path, work)
    except TimeoutError:
        raise BackupError('已有备份或恢复任务正在执行，请稍后重试', 409) from None
    except StorageError as exc:
        raise BackupError(str(exc), 502) from None
    except (OSError, sqlite3.Error):
        raise BackupError('恢复失败，已尝试回退原账本，请检查服务器日志和存储空间', 500) from None


def scheduler_tick():
    try:
        with file_lock('.backup-operation.lock', timeout=0):
            control = read_control()
            conf, status = control['config'], control['status']
            if not conf['schedule']['enabled']:
                return
            due = status.get('next_run')
            if not due:
                _status(next_run=next_run(conf['schedule']))
                return
            if datetime.fromisoformat(due) > utcnow() and not status.get('scheduled_running'):
                return
            # Persist the claim while holding the operation lock across all workers.
            # An interrupted run remains retryable after a process restart.
            _status(next_run=next_run(conf['schedule']), scheduled_running=True)
            try:
                _perform_backup(conf['schedule']['targets'], conf, automatic=True)
                _status(scheduled_running=False)
            except Exception as exc:
                message = str(exc) if isinstance(exc, BackupError) else '自动备份失败，请检查存储配置和服务器空间'
                _status(last_error=message, scheduled_running=False,
                        next_run=(utcnow() + timedelta(minutes=5)).isoformat(timespec='seconds'))
                log.exception('Scheduled backup failed')
    except TimeoutError:
        return


class Scheduler:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='ledger-backups', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(15):
            try:
                scheduler_tick()
            except Exception:
                log.exception('Backup scheduler tick failed')

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
