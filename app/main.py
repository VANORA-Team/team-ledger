import base64
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from fastapi import FastAPI, Query, Request, Response, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from . import config, ai, ai_context, ai_plan, backups, excel_export, fx, oidc, permissions_sync
from .backup_api import router as backup_router
from .db import USER_ROLES, db, init_db, settings, remove_receipt_file, authenticated_session
from .domain import (fail, admin, owner, is_finance, require_entry_access, public_user,
                     text_value, add_entry, delete_entry as remove_entry, edit_entry, transition, entry_view, audit,
                     save_settings, make_payment_quote, currency, receipt_media_type)

PUBLIC = config.ROOT / 'public'
attempts = defaultdict(deque)
attempt_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler = backups.Scheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title='Vanora 财务管理', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(backup_router)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({'error': exc.detail}, status_code=exc.status_code)


@app.exception_handler(backups.BackupError)
async def backup_error(request, exc):
    return JSONResponse({'error': str(exc)}, status_code=exc.status)


@app.exception_handler(oidc.OidcError)
async def oidc_error(request, exc):
    return JSONResponse({'error': exc.message, 'code': exc.code}, status_code=exc.status)


def _feishu_embed_ancestors():
    custom = os.getenv('FEISHU_FRAME_ANCESTORS', '').strip()
    if custom:
        origins = [item for item in custom.split() if re.fullmatch(r'https://[A-Za-z0-9.*:-]{1,180}', item)]
        if origins:
            return "'self' " + ' '.join(origins)
    if os.getenv('FEISHU_EMBED', 'false').lower() == 'true':
        return "'self' https://*.feishu.cn https://*.larkoffice.com https://*.larksuite.com"
    return None


