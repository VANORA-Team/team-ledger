"""Synchronize authoritative member roles and enabled status from Vanora OAuth."""
import logging
import re
import secrets
import threading
import time

from . import config, oidc
from .db import db

LOG = logging.getLogger(__name__)

_sync_lock = threading.Lock()
_last_full_sync: dict[tuple[str, str], float] = {}
_last_member_sync: dict[tuple[str, str, str], float] = {}


def clear_cache():
    with _sync_lock:
        _last_full_sync.clear()
        _last_member_sync.clear()


def mark_member_synced(cfg: oidc.OidcConfig, sub: str):
    if not cfg.configured or not sub:
        return
    with _sync_lock:
        _last_member_sync[(cfg.issuer, cfg.client_id, sub)] = time.monotonic()


def _sanitize_username(preferred_username, sub=''):
    raw = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(preferred_username or '').strip())
    raw = re.sub(r'_+', '_', raw).strip('._-')
    if len(raw) < 3:
        fallback = re.sub(r'[^a-zA-Z0-9]', '', str(sub or '')).lower()[:12]
        raw = f'vanora_{fallback}' if len(fallback) >= 3 else f'member_{secrets.token_hex(4)}'
    return raw[:26]


def unique_username(conn, preferred_username, sub='', *, exclude_id=None):
    base = _sanitize_username(preferred_username, sub)
    candidate = base
    suffix = 2
    while True:
        if exclude_id is None:
            taken = conn.execute('SELECT id FROM users WHERE username=?', (candidate,)).fetchone()
        else:
            taken = conn.execute('SELECT id FROM users WHERE username=? AND id<>?', (candidate, exclude_id)).fetchone()
        if not taken:
            return candidate
        tail = f'_{suffix}' if suffix <= 99 else f'_{secrets.token_hex(2)}'
        candidate = base[:32 - len(tail)] + tail
        suffix += 1


def purge_member_ephemeral_state(conn, member_id: int):
    virtual_ids = [row['telegram_id'] for row in conn.execute('SELECT telegram_id FROM fsui_links WHERE user_id=?', (member_id,))]
    for virtual_id in virtual_ids:
        conn.execute('DELETE FROM fsui_actions WHERE telegram_id=?', (virtual_id,))
        conn.execute('DELETE FROM fsui_prompts WHERE telegram_id=?', (virtual_id,))
    conn.execute('DELETE FROM fsui_drafts WHERE user_id=?', (member_id,))
    conn.execute('DELETE FROM tg_prompts WHERE token IN (SELECT token FROM tg_drafts WHERE user_id=?)', (member_id,))
    conn.execute('DELETE FROM tg_drafts WHERE user_id=?', (member_id,))
    conn.execute('DELETE FROM feishu_pending WHERE user_id=?', (member_id,))
    conn.execute('DELETE FROM feishu_link_codes WHERE user_id=?', (member_id,))
    conn.execute('DELETE FROM link_codes WHERE user_id=?', (member_id,))
    conn.execute('DELETE FROM payment_quotes WHERE user_id=?', (member_id,))
    conn.execute('DELETE FROM ai_plans WHERE user_id=? AND executed_at IS NULL', (member_id,))


def upsert_synced_member(
    conn,
    *,
    issuer: str,
    sub: str,
    preferred_username: str = '',
    name: str = '',
    email: str = '',
    role: str | None = None,
    enabled: bool = True,
):
    """Project an authoritative Vanora OAuth member into local SQLite storage.

    Returns (user_row | None, permissions_changed: bool).
    """
    resolved_role = oidc.resolve_role(role) if role else None
    is_active = 1 if (enabled and resolved_role) else 0
    clean_email = (email or '').strip()
    existing = conn.execute(
        'SELECT * FROM users WHERE oidc_issuer=? AND oidc_sub=?',
        (issuer, sub),
    ).fetchone()

    if existing:
        target_role = resolved_role or 'member'
        role_changed = target_role != existing['role']
        active_changed = is_active != existing['active']
        next_name = existing['name'] or (name or preferred_username or existing['username'])[:30]
        if (
            role_changed
            or active_changed
            or clean_email != (existing['oidc_email'] or '')
            or next_name != existing['name']
        ):
            conn.execute(
                'UPDATE users SET role=?,active=?,oidc_email=?,name=? WHERE id=?',
                (target_role, is_active, clean_email, next_name, existing['id']),
            )
        if role_changed or not is_active:
            conn.execute('DELETE FROM sessions WHERE user_id=?', (existing['id'],))
            purge_member_ephemeral_state(conn, existing['id'])
        updated = conn.execute('SELECT * FROM users WHERE id=?', (existing['id'],)).fetchone()
        return updated, (role_changed or active_changed)

    if not is_active:
        return None, False

    username = unique_username(conn, preferred_username, sub)
    display_name = (name or preferred_username or username).strip()[:30] or username
    cursor = conn.execute(
        'INSERT INTO users(username,name,password_hash,role,active,created_at,oidc_issuer,oidc_sub,oidc_email) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (username, display_name, '', resolved_role, 1, config.now(), issuer, sub, clean_email),
    )
    created = conn.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()
    return created, True


