"""Vanora OAuth (OpenID Connect) client: PKCE S256, JWKS RS256 verification, client roles & permission sync."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import jwt
from jwt import InvalidTokenError, PyJWK

ALLOWED_ALGS = (
    'RS256', 'RS384', 'RS512',
    'PS256', 'PS384', 'PS512',
    'ES256', 'ES384', 'ES512',
    'EdDSA',
)
BACKCHANNEL_LOGOUT_EVENT = 'http://schemas.openid.net/event/backchannel-logout'
PERMISSIONS_CHANGED_EVENT = 'https://vanora.dev/events/permissions-changed'
ACCOUNT_DELETED_EVENT = 'https://vanora.dev/events/account-deleted'
ROLE_PRECEDENCE = ('owner', 'finance', 'member')
SUBJECT_PATTERN = re.compile(r'[A-Za-z0-9._:@/-]{1,128}')
RETURN_HASH_PATTERN = re.compile(r'^#[a-z]{1,30}(?:/\d{1,12})?$')
HTTP_TIMEOUT = 10
CACHE_TTL = 600
JWKS_REFRESH_COOLDOWN = 15
DEFAULT_PERMISSIONS_TTL = 30

_cache_lock = threading.Lock()
_discovery_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, float, dict]] = {}


class OidcError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    scopes: str
    internal_url: str
    ca_file: str
    acr_values: str
    end_session: bool
    permissions_sync_ttl: int
    admin_url: str
    roles_claim: str

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret)

    @property
    def fetch_base(self) -> str:
        return self.internal_url or self.issuer

    @property
    def account_url(self) -> str:
        return f'{self.issuer}/account/' if self.issuer else ''


def _clean_url(raw: str) -> str:
    value = (raw or '').strip().rstrip('/')
    if not value:
        return ''
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError:
        return ''
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return ''
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return ''
    return value


def _read_secret_file(path_value: str) -> str:
    path_str = (path_value or '').strip()
    if not path_str:
        return ''
    try:
        return Path(path_str).read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def _clean_ttl(raw: str, default: int = DEFAULT_PERMISSIONS_TTL) -> int:
    text = (raw or '').strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return max(0, min(value, 3600))


def load_config() -> OidcConfig:
    issuer = _clean_url(os.getenv('VANORA_OIDC_ISSUER', ''))
    client_id = os.getenv('VANORA_OIDC_CLIENT_ID', '').strip()
    client_secret = os.getenv('VANORA_OIDC_CLIENT_SECRET', '').strip()
    if not client_secret:
        client_secret = _read_secret_file(os.getenv('VANORA_OIDC_CLIENT_SECRET_FILE', ''))
    scopes = ' '.join(os.getenv('VANORA_OIDC_SCOPES', 'openid profile email').split()) or 'openid profile email'
    if 'openid' not in scopes.split():
        scopes = 'openid ' + scopes
    internal_url = _clean_url(os.getenv('VANORA_OIDC_INTERNAL_URL', ''))
    ca_file = os.getenv('VANORA_OIDC_CA_FILE', '').strip()
    acr_values = ' '.join(os.getenv('VANORA_OIDC_ACR_VALUES', '').split())
    end_session = os.getenv('VANORA_OIDC_END_SESSION', 'false').strip().lower() == 'true'
    permissions_sync_ttl = _clean_ttl(os.getenv('VANORA_OIDC_PERMISSIONS_TTL', ''))
    admin_url = _clean_url(os.getenv('VANORA_OIDC_ADMIN_URL', ''))
    roles_claim = os.getenv('VANORA_OIDC_ROLES_CLAIM', '').strip()
    return OidcConfig(
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        internal_url=internal_url,
        ca_file=ca_file,
        acr_values=acr_values,
        end_session=end_session,
        permissions_sync_ttl=permissions_sync_ttl,
        admin_url=admin_url,
        roles_claim=roles_claim,
    )


def clear_cache():
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_cache.clear()
    try:
        from . import permissions_sync
        permissions_sync.clear_cache()
    except Exception:
        pass


def require_config() -> OidcConfig:
    cfg = load_config()
    if not cfg.configured:
        raise OidcError(
            'oidc_unconfigured',
            '尚未配置 Vanora OAuth，请在服务器环境变量中设置 VANORA_OIDC_ISSUER、VANORA_OIDC_CLIENT_ID 和 VANORA_OIDC_CLIENT_SECRET。',
            503,
        )
    return cfg


def public_base_url(request) -> str:
    configured = _clean_url(os.getenv('PUBLIC_URL', ''))
    if configured:
        return configured
    return str(request.base_url).rstrip('/')


def callback_url(request) -> str:
    return public_base_url(request) + '/api/auth/oidc/callback'


def backchannel_logout_url(request) -> str:
    return public_base_url(request) + '/api/auth/oidc/backchannel-logout'


def sanitize_return_hash(value: str | None) -> str:
    text = (value or '').strip()
    if not text:
        return ''
    if not text.startswith('#'):
        text = '#' + text
    return text if RETURN_HASH_PATTERN.fullmatch(text) else ''


def validate_subject(value: str) -> str:
    if not isinstance(value, str):
        raise OidcError('invalid_subject', 'Vanora 用户 ID 格式不正确')
    cleaned = value.strip()
    if not SUBJECT_PATTERN.fullmatch(cleaned):
        raise OidcError('invalid_subject', 'Vanora 用户 ID 格式不正确')
    return cleaned


def resolve_role(roles) -> str | None:
    if isinstance(roles, str):
        normalized = {roles.strip()}
    elif isinstance(roles, (list, tuple, set)):
        normalized = {str(item).strip() for item in roles if isinstance(item, str) and item.strip()}
    else:
        return None
    for candidate in ROLE_PRECEDENCE:
        if candidate in normalized:
            return candidate
    return None


def _nested_claim(claims: dict, dotted_path: str):
    cursor = claims
    for part in dotted_path.split('.'):
        if not isinstance(cursor, dict) or part not in cursor:
            return None, False
        cursor = cursor[part]
    return cursor, True


def extract_client_roles(claims: dict, cfg: OidcConfig) -> tuple[list[str], bool]:
    """Return (roles_list, claim_present) extracted from OIDC JWT claims."""
    if not isinstance(claims, dict):
        return [], False
    if cfg.roles_claim:
        val, present = _nested_claim(claims, cfg.roles_claim)
        if present:
            return ([str(r).strip() for r in val if isinstance(r, str) and r.strip()] if isinstance(val, list) else []), True
        return [], False
    resource_access = claims.get('resource_access')
    if isinstance(resource_access, dict):
        client_access = resource_access.get(cfg.client_id)
        if isinstance(client_access, dict) and 'roles' in client_access:
            raw = client_access.get('roles')
            roles = [str(r).strip() for r in raw if isinstance(r, str) and r.strip()] if isinstance(raw, list) else []
            return roles, True
    if 'roles' in claims and isinstance(claims.get('roles'), list):
        roles = [str(r).strip() for r in claims['roles'] if isinstance(r, str) and r.strip()]
        return roles, True
    return [], False


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


def _rewrite_internal(url: str, cfg: OidcConfig) -> str:
    if cfg.internal_url and url.startswith(cfg.issuer + '/'):
        return cfg.internal_url + url[len(cfg.issuer):]
    if cfg.internal_url and url == cfg.issuer:
        return cfg.internal_url
    return url


def _ssl_context(cfg: OidcConfig) -> ssl.SSLContext | None:
    if cfg.ca_file:
        return ssl.create_default_context(cafile=cfg.ca_file)
    return None


def _client_basic_auth_header(cfg: OidcConfig) -> str:
    # RFC 6749 §2.3.1 requires form-urlencoded client_id and client_secret before Base64.
    encoded_id = urllib.parse.quote_plus(cfg.client_id)
    encoded_secret = urllib.parse.quote_plus(cfg.client_secret)
    raw = f'{encoded_id}:{encoded_secret}'.encode('utf-8')
    return 'Basic ' + base64.b64encode(raw).decode('ascii')


def _http_json(url: str, cfg: OidcConfig, *, data: dict | None = None, headers: dict | None = None) -> dict:
    encoded = None
    req_headers = {'Accept': 'application/json', 'User-Agent': 'team-ledger-oidc/1'}
    if headers:
        req_headers.update(headers)
    method = 'GET'
    if data is not None:
        encoded = urllib.parse.urlencode(data).encode('utf-8')
        req_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        method = 'POST'
    req = urllib.request.Request(url, data=encoded, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_ssl_context(cfg)) as resp:
            raw = resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise OidcError('upstream_http_error', f'Vanora OAuth 接口返回错误（HTTP {exc.code}）', 502) from None
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
        raise OidcError('upstream_unreachable', '无法连接到 Vanora OAuth 服务，请稍后重试', 502) from None
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OidcError('upstream_invalid_json', 'Vanora OAuth 返回了无效的响应格式', 502) from None
    if not isinstance(payload, dict):
        raise OidcError('upstream_invalid_json', 'Vanora OAuth 返回了无效的响应格式', 502)
    return payload


def discover(cfg: OidcConfig, *, force: bool = False) -> dict:
    key = (cfg.issuer, cfg.fetch_base)
    now_ts = time.time()
    if not force:
        with _cache_lock:
            cached = _discovery_cache.get(key)
            if cached and cached[0] > now_ts:
                return cached[1]
    url = f'{cfg.fetch_base}/.well-known/openid-configuration'
    doc = _http_json(url, cfg)
    if str(doc.get('issuer', '')).rstrip('/') != cfg.issuer:
        raise OidcError('invalid_discovery', 'Vanora OAuth 发现文档的 issuer 与配置不匹配', 502)
    for field in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        if not isinstance(doc.get(field), str) or not _clean_url(doc[field]):
            raise OidcError('invalid_discovery', f'Vanora OAuth 发现文档缺少有效的 {field}', 502)
    with _cache_lock:
        _discovery_cache[key] = (now_ts + CACHE_TTL, doc)
    return doc


def fetch_jwks(jwks_uri: str, cfg: OidcConfig, *, force: bool = False) -> dict:
    target = _rewrite_internal(jwks_uri, cfg)
    now_ts = time.time()
    with _cache_lock:
        cached = _jwks_cache.get(target)
        if cached:
            expires_at, last_fetched, doc = cached
            if not force and expires_at > now_ts:
                return doc
            if force and (now_ts - last_fetched) < JWKS_REFRESH_COOLDOWN:
                return doc
    doc = _http_json(target, cfg)
    keys = doc.get('keys')
    if not isinstance(keys, list) or not keys:
        raise OidcError('invalid_jwks', 'Vanora OAuth 公钥列表为空或格式无效', 502)
    with _cache_lock:
        _jwks_cache[target] = (now_ts + CACHE_TTL, now_ts, doc)
    return doc


def fetch_members(cfg: OidcConfig) -> list[dict]:
    """Fetch all users holding client roles for this client from Vanora OAuth."""
    url = f'{cfg.fetch_base}/vanora-permissions/members'
    doc = _http_json(url, cfg, headers={'Authorization': _client_basic_auth_header(cfg)})
    members = doc.get('members')
    if not isinstance(members, list):
        raise OidcError('upstream_invalid_json', 'Vanora OAuth 成员权限列表格式无效', 502)
    result = []
    for item in members:
        if not isinstance(item, dict):
            continue
        sub = str(item.get('sub') or '').strip()
        if not sub or not SUBJECT_PATTERN.fullmatch(sub):
            continue
        roles = [str(r).strip() for r in (item.get('roles') or []) if isinstance(r, str) and r.strip()]
        result.append({
            'sub': sub,
            'username': str(item.get('username') or '').strip(),
            'name': str(item.get('name') or '').strip(),
            'email': str(item.get('email') or '').strip(),
            'enabled': bool(item.get('enabled', False)),
            'roles': roles,
            'role': resolve_role(roles),
        })
    return result


def fetch_member(cfg: OidcConfig, sub: str) -> dict:
    """Fetch a single user's authoritative role and enabled status from Vanora OAuth."""
    cleaned_sub = validate_subject(sub)
    url = f'{cfg.fetch_base}/vanora-permissions/members/{urllib.parse.quote(cleaned_sub, safe="")}'
    doc = _http_json(url, cfg, headers={'Authorization': _client_basic_auth_header(cfg)})
    roles = [str(r).strip() for r in (doc.get('roles') or []) if isinstance(r, str) and r.strip()]
    role = resolve_role(roles)
    enabled = bool(doc.get('enabled', False))
    exists = bool(doc.get('exists', False))
    authorized = bool(doc.get('authorized', enabled and bool(role))) and enabled and bool(role)
    return {
        'sub': cleaned_sub,
        'exists': exists,
        'username': str(doc.get('username') or '').strip(),
        'name': str(doc.get('name') or '').strip(),
        'email': str(doc.get('email') or '').strip(),
        'enabled': enabled,
        'roles': roles,
        'role': role,
        'authorized': authorized,
    }