@app.middleware('http')
async def security(request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        accepted = {str(request.base_url).rstrip('/')}
        if os.getenv('PUBLIC_URL'):
            accepted.add(os.environ['PUBLIC_URL'].rstrip('/'))
        if origin and origin.rstrip('/') not in accepted:
            return JSONResponse({'error': '请求来源不正确'}, status_code=403)
        uploading_backup = request.url.path == '/api/backups/restore/upload'
        maximum = backups.max_bytes() if uploading_backup else 9_000_000
        try:
            length = int(request.headers.get('content-length', '0') or 0)
        except ValueError:
            return JSONResponse({'error': '上传长度不正确'}, status_code=400)
        if length < 0 or length > maximum:
            return JSONResponse({'error': '上传内容过大'}, status_code=413)
        if not uploading_backup:
            total = 0
            chunks = []
            async for chunk in request.stream():
                total += len(chunk)
                if total > maximum:
                    return JSONResponse({'error': '上传内容过大'}, status_code=413)
                chunks.append(chunk)
            raw_body = b''.join(chunks)
            request._body = raw_body
            request.state.raw_body = raw_body
    response = await call_next(request)
    ancestors = _feishu_embed_ancestors()
    is_receipt = bool(re.fullmatch(r'/api/entries/\d+/receipt', request.url.path))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if ancestors is None:
        response.headers['X-Frame-Options'] = 'SAMEORIGIN' if is_receipt else 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    frame_policy = ancestors if ancestors else ("'self'" if is_receipt else "'none'")
    response.headers['Content-Security-Policy'] = (
        f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src 'self'; frame-ancestors {frame_policy}; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def limited(request, scope, maximum=12):
    key = (scope, request.client.host if request.client else 'local')
    with attempt_lock:
        cutoff = time.time() - 300
        # Evict inactive clients so the limiter cannot grow indefinitely.
        if len(attempts) > 5000:
            for old in list(attempts):
                if not attempts[old] or attempts[old][-1] < cutoff:
                    del attempts[old]
        queue = attempts[key]
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= maximum:
            fail('操作过于频繁，请稍后再试', 429)
        queue.append(time.time())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def current(request):
    authenticated_session.set(None)
    token = request.cookies.get('ledger_session', '')
    if not token:
        fail('请先登录', 401)
    token_hash = digest(token)
    with db() as conn:
        row = conn.execute(
            'SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id '
            'WHERE s.token_hash=? AND s.expires>? AND u.active=1',
            (token_hash, int(time.time())),
        ).fetchone()
    if not row:
        fail('登录已失效，请重新登录', 401)
    cfg = oidc.load_config()
    if cfg.configured and row['oidc_sub'] and row['oidc_issuer'] == cfg.issuer:
        if permissions_sync.sync_member(row['oidc_sub'], force=False):
            with db() as conn:
                row = conn.execute(
                    'SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id '
                    'WHERE s.token_hash=? AND s.expires>? AND u.active=1',
                    (token_hash, int(time.time())),
                ).fetchone()
            if not row:
                fail('权限或账号状态已更新，请重新登录', 401)
    if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
        fail('安全校验已失效，请刷新页面', 403)
    authenticated_session.set(token_hash)
    return dict(row)


def _cookie_policy(*, oidc_flow=False):
    secure = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
    if oidc_flow:
        # Cross-site top-level callback redirects from Vanora OAuth must include this cookie.
        samesite = 'none' if (secure and _feishu_embed_ancestors()) else 'lax'
        return secure, samesite
    configured_samesite = os.getenv('COOKIE_SAMESITE', '').strip().lower()
    if configured_samesite in ('lax', 'strict', 'none'):
        samesite = configured_samesite if (configured_samesite != 'none' or secure) else 'lax'
    else:
        samesite = 'none' if (secure and _feishu_embed_ancestors()) else 'lax'
    return secure, samesite


def new_session(conn, user_id, response, oidc_sid=''):
    token = secrets.token_urlsafe(36)
    csrf = secrets.token_urlsafe(32)
    conn.execute('DELETE FROM sessions WHERE expires<?', (int(time.time()),))
    conn.execute(
        'INSERT INTO sessions(token_hash,user_id,csrf,expires,oidc_sid) VALUES (?,?,?,?,?)',
        (digest(token), user_id, csrf, int(time.time()) + 7 * 86400, oidc_sid or ''),
    )
    secure, samesite = _cookie_policy()
    response.set_cookie('ledger_session', token, httponly=True, samesite=samesite, max_age=7 * 86400,
                        secure=secure, path='/')
    return csrf


def _auth_redirect(location: str):
    response = RedirectResponse(location, status_code=302)
    response.headers['Cache-Control'] = 'no-store'
    response.delete_cookie('ledger_oidc_flow', path='/api/auth/oidc')
    return response


@app.get('/api/health')
def health():
    return {'ok': True}


@app.get('/api/bootstrap')
def bootstrap_status(request: Request):
    cfg = oidc.load_config()
    with db() as conn:
        has_users = bool(conn.execute('SELECT id FROM users LIMIT 1').fetchone())
    return {
        'needs_setup': not has_users,
        'oidc_configured': cfg.configured,
        'issuer': cfg.issuer,
        'account_url': cfg.account_url,
        'admin_url': cfg.admin_url,
        'callback_url': oidc.callback_url(request),
        'backchannel_logout_url': oidc.backchannel_logout_url(request),
        'permissions_managed_by': 'vanora-oauth',
        'required_roles': list(USER_ROLES),
    }


@app.get('/api/auth/oidc/login')
def oidc_login(request: Request, team_name: str = '', next: str = ''):
    limited(request, 'oidc-login', 30)
    cfg = oidc.require_config()
    clean_team_name = team_name.strip()[:40] if isinstance(team_name, str) else ''
    return_hash = oidc.sanitize_return_hash(next)
    state_value = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    browser_token = secrets.token_urlsafe(32)
    code_verifier, code_challenge = oidc.pkce_pair()
    auth_url = oidc.build_authorization_url(
        cfg,
        redirect_uri=oidc.callback_url(request),
        state=state_value,
        nonce=nonce,
        code_challenge=code_challenge,
    )
    now_ts = int(time.time())
    with db(write=True) as conn:
        conn.execute('DELETE FROM oidc_flows WHERE expires<?', (now_ts,))
        conn.execute(
            'INSERT INTO oidc_flows(state_hash,browser_hash,nonce,code_verifier,team_name,return_hash,expires) '
            'VALUES (?,?,?,?,?,?,?)',
            (digest(state_value), digest(browser_token), nonce, code_verifier,
             clean_team_name, return_hash, now_ts + 600),
        )
    response = RedirectResponse(auth_url, status_code=302)
    response.headers['Cache-Control'] = 'no-store'
    secure, samesite = _cookie_policy(oidc_flow=True)
    response.set_cookie(
        'ledger_oidc_flow', browser_token, httponly=True, samesite=samesite,
        max_age=600, secure=secure, path='/api/auth/oidc',
    )
    return response


@app.get('/api/auth/oidc/callback')
def oidc_callback(request: Request, code: str = '', state: str = '', error: str = ''):
    limited(request, 'oidc-callback', 30)
    cfg = oidc.load_config()
    if not cfg.configured:
        return _auth_redirect('/?auth_error=oidc_unconfigured')
    if error:
        return _auth_redirect('/?auth_error=cancelled')
    browser_token = request.cookies.get('ledger_oidc_flow', '')
    if not code or not state or not browser_token:
        return _auth_redirect('/?auth_error=invalid_state')
    now_ts = int(time.time())
    with db(write=True) as conn:
        flow = conn.execute('SELECT * FROM oidc_flows WHERE state_hash=?', (digest(state),)).fetchone()
        conn.execute('DELETE FROM oidc_flows WHERE state_hash=? OR expires<?', (digest(state), now_ts))
    if not flow or flow['expires'] <= now_ts or not secrets.compare_digest(flow['browser_hash'], digest(browser_token)):
        return _auth_redirect('/?auth_error=invalid_state')
    try:
        tokens = oidc.exchange_code(
            cfg,
            code=code,
            redirect_uri=oidc.callback_url(request),
            code_verifier=flow['code_verifier'],
        )
        id_token = tokens.get('id_token', '')
        identity = oidc.verify_id_token(id_token, cfg, expected_nonce=flow['nonce'])
    except oidc.OidcError as exc:
        err_code = 'invalid_token' if exc.code == 'invalid_token' else 'exchange_failed'
        return _auth_redirect(f'/?auth_error={err_code}')

    effective_role = identity['role']
    remote_disabled = False
    with db() as conn:
        existing_row = conn.execute(
            'SELECT active FROM users WHERE oidc_issuer=? AND oidc_sub=?',
            (identity['issuer'], identity['sub']),
        ).fetchone()
    locally_inactive = bool(existing_row and not existing_row['active'])
    if (effective_role is None and not identity['has_roles_claim']) or locally_inactive:
        # Verify authoritative status & role with Vanora OAuth when the ID token omitted roles
        # or when the local projection is currently disabled (guards against stale tokens while
        # still allowing re-enabled Vanora accounts to log back in).
        try:
            remote = oidc.fetch_member(cfg, identity['sub'])
            if not remote['exists']:
                effective_role = None
            elif not remote['enabled']:
                remote_disabled = True
                effective_role = None
            elif remote['authorized']:
                effective_role = remote['role']
            else:
                effective_role = None
        except oidc.OidcError:
            if locally_inactive:
                remote_disabled = True
            if not identity['has_roles_claim']:
                effective_role = None

    with db(write=True) as conn:
        had_users = bool(conn.execute('SELECT id FROM users LIMIT 1').fetchone())
        if remote_disabled:
            permissions_sync.deactivate_member_by_sub(conn, identity['issuer'], identity['sub'])
            return _auth_redirect('/?auth_error=disabled')
        if not effective_role:
            permissions_sync.deactivate_member_by_sub(conn, identity['issuer'], identity['sub'])
            return _auth_redirect('/?auth_error=not_member')
        user, _ = permissions_sync.upsert_synced_member(
            conn,
            issuer=identity['issuer'],
            sub=identity['sub'],
            preferred_username=identity['preferred_username'],
            name=identity['name'],
            email=identity['email'],
            role=effective_role,
            enabled=True,
        )
        if not had_users and flow['team_name'] and user['role'] == 'owner':
            save_settings(conn, dict(user), {'team_name': flow['team_name']})
        destination = '/' + (flow['return_hash'] or '')
        response = _auth_redirect(destination)
        new_session(conn, user['id'], response, oidc_sid=identity['sid'])
    permissions_sync.mark_member_synced(cfg, identity['sub'])
    return response


@app.post('/api/auth/oidc/backchannel-logout')
def oidc_backchannel_logout(request: Request):
    limited(request, 'oidc-logout', 60)
    cfg = oidc.require_config()
    raw_body = getattr(request.state, 'raw_body', None)
    if raw_body is None:
        raw_body = getattr(request, '_body', b'') or b''
    try:
        form = parse_qs(raw_body.decode('utf-8'), keep_blank_values=False, strict_parsing=False)
    except UnicodeDecodeError:
        fail('注销请求编码无效', 400)
    tokens = form.get('logout_token') or []
    if len(tokens) != 1 or not tokens[0].strip():
        fail('缺少有效的 logout_token', 400)
    claims = oidc.verify_logout_token(tokens[0].strip(), cfg)
    now_ts = int(time.time())
    jti_hash = digest(claims['jti'])
    events = set(claims.get('events') or ())
    should_sync_member = False
    with db(write=True) as conn:
        conn.execute('DELETE FROM oidc_revoked_jti WHERE expires<?', (now_ts,))
        if conn.execute('SELECT 1 FROM oidc_revoked_jti WHERE jti_hash=?', (jti_hash,)).fetchone():
            fail('Logout Token 已被使用', 400)
        conn.execute('INSERT INTO oidc_revoked_jti(jti_hash,expires) VALUES (?,?)', (jti_hash, max(claims['exp'], now_ts + 300)))
        if claims['sid'] and claims['sub']:
            conn.execute(
                'DELETE FROM sessions WHERE oidc_sid=? AND user_id IN (SELECT id FROM users WHERE oidc_issuer=? AND oidc_sub=?)',
                (claims['sid'], claims['issuer'], claims['sub']),
            )
        elif claims['sid']:
            conn.execute('DELETE FROM sessions WHERE oidc_sid=?', (claims['sid'],))
        else:
            conn.execute(
                'DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE oidc_issuer=? AND oidc_sub=?)',
                (claims['issuer'], claims['sub']),
            )
        if claims['sub']:
            if oidc.ACCOUNT_DELETED_EVENT in events:
                permissions_sync.deactivate_member_by_sub(conn, claims['issuer'], claims['sub'])
            elif oidc.PERMISSIONS_CHANGED_EVENT in events or not claims['sid']:
                target = conn.execute(
                    'SELECT id FROM users WHERE oidc_issuer=? AND oidc_sub=?',
                    (claims['issuer'], claims['sub']),
                ).fetchone()
                if target:
                    permissions_sync.purge_member_ephemeral_state(conn, target['id'])
                should_sync_member = True
    if should_sync_member and claims['sub']:
        permissions_sync.sync_member(claims['sub'], force=True)
    return {'ok': True}


@app.get('/api/session')
def session(request: Request):
    user = current(request)
    cfg = oidc.load_config()
    return {
        'user': public_user(user),
        'csrf': user['csrf'],
        'today': config.today(),
        'account_url': cfg.account_url,
        'admin_url': cfg.admin_url if user['role'] == 'owner' else '',
    }


@app.patch('/api/me')
def update_me(body: dict, request: Request):
    user = current(request)
    name = text_value(body.get('name', ''), '成员姓名', 30)
    with db(write=True) as conn:
        if name != user['name']:
            conn.execute('UPDATE users SET name=? WHERE id=?', (name, user['id']))
        updated = conn.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
    return {'user': public_user(updated)}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    current(request)
    with db(write=True) as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies.get('ledger_session', '')),))
    response.delete_cookie('ledger_session', path='/')
    logout_url = oidc.build_end_session_url(oidc.load_config(), post_logout_redirect_uri=oidc.public_base_url(request))
    result = {'ok': True}
    if logout_url:
        result['logout_url'] = logout_url
    return result


