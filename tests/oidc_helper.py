"""Reusable RS256 Vanora OAuth (OIDC) test double with centralized permissions API."""
import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app import oidc, permissions_sync

DEFAULT_ISSUER = 'https://auth.vanora.test/realms/vanora'
DEFAULT_CLIENT_ID = 'team-ledger-test'
DEFAULT_CLIENT_SECRET = 'test-oidc-client-secret'


def _generate_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_DEFAULT_KEY = _generate_rsa_key()


class FakeVanoraOidc:
    """In-memory RS256 Vanora OAuth provider with client roles and vanora-permissions sync API."""

    def __init__(
        self,
        *,
        issuer: str = DEFAULT_ISSUER,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        internal_url: str = '',
        extra_env: dict | None = None,
    ):
        self.issuer = issuer.rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self.internal_url = internal_url.rstrip('/')
        self.extra_env = dict(extra_env or {})
        self.keys: dict[str, object] = {'vanora-key-1': _DEFAULT_KEY}
        self.active_kid = 'vanora-key-1'
        self.codes: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.requests: list[tuple[str, dict | None]] = []
        self.fail_exchange = False
        self.fail_permissions = False
        self._env_patch = None
        self._http_patch = None

    @property
    def authorization_endpoint(self) -> str:
        return f'{self.issuer}/protocol/openid-connect/auth'

    @property
    def token_endpoint(self) -> str:
        return f'{self.issuer}/protocol/openid-connect/token'

    @property
    def jwks_uri(self) -> str:
        return f'{self.issuer}/protocol/openid-connect/certs'

    @property
    def end_session_endpoint(self) -> str:
        return f'{self.issuer}/protocol/openid-connect/logout'

    def set_user(
        self,
        sub: str,
        *,
        username: str = '',
        name: str = '',
        email: str = '',
        roles=('member',),
        enabled: bool = True,
    ) -> dict:
        existing = self.users.get(sub, {})
        record = {
            'sub': sub,
            'username': username or existing.get('username') or sub,
            'name': name or existing.get('name') or username or existing.get('username') or sub,
            'email': email if email != '' else existing.get('email', ''),
            'enabled': bool(enabled),
            'roles': list(roles),
        }
        self.users[sub] = record
        return dict(record)

    def remove_user(self, sub: str) -> None:
        self.users.pop(sub, None)

    def env(self, **overrides) -> dict[str, str]:
        data = {
            'VANORA_OIDC_ISSUER': self.issuer,
            'VANORA_OIDC_CLIENT_ID': self.client_id,
            'VANORA_OIDC_CLIENT_SECRET': self.client_secret,
            'VANORA_OIDC_CLIENT_SECRET_FILE': '',
            'VANORA_OIDC_SCOPES': 'openid profile email',
            'VANORA_OIDC_ACR_VALUES': '',
            'VANORA_OIDC_INTERNAL_URL': self.internal_url,
            'VANORA_OIDC_CA_FILE': '',
            'VANORA_OIDC_END_SESSION': 'false',
            'VANORA_OIDC_PERMISSIONS_TTL': '30',
            'VANORA_OIDC_ADMIN_URL': '',
            'VANORA_OIDC_ROLES_CLAIM': '',
        }
        data.update(self.extra_env)
        data.update({k: str(v) for k, v in overrides.items()})
        return data

    def start(self, **env_overrides):
        oidc.clear_cache()
        permissions_sync.clear_cache()
        self._env_patch = patch.dict(os.environ, self.env(**env_overrides))
        self._http_patch = patch('app.oidc._http_json', side_effect=self.handle_http_json)
        self._env_patch.start()
        self._http_patch.start()
        return self

    def stop(self):
        if self._http_patch is not None:
            self._http_patch.stop()
            self._http_patch = None
        if self._env_patch is not None:
            self._env_patch.stop()
            self._env_patch = None
        oidc.clear_cache()
        permissions_sync.clear_cache()

    def rotate_key(self, new_kid: str | None = None, *, keep_old: bool = False) -> str:
        kid = new_kid or f'vanora-key-{len(self.keys) + 1}'
        new_key = _generate_rsa_key()
        if not keep_old:
            self.keys = {kid: new_key}
        else:
            self.keys[kid] = new_key
        self.active_kid = kid
        return kid

    def discovery_document(self) -> dict:
        return {
            'issuer': self.issuer,
            'authorization_endpoint': self.authorization_endpoint,
            'token_endpoint': self.token_endpoint,
            'jwks_uri': self.jwks_uri,
            'end_session_endpoint': self.end_session_endpoint,
        }

    def jwks_document(self) -> dict:
        entries = []
        for kid, private_key in self.keys.items():
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
            jwk['use'] = 'sig'
            jwk['alg'] = 'RS256'
            jwk['kid'] = kid
            entries.append(jwk)
        return {'keys': entries}

    def sign_jwt(
        self,
        claims: dict,
        *,
        kid: str | None = None,
        algorithm: str = 'RS256',
        signing_key=None,
    ) -> str:
        chosen_kid = self.active_kid if kid is None else kid
        headers = {'kid': chosen_kid} if chosen_kid else {}
        key = signing_key if signing_key is not None else self.keys[self.active_kid]
        return jwt.encode(claims, key, algorithm=algorithm, headers=headers)

    def make_id_token(
        self,
        *,
        sub: str,
        nonce: str,
        preferred_username: str = '',
        name: str = '',
        email: str | None = '',
        roles=...,
        omit_roles: bool = False,
        sid: str | None = None,
        acr: str = '1',
        aud=None,
        azp: str | None = ...,
        iss: str | None = None,
        exp: int | None = None,
        iat: int | None = None,
        kid: str | None = None,
        extra_claims: dict | None = None,
        signing_key=None,
        algorithm: str = 'RS256',
    ) -> str:
        now_ts = int(time.time())
        directory_user = self.users.get(sub)
        if roles is ...:
            effective_roles = list(directory_user['roles']) if directory_user else []
        else:
            effective_roles = list(roles or ())
        claims = {
            'iss': self.issuer if iss is None else iss,
            'sub': sub,
            'aud': self.client_id if aud is None else aud,
            'iat': now_ts if iat is None else iat,
            'exp': (now_ts + 300) if exp is None else exp,
            'nonce': nonce,
            'sid': secrets.token_hex(8) if sid is None else sid,
            'acr': acr,
            'preferred_username': preferred_username or (directory_user['username'] if directory_user else ''),
            'name': name or preferred_username or (directory_user['name'] if directory_user else ''),
        }
        if not omit_roles:
            claims['resource_access'] = {self.client_id: {'roles': effective_roles}}
        if azp is ...:
            claims['azp'] = self.client_id
        elif azp is not None:
            claims['azp'] = azp
        effective_email = email if email is not None else ''
        if not effective_email and email is not None and directory_user:
            effective_email = directory_user.get('email', '')
        if effective_email:
            claims['email'] = effective_email
            claims['email_verified'] = True
        if extra_claims:
            claims.update(extra_claims)
        return self.sign_jwt(claims, kid=kid, algorithm=algorithm, signing_key=signing_key)

    def make_logout_token(
        self,
        *,
        sub: str = '',
        sid: str = '',
        jti: str | None = None,
        iss: str | None = None,
        aud=None,
        exp: int | None = None,
        iat: int | None = None,
        include_events: bool = True,
        extra_events: list[str] | tuple[str, ...] | None = None,
        include_nonce: bool = False,
        kid: str | None = None,
        extra_claims: dict | None = None,
    ) -> str:
        now_ts = int(time.time())
        claims = {
            'iss': self.issuer if iss is None else iss,
            'aud': self.client_id if aud is None else aud,
            'iat': now_ts if iat is None else iat,
            'exp': (now_ts + 120) if exp is None else exp,
            'jti': secrets.token_urlsafe(16) if jti is None else jti,
        }
        if include_events:
            events = {oidc.BACKCHANNEL_LOGOUT_EVENT: {}}
            for event_uri in extra_events or ():
                events[event_uri] = {}
            claims['events'] = events
        if sub:
            claims['sub'] = sub
        if sid:
            claims['sid'] = sid
        if include_nonce:
            claims['nonce'] = 'forbidden-nonce'
        if extra_claims:
            claims.update(extra_claims)
        return self.sign_jwt(claims, kid=kid)

    def send_backchannel_logout(
        self,
        client,
        *,
        sub: str = '',
        sid: str = '',
        jti: str | None = None,
        extra_events: list[str] | tuple[str, ...] | None = None,
    ):
        token = self.make_logout_token(sub=sub, sid=sid, jti=jti, extra_events=extra_events)
        body = urllib.parse.urlencode({'logout_token': token})
        return client.post(
            '/api/auth/oidc/backchannel-logout',
            content=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )

    def _verify_client_basic_auth(self, headers: dict | None) -> None:
        auth = (headers or {}).get('Authorization', '')
        expected_raw = f'{urllib.parse.quote_plus(self.client_id)}:{urllib.parse.quote_plus(self.client_secret)}'
        expected = 'Basic ' + base64.b64encode(expected_raw.encode('ascii')).decode('ascii')
        if auth != expected:
            raise oidc.OidcError('upstream_http_error', 'Invalid Basic client authentication', 401)

    def handle_http_json(
        self,
        url: str,
        cfg: oidc.OidcConfig,
        *,
        data: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        self.requests.append((url, data))
        if url.endswith('/.well-known/openid-configuration'):
            return self.discovery_document()
        if url.endswith('/protocol/openid-connect/certs'):
            return self.jwks_document()
        if url.endswith('/vanora-permissions/members'):
            self._verify_client_basic_auth(headers)
            if self.fail_permissions:
                raise oidc.OidcError('upstream_unreachable', 'Simulated permissions sync failure', 502)
            members = [
                {
                    'sub': u['sub'],
                    'username': u['username'],
                    'name': u['name'],
                    'email': u.get('email', ''),
                    'enabled': bool(u['enabled']),
                    'roles': list(u['roles']),
                }
                for u in self.users.values()
                if u.get('roles')
            ]
            return {'issuer': self.issuer, 'client_id': self.client_id, 'members': members}
        if '/vanora-permissions/members/' in url:
            self._verify_client_basic_auth(headers)
            if self.fail_permissions:
                raise oidc.OidcError('upstream_unreachable', 'Simulated permissions sync failure', 502)
            encoded_sub = url.split('/vanora-permissions/members/', 1)[1].split('?', 1)[0]
            subject = urllib.parse.unquote(encoded_sub)
            u = self.users.get(subject)
            if not u:
                return {
                    'issuer': self.issuer,
                    'client_id': self.client_id,
                    'sub': subject,
                    'exists': False,
                    'enabled': False,
                    'roles': [],
                    'authorized': False,
                }
            roles = list(u.get('roles') or [])
            if not roles:
                return {
                    'issuer': self.issuer,
                    'client_id': self.client_id,
                    'sub': subject,
                    'exists': True,
                    'enabled': bool(u['enabled']),
                    'roles': [],
                    'authorized': False,
                }
            return {
                'issuer': self.issuer,
                'client_id': self.client_id,
                'sub': subject,
                'exists': True,
                'enabled': bool(u['enabled']),
                'roles': roles,
                'authorized': bool(u['enabled'] and roles),
                'username': u['username'],
                'name': u['name'],
                'email': u.get('email', ''),
            }
        if url.endswith('/protocol/openid-connect/token'):
            if self.fail_exchange:
                raise oidc.OidcError('upstream_http_error', 'Simulated token exchange failure', 502)
            if not isinstance(data, dict) or data.get('grant_type') != 'authorization_code':
                raise oidc.OidcError('upstream_http_error', 'Invalid grant_type', 400)
            if data.get('client_id') != self.client_id or data.get('client_secret') != self.client_secret:
                raise oidc.OidcError('upstream_http_error', 'Invalid client credentials', 401)
            code = data.get('code', '')
            record = self.codes.pop(code, None)
            if not record:
                raise oidc.OidcError('upstream_http_error', 'Invalid or reused authorization code', 400)
            if data.get('redirect_uri') != record['redirect_uri']:
                raise oidc.OidcError('upstream_http_error', 'redirect_uri mismatch', 400)
            verifier = data.get('code_verifier', '')
            digest = hashlib.sha256(verifier.encode('ascii')).digest()
            computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
            if computed != record['code_challenge']:
                raise oidc.OidcError('upstream_http_error', 'PKCE S256 verification failed', 400)
            id_token = record.get('raw_id_token')
            if id_token is None:
                id_token = self.make_id_token(nonce=record['nonce'], **record['token_kwargs'])
            return {
                'access_token': 'fake-access-token',
                'token_type': 'Bearer',
                'expires_in': 300,
                'id_token': id_token,
            }
        raise oidc.OidcError('upstream_unreachable', f'Unexpected OIDC URL: {url}', 502)

    def start_flow(self, client, *, team_name: str = '', next_hash: str = ''):
        params = {}
        if team_name:
            params['team_name'] = team_name
        if next_hash:
            params['next'] = next_hash
        resp = client.get('/api/auth/oidc/login', params=params, follow_redirects=False)
        if resp.status_code != 302:
            raise AssertionError(f'Expected 302 from /api/auth/oidc/login, got {resp.status_code}: {resp.text}')
        parsed = urllib.parse.urlsplit(resp.headers['location'])
        query = urllib.parse.parse_qs(parsed.query)
        assert query.get('response_type') == ['code']
        assert query.get('code_challenge_method') == ['S256']
        assert query.get('client_id') == [self.client_id]
        return {
            'response': resp,
            'location': resp.headers['location'],
            'state': query['state'][0],
            'nonce': query['nonce'][0],
            'redirect_uri': query['redirect_uri'][0],
            'code_challenge': query['code_challenge'][0],
            'scope': query['scope'][0],
            'acr_values': query.get('acr_values', [''])[0],
        }

    def issue_code(self, flow: dict, *, raw_id_token: str | None = None, **token_kwargs) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            'redirect_uri': flow['redirect_uri'],
            'code_challenge': flow['code_challenge'],
            'nonce': flow['nonce'],
            'raw_id_token': raw_id_token,
            'token_kwargs': token_kwargs,
        }
        return code

    def login(
        self,
        client,
        *,
        sub: str,
        preferred_username: str = '',
        name: str = '',
        email: str | None = '',
        roles=...,
        omit_roles: bool = False,
        enabled: bool = True,
        register: bool = True,
        sid: str | None = None,
        acr: str = '1',
        team_name: str = '',
        next_hash: str = '',
        raw_id_token: str | None = None,
        extra_claims: dict | None = None,
        expect_error: str | None = None,
        token_overrides: dict | None = None,
    ):
        if register and roles is not ...:
            self.set_user(
                sub,
                username=preferred_username,
                name=name,
                email='' if email is None else email,
                roles=roles,
                enabled=enabled,
            )
        flow = self.start_flow(client, team_name=team_name, next_hash=next_hash)
        kwargs = {
            'sub': sub,
            'preferred_username': preferred_username,
            'name': name,
            'email': email,
            'roles': roles,
            'omit_roles': omit_roles,
            'sid': sid,
            'acr': acr,
            'extra_claims': extra_claims,
        }
        if token_overrides:
            kwargs.update(token_overrides)
        code = self.issue_code(flow, raw_id_token=raw_id_token, **kwargs)
        callback = client.get(
            '/api/auth/oidc/callback',
            params={'code': code, 'state': flow['state']},
            follow_redirects=False,
        )
        assert callback.status_code == 302, f'Expected 302 from callback, got {callback.status_code}: {callback.text}'
        location = callback.headers['location']
        if expect_error is not None:
            assert location == f'/?auth_error={expect_error}', f'Expected auth_error={expect_error}, got {location}'
            return callback
        if 'auth_error=' in location:
            raise AssertionError(f'OIDC login unexpectedly failed with redirect: {location}')
        session_resp = client.get('/api/session')
        assert session_resp.status_code == 200, f'Session failed after OIDC login: {session_resp.status_code} {session_resp.text}'
        return {**session_resp.json(), 'callback_location': location}