def _select_jwk(jwks: dict, kid: str | None, alg: str) -> dict | None:
    candidates = []
    for entry in jwks.get('keys', []):
        if not isinstance(entry, dict):
            continue
        use = entry.get('use')
        if use and use != 'sig':
            continue
        key_alg = entry.get('alg')
        if key_alg and key_alg != alg:
            continue
        if kid:
            if entry.get('kid') == kid:
                return entry
        else:
            candidates.append(entry)
    if not kid and len(candidates) == 1:
        return candidates[0]
    return None


def _decode_jwt(token: str, cfg: OidcConfig, *, required_claims: list[str], code: str) -> dict:
    if not isinstance(token, str) or token.count('.') != 2:
        raise OidcError(code, 'Vanora 令牌格式无效')
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError:
        raise OidcError(code, 'Vanora 令牌头格式无效') from None
    alg = header.get('alg')
    if alg not in ALLOWED_ALGS:
        raise OidcError(code, '不支持的令牌签名算法')
    kid = header.get('kid')
    if kid is not None and (not isinstance(kid, str) or not kid.strip()):
        raise OidcError(code, '令牌密钥标识无效')

    doc = discover(cfg)
    jwks = fetch_jwks(doc['jwks_uri'], cfg, force=False)
    jwk_dict = _select_jwk(jwks, kid, alg)
    if jwk_dict is None:
        jwks = fetch_jwks(doc['jwks_uri'], cfg, force=True)
        jwk_dict = _select_jwk(jwks, kid, alg)
    if jwk_dict is None:
        raise OidcError(code, '未找到匹配的 Vanora 签名公钥')

    try:
        signing_key = PyJWK.from_dict(jwk_dict, algorithm=alg).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=[alg],
            audience=cfg.client_id,
            issuer=cfg.issuer,
            leeway=30,
            options={'require': required_claims},
        )
    except InvalidTokenError:
        raise OidcError(code, 'Vanora 令牌校验失败') from None
    except Exception:
        raise OidcError(code, 'Vanora 令牌校验失败') from None
    return claims