@app.get('/api/settings')
def get_settings(request: Request):
    current(request)
    with db() as conn:
        conf = settings(conn)
    return {**conf, 'today': config.today(), 'timezone': config.TIMEZONE}


@app.put('/api/settings')
def put_settings(body: dict, request: Request):
    user = current(request)
    with db(write=True) as conn:
        return save_settings(conn, user, body)


@app.get('/api/members')
def members(request: Request):
    caller = current(request)
    caller_is_owner = caller['role'] == 'owner'
    permissions_sync.sync_all_members(force=False)
    with db() as conn:
        rows = conn.execute(
            'SELECT u.id,u.username,u.name,u.role,u.active,u.oidc_sub,u.oidc_email,'
            'CASE WHEN t.telegram_id IS NULL THEN 0 ELSE 1 END telegram_linked,'
            'CASE WHEN f.open_id IS NULL THEN 0 ELSE 1 END feishu_linked '
            'FROM users u LEFT JOIN tg_links t ON t.user_id=u.id '
            'LEFT JOIN feishu_links f ON f.user_id=u.id ORDER BY u.id'
        ).fetchall()
    result = []
    for row in rows:
        item = {
            'id': row['id'], 'username': row['username'], 'name': row['name'],
            'role': row['role'], 'active': row['active'],
            'oidc_linked': bool(row['oidc_sub']),
            'oidc_email': row['oidc_email'] or '',
            'telegram_linked': row['telegram_linked'],
            'feishu_linked': row['feishu_linked'],
        }
        if caller_is_owner:
            item['oidc_sub'] = row['oidc_sub'] or ''
        result.append(item)
    return result


