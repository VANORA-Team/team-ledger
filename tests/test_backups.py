import base64
import hashlib
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

from fastapi.testclient import TestClient

from app import backups, config
from app.backup_storage import S3, WebDAV, StorageError
from app.db import db, remove_receipt_file
from app.main import app, attempts


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
        self.client = TestClient(app)
        self.client.__enter__()
        result = self.client.post('/api/bootstrap', json={'username': 'owner', 'name': '测试成员',
                                                        'password': 'test-pass', 'team_name': '备份测试'})
        self.assertEqual(result.status_code, 200, result.text)
        self.headers = {'X-CSRF-Token': result.json()['csrf']}

    def tearDown(self):
        self.client.__exit__(None, None, None)
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
        response = self.client.post('/api/login', json={'username': 'owner', 'password': 'test-pass'})
        self.assertEqual(response.status_code, 200, response.text)
        self.headers = {'X-CSRF-Token': response.json()['csrf']}

    def test_local_roundtrip_receipts_settings_and_sessions(self):
        entry = self.entry()
        member = self.post('/members', {'name': '财务', 'username': 'finance',
                                        'password': 'role-test-password', 'role': 'finance'}).json()
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
        member_path = f'/api/members/{member["id"]}'
        self.assertEqual(self.client.patch(member_path, json={'role': 'member'}, headers=self.headers).status_code, 200)
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
        restored_members = {item['id']: item for item in self.client.get('/api/members').json()}
        self.assertEqual(restored_members[member['id']]['role'], 'finance')
        events = self.client.get('/api/member-events').json()
        self.assertEqual([(event['action'], event['new_role']) for event in events if event['member_id'] == member['id']],
                         [('created', 'finance')])
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
                conn.execute('DROP TABLE member_events')
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
        self.assertEqual(self.client.get('/api/member-events').json(), [])

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