def build_authorization_url(
    cfg: OidcConfig,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    doc = discover(cfg)
    params = {
        'response_type': 'code',
        'client_id': cfg.client_id,
        'redirect_uri': redirect_uri,
        'scope': cfg.scopes,
        'state': state,
        'nonce': nonce,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    if cfg.acr_values:
        params['acr_values'] = cfg.acr_values
    sep = '&' if '?' in doc['authorization_endpoint'] else '?'
    return doc['authorization_endpoint'] + sep + urllib.parse.urlencode(params)


def build_end_session_url(cfg: OidcConfig, *, post_logout_redirect_uri: str) -> str:
    if not cfg.configured or not cfg.end_session:
        return ''
    try:
        doc = discover(cfg)
    except OidcError:
        return ''
    endpoint = doc.get('end_session_endpoint')
    if not isinstance(endpoint, str) or not _clean_url(endpoint):
        return ''
    params = {
        'client_id': cfg.client_id,
        'post_logout_redirect_uri': post_logout_redirect_uri,
    }
    sep = '&' if '?' in endpoint else '?'
    return endpoint + sep + urllib.parse.urlencode(params)


def exchange_code(cfg: OidcConfig, *, code: str, redirect_uri: str, code_verifier: str) -> dict:
    doc = discover(cfg)
    token_url = _rewrite_internal(doc['token_endpoint'], cfg)
    return _http_json(
        token_url,
        cfg,
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
            'client_id': cfg.client_id,
            'client_secret': cfg.client_secret,
            'code_verifier': code_verifier,
        },
    )


def verify_id_token(id_token: str, cfg: OidcConfig, *, expected_nonce: str) -> dict:
    claims = _decode_jwt(
        id_token,
        cfg,
        required_claims=['iss', 'sub', 'aud', 'exp', 'iat'],
        code='invalid_token',
    )
    sub = claims.get('sub')
    if not isinstance(sub, str) or not SUBJECT_PATTERN.fullmatch(sub.strip()):
        raise OidcError('invalid_token', 'ID Token 缺少有效的用户标识')
    aud = claims.get('aud')
    azp = claims.get('azp')
    if isinstance(aud, list) and len(aud) > 1 and azp != cfg.client_id:
        raise OidcError('invalid_token', 'ID Token 授权方与客户端不匹配')
    if azp is not None and azp != cfg.client_id:
        raise OidcError('invalid_token', 'ID Token 授权方与客户端不匹配')
    nonce = claims.get('nonce')
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
        raise OidcError('invalid_token', 'ID Token 一次性校验值不匹配')
    if cfg.acr_values:
        allowed_acrs = set(cfg.acr_values.split())
        token_acr = str(claims.get('acr', '')).strip()
        if token_acr not in allowed_acrs:
            raise OidcError('invalid_token', '当前认证强度不满足安全要求')
    preferred_username = claims.get('preferred_username')
    if not isinstance(preferred_username, str):
        preferred_username = ''
    name = claims.get('name')
    if not isinstance(name, str):
        name = ''
    email = claims.get('email')
    if not isinstance(email, str):
        email = ''
    sid = claims.get('sid')
    if not isinstance(sid, str):
        sid = ''
    roles, has_roles_claim = extract_client_roles(claims, cfg)
    return {
        'issuer': cfg.issuer,
        'sub': sub.strip(),
        'sid': sid.strip(),
        'preferred_username': preferred_username.strip(),
        'name': (name.strip() or preferred_username.strip()),
        'email': email.strip(),
        'acr': str(claims.get('acr', '')).strip(),
        'roles': roles,
        'role': resolve_role(roles),
        'has_roles_claim': has_roles_claim,
    }


def verify_logout_token(logout_token: str, cfg: OidcConfig) -> dict:
    claims = _decode_jwt(
        logout_token,
        cfg,
        required_claims=['iss', 'aud', 'exp', 'iat', 'jti', 'events'],
        code='invalid_logout_token',
    )
    if 'nonce' in claims:
        raise OidcError('invalid_logout_token', 'Logout Token 不得包含 nonce')
    events = claims.get('events')
    if not isinstance(events, dict) or not isinstance(events.get(BACKCHANNEL_LOGOUT_EVENT), dict):
        raise OidcError('invalid_logout_token', 'Logout Token 缺少后端注销事件声明')
    jti = claims.get('jti')
    if not isinstance(jti, str) or not jti.strip():
        raise OidcError('invalid_logout_token', 'Logout Token 缺少 jti')
    sid = claims.get('sid')
    sub = claims.get('sub')
    sid_str = sid.strip() if isinstance(sid, str) else ''
    sub_str = sub.strip() if isinstance(sub, str) else ''
    if not sid_str and not sub_str:
        raise OidcError('invalid_logout_token', 'Logout Token 缺少 sid 或 sub')
    exp = int(claims['exp'])
    return {
        'issuer': cfg.issuer,
        'jti': jti.strip(),
        'sid': sid_str,
        'sub': sub_str,
        'exp': exp,
        'events': list(events.keys()),
    }