@app.post('/api/members/sync')
def sync_members(request: Request):
    user = current(request)
    admin(user)
    limited(request, 'members-sync', 10)
    oidc.require_config()
    synced = permissions_sync.sync_all_members(force=True, raise_on_error=True)
    return {'ok': True, 'synced': synced}


@app.get('/api/entries')
def entries(request: Request):
    current(request)
    with db() as conn:
        ids = conn.execute('SELECT id FROM entries ORDER BY date DESC,id DESC').fetchall()
        return [entry_view(conn, r['id']) for r in ids]


@app.post('/api/entries')
def create_entry(body: dict, request: Request):
    user = current(request)
    key = request.headers.get('idempotency-key', '')
    if not re.fullmatch('[A-Za-z0-9_-]{16,100}', key):
        fail('缺少有效的防重复提交标识')
    with db(write=True) as conn:
        return add_entry(conn, user, body, source_key=f'web:{user["id"]}:{key}')


@app.put('/api/entries/{entry_id}')
def update_entry(entry_id: int, body: dict, request: Request):
    user = current(request)
    with db(write=True) as conn:
        return edit_entry(conn, user, entry_id, body)


@app.delete('/api/entries/{entry_id}')
def delete_entry(entry_id: int, request: Request):
    user = current(request)
    with db(write=True) as conn:
        receipt = remove_entry(conn, user, entry_id)
    if receipt:
        remove_receipt_file(receipt)
    return {'ok': True}