def deactivate_member_by_sub(conn, issuer: str, sub: str) -> bool:
    existing = conn.execute(
        'SELECT * FROM users WHERE oidc_issuer=? AND oidc_sub=?',
        (issuer, sub),
    ).fetchone()
    if not existing:
        return False
    changed = existing['active'] != 0 or existing['role'] != 'member'
    conn.execute("UPDATE users SET active=0,role='member' WHERE id=?", (existing['id'],))
    conn.execute('DELETE FROM sessions WHERE user_id=?', (existing['id'],))
    purge_member_ephemeral_state(conn, existing['id'])
    return changed


def sync_member(sub: str, *, force: bool = False, raise_on_error: bool = False, db_provider=None) -> bool:
    """Refresh one user's authoritative role and enabled state from Vanora OAuth (outside SQLite lock)."""
    cfg = oidc.load_config()
    if not cfg.configured or not sub:
        return False
    key = (cfg.issuer, cfg.client_id, sub)
    now_mono = time.monotonic()
    if not force and cfg.permissions_sync_ttl > 0:
        with _sync_lock:
            last = _last_member_sync.get(key)
            if last is not None and (now_mono - last) < cfg.permissions_sync_ttl:
                return False
    try:
        remote = oidc.fetch_member(cfg, sub)
    except oidc.OidcError:
        if raise_on_error:
            raise
        LOG.warning('Unable to refresh Vanora OAuth permissions for subject %s; using cached state', sub)
        return False

    provider = db_provider or db
    with provider(write=True) as conn:
        if remote['authorized'] and remote['role']:
            _, changed = upsert_synced_member(
                conn,
                issuer=cfg.issuer,
                sub=sub,
                preferred_username=remote['username'],
                name=remote['name'],
                email=remote['email'],
                role=remote['role'],
                enabled=remote['enabled'],
            )
        else:
            changed = deactivate_member_by_sub(conn, cfg.issuer, sub)
    with _sync_lock:
        _last_member_sync[key] = time.monotonic()
    return changed


def sync_all_members(*, force: bool = False, raise_on_error: bool = False, db_provider=None) -> int:
    """Reconcile all authorized members for this client from Vanora OAuth (outside SQLite lock)."""
    cfg = oidc.load_config()
    if not cfg.configured:
        return 0
    key = (cfg.issuer, cfg.client_id)
    now_mono = time.monotonic()
    if not force and cfg.permissions_sync_ttl > 0:
        with _sync_lock:
            last = _last_full_sync.get(key)
            if last is not None and (now_mono - last) < cfg.permissions_sync_ttl:
                return 0
    try:
        members = oidc.fetch_members(cfg)
    except oidc.OidcError:
        if raise_on_error:
            raise
        LOG.warning('Unable to refresh Vanora OAuth member directory; using cached projection')
        return 0

    seen_subs: set[str] = set()
    synced = 0
    provider = db_provider or db
    with provider(write=True) as conn:
        for item in members:
            sub = item['sub']
            seen_subs.add(sub)
            user_row, _ = upsert_synced_member(
                conn,
                issuer=cfg.issuer,
                sub=sub,
                preferred_username=item['username'],
                name=item['name'],
                email=item['email'],
                role=item['role'],
                enabled=item['enabled'],
            )
            if user_row is not None:
                synced += 1
        local_rows = conn.execute(
            "SELECT id,oidc_sub FROM users WHERE oidc_issuer=? AND oidc_sub IS NOT NULL AND oidc_sub<>''",
            (cfg.issuer,),
        ).fetchall()
        for row in local_rows:
            if row['oidc_sub'] not in seen_subs:
                deactivate_member_by_sub(conn, cfg.issuer, row['oidc_sub'])

    stamp = time.monotonic()
    with _sync_lock:
        _last_full_sync[key] = stamp
        for sub in seen_subs:
            _last_member_sync[(cfg.issuer, cfg.client_id, sub)] = stamp
    return synced
