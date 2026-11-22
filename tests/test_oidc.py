import json
import os
import secrets
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import bot, config, feishu, oidc, permissions_sync
from app.db import db
from app.main import app, attempts
from tests.oidc_helper import FakeVanoraOidc, _generate_rsa_key


class OidcIntegrationTest(unittest.TestCase):
    EMPTY_OIDC_ENV = {
        'VANORA_OIDC_ISSUER': '',
        'VANORA_OIDC_CLIENT_ID': '',
        'VANORA_OIDC_CLIENT_SECRET': '',
        'VANORA_OIDC_CLIENT_SECRET_FILE': '',
        'VANORA_OIDC_ACR_VALUES': '',
        'VANORA_OIDC_INTERNAL_URL': '',
        'VANORA_OIDC_CA_FILE': '',
        'VANORA_OIDC_END_SESSION': 'false',
        'VANORA_OIDC_PERMISSIONS_TTL': '30',
        'VANORA_OIDC_ADMIN_URL': '',
        'VANORA_OIDC_ROLES_CLAIM': '',
        'PUBLIC_URL': '',
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ledger-oidc-test-', dir=os.getenv('LEDGER_TEST_TMP'))
        self.old_data, self.old_db = config.DATA, config.DB_PATH
        config.DATA = Path(self.temp.name)
        config.DB_PATH = config.DATA / 'test.sqlite3'
        attempts.clear()
        oidc.clear_cache()
        permissions_sync.clear_cache()

    def tearDown(self):
        oidc.clear_cache()
        permissions_sync.clear_cache()
        config.DATA, config.DB_PATH = self.old_data, self.old_db
        self.temp.cleanup()

    def test_unconfigured_oidc_starts_cleanly_and_reports_bootstrap_urls(self):
        with patch.dict(os.environ, self.EMPTY_OIDC_ENV):
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/health').status_code, 200)
                boot = client.get('/api/bootstrap')
                self.assertEqual(boot.status_code, 200, boot.text)
                data = boot.json()
                self.assertTrue(data['needs_setup'])
                self.assertFalse(data['oidc_configured'])
                self.assertEqual(data['permissions_managed_by'], 'vanora-oauth')
                self.assertEqual(data['required_roles'], ['owner', 'finance', 'member'])
                self.assertEqual(data['callback_url'], 'http://testserver/api/auth/oidc/callback')
                self.assertEqual(data['backchannel_logout_url'], 'http://testserver/api/auth/oidc/backchannel-logout')

                login = client.get('/api/auth/oidc/login', follow_redirects=False)
                self.assertEqual(login.status_code, 503)
                self.assertEqual(login.json()['code'], 'oidc_unconfigured')

                cb = client.get('/api/auth/oidc/callback', params={'code': 'x', 'state': 'y'}, follow_redirects=False)
                self.assertEqual(cb.status_code, 302)
                self.assertEqual(cb.headers['location'], '/?auth_error=oidc_unconfigured')

                bc = client.post('/api/auth/oidc/backchannel-logout', content='logout_token=abc',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
                self.assertEqual(bc.status_code, 503)

    def test_auto_provision_roles_precedence_no_email_and_owner_team_name(self):
        provider = FakeVanoraOidc()
        provider.start()
        try:
            with TestClient(app) as client:
                boot = client.get('/api/bootstrap').json()
                self.assertTrue(boot['needs_setup'])
                self.assertTrue(boot['oidc_configured'])

                # User without any team-ledger client role is rejected
                provider.set_user('stranger-uuid', username='stranger', name='stranger', roles=())
                provider.login(
                    client,
                    sub='stranger-uuid',
                    preferred_username='stranger',
                    name='stranger',
                    email=None,
                    register=False,
                    expect_error='not_member',
                )
                self.assertEqual(client.get('/api/session').status_code, 401)
                with db() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)

                # Non-owner authorized user on empty DB cannot overwrite team_name, but CAN log in
                with TestClient(app) as member_client:
                    m_session = provider.login(
                        member_client,
                        sub='member-only-uuid',
                        preferred_username='member_one',
                        name='普通伙伴',
                        email=None,
                        roles=('member',),
                        team_name='非法改名',
                    )
                    self.assertEqual(m_session['user']['role'], 'member')
                    self.assertEqual(member_client.get('/api/settings').json()['team_name'], '一起记')

            # Fresh DB: owner with multiple roles resolves highest precedence (owner > finance > member) and sets team_name
            with db(write=True) as conn:
                conn.execute('DELETE FROM sessions')
                conn.execute('DELETE FROM users')
            with TestClient(app) as client:
                session = provider.login(
                    client,
                    sub='owner-multi-uuid',
                    preferred_username='wechat_owner',
                    name='wechat_owner',
                    email=None,
                    roles=('member', 'finance', 'owner'),
                    team_name='极简账本',
                )
                self.assertEqual(session['user']['role'], 'owner')
                self.assertEqual(session['user']['username'], 'wechat_owner')
                self.assertEqual(session['user']['name'], 'wechat_owner')
                self.assertTrue(session['user']['oidc_linked'])
                self.assertEqual(session['user']['oidc_email'], '')
                self.assertEqual(client.get('/api/settings').json()['team_name'], '极简账本')
                self.assertFalse(client.get('/api/bootstrap').json()['needs_setup'])

                # Finance + member precedence resolves to finance
                with TestClient(app) as fin_client:
                    fin = provider.login(
                        fin_client,
                        sub='fin-uuid',
                        preferred_username='chen',
                        name='财务小陈',
                        email='chen@example.test',
                        roles=('member', 'finance'),
                    )
                    self.assertEqual(fin['user']['role'], 'finance')
                    self.assertEqual(fin['user']['oidc_email'], 'chen@example.test')
        finally:
            provider.stop()

    def test_id_token_fallback_to_permissions_api_and_disabled_rejection(self):
        provider = FakeVanoraOidc()
        provider.start()
        try:
            provider.set_user('fallback-owner', username='fb_owner', name='回退所有者', roles=('owner',), enabled=True)
            provider.set_user('disabled-user', username='disabled_u', name='停用账号', roles=('member',), enabled=False)
            provider.set_user('no-role-user', username='no_role', name='无角色', roles=(), enabled=True)

            with TestClient(app) as client:
                # Token omits resource_access -> callback falls back to /vanora-permissions/members/{sub}
                session = provider.login(
                    client,
                    sub='fallback-owner',
                    preferred_username='fb_owner',
                    name='回退所有者',
                    omit_roles=True,
                    register=False,
                )
                self.assertEqual(session['user']['role'], 'owner')
                self.assertTrue(any('/vanora-permissions/members/fallback-owner' in url for url, _ in provider.requests))

                # Disabled user rejected with disabled
                with TestClient(app) as c_dis:
                    provider.login(
                        c_dis,
                        sub='disabled-user',
                        preferred_username='disabled_u',
                        name='停用账号',
                        omit_roles=True,
                        register=False,
                        expect_error='disabled',
                    )

                # User without roles rejected even on fallback
                with TestClient(app) as c_norole:
                    provider.login(
                        c_norole,
                        sub='no-role-user',
                        preferred_username='no_role',
                        name='无角色',
                        omit_roles=True,
                        register=False,
                        expect_error='not_member',
                    )

                # Custom VANORA_OIDC_ROLES_CLAIM override works
                with patch.dict(os.environ, {'VANORA_OIDC_ROLES_CLAIM': 'custom.ledger_roles'}):
                    with TestClient(app) as c_custom:
                        provider.set_user('custom-claim-user', username='custom_u', name='自定义声明', roles=('finance',))
                        res = provider.login(
                            c_custom,
                            sub='custom-claim-user',
                            preferred_username='custom_u',
                            name='自定义声明',
                            omit_roles=True,
                            register=False,
                            extra_claims={'custom': {'ledger_roles': ['finance']}},
                        )
                        self.assertEqual(res['user']['role'], 'finance')
        finally:
            provider.stop()

    def test_security_checks_pkce_claims_acr_and_jwks_rotation(self):
        provider = FakeVanoraOidc()
        provider.start()
        try:
            with TestClient(app) as client:
                provider.login(client, sub='owner-1', preferred_username='owner1', name='所有者', roles=('owner',))

                # User cancelled on IdP
                cancelled = client.get('/api/auth/oidc/callback', params={'error': 'access_denied'}, follow_redirects=False)
                self.assertEqual(cancelled.status_code, 302)
                self.assertEqual(cancelled.headers['location'], '/?auth_error=cancelled')

                # Missing / wrong state / missing browser cookie
                flow = provider.start_flow(client)
                code = provider.issue_code(flow, sub='owner-1', preferred_username='owner1', name='所有者')
                bad_state = client.get('/api/auth/oidc/callback', params={'code': code, 'state': 'wrong-state'}, follow_redirects=False)
                self.assertEqual(bad_state.headers['location'], '/?auth_error=invalid_state')

                flow2 = provider.start_flow(client)
                code2 = provider.issue_code(flow2, sub='owner-1', preferred_username='owner1', name='所有者')
                with TestClient(app) as other_browser:
                    no_cookie = other_browser.get('/api/auth/oidc/callback',
                                                  params={'code': code2, 'state': flow2['state']},
                                                  follow_redirects=False)
                    self.assertEqual(no_cookie.headers['location'], '/?auth_error=invalid_state')

                # Expired flow
                flow3 = provider.start_flow(client)
                code3 = provider.issue_code(flow3, sub='owner-1', preferred_username='owner1', name='所有者')
                with db(write=True) as conn:
                    conn.execute('UPDATE oidc_flows SET expires=1')
                expired = client.get('/api/auth/oidc/callback', params={'code': code3, 'state': flow3['state']}, follow_redirects=False)
                self.assertEqual(expired.headers['location'], '/?auth_error=invalid_state')

                # Tampered PKCE code_verifier rejected by token endpoint
                flow4 = provider.start_flow(client)
                code4 = provider.issue_code(flow4, sub='owner-1', preferred_username='owner1', name='所有者')
                with db(write=True) as conn:
                    conn.execute('UPDATE oidc_flows SET code_verifier=?', ('tampered-verifier-value',))
                pkce_fail = client.get('/api/auth/oidc/callback', params={'code': code4, 'state': flow4['state']}, follow_redirects=False)
                self.assertEqual(pkce_fail.headers['location'], '/?auth_error=exchange_failed')

                # Upstream token exchange failure
                provider.fail_exchange = True
                try:
                    provider.login(client, sub='owner-1', expect_error='exchange_failed')
                finally:
                    provider.fail_exchange = False

                # Invalid ID token claims and signatures
                rogue_key = _generate_rsa_key()
                invalid_cases = [
                    ('wrong_nonce', {'extra_claims': {'nonce': 'bad-nonce'}}),
                    ('wrong_aud', {'aud': 'other-client'}),
                    ('wrong_azp', {'azp': 'other-client'}),
                    ('multi_aud_bad_azp', {'aud': [provider.client_id, 'extra-aud'], 'azp': 'extra-aud'}),
                    ('wrong_iss', {'iss': 'https://evil.example/realms/vanora'}),
                    ('expired', {'exp': int(time.time()) - 120}),
                    ('bad_signature', {'signing_key': rogue_key}),
                    ('symmetric_alg', {'algorithm': 'HS256', 'signing_key': 'shared-secret-key-for-test-only-32b', 'kid': ''}),
                ]
                for label, overrides in invalid_cases:
                    with self.subTest(case=label):
                        attempts.clear()
                        provider.login(client, sub='owner-1', token_overrides=overrides, expect_error='invalid_token')

                # Optional ACR enforcement
                with patch.dict(os.environ, {'VANORA_OIDC_ACR_VALUES': '2'}):
                    attempts.clear()
                    acr_flow = provider.start_flow(client)
                    self.assertEqual(acr_flow['acr_values'], '2')
                    provider.login(client, sub='owner-1', acr='1', expect_error='invalid_token')
                    ok_acr = provider.login(client, sub='owner-1', acr='2')
                    self.assertEqual(ok_acr['user']['username'], 'owner1')

                # JWKS key rotation with unknown kid refresh
                attempts.clear()
                with oidc._cache_lock:
                    for key_url, (exp_at, _last, doc) in list(oidc._jwks_cache.items()):
                        oidc._jwks_cache[key_url] = (exp_at, time.time() - 30, doc)
                new_kid = provider.rotate_key('vanora-key-rotated')
                self.assertEqual(new_kid, 'vanora-key-rotated')
                rotated = provider.login(client, sub='owner-1')
                self.assertEqual(rotated['user']['username'], 'owner1')
        finally:
            provider.stop()

    def test_directory_sync_preserves_custom_name_and_exposes_admin_url_only_to_owner(self):
        provider = FakeVanoraOidc()
        provider.start(VANORA_OIDC_ADMIN_URL='https://admin.vanora.test/admin/vanora/console/')
        try:
            provider.set_user('owner-sub', username='boss', name='老板', roles=('owner',))
            provider.set_user('unlogged-member', username='newbie', name='未登录成员', email='new@example.test', roles=('member',))
            provider.set_user('finance-sub', username='fin', name='财务', roles=('finance',))

            with TestClient(app) as owner_client, TestClient(app) as fin_client:
                owner_sess = provider.login(owner_client, sub='owner-sub', preferred_username='boss', name='老板', register=False)
                self.assertEqual(owner_sess['admin_url'], 'https://admin.vanora.test/admin/vanora/console')
                owner_headers = {'X-CSRF-Token': owner_sess['csrf']}

                fin_sess = provider.login(fin_client, sub='finance-sub', preferred_username='fin', name='财务', register=False)
                self.assertEqual(fin_sess['admin_url'], '')
                fin_headers = {'X-CSRF-Token': fin_sess['csrf']}

                # GET /api/members automatically syncs unlogged-in Vanora members
                members = {m['username']: m for m in owner_client.get('/api/members').json()}
                self.assertIn('newbie', members)
                self.assertEqual(members['newbie']['role'], 'member')
                self.assertEqual(members['newbie']['active'], 1)

                # Personal display name customized via PATCH /api/me is preserved across sync
                renamed = owner_client.patch('/api/me', json={'name': '账本老板昵称'}, headers=owner_headers)
                self.assertEqual(renamed.status_code, 200, renamed.text)
                self.assertEqual(renamed.json()['user']['name'], '账本老板昵称')

                # Removing newbie's client role in Vanora + POST /api/members/sync marks newbie inactive
                provider.set_user('unlogged-member', roles=())
                sync_resp = fin_client.post('/api/members/sync', headers=fin_headers)
                self.assertEqual(sync_resp.status_code, 200, sync_resp.text)
                after = {m['username']: m for m in owner_client.get('/api/members').json()}
                self.assertEqual(after['newbie']['active'], 0)
                self.assertEqual(after['boss']['name'], '账本老板昵称')
        finally:
            provider.stop()

    def test_backchannel_logout_sid_permissions_changed_account_deleted_and_jti_replay(self):
        provider = FakeVanoraOidc()
        provider.start()
        try:
            with TestClient(app) as client_a, TestClient(app) as client_b:
                sess_a = provider.login(
                    client_a, sub='user-bc', preferred_username='alice', name='小林', roles=('owner',), sid='sid-a'
                )
                sess_b = provider.login(
                    client_b, sub='user-bc', preferred_username='alice', name='小林', roles=('owner',), sid='sid-b'
                )
                self.assertEqual(client_a.get('/api/session').status_code, 200)
                self.assertEqual(client_b.get('/api/session').status_code, 200)

                def send_raw(token: str | None):
                    body = '' if token is None else urllib.parse.urlencode({'logout_token': token})
                    return client_a.post(
                        '/api/auth/oidc/backchannel-logout',
                        content=body,
                        headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    )

                # Invalid payloads rejected
                self.assertEqual(send_raw(None).status_code, 400)
                self.assertEqual(send_raw(provider.make_logout_token(sub='user-bc', include_nonce=True)).status_code, 400)
                self.assertEqual(send_raw(provider.make_logout_token(sub='user-bc', include_events=False)).status_code, 400)
                self.assertEqual(send_raw(provider.make_logout_token(sub='', sid='')).status_code, 400)

                # Precise sid logout invalidates only client_a
                sid_token = provider.make_logout_token(sub='user-bc', sid='sid-a', jti='jti-sid-a')
                self.assertEqual(send_raw(sid_token).status_code, 200)
                self.assertEqual(client_a.get('/api/session').status_code, 401)
                self.assertEqual(client_b.get('/api/session').status_code, 200)

                # Replay of same jti rejected
                self.assertEqual(send_raw(sid_token).status_code, 400)

                # Seed ephemeral state (payment_quotes) for user-bc, downgrade role in Vanora, send permissions-changed
                b_headers = {'X-CSRF-Token': sess_b['csrf']}
                created = client_b.post(
                    '/api/entries',
                    json={'kind': 'expense', 'amount': '10.00', 'currency': 'CNY', 'description': '测试', 'date': config.today()},
                    headers={**b_headers, 'Idempotency-Key': secrets.token_hex(16)},
                )
                self.assertEqual(created.status_code, 200, created.text)
                quoted = client_b.post(
                    '/api/entries/payment-quotes',
                    json={'ids': [created.json()['id']], 'currency': 'original'},
                    headers=b_headers,
                )
                self.assertEqual(quoted.status_code, 200, quoted.text)
                with db() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM payment_quotes').fetchone()[0], 1)

                provider.set_user('user-bc', roles=('member',))
                perm_resp = provider.send_backchannel_logout(
                    client_a,
                    sub='user-bc',
                    jti='jti-perm-1',
                    extra_events=[oidc.PERMISSIONS_CHANGED_EVENT],
                )
                self.assertEqual(perm_resp.status_code, 200)
                self.assertEqual(client_b.get('/api/session').status_code, 401)
                with db() as conn:
                    self.assertEqual(conn.execute('SELECT role,active FROM users WHERE oidc_sub=?', ('user-bc',)).fetchone()[:], ('member', 1))
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM payment_quotes').fetchone()[0], 0)

                # Account-deleted event deactivates user immediately even if permissions API fails
                provider.fail_permissions = True
                try:
                    del_resp = provider.send_backchannel_logout(
                        client_a,
                        sub='user-bc',
                        jti='jti-del-1',
                        extra_events=[oidc.ACCOUNT_DELETED_EVENT],
                    )
                    self.assertEqual(del_resp.status_code, 200)
                finally:
                    provider.fail_permissions = False
                with db() as conn:
                    self.assertEqual(conn.execute('SELECT active FROM users WHERE oidc_sub=?', ('user-bc',)).fetchone()[0], 0)
        finally:
            provider.stop()

    def test_ttl_live_sync_revokes_web_telegram_and_feishu_without_backchannel(self):
        provider = FakeVanoraOidc()
        provider.start(VANORA_OIDC_PERMISSIONS_TTL='1')
        try:
            with TestClient(app) as client:
                sess = provider.login(
                    client, sub='ttl-user', preferred_username='ttluser', name='定时成员', roles=('finance',)
                )
                uid = sess['user']['id']
                with db(write=True) as conn:
                    conn.execute('INSERT INTO tg_links VALUES (?,?)', ('888', uid))
                    conn.execute('INSERT INTO feishu_links VALUES (?,?)', ('ou_ttl', uid))

                # Revoke role in Vanora WITHOUT sending backchannel logout
                provider.set_user('ttl-user', roles=())
                permissions_sync.clear_cache()

                # Next Web API call triggers TTL sync -> session immediately revoked (401)
                self.assertEqual(client.get('/api/entries').status_code, 401)

                # Re-grant member role, then disable in Vanora and test Telegram + Feishu live sync
                provider.set_user('ttl-user', roles=('member',), enabled=True)
                permissions_sync.clear_cache()
                permissions_sync.sync_member('ttl-user', force=True)

                provider.set_user('ttl-user', roles=('member',), enabled=False)
                permissions_sync.clear_cache()
                bot.process_update({
                    'update_id': 1,
                    'message': {'text': '/book', 'from': {'id': 888}, 'chat': {'id': 888, 'type': 'private'}},
                })
                with db() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM tg_drafts').fetchone()[0], 0)

                # Feishu inbound also checks live sync
                feishu.accept_event(NS(event=NS(
                    message=NS(chat_type='p2p', message_type='text', message_id='om_ttl_1',
                               chat_id='oc_ttl', content=json.dumps({'text': '最近账目'})),
                    sender=NS(sender_type='user', sender_id=NS(open_id='ou_ttl')),
                )))
                self.assertTrue(feishu.process_one())
                with db() as conn:
                    reply = conn.execute('SELECT text FROM feishu_outbox ORDER BY id DESC LIMIT 1').fetchone()['text']
                self.assertIn('请先登录', reply)
        finally:
            provider.stop()

    def test_removed_local_management_and_password_endpoints_and_me_rename(self):
        provider = FakeVanoraOidc()
        provider.start()
        try:
            with TestClient(app) as client:
                owner = provider.login(client, sub='owner-one', preferred_username='owner1', name='第一位', roles=('owner',))
                headers = {'X-CSRF-Token': owner['csrf']}
                owner_id = owner['user']['id']

                self.assertEqual(client.post('/api/login', json={'username': 'owner1', 'password': 'x'}).status_code, 404)
                self.assertEqual(client.post('/api/password', json={'old_password': 'a', 'password': 'b'}, headers=headers).status_code, 404)
                self.assertEqual(client.post('/api/bootstrap', json={'username': 'x'}).status_code, 405)
                self.assertEqual(client.post('/api/members', json={'name': '本地'}, headers=headers).status_code, 405)
                self.assertEqual(client.patch(f'/api/members/{owner_id}', json={'role': 'finance'}, headers=headers).status_code, 404)
                self.assertEqual(client.get('/api/invites').status_code, 404)
                self.assertEqual(client.post('/api/invites', json={'role': 'member'}, headers=headers).status_code, 404)
                self.assertEqual(client.get('/api/auth/invite', params={'token': 't'}).status_code, 404)
                self.assertEqual(client.get('/api/member-events').status_code, 404)

                renamed = client.patch('/api/me', json={'name': '新昵称'}, headers=headers)
                self.assertEqual(renamed.status_code, 200, renamed.text)
                self.assertEqual(renamed.json()['user']['name'], '新昵称')
        finally:
            provider.stop()

    def test_return_hash_deep_link_sanitization_and_end_session(self):
        provider = FakeVanoraOidc()
        provider.start()
        try:
            with TestClient(app) as client:
                first = provider.login(
                    client, sub='deep-user', preferred_username='deep', name='深链', roles=('owner',), next_hash='#entry/42'
                )
                self.assertEqual(first['callback_location'], '/#entry/42')

                for bad_next in ('https://evil.example', '//evil.example', '#entry/42?x=1', '#javascript:alert(1)'):
                    with self.subTest(next_hash=bad_next):
                        attempts.clear()
                        res = provider.login(client, sub='deep-user', next_hash=bad_next)
                        self.assertEqual(res['callback_location'], '/')

                headers = {'X-CSRF-Token': client.get('/api/session').json()['csrf']}
                local_out = client.post('/api/logout', headers=headers)
                self.assertEqual(local_out.status_code, 200)
                self.assertNotIn('logout_url', local_out.json())

                with patch.dict(os.environ, {'VANORA_OIDC_END_SESSION': 'true'}):
                    session = provider.login(client, sub='deep-user')
                    full_out = client.post('/api/logout', headers={'X-CSRF-Token': session['csrf']})
                    self.assertEqual(full_out.status_code, 200)
                    logout_url = full_out.json().get('logout_url', '')
                    self.assertTrue(logout_url.startswith(provider.end_session_endpoint + '?'))
                    self.assertIn('client_id=' + provider.client_id, logout_url)
                    self.assertIn('post_logout_redirect_uri=http%3A%2F%2Ftestserver', logout_url)
        finally:
            provider.stop()

    def test_secret_file_and_internal_url_rewrite(self):
        secret_file = Path(self.temp.name) / 'oidc-secret.txt'
        secret_file.write_text('file-based-secret\n', encoding='utf-8')
        provider = FakeVanoraOidc(
            client_secret='file-based-secret',
            internal_url='http://vanora-internal:8080/realms/vanora',
        )
        provider.start(
            VANORA_OIDC_CLIENT_SECRET='',
            VANORA_OIDC_CLIENT_SECRET_FILE=str(secret_file),
            VANORA_OIDC_INTERNAL_URL='http://vanora-internal:8080/realms/vanora',
        )
        try:
            cfg = oidc.load_config()
            self.assertTrue(cfg.configured)
            self.assertEqual(cfg.client_secret, 'file-based-secret')
            provider.set_user('internal-user', username='intuser', name='内网测试', roles=('owner',))
            with TestClient(app) as client:
                flow = provider.start_flow(client)
                self.assertTrue(flow['location'].startswith(provider.authorization_endpoint + '?'))
                code = provider.issue_code(flow, sub='internal-user', preferred_username='intuser', name='内网测试')
                cb = client.get('/api/auth/oidc/callback', params={'code': code, 'state': flow['state']}, follow_redirects=False)
                self.assertEqual(cb.headers['location'], '/')
                sess = client.get('/api/session').json()
                self.assertEqual(client.post('/api/members/sync', headers={'X-CSRF-Token': sess['csrf']}).status_code, 200)
                fetched_urls = [url for url, _ in provider.requests]
                self.assertTrue(any(u == 'http://vanora-internal:8080/realms/vanora/.well-known/openid-configuration' for u in fetched_urls))
                self.assertTrue(any(u == 'http://vanora-internal:8080/realms/vanora/protocol/openid-connect/token' for u in fetched_urls))
                self.assertTrue(any(u == 'http://vanora-internal:8080/realms/vanora/protocol/openid-connect/certs' for u in fetched_urls))
                self.assertTrue(any(u == 'http://vanora-internal:8080/realms/vanora/vanora-permissions/members' for u in fetched_urls))
        finally:
            provider.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