@app.post('/api/entries/action')
def entry_action(body: dict, request: Request):
    user = current(request)
    ids = body.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        fail('请选择 1–100 笔不同账目')
    payment_data, prefix = {}, ''
    if body.get('action') == 'pay':
        admin(user)
        key = request.headers.get('idempotency-key', '')
        if not re.fullmatch('[A-Za-z0-9_-]{16,100}', key):
            fail('缺少付款防重复标识，请刷新页面后重试')
        payments = body.get('payments')
        if not isinstance(payments, list) or len(payments) != len(ids):
            fail('请逐笔填写本次报销金额，旧页面请刷新后重试')
        for payment in payments:
            if not isinstance(payment, dict) or type(payment.get('id')) is not int or not isinstance(payment.get('quote_id'), str):
                fail('付款项目格式不正确，请刷新后重试')
            if payment['id'] in payment_data:
                fail('付款项目不能重复')
            payment_data[payment['id']] = payment
        if set(payment_data) != set(ids):
            fail('付款项目与所选账目不一致')
        prefix = f'pay:web:{user["id"]}:{key}:'
    with db(write=True) as conn:
        # Entire batch is one transaction: a stale or unauthorized row rolls back all rows.
        if prefix:
            previous = conn.execute('SELECT entry_id FROM payments WHERE substr(source_key,1,?)=?', (len(prefix), prefix)).fetchall()
            if previous and {r['entry_id'] for r in previous} != set(ids):
                fail('同一付款标识不能用于不同的付款项目', 409)
        return [transition(conn, user, entry_id, body.get('action'), body.get('note', ''),
                           **({'amount': payment_data[entry_id].get('amount'), 'payment_key': prefix + str(entry_id),
                               'quote_id': payment_data[entry_id]['quote_id']} if prefix else {})) for entry_id in ids]


@app.post('/api/entries/payment-quotes')
def payment_quotes(body: dict, request: Request):
    user = current(request)
    admin(user)
    ids = body.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        fail('请选择 1–100 笔不同账目')
    with db() as conn:
        conf = settings(conn)
        codes = {entry_view(conn, entry_id)['currency'] for entry_id in ids}
    code = body.get('currency', 'original')
    rates = fx.rates_for(conf['reference_currency'], codes) if code == conf['reference_currency'] else None
    with db(write=True) as conn:
        return [make_payment_quote(conn, user, entry_id, code, rates) for entry_id in ids]


@app.get('/api/entries/{entry_id}/events')
def events(entry_id: int, request: Request):
    current(request)
    with db() as conn:
        entry_view(conn, entry_id)
        return [dict(r) for r in conn.execute('SELECT e.*,u.name FROM events e JOIN users u ON u.id=e.user_id WHERE entry_id=? ORDER BY e.id', (entry_id,))]


