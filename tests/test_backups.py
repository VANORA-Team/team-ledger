import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, unquote, urlsplit

from fastapi.testclient import TestClient

from app import backups, config
from app.backup_storage import S3, WebDAV, StorageError
from app.db import db, remove_receipt_file
from app.main import app, attempts
from tests.oidc_helper import FakeVanoraOidc


class MemoryStorage:
    def __init__(self):
        self.files = {}

    def upload(self, name, path):
        self.files[name] = path.read_bytes()

    def download(self, name, destination, maximum):
        data = self.files[name]
        if len(data) > maximum:
            raise StorageError('备份文件超过服务器允许的大小')
        destination.write_bytes(data)

    def list(self):
        return [{'name': name, 'size': len(data), 'modified_at': '2026-11-14T00:00:00Z'}
                for name, data in self.files.items()]

    def delete(self, name):
        self.files.pop(name, None)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ledger-backup-test-', dir=os.getenv('LEDGER_TEST_TMP'))
        self.old_data, self.old_path = config.DATA, config.DB_PATH
        config.DATA = Path(self.temporary.name)
        config.DB_PATH = config.DATA / 'test.sqlite3'
        attempts.clear()
        self.oidc = FakeVanoraOidc().start()
        self.client = TestClient(app)
        self.client.__enter__()
        session = self.oidc.login(
            self.client,
            sub='vanora-backup-owner',
            preferred_username='owner',
            name='测试成员',
            roles=('owner',),
            team_name='备份测试',
        )
        self.headers = {'X-CSRF-Token': session['csrf']}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.oidc.stop()
        config.DATA, config.DB_PATH = self.old_data, self.old_path
        self.temporary.cleanup()

    def post(self, path, payload):
        return self.client.post('/api' + path, json=payload, headers=self.headers)

    def save(self, payload):
        return self.client.put('/api/backups/config', json=payload, headers=self.headers)

    def entry(self, description='备份前'):
        response = self.client.post('/api/entries', headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
                                    json={'kind': 'expense', 'amount': '12.34', 'currency': 'CNY',
                                          'description': description, 'date': config.today()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def backup(self, target='local'):
        response = self.post('/backups', {'target': target})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['name']

    def configure_remote(self):
        response = self.save({
            'webdav': {'url': 'https://dav.example.test/root', 'username': 'tester', 'password': 'test-dav-password'},
            's3': {'endpoint': 'https://s3.example.test', 'bucket': 'test-bucket',
                   'access_key_id': 'test-id', 'secret_access_key': 'test-s3-secret'}})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def login(self):
        session = self.oidc.login(
            self.client,
            sub='vanora-backup-owner',
            preferred_username='owner',
            name='测试成员',
            roles=('owner',),
        )
        self.headers = {'X-CSRF-Token': session['csrf']}

    def test_local_roundtrip_receipts_settings_and_sessions(self):
        entry = self.entry()
        self.oidc.set_user('vanora-backup-finance', username='finance', name='财务', roles=('finance',))
        self.assertEqual(self.post('/members/sync', {}).status_code, 200)
        member = next(m for m in self.client.get('/api/members').json() if m.get('oidc_sub') == 'vanora-backup-finance')
        receipt = b'%PDF-1.4\nbackup receipt'
        response = self.post(f"/entries/{entry['id']}/receipt",
                             {'name': '测试.pdf', 'data': base64.b64encode(receipt).decode()})
        self.assertEqual(response.status_code, 200, response.text)
        with db(write=True) as conn:
            conn.execute('INSERT INTO tg_links VALUES (?,?)', ('12345', 1))
            conn.execute('INSERT INTO feishu_config(id,app_id,app_secret,version,domain) VALUES (1,?,?,1,?)',
                         ('app-test', 'db-secret', 'lark'))
        name = self.backup()
        downloaded = self.client.get('/api/backups/download/' + name)
        self.assertEqual(downloaded.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['version'], 1)
            self.assertEqual(len(manifest['files']), 2)
            for path, info in manifest['files'].items():
                data = archive.read(path)
                self.assertEqual(info['size'], len(data))
                self.assertEqual(info['sha256'], hashlib.sha256(data).hexdigest())
            self.assertNotIn('backup-control.json', archive.namelist())
        self.entry('备份后')
        self.oidc.set_user('vanora-backup-finance', roles=('member',))
        self.assertEqual(self.post('/members/sync', {}).status_code, 200)
        self.client.put('/api/settings', json={'team_name': '后来的团队'}, headers=self.headers)
        response = self.post('/backups/restore', {'name': name, 'target': 'local', 'confirm': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['requires_login'])
        self.assertTrue(backups.local_path(response.json()['safety_copy']).is_file())
        self.assertEqual(self.client.get('/api/session').status_code, 401)
        self.login()
        self.assertEqual(len(self.client.get('/api/entries').json()), 1)
        self.assertEqual(self.client.get('/api/settings').json()['team_name'], '备份测试')
        self.assertEqual(self.client.get(f"/api/entries/{entry['id']}/receipt").content, receipt)
        # Authoritative Vanora-oauth role wins after restore + sync (changed to 'member' above)
        self.assertEqual(self.post('/members/sync', {}).status_code, 200)
        restored_members = {item['id']: item for item in self.client.get('/api/members').json()}
        self.assertEqual(restored_members[member['id']]['role'], 'member')
        with db() as conn:
            self.assertEqual(conn.execute('SELECT telegram_id FROM tg_links').fetchone()[0], '12345')
            self.assertEqual(conn.execute('SELECT app_secret,domain FROM feishu_config').fetchone()[:], ('db-secret', 'lark'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)

    def test_upload_restore_preserves_destination_backup_settings(self):
        self.entry()
        name = self.backup()
        archive = backups.local_path(name).read_bytes()
        self.save({'keep': 7})
        self.entry('稍后')
        response = self.client.post('/api/backups/restore/upload', content=archive,
                                    headers={**self.headers, 'Content-Type': 'application/zip',
                                             'X-Backup-Confirm': 'restore'})
        self.assertEqual(response.status_code, 200, response.text)
        self.login()
        self.assertEqual(len(self.client.get('/api/entries').json()), 1)
        self.assertEqual(self.client.get('/api/backups/config').json()['keep'], 7)

    def test_ai_config_roundtrip_and_legacy_backup_without_ai_config_table(self):
        saved = self.client.put('/api/ai/config', json={
            'enabled': True, 'base_url': 'https://ai.backup.test/v1',
            'api_key': 'sk-backup-secret', 'model': 'backup-model', 'timeout': 55,
        }, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        name = self.backup()
        self.assertEqual(self.client.delete('/api/ai/config', headers=self.headers).status_code, 200)
        self.assertFalse(self.client.get('/api/ai/config').json()['configured'])
        restored = self.post('/backups/restore', {'name': name, 'target': 'local', 'confirm': True})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.login()
        ai_cfg = self.client.get('/api/ai/config').json()
        self.assertTrue(ai_cfg['configured'])
        self.assertEqual(ai_cfg['base_url'], 'https://ai.backup.test/v1')
        self.assertEqual(ai_cfg['model'], 'backup-model')
        self.assertEqual(ai_cfg['timeout'], 55)

        original = backups.local_path(name).read_bytes()
        with zipfile.ZipFile(io.BytesIO(original)) as source:
            files = {n: source.read(n) for n in source.namelist()}
        manifest = json.loads(files.pop('manifest.json'))
        legacy_db = config.DATA / 'legacy.sqlite3'
        legacy_db.write_bytes(files['ledger.sqlite3'])
        with closing(sqlite3.connect(legacy_db)) as conn:
            with conn:
                conn.execute('DROP TABLE ai_config')
                conn.execute('DROP TABLE IF EXISTS member_events')
        files['ledger.sqlite3'] = legacy_db.read_bytes()
        manifest['files']['ledger.sqlite3'] = {
            'size': len(files['ledger.sqlite3']),
            'sha256': hashlib.sha256(files['ledger.sqlite3']).hexdigest(),
        }
        legacy_zip = io.BytesIO()
        with zipfile.ZipFile(legacy_zip, 'w') as archive:
            for key, data in files.items():
                archive.writestr(key, data)
            archive.writestr('manifest.json', json.dumps(manifest))
        response = self.client.post('/api/backups/restore/upload', content=legacy_zip.getvalue(),
                                    headers={**self.headers, 'Content-Type': 'application/zip',
                                             'X-Backup-Confirm': 'restore'})
        self.assertEqual(response.status_code, 200, response.text)
        self.login()
        with db() as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_config'").fetchone()
        self.assertIsNotNone(row)

    def test_legacy_pre_oidc_backup_restore_and_vanora_resync(self):
        self.entry('旧版账目')
        name = self.backup()
        original = backups.local_path(name).read_bytes()
        with zipfile.ZipFile(io.BytesIO(original)) as source:
            files = {n: source.read(n) for n in source.namelist()}
        manifest = json.loads(files.pop('manifest.json'))
        legacy_db = config.DATA / 'legacy-pre-oidc.sqlite3'
        legacy_db.write_bytes(files['ledger.sqlite3'])
        with closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('PRAGMA legacy_alter_table=ON')
            with conn:
                for table in ('oidc_flows', 'oidc_invites', 'oidc_revoked_jti'):
                    conn.execute(f'DROP TABLE IF EXISTS {table}')
                conn.execute('DROP INDEX IF EXISTS users_oidc_identity')
                conn.execute('DROP INDEX IF EXISTS sessions_oidc_sid')
                conn.execute('ALTER TABLE users RENAME TO users_oidc')
                conn.execute('''CREATE TABLE users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)''')
                conn.execute(
                    "INSERT INTO users(id,username,name,password_hash,role,active,created_at) "
                    "SELECT id,username,name,'pbkdf2_sha256$legacy',role,active,created_at FROM users_oidc"
                )
                conn.execute('DROP TABLE users_oidc')
                conn.execute('ALTER TABLE sessions RENAME TO sessions_oidc')
                conn.execute('''CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires INTEGER NOT NULL)''')
                conn.execute('DROP TABLE sessions_oidc')
            conn.execute('PRAGMA legacy_alter_table=OFF')
        files['ledger.sqlite3'] = legacy_db.read_bytes()
        manifest['files']['ledger.sqlite3'] = {
            'size': len(files['ledger.sqlite3']),
            'sha256': hashlib.sha256(files['ledger.sqlite3']).hexdigest(),
        }
        legacy_zip = io.BytesIO()
        with zipfile.ZipFile(legacy_zip, 'w') as archive:
            for key, data in files.items():
                archive.writestr(key, data)
            archive.writestr('manifest.json', json.dumps(manifest))

        response = self.client.post('/api/backups/restore/upload', content=legacy_zip.getvalue(),
                                    headers={**self.headers, 'Content-Type': 'application/zip',
                                             'X-Backup-Confirm': 'restore'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/session').status_code, 401)

        with db() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'oidc_flows', 'oidc_revoked_jti'}.issubset(tables))
            user_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
            self.assertTrue({'oidc_issuer', 'oidc_sub', 'oidc_email'}.issubset(user_cols))
            session_cols = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
            self.assertIn('oidc_sid', session_cols)

        # Authorized Vanora owner can sign in immediately and see all restored legacy entries
        session = self.oidc.login(
            self.client,
            sub='vanora-migrated-owner',
            preferred_username='owner_new',
            name='新同步所有者',
            roles=('owner',),
        )
        self.headers = {'X-CSRF-Token': session['csrf']}
        entries = self.client.get('/api/entries').json()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['description'], '旧版账目')

    def test_remote_backup_restore_download_and_connection_probe(self):
        self.configure_remote()
        stores = {target: MemoryStorage() for target in ('webdav', 's3')}
        with patch('app.backups.remote', side_effect=lambda target, conf: stores[target]), \
             patch('app.backup_api.remote', side_effect=lambda target, conf: stores[target]):
            for target in stores:
                with self.subTest(target=target):
                    self.assertEqual(self.post('/backups/test', {'target': target}).status_code, 200)
                    self.assertEqual(stores[target].files, {})
                    name = self.backup(target)
                    self.assertIn(name, stores[target].files)
                    listed = self.client.get('/api/backups', params={'target': target})
                    self.assertEqual(listed.json()[0]['name'], name)
                    download = self.client.get('/api/backups/download/' + name, params={'target': target})
                    self.assertEqual(download.content, stores[target].files[name])
                    self.entry('远程备份之后')
                    response = self.post('/backups/restore', {'target': target, 'name': name, 'confirm': True})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.login()
                    self.assertEqual(self.client.get('/api/entries').json(), [])

    def test_secrets_are_redacted_and_blank_values_preserve_them(self):
        response = self.configure_remote()
        self.assertNotIn('test-dav-password', response.text)
        self.assertNotIn('test-s3-secret', response.text)
        self.assertTrue(response.json()['webdav']['has_password'])
        self.assertTrue(response.json()['s3']['has_secret_access_key'])
        response = self.save({'webdav': {'password': ''}, 's3': {'secret_access_key': ''}})
        self.assertTrue(response.json()['webdav']['has_password'])
        self.assertEqual(backups.read_control()['config']['s3']['secret_access_key'], 'test-s3-secret')
        self.assertNotIn('backup', self.client.get('/api/settings').text.lower())

    def test_auth_csrf_confirmation_limits_and_invalid_config(self):
        self.assertEqual(self.client.post('/api/backups', json={'target': 'local'}).status_code, 403)
        self.assertEqual(self.post('/backups/restore', {'name': 'none'}).status_code, 400)
        self.assertEqual(self.post('/backups', {'target': 'unknown'}).status_code, 400)
        self.assertEqual(self.post('/backups', {'target': 's3'}).status_code, 400)
        for payload in ({'keep': 0}, {'keep': True}, {'schedule': {'time': '25:00'}},
                        {'schedule': {'targets': []}}, {'webdav': {'prefix': '../escape'}},
                        {'webdav': {'url': 'https://name:pass@example.com/'}},
                        {'s3': {'endpoint': 'file:///etc/passwd'}},
                        {'schedule': {'enabled': True, 'targets': ['webdav']}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.save(payload).status_code, 400)
        self.assertEqual(self.client.get('/api/backups/download/invalid.zip').status_code, 400)
        response = self.client.post('/api/backups/restore/upload', content=b'12345',
                                    headers={**self.headers, 'X-Backup-Confirm': 'restore'})
        self.assertEqual(response.status_code, 400)
        with patch('app.backups.max_bytes', return_value=4):
            response = self.client.post('/api/backups/restore/upload', content=b'12345',
                                        headers={**self.headers, 'X-Backup-Confirm': 'restore'})
            self.assertEqual(response.status_code, 413)
        self.post('/logout', {})
        self.assertEqual(self.client.get('/api/backups').status_code, 401)

    def test_corrupt_and_unsafe_archives_leave_current_data_and_session_intact(self):
        self.entry()
        name = self.backup()
        original = backups.local_path(name).read_bytes()
        for case in ('checksum', 'traversal', 'missing-receipt', 'future-version', 'duplicate', 'bad-schema'):
            with self.subTest(case=case):
                with zipfile.ZipFile(io.BytesIO(original)) as source:
                    files = {n: source.read(n) for n in source.namelist()}
                manifest = json.loads(files.pop('manifest.json'))
                if case == 'checksum':
                    files['ledger.sqlite3'] += b'bad'
                elif case == 'traversal':
                    files['../escape'] = b'bad'
                    manifest['files']['../escape'] = {'size': 3, 'sha256': hashlib.sha256(b'bad').hexdigest()}
                elif case == 'missing-receipt':
                    database = config.DATA / 'tampered.sqlite3'
                    database.write_bytes(files['ledger.sqlite3'])
                    with closing(sqlite3.connect(database)) as conn:
                        with conn:
                            conn.execute("UPDATE entries SET receipt='missing.pdf'")
                    files['ledger.sqlite3'] = database.read_bytes()
                    manifest['files']['ledger.sqlite3'] = {'size': len(files['ledger.sqlite3']),
                                                           'sha256': hashlib.sha256(files['ledger.sqlite3']).hexdigest()}
                elif case == 'future-version':
                    manifest['version'] = 99
                elif case == 'bad-schema':
                    files['ledger.sqlite3'] = b'not sqlite'
                    manifest['files']['ledger.sqlite3'] = {'size': 10, 'sha256': hashlib.sha256(b'not sqlite').hexdigest()}
                output = io.BytesIO()
                with zipfile.ZipFile(output, 'w') as archive:
                    for key, data in files.items():
                        archive.writestr(key, data)
                    archive.writestr('manifest.json', json.dumps(manifest))
                    if case == 'duplicate':
                        with self.assertWarns(UserWarning):
                            archive.writestr('manifest.json', '{}')
                response = self.client.post('/api/backups/restore/upload', content=output.getvalue(),
                                            headers={**self.headers, 'X-Backup-Confirm': 'restore'})
                # This loop intentionally tests more than the user-facing rate limit.
                if response.status_code == 429:
                    attempts.clear()
                    response = self.client.post('/api/backups/restore/upload', content=output.getvalue(),
                                                headers={**self.headers, 'X-Backup-Confirm': 'restore'})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(len(self.client.get('/api/entries').json()), 1)
                self.assertEqual(self.client.get('/api/session').status_code, 200)
                self.assertFalse((config.DATA.parent / 'escape').exists())

    def test_restore_failure_rolls_back_database_and_receipts(self):
        original = self.entry()
        name = self.backup()
        newer = self.entry('恢复前的新账目')
        self.post(f"/entries/{newer['id']}/receipt",
                  {'name': 'new.pdf', 'data': base64.b64encode(b'%PDF-new').decode()})
        with patch('app.backups.init_db', side_effect=sqlite3.OperationalError('simulated failure')):
            response = self.post('/backups/restore', {'target': 'local', 'name': name, 'confirm': True})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.client.get('/api/session').status_code, 200)
        entries = self.client.get('/api/entries').json()
        self.assertEqual({e['id'] for e in entries}, {original['id'], newer['id']})
        self.assertEqual(self.client.get(f"/api/entries/{newer['id']}/receipt").content, b'%PDF-new')

    def test_inflight_write_cannot_use_a_session_invalidated_by_restore(self):
        from app.main import current
        name = self.backup()
        entered, release = threading.Event(), threading.Event()

        def paused_current(request):
            result = current(request)
            if request.url.path == '/api/entries' and request.method == 'POST':
                entered.set()
                release.wait(timeout=10)
            return result

        with patch('app.main.current', side_effect=paused_current):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.client.post, '/api/entries',
                                      headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
                                      json={'kind': 'expense', 'amount': '10', 'currency': 'CNY',
                                            'date': config.today(), 'description': '过期会话的并发写入'})
                self.assertTrue(entered.wait(timeout=5))
                try:
                    restored = self.post('/backups/restore', {'name': name, 'confirm': True})
                    self.assertEqual(restored.status_code, 200, restored.text)
                finally:
                    release.set()
                response = pending.result(timeout=10)
                self.assertEqual(response.status_code, 401, response.text)
        self.login()
        self.assertEqual(self.client.get('/api/entries').json(), [])

    def test_missing_receipt_aborts_backup_and_cleanup_never_deletes_a_live_receipt(self):
        entry = self.entry()
        self.post(f"/entries/{entry['id']}/receipt",
                  {'name': 'new.pdf', 'data': base64.b64encode(b'%PDF-new').decode()})
        with db() as conn:
            name = conn.execute('SELECT receipt FROM entries').fetchone()[0]
        remove_receipt_file(name)
        path = config.DATA / 'receipts' / name
        self.assertTrue(path.exists())
        path.unlink()
        response = self.post('/backups', {'target': 'local'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(backups.list_backups(), [])

    def test_retention_keeps_the_newest_backup_even_in_the_same_second(self):
        self.save({'keep': 1})
        moment = datetime(2026, 11, 14, 0, tzinfo=timezone.utc)
        with patch('app.backups.utcnow', return_value=moment):
            with patch('app.backups.secrets.token_hex', return_value='ffffffff'):
                first = self.backup()
            with patch('app.backups.secrets.token_hex', return_value='00000000'):
                second = self.backup()
        self.assertNotEqual(first, second)
        self.assertEqual([b['name'] for b in backups.list_backups()], [second])

    def test_partial_remote_failure_retains_local_copy_and_reports_failure(self):
        self.configure_remote()
        bad, good = MemoryStorage(), MemoryStorage()
        bad.upload = Mock(side_effect=StorageError('WebDAV 测试故障'))
        with patch('app.backups.remote', side_effect=lambda target, conf: bad if target == 'webdav' else good):
            response = self.post('/backups', {'targets': ['webdav', 's3']})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(len(backups.list_backups()), 1)
        status = backups.read_control()['status']
        self.assertEqual(status['last_results']['s3'], 'ok')
        self.assertIn('测试故障', status['last_error'])
        self.assertIn(status['last_name'], good.files)

    def test_scheduler_due_run_persists_restart_catchup_and_does_not_duplicate(self):
        self.save({'schedule': {'enabled': True}})
        backups._status(next_run=(backups.utcnow() - timedelta(days=2)).isoformat())
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: backups.scheduler_tick(), range(2)))
        self.assertEqual(len(backups.list_backups()), 1)
        control = backups.read_control()
        self.assertTrue(control['status']['last_automatic'])
        self.assertGreater(datetime.fromisoformat(control['status']['next_run']), backups.utcnow())
        backups.scheduler_tick()
        self.assertEqual(len(backups.list_backups()), 1)
        self.save({'schedule': {'enabled': False}})
        self.assertIsNone(backups.read_control()['status']['next_run'])

    def test_scheduler_retries_failure_and_manual_tasks_are_exclusive(self):
        self.save({'schedule': {'enabled': True}})
        backups._status(next_run=(backups.utcnow() - timedelta(days=1)).isoformat())
        with patch('app.backups._snapshot', side_effect=OSError('disk full')), \
             patch('app.backups.log.exception'):
            backups.scheduler_tick()
        status = backups.read_control()['status']
        self.assertIn('自动备份失败', status['last_error'])
        self.assertLess(datetime.fromisoformat(status['next_run']) - backups.utcnow(), timedelta(minutes=6))
        entered, release = threading.Event(), threading.Event()
        original = backups._snapshot

        def blocking(*args):
            entered.set()
            release.wait(timeout=10)
            return original(*args)

        with patch('app.backups._snapshot', side_effect=blocking):
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(backups.create_backup, ['local'])
                self.assertTrue(entered.wait(timeout=5))
                try:
                    response = self.post('/backups', {'target': 'local'})
                    self.assertEqual(response.status_code, 409, response.text)
                finally:
                    release.set()
                task.result(timeout=10)

    def test_interrupted_scheduled_job_is_retried_on_restart(self):
        self.save({'schedule': {'enabled': True}})
        backups._status(scheduled_running=True, next_run=(backups.utcnow() + timedelta(days=1)).isoformat())
        backups.scheduler_tick()
        self.assertEqual(len(backups.list_backups()), 1)
        self.assertFalse(backups.read_control()['status']['scheduled_running'])

    def test_schedule_calculations_use_team_timezone(self):
        with patch.object(config, 'TIMEZONE', 'Asia/Taipei'):
            after = datetime(2026, 11, 14, 1, 23, tzinfo=timezone.utc)  # Saturday 09:23
            schedule = {'frequency': 'hourly', 'time': '03:00', 'weekday': 0}
            self.assertEqual(backups.next_run(schedule, after), '2026-11-14T02:00:00+00:00')
            schedule['frequency'] = 'daily'
            self.assertEqual(backups.next_run(schedule, after), '2026-11-14T19:00:00+00:00')
            schedule['frequency'] = 'weekly'
            self.assertEqual(backups.next_run(schedule, after), '2026-11-15T19:00:00+00:00')


class StorageProtocolTest(unittest.TestCase):
    def test_webdav_real_http_roundtrip(self):
        files, folders = {}, set()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_MKCOL(self):
                self.send_response(405 if self.path in folders else 201)
                folders.add(self.path)
                self.end_headers()

            def do_PUT(self):
                files[self.path] = self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(201)
                self.end_headers()

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(files[self.path])

            def do_DELETE(self):
                files.pop(self.path, None)
                self.send_response(204)
                self.end_headers()

            def do_PROPFIND(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                entries = ''.join(f'<d:response><d:href>{name}</d:href><d:propstat><d:prop>'
                                  f'<d:getcontentlength>{len(value)}</d:getcontentlength></d:prop>'
                                  '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                                  for name, value in files.items() if name.startswith(self.path))
                payload = f'<d:multistatus xmlns:d="DAV:">{entries}</d:multistatus>'.encode()
                self.send_response(207)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            store = WebDAV({'url': f'http://127.0.0.1:{server.server_port}/root/', 'prefix': '账本/backups',
                            'username': 'tester', 'password': 'test-password'})
            name = 'team-ledger-20261114T000000Z-1234abcd.zip'
            with tempfile.TemporaryDirectory(dir=os.getenv('LEDGER_TEST_TMP')) as directory:
                path = Path(directory) / 'backup.zip'
                path.write_bytes(b'zip payload' * 10000)
                store.upload(name, path)
                # The second upload exercises MKCOL's existing-directory response.
                store.upload(name, path)
                self.assertEqual(store.list()[0]['name'], name)
                store.download(name, path, 200000)
                self.assertEqual(path.read_bytes(), b'zip payload' * 10000)
                store.delete(name)
                self.assertEqual(store.list(), [])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_webdav_protocol_and_auth(self):
        conf = {'url': 'https://dav.example.test/root/', 'prefix': '账本/backups',
                'username': 'tester', 'password': 'test-password'}
        store = WebDAV(conf)
        name = 'team-ledger-20261114T000000Z-1234abcd.zip'
        xml = f'''<d:multistatus xmlns:d="DAV:"><d:response><d:href>/root/账本/backups/{name}</d:href>
            <d:propstat><d:prop><d:getcontentlength>3</d:getcontentlength></d:prop>
            <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
            <d:response><d:href>/root/other/{name}</d:href></d:response></d:multistatus>'''.encode()
        calls = []

        def open_request(request, timeout):
            calls.append(request)
            if request.method == 'PROPFIND':
                return io.BytesIO(xml)
            if request.method == 'GET':
                return io.BytesIO(b'zip')
            return io.BytesIO(b'')

        store.opener = Mock()
        store.opener.open.side_effect = open_request
        with tempfile.TemporaryDirectory(dir=os.getenv('LEDGER_TEST_TMP')) as directory:
            path = Path(directory) / 'backup.zip'
            path.write_bytes(b'zip')
            store.upload(name, path)
            self.assertEqual(store.list()[0]['name'], name)
            store.download(name, path, 10)
            self.assertEqual(path.read_bytes(), b'zip')
            store.delete(name)
        self.assertEqual([r.method for r in calls], ['MKCOL', 'MKCOL', 'PUT', 'PROPFIND', 'GET', 'DELETE'])
        self.assertIn('%E8%B4%A6%E6%9C%AC', calls[2].full_url)
        self.assertEqual(calls[3].get_header('Depth'), '1')
        self.assertEqual(calls[2].get_header('Content-length'), '3')
        self.assertTrue(calls[0].get_header('Authorization').startswith('Basic '))
        empty = WebDAV({**conf, 'prefix': ''})
        self.assertEqual(empty.folder, conf['url'])

    def test_webdav_rejects_redirects_errors_and_entity_documents(self):
        store = WebDAV({'url': 'https://dav.example.test/', 'prefix': 'backups',
                        'username': 'tester', 'password': 'test-password'})
        store.opener = Mock()
        store.opener.open.side_effect = urllib.error.HTTPError(
            'https://dav.example.test/', 302, 'redirect', {}, io.BytesIO(b''))
        with self.assertRaisesRegex(StorageError, 'HTTP 302'):
            store.list()
        store.opener.open.side_effect = None
        store.opener.open.return_value = io.BytesIO(b'<!DOCTYPE a [<!ENTITY x "bad">]><a/>')
        with self.assertRaisesRegex(StorageError, '无效'):
            store.list()

    def test_s3_sdk_upload_pagination_download_and_delete(self):
        from botocore.response import StreamingBody
        from botocore.stub import ANY, Stubber
        store = S3({'endpoint': 'https://s3.example.test', 'region': 'us-east-1', 'bucket': 'test-bucket',
                    'access_key_id': 'test-id', 'secret_access_key': 'test-secret',
                    'prefix': 'ledger', 'path_style': True})
        name = 'team-ledger-20261114T000000Z-1234abcd.zip'
        with Stubber(store.client) as stubber:
            stubber.add_response('put_object', {}, {'Bucket': 'test-bucket', 'Key': 'ledger/' + name,
                                                   'Body': ANY, 'ContentType': 'application/zip'})
            stubber.add_response('list_objects_v2', {'IsTruncated': True, 'NextContinuationToken': 'page2',
                                 'Contents': [{'Key': 'ledger/other.txt', 'Size': 1}]},
                                 {'Bucket': 'test-bucket', 'Prefix': 'ledger/'})
            stubber.add_response('list_objects_v2', {'IsTruncated': False,
                                 'Contents': [{'Key': 'ledger/' + name, 'Size': 3,
                                               'LastModified': datetime(2026, 11, 14, tzinfo=timezone.utc)}]},
                                 {'Bucket': 'test-bucket', 'Prefix': 'ledger/', 'ContinuationToken': 'page2'})
            stubber.add_response('get_object', {'Body': StreamingBody(io.BytesIO(b'zip'), 3)},
                                 {'Bucket': 'test-bucket', 'Key': 'ledger/' + name})
            stubber.add_response('delete_object', {}, {'Bucket': 'test-bucket', 'Key': 'ledger/' + name})
            with tempfile.TemporaryDirectory(dir=os.getenv('LEDGER_TEST_TMP')) as directory:
                path = Path(directory) / 'backup.zip'
                path.write_bytes(b'zip')
                store.upload(name, path)
                self.assertEqual(store.list()[0]['name'], name)
                store.download(name, path, 100)
                self.assertEqual(path.read_bytes(), b'zip')
                store.delete(name)
            stubber.assert_no_pending_responses()
        store.close()

    def test_s3_isolates_host_aws_environment_and_maps_diagnostics(self):
        from botocore.exceptions import ClientError, SSLError
        from botocore.stub import Stubber

        hostile_env = {
            'AWS_PROFILE': 'nonexistent-profile-from-host',
            'AWS_DEFAULT_PROFILE': 'nonexistent-default-profile',
            'AWS_CONFIG_FILE': '/nonexistent/aws/config',
            'AWS_SHARED_CREDENTIALS_FILE': '/nonexistent/aws/credentials',
            'AWS_ENDPOINT_URL': 'https://hijacked-global.example.invalid',
            'AWS_ENDPOINT_URL_S3': 'https://hijacked-s3.example.invalid',
            'AWS_SESSION_TOKEN': 'stale-host-session-token',
        }
        with patch.dict(os.environ, hostile_env, clear=False):
            aws_store = S3({
                'endpoint': '',
                'region': 'ap-northeast-1',
                'bucket': 'team-ledger-prod',
                'access_key_id': 'AKIAIOSFODNN7EXAMPLE',
                'secret_access_key': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                'prefix': 'team-ledger',
                'path_style': False,
            })
            try:
                self.assertIn('amazonaws.com', aws_store.client._endpoint.host)
                self.assertNotIn('hijacked', aws_store.client._endpoint.host)
                with Stubber(aws_store.client) as stubber:
                    stubber.add_client_error('list_objects_v2', service_error_code='SecondLevelDomainForbidden', http_status_code=403)
                    with self.assertRaisesRegex(StorageError, 'Path style'):
                        aws_store.list()
                    stubber.add_client_error('list_objects_v2', service_error_code='NoSuchBucket', http_status_code=404)
                    with self.assertRaisesRegex(StorageError, '存储桶不存在'):
                        aws_store.list()
                    stubber.add_client_error('list_objects_v2', service_error_code='SignatureDoesNotMatch', http_status_code=403)
                    with self.assertRaisesRegex(StorageError, '签名校验失败'):
                        aws_store.list()
                    stubber.add_client_error('get_object', service_error_code='NoSuchKey', http_status_code=404)
                    with tempfile.TemporaryDirectory(dir=os.getenv('LEDGER_TEST_TMP')) as directory:
                        with self.assertRaises(StorageError) as ctx:
                            aws_store.download('team-ledger-20261114T000000Z-1234abcd.zip', Path(directory) / 'out.zip', 1024)
                        self.assertEqual(ctx.exception.status, 404)
                with patch.object(aws_store.client, 'list_objects_v2', side_effect=SSLError(endpoint_url='https://bad', error='cert')):
                    with self.assertRaisesRegex(StorageError, 'TLS 证书'):
                        aws_store.list()
                # Ensure raw endpoint/credentials never leak even on unmapped errors
                unmapped = ClientError(
                    {'Error': {'Code': 'InternalError', 'Message': 'secret-host.internal AKIAIOSFODNN7EXAMPLE'},
                     'ResponseMetadata': {'HTTPStatusCode': 500}},
                    'ListObjectsV2',
                )
                with patch.object(aws_store.client, 'list_objects_v2', side_effect=unmapped):
                    with self.assertRaises(StorageError) as ctx:
                        aws_store.list()
                    self.assertNotIn('AKIAIOSFODNN7EXAMPLE', str(ctx.exception))
                    self.assertNotIn('secret-host.internal', str(ctx.exception))
            finally:
                aws_store.close()


class LocalSigV4S3Server:
    """Minimal real HTTP S3 server verifying AWS SigV4 signatures and SHA-256 payload hashes."""

    def __init__(self, bucket='ledger-bucket', access_key='test-ak', secret_key='test-sk', region='auto'):
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.temp_dir = tempfile.TemporaryDirectory(prefix='local-s3-server-', dir=os.getenv('LEDGER_TEST_TMP'))
        self.root = Path(self.temp_dir.name)
        self.objects = {}
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _xml_error(self, status, code, message=''):
                payload = (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<Error><Code>{code}</Code><Message>{message}</Message></Error>'
                ).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _verify_sigv4(self, body):
                auth = self.headers.get('Authorization', '')
                amz_date = self.headers.get('X-Amz-Date', '')
                payload_hash = self.headers.get('X-Amz-Content-SHA256', '')
                if not auth.startswith('AWS4-HMAC-SHA256 ') or not amz_date or not payload_hash:
                    self._xml_error(403, 'AccessDenied', 'Missing SigV4 headers')
                    return False
                if payload_hash != 'UNSIGNED-PAYLOAD':
                    actual_hash = hashlib.sha256(body).hexdigest()
                    if payload_hash != actual_hash:
                        self._xml_error(400, 'XAmzContentSHA256Mismatch', 'Payload hash mismatch')
                        return False
                parts = {}
                for item in auth[len('AWS4-HMAC-SHA256 '):].split(','):
                    k, v = item.strip().split('=', 1)
                    parts[k] = v
                cred = parts.get('Credential', '').split('/')
                if len(cred) != 5 or cred[0] != outer.access_key or cred[2] != outer.region or cred[3] != 's3' or cred[4] != 'aws4_request':
                    self._xml_error(403, 'InvalidAccessKeyId' if cred[0] != outer.access_key else 'AuthorizationHeaderMalformed')
                    return False
                signed_headers = parts.get('SignedHeaders', '').split(';')
                lower_headers = {k.lower(): ' '.join(v.strip().split()) for k, v in self.headers.items()}
                canonical_headers = ''.join(f'{h}:{lower_headers.get(h, "")}\n' for h in signed_headers)
                parsed = urlsplit(self.path)
                canonical_uri = quote(unquote(parsed.path or '/'), safe='/~')
                query_pairs = []
                if parsed.query:
                    for pair in parsed.query.split('&'):
                        if '=' in pair:
                            qk, qv = pair.split('=', 1)
                        else:
                            qk, qv = pair, ''
                        query_pairs.append((quote(unquote(qk), safe='-_.~'), quote(unquote(qv), safe='-_.~')))
                query_pairs.sort()
                canonical_query = '&'.join(f'{k}={v}' for k, v in query_pairs)
                canonical_request = '\n'.join([
                    self.command, canonical_uri, canonical_query,
                    canonical_headers, ';'.join(signed_headers), payload_hash,
                ])
                scope = '/'.join(cred[1:])
                string_to_sign = '\n'.join([
                    'AWS4-HMAC-SHA256', amz_date, scope,
                    hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
                ])

                def _sign(key, msg):
                    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

                k_date = _sign(('AWS4' + outer.secret_key).encode('utf-8'), cred[1])
                k_region = _sign(k_date, cred[2])
                k_service = _sign(k_region, cred[3])
                k_signing = _sign(k_service, cred[4])
                expected_sig = hmac.new(k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected_sig, parts.get('Signature', '')):
                    self._xml_error(403, 'SignatureDoesNotMatch', 'Signature mismatch')
                    return False
                return True

            def _resolve_bucket_and_key(self):
                parsed = urlsplit(self.path)
                raw_path = unquote(parsed.path).lstrip('/')
                host = self.headers.get('Host', '').split(':', 1)[0]
                if host.startswith(outer.bucket + '.'):
                    return outer.bucket, raw_path, parse_qs(parsed.query, keep_blank_values=True)
                if '/' in raw_path:
                    bucket, key = raw_path.split('/', 1)
                else:
                    bucket, key = raw_path, ''
                return bucket, key, parse_qs(parsed.query, keep_blank_values=True)

            def _read_body(self):
                if self.headers.get('Expect', '').lower() == '100-continue':
                    self.send_response_only(100)
                    self.end_headers()
                length = int(self.headers.get('Content-Length', '0') or 0)
                return self.rfile.read(length) if length else b''

            def do_PUT(self):
                body = self._read_body()
                if not self._verify_sigv4(body):
                    return
                bucket, key, _ = self._resolve_bucket_and_key()
                with outer.lock:
                    outer.requests.append(('PUT', bucket, key, len(body)))
                    if bucket != outer.bucket:
                        self._xml_error(404, 'NoSuchBucket')
                        return
                    disk_file = outer.root / hashlib.sha256(key.encode('utf-8')).hexdigest()
                    disk_file.write_bytes(body)
                    outer.objects[key] = {
                        'path': disk_file,
                        'size': len(body),
                        'modified': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                    }
                self.send_response(200)
                self.send_header('ETag', f'"{hashlib.md5(body).hexdigest()}"')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                body = self._read_body()
                if not self._verify_sigv4(body):
                    return
                bucket, key, query = self._resolve_bucket_and_key()
                with outer.lock:
                    outer.requests.append(('GET', bucket, key, 0))
                    if bucket != outer.bucket:
                        self._xml_error(404, 'NoSuchBucket')
                        return
                    if query.get('list-type') == ['2'] and not key:
                        prefix = query.get('prefix', [''])[0]
                        use_url_encoding = query.get('encoding-type') == ['url']
                        matched = sorted(k for k in outer.objects if k.startswith(prefix))
                        max_keys = int(query.get('max-keys', ['1000'])[0])
                        start = int(query.get('continuation-token', ['0'])[0])
                        page = matched[start:start + max_keys]
                        truncated = start + max_keys < len(matched)
                        items_xml = []
                        for obj_key in page:
                            meta = outer.objects[obj_key]
                            encoded_key = quote(obj_key, safe='/') if use_url_encoding else obj_key
                            items_xml.append(
                                f'<Contents><Key>{encoded_key}</Key><LastModified>{meta["modified"]}</LastModified>'
                                f'<Size>{meta["size"]}</Size></Contents>'
                            )
                        next_xml = f'<NextContinuationToken>{start + max_keys}</NextContinuationToken>' if truncated else ''
                        enc_xml = '<EncodingType>url</EncodingType>' if use_url_encoding else ''
                        payload = (
                            '<?xml version="1.0" encoding="UTF-8"?>'
                            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                            f'<Name>{outer.bucket}</Name>{enc_xml}'
                            f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>'
                            f'{next_xml}{"".join(items_xml)}</ListBucketResult>'
                        ).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/xml')
                        self.send_header('Content-Length', str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    meta = outer.objects.get(key)
                    if not meta:
                        self._xml_error(404, 'NoSuchKey')
                        return
                    data = meta['path'].read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_DELETE(self):
                body = self._read_body()
                if not self._verify_sigv4(body):
                    return
                bucket, key, _ = self._resolve_bucket_and_key()
                with outer.lock:
                    outer.requests.append(('DELETE', bucket, key, 0))
                    if bucket != outer.bucket:
                        self._xml_error(404, 'NoSuchBucket')
                        return
                    meta = outer.objects.pop(key, None)
                    if meta:
                        meta['path'].unlink(missing_ok=True)
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp_dir.cleanup()


class RealS3EndToEndBackupRestoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ledger-s3-e2e-', dir=os.getenv('LEDGER_TEST_TMP'))
        self.old_data, self.old_path = config.DATA, config.DB_PATH
        config.DATA = Path(self.temporary.name)
        config.DB_PATH = config.DATA / 'ledger.sqlite3'
        attempts.clear()
        self.s3_server = LocalSigV4S3Server(
            bucket='vanora-e2e-backups',
            access_key='AKIAE2ETESTKEY0001',
            secret_key='e2e-super-secret-signing-key-9999',
            region='auto',
        ).start()
        self.oidc = FakeVanoraOidc().start()
        self.client = TestClient(app)
        self.client.__enter__()
        session = self.oidc.login(
            self.client,
            sub='vanora-s3-e2e-owner',
            preferred_username='s3owner',
            name='S3 审计员',
            roles=('owner',),
            team_name='S3 隔离实测团队',
        )
        self.headers = {'X-CSRF-Token': session['csrf']}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.oidc.stop()
        self.s3_server.stop()
        config.DATA, config.DB_PATH = self.old_data, self.old_path
        self.temporary.cleanup()

    def login(self):
        session = self.oidc.login(
            self.client,
            sub='vanora-s3-e2e-owner',
            preferred_username='s3owner',
            name='S3 审计员',
            roles=('owner',),
        )
        self.headers = {'X-CSRF-Token': session['csrf']}

    def test_real_s3_end_to_end_backup_pruning_and_restore_without_local_copy(self):
        # 1. Validate S3 endpoint copy-paste guardrails and blank-region / secret-trim handling
        bad_r2 = self.client.put('/api/backups/config', headers=self.headers, json={
            's3': {
                'endpoint': 'https://acct123.r2.cloudflarestorage.com/vanora-e2e-backups',
                'bucket': 'vanora-e2e-backups',
                'access_key_id': 'AKIA',
                'secret_access_key': 'SK',
            }
        })
        self.assertEqual(bad_r2.status_code, 400)
        self.assertIn('请勿包含存储桶路径', bad_r2.json()['error'])

        bad_oss = self.client.put('/api/backups/config', headers=self.headers, json={
            's3': {
                'endpoint': 'https://vanora-e2e-backups.oss-cn-hangzhou.aliyuncs.com',
                'bucket': 'vanora-e2e-backups',
                'access_key_id': 'AKIA',
                'secret_access_key': 'SK',
            }
        })
        self.assertEqual(bad_oss.status_code, 400)
        self.assertIn('不含存储桶前缀', bad_oss.json()['error'])

        # 2. Configure real local SigV4 S3 server (including accidental spaces around secret key)
        saved = self.client.put('/api/backups/config', headers=self.headers, json={
            'keep': 2,
            's3': {
                'endpoint': self.s3_server.endpoint,
                'region': 'auto',
                'bucket': 'vanora-e2e-backups',
                'access_key_id': 'AKIAE2ETESTKEY0001',
                'secret_access_key': '  e2e-super-secret-signing-key-9999  ',
                'prefix': '账本备份/prod',
                'path_style': True,
            },
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()['s3']['has_secret_access_key'])

        # 3. Connection probe against real SigV4 HTTP S3 server (verifies PUT + LIST + GET + DELETE)
        probe = self.client.post('/api/backups/test', headers=self.headers, json={'target': 's3'})
        self.assertEqual(probe.status_code, 200, probe.text)
        self.assertEqual(self.s3_server.objects, {})

        # 4. Populate real ledger data + receipt + AI settings
        created_entry = self.client.post(
            '/api/entries',
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
            json={
                'kind': 'expense', 'expense_type': 'advance', 'amount': '256.80',
                'currency': 'USD', 'fx_mode': 'manual', 'fx_rate': '7.25',
                'description': 'S3 云存储服务器账单', 'date': config.today(), 'note': '真实备份前记录',
            },
        )
        self.assertEqual(created_entry.status_code, 200, created_entry.text)
        entry_id = created_entry.json()['id']
        pdf_bytes = b'%PDF-1.4\n%real-s3-receipt-payload-verification\n%%EOF'
        up_receipt = self.client.post(
            f'/api/entries/{entry_id}/receipt',
            headers=self.headers,
            json={'name': 'aws-invoice.pdf', 'data': base64.b64encode(pdf_bytes).decode()},
        )
        self.assertEqual(up_receipt.status_code, 200, up_receipt.text)

        # 5. Create 3 backups to S3 and verify retention keep=2 prunes oldest S3 object
        names = []
        for _ in range(3):
            res = self.client.post('/api/backups', headers=self.headers, json={'target': 's3'})
            self.assertEqual(res.status_code, 200, res.text)
            names.append(res.json()['name'])
        listed = self.client.get('/api/backups', params={'target': 's3'}).json()
        self.assertEqual(len(listed), 2)
        self.assertIn(names[-1], {item['name'] for item in listed})
        target_backup = names[-1]

        # Download from S3 via API and verify ZIP manifest + SHA-256
        dl = self.client.get(f'/api/backups/download/{target_backup}', params={'target': 's3'})
        self.assertEqual(dl.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            manifest = json.loads(zf.read('manifest.json'))
            self.assertIn('ledger.sqlite3', manifest['files'])
            self.assertEqual(len(manifest['files']), 2)

        # Missing S3 backup returns 404 (not 502)
        missing_dl = self.client.get('/api/backups/download/team-ledger-20260101T000000Z-deadbeef.zip', params={'target': 's3'})
        self.assertEqual(missing_dl.status_code, 404)

        # 6. Delete all local backup copies so restore MUST fetch exclusively from S3, then mutate live DB
        for p in backups.local_folder().glob('*.zip'):
            p.unlink()
        self.assertEqual(backups.list_backups('local'), [])

        self.client.post(
            '/api/entries',
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
            json={'kind': 'income', 'income_type': 'operating', 'amount': '9999.00',
                  'currency': 'CNY', 'description': '备份后误操作新增收入', 'date': config.today()},
        )
        self.client.put('/api/settings', headers=self.headers, json={'team_name': '被篡改的团队名'})
        self.assertEqual(len(self.client.get('/api/entries').json()), 2)

        # 7. Restore directly from S3 and verify full state recovery + local safety copy
        restored = self.client.post(
            '/api/backups/restore',
            headers=self.headers,
            json={'target': 's3', 'name': target_backup, 'confirm': True},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        safety_name = restored.json()['safety_copy']
        self.assertTrue(safety_name.startswith('before-restore-'))
        self.assertTrue(backups.local_path(safety_name).is_file())
        self.assertEqual(self.client.get('/api/session').status_code, 401)

        self.login()
        entries_after = self.client.get('/api/entries').json()
        self.assertEqual(len(entries_after), 1)
        self.assertEqual(entries_after[0]['description'], 'S3 云存储服务器账单')
        self.assertEqual(entries_after[0]['fx_rate'], '7.25')
        self.assertEqual(self.client.get('/api/settings').json()['team_name'], 'S3 隔离实测团队')
        self.assertEqual(self.client.get(f'/api/entries/{entry_id}/receipt').content, pdf_bytes)

    def test_test_storage_does_not_delete_when_upload_fails_or_mask_read_errors(self):
        self.client.put('/api/backups/config', headers=self.headers, json={
            's3': {
                'endpoint': self.s3_server.endpoint,
                'region': 'auto',
                'bucket': 'wrong-nonexistent-bucket',
                'access_key_id': 'AKIAE2ETESTKEY0001',
                'secret_access_key': 'e2e-super-secret-signing-key-9999',
            },
        })
        self.s3_server.requests.clear()
        failed_probe = self.client.post('/api/backups/test', headers=self.headers, json={'target': 's3'})
        self.assertEqual(failed_probe.status_code, 502)
        self.assertIn('存储桶不存在', failed_probe.json()['error'])
        # Only PUT should have been attempted (2 attempts with botocore standard retry = PUTs only, zero DELETEs)
        methods = [r[0] for r in self.s3_server.requests]
        self.assertNotIn('DELETE', methods)

    def test_scheduled_s3_outage_retries_remote_only_without_wiping_local_backups(self):
        # Create 2 historical local backups that must survive a prolonged S3 outage
        self.client.put('/api/backups/config', headers=self.headers, json={'keep': 2})
        old_b1 = self.client.post('/api/backups', headers=self.headers, json={'target': 'local'}).json()['name']
        self.client.put('/api/backups/config', headers=self.headers, json={
            'keep': 2,
            's3': {
                'endpoint': self.s3_server.endpoint,
                'region': 'auto',
                'bucket': 'wrong-bucket-during-outage',
                'access_key_id': 'AKIAE2ETESTKEY0001',
                'secret_access_key': 'e2e-super-secret-signing-key-9999',
            },
            'schedule': {'enabled': True, 'frequency': 'daily', 'time': '03:00', 'weekday': 0, 'targets': ['local', 's3']},
        })

        # Trigger initial scheduled run while S3 fails
        backups._status(next_run=(backups.utcnow() - timedelta(minutes=10)).isoformat())
        with patch('app.backups.log.exception'):
            backups.scheduler_tick()

        local_after_first = [b['name'] for b in backups.list_backups('local')]
        self.assertEqual(len(local_after_first), 2)
        self.assertIn(old_b1, local_after_first)

        # Simulate 5 consecutive retry ticks during S3 outage: old_b1 MUST NOT be wiped out!
        for _ in range(5):
            backups._status(next_run=(backups.utcnow() - timedelta(seconds=5)).isoformat())
            with patch('app.backups.log.exception'):
                backups.scheduler_tick()

        local_after_retries = [b['name'] for b in backups.list_backups('local')]
        self.assertEqual(local_after_retries, local_after_first)
        self.assertIn(old_b1, local_after_retries)

        # Fix S3 bucket configuration; retry uploads the pending snapshot to S3 and clears error!
        self.client.put('/api/backups/config', headers=self.headers, json={
            's3': {'bucket': 'vanora-e2e-backups'},
        })
        backups.scheduler_tick()
        status = backups.read_control()['status']
        self.assertIsNone(status['last_error'])
        self.assertEqual(status['last_results']['s3'], 'ok')
        self.assertIsNone(status.get('retry_targets'))
        s3_listed = self.client.get('/api/backups', params={'target': 's3'}).json()
        self.assertEqual(len(s3_listed), 1)
        self.assertEqual(s3_listed[0]['name'], status['last_name'])