@app.post('/api/entries/{entry_id}/receipt')
def upload_receipt(entry_id: int, body: dict, request: Request):
    user = current(request)
    try:
        data = base64.b64decode(body.get('data', ''), validate=True)
    except Exception:
        fail('凭证文件格式不正确')
    if not data or len(data) > 6 * 1024 * 1024:
        fail('凭证大小需小于 6 MB')
    suffix = '.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if data.startswith(b'\xff\xd8\xff') else '.pdf' if data.startswith(b'%PDF-') else None
    if not suffix:
        fail('仅支持 PNG、JPEG 和 PDF 凭证')
    name = text_value(body.get('name', '凭证' + suffix), '文件名', 150)
    with db(write=True) as conn:
        entry = conn.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
        if not entry:
            fail('账目不存在', 404)
        require_entry_access(user, entry)
        if entry['status'] not in ('draft', 'pending', 'posted') or (entry['reimbursed_minor'] and not is_finance(user)):
            fail('已报销或已作废的账目不能替换凭证', 409)
        folder = config.DATA / 'receipts'
        folder.mkdir(exist_ok=True)
        filename = secrets.token_hex(24) + suffix
        (folder / filename).write_bytes(data)
        try:
            conn.execute('UPDATE entries SET receipt=?,receipt_name=?,updated_at=? WHERE id=?', (filename, name, config.now(), entry_id))
            audit(conn, entry_id, user['id'], 'receipt', '上传凭证')
        except Exception:
            (folder / filename).unlink(missing_ok=True)
            raise
        old = entry['receipt']
    if old:
        remove_receipt_file(old)
    return {'ok': True}


@app.get('/api/entries/{entry_id}/receipt')
def get_receipt(entry_id: int, request: Request, download: int = 0):
    current(request)
    with db() as conn:
        entry = conn.execute('SELECT receipt,receipt_name FROM entries WHERE id=?', (entry_id,)).fetchone()
    if not entry or not entry['receipt']:
        fail('未上传凭证', 404)
    path = config.DATA / 'receipts' / entry['receipt']
    if not path.is_file():
        fail('凭证文件不存在，请检查备份', 404)
    return FileResponse(path, filename=entry['receipt_name'],
                        media_type=receipt_media_type(entry['receipt']),
                        content_disposition_type='attachment' if download else 'inline')


@app.get('/api/report')
def get_report(request: Request, base: str | None = None):
    current(request)
    return fx.report(base)


@app.post('/api/calculate')
def calculate(body: dict, request: Request):
    current(request)
    amount = body.get('amount', '')
    scenario = None if amount in ('', None) else {'amount': amount, 'currency': body.get('currency')}
    return fx.report(body.get('base'), scenario, reimbursement=body.get('reimbursement'))


@app.get('/api/public-balance')
def get_public_balance(request: Request, base: str | None = None, as_of: str = ''):
    current(request)
    return fx.public_balance_report(base=base, as_of=as_of)


@app.post('/api/public-balance/calculate')
def calculate_public_balance(body: dict, request: Request):
    current(request)
    if not isinstance(body, dict):
        fail('计算参数格式不正确')
    return fx.public_balance_report(
        base=body.get('base'),
        as_of=body.get('as_of', ''),
        adjustments=body.get('adjustments'),
        bank_actual=body.get('bank_actual'),
        include_pending=body.get('include_pending', True),
    )


@app.post('/api/fx/refresh')
def refresh_fx(request: Request):
    current(request)
    limited(request, 'fx', 5)
    return fx.report(force=True)


@app.post('/api/ai/parse')
def ai_parse(body: dict, request: Request):
    user = current(request)
    limited(request, 'ai', 20)
    with db() as conn:
        conf = settings(conn)
    result = ai.parse(body.get('text'), conf)
    from .domain import normalized_entry
    with db() as conn:
        normalized_entry(conn, user, result)
    return result


@app.post('/api/ai/plan')
def ai_plan_preview(body: dict, request: Request):
    user = current(request)
    limited(request, 'ai', 20)
    text = text_value(body.get('text'), '记账内容', 4000)
    history = body.get('history')
    if not isinstance(history, list):
        history = []
    with db() as conn:
        conf = settings(conn)
        context = ai_context.build(conn, conf, user)
    raw = ai.plan(text, conf, context, history)
    # Fetch FX before taking the write lock; rate fetching may update the cache.
    with db() as conn:
        conf = settings(conn)
        rates = ai_plan.needed_rates(conn, conf, raw)
    with db(write=True) as conn:
        conf = settings(conn)
        plan = ai_plan.resolve(conn, user, conf, raw, context, rates)
        return ai_plan.store(conn, user, plan, 'web')


@app.post('/api/ai/execute')
def ai_execute(body: dict, request: Request):
    user = current(request)
    key = request.headers.get('idempotency-key', '')
    if not re.fullmatch('[A-Za-z0-9_-]{16,100}', key):
        fail('缺少有效的防重复提交标识')
    with db(write=True) as conn:
        result = ai_plan.execute(conn, user, body.get('plan_id'), body.get('actions'))
    for name in result.pop('receipts', []):
        remove_receipt_file(name)
    return result


@app.get('/api/ai/config')
def get_ai_config(request: Request):
    user = current(request)
    owner(user)
    with db() as conn:
        return ai.public_config(conn)


@app.put('/api/ai/config')
def update_ai_config(body: dict, request: Request):
    user = current(request)
    owner(user)
    limited(request, 'ai-config', 20)
    with db(write=True) as conn:
        return ai.save_config(conn, body)


@app.delete('/api/ai/config')
def delete_ai_config(request: Request):
    user = current(request)
    owner(user)
    limited(request, 'ai-config', 20)
    with db(write=True) as conn:
        return ai.clear_config(conn)


@app.post('/api/ai/test')
def test_ai_config(request: Request):
    user = current(request)
    owner(user)
    limited(request, 'ai-test', 10)
    with db() as conn:
        cfg = ai.load_config(conn)
    return ai.test_connection(cfg)


@app.get('/api/integrations')
def integrations(request: Request):
    user = current(request)
    with db() as conn:
        conf = settings(conn)
        linked = conn.execute('SELECT telegram_id FROM tg_links WHERE user_id=?', (user['id'],)).fetchone()
        feishu_linked = conn.execute('SELECT open_id FROM feishu_links WHERE user_id=?', (user['id'],)).fetchone()
        feishu_config = conn.execute('SELECT app_id,domain FROM feishu_config WHERE id=1').fetchone()
        ai_conf = ai.public_config(conn)
    heartbeat = conf.get('bot_heartbeat', 0)
    feishu_heartbeat = conf.get('feishu_heartbeat', 0)
    raw_domain = (feishu_config['domain'] if feishu_config and feishu_config['domain'] else os.getenv('FEISHU_DOMAIN', '')).strip()
    ai_public = ai_conf if user['role'] == 'owner' else {
        'enabled': ai_conf['enabled'],
        'configured': ai_conf['configured'],
        'model': ai_conf['model'] if ai_conf['configured'] else '',
        'context_entries': ai_conf['context_entries'],
        'max_actions': ai_conf['max_actions'],
    }
    return {'ai_configured': ai_conf['configured'], 'ai': ai_public,
            'bot_configured': bool(os.getenv('TELEGRAM_BOT_TOKEN')),
            'bot_online': time.time() - heartbeat < 90, 'bot_username': conf.get('bot_username'),
            'telegram_linked': bool(linked), 'feishu_linked': bool(feishu_linked),
            'feishu_configured': bool(feishu_config or (os.getenv('FEISHU_APP_ID') and os.getenv('FEISHU_APP_SECRET'))),
            'feishu_app_id': feishu_config['app_id'] if feishu_config else os.getenv('FEISHU_APP_ID', ''),
            'feishu_domain': raw_domain or 'feishu',
            'feishu_online': time.time() - feishu_heartbeat < 90,
            'feishu_bot_name': conf.get('feishu_bot_name', ''),
            'feishu_last_error': conf.get('feishu_last_error', ''),
            'feishu_embed_enabled': bool(_feishu_embed_ancestors())}


@app.post('/api/feishu/config')
def feishu_configure(body: dict, request: Request):
    user = current(request)
    owner(user)
    limited(request, 'feishu-config', 10)
    app_id = text_value(body.get('app_id'), 'App ID', 128)
    secret = text_value(body.get('app_secret'), 'App Secret', 512, False)
    domain_raw = text_value(body.get('domain', ''), '服务域名', 200, False)
    if domain_raw and domain_raw.lower() not in ('feishu', 'lark') and not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._/-]*)?', domain_raw):
        fail('飞书服务域名支持 feishu、lark 或完整的 https:// 地址。')
    domain = '' if domain_raw.lower() == 'feishu' else domain_raw.lower() if domain_raw.lower() == 'lark' else domain_raw.rstrip('/')
    with db(write=True) as conn:
        existing = conn.execute('SELECT * FROM feishu_config WHERE id=1').fetchone()
        old_id = existing['app_id'] if existing else os.getenv('FEISHU_APP_ID', '')
        if app_id != old_id and conn.execute('SELECT 1 FROM feishu_links LIMIT 1').fetchone():
            fail('更换飞书应用前，请先解除所有成员的飞书绑定。', 409)
        if not secret:
            if existing and existing['app_id'] == app_id:
                secret = existing['app_secret']
            elif not existing and old_id == app_id and os.getenv('FEISHU_APP_SECRET'):
                secret = os.environ['FEISHU_APP_SECRET']
            else:
                fail('请填写 App Secret。')
        if app_id != old_id:
            conn.execute('DELETE FROM fsui_actions')
            conn.execute('DELETE FROM fsui_prompts')
            conn.execute('DELETE FROM fsui_drafts')
            conn.execute('DELETE FROM fsui_links')
            conn.execute('DELETE FROM fsui_updates')
            conn.execute('DELETE FROM fsui_outbox')
            conn.execute('DELETE FROM fsui_messages')
            conn.execute('DELETE FROM fsui_identities')
            conn.execute('DELETE FROM feishu_pending')
            conn.execute('DELETE FROM feishu_inbox')
            conn.execute('DELETE FROM feishu_outbox')
            conn.execute('DELETE FROM feishu_link_codes')
        version = (existing['version'] if existing else 0) + 1
        conn.execute('INSERT OR REPLACE INTO feishu_config(id,app_id,app_secret,version,domain) VALUES (1,?,?,?,?)',
                     (app_id, secret, version, domain))
    return {'ok': True}


@app.post('/api/feishu/link-code')
def feishu_link_code(request: Request):
    user = current(request)
    limited(request, 'link', 10)
    code = secrets.token_urlsafe(12)
    with db(write=True) as conn:
        conn.execute('DELETE FROM feishu_link_codes WHERE user_id=? OR expires<?', (user['id'], int(time.time())))
        conn.execute('INSERT INTO feishu_link_codes VALUES (?,?,?)', (digest(code), user['id'], int(time.time()) + 600))
    return {'code': code, 'expires_in': 600}


@app.post('/api/feishu/unlink')
def feishu_unlink(request: Request):
    user = current(request)
    with db(write=True) as conn:
        ids = [row['telegram_id'] for row in conn.execute('SELECT telegram_id FROM fsui_links WHERE user_id=?', (user['id'],))]
        for virtual_id in ids:
            conn.execute('DELETE FROM fsui_actions WHERE telegram_id=?', (virtual_id,))
            conn.execute('DELETE FROM fsui_prompts WHERE telegram_id=?', (virtual_id,))
            conn.execute('DELETE FROM fsui_drafts WHERE telegram_id=?', (virtual_id,))
        conn.execute('DELETE FROM fsui_links WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM feishu_pending WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM feishu_links WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM feishu_link_codes WHERE user_id=?', (user['id'],))
    return {'ok': True}


@app.post('/api/telegram/link-code')
def link_code(request: Request):
    user = current(request)
    limited(request, 'link', 10)
    code = secrets.token_urlsafe(12)
    with db(write=True) as conn:
        conn.execute('DELETE FROM link_codes WHERE user_id=? OR expires<?', (user['id'], int(time.time())))
        conn.execute('INSERT INTO link_codes VALUES (?,?,?)', (digest(code), user['id'], int(time.time()) + 600))
    return {'code': code, 'expires_in': 600}


@app.post('/api/telegram/unlink')
def unlink(request: Request):
    user = current(request)
    with db(write=True) as conn:
        conn.execute('DELETE FROM tg_links WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM link_codes WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM tg_drafts WHERE user_id=?', (user['id'],))
    return {'ok': True}


@app.get('/api/export')
@app.get('/api/export.xlsx')
def export_excel(
    request: Request,
    kind: str = 'all',
    status: str | None = None,
    currency: str = 'all',
    payer: str | None = None,
    date_from: str = Query(default='', alias='from'),
    date_to: str = Query(default='', alias='to'),
    q: str = '',
    ids: str = '',
    base: str | None = None,
    include_voided: str | None = None,
    include_receipts: str = '1',
):
    admin(current(request))
    result = excel_export.build_excel_export(
        kind=kind,
        status=status,
        currency_code=currency,
        payer_id=payer,
        date_from=date_from,
        date_to=date_to,
        text=q,
        ids=ids,
        base=base,
        include_voided=include_voided,
        include_receipts=include_receipts,
    )
    return Response(
        content=result['content'],
        media_type=excel_export.EXCEL_MEDIA_TYPE,
        headers={
            'Content-Disposition': excel_export.content_disposition_header(
                result['filename'],
                result['ascii_filename'],
            ),
        },
    )


@app.get('/')
def index():
    html = (PUBLIC / 'index.html').read_text(encoding='utf-8')
    for asset in ('app.js', 'style.css', 'backups.js', 'backups.css'):
        if f'/static/{asset}"' not in html:
            continue
        version = hashlib.sha256((PUBLIC / asset).read_bytes()).hexdigest()[:12]
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={version}"')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


app.mount('/static', StaticFiles(directory=PUBLIC), name='static')
