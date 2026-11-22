"""Isolated Feishu adapter for the shared interactive ledger flows."""
import contextlib
import json
import logging
import re
import secrets
import time
import uuid

from . import bot, config
from .db import db, settings

LOG = logging.getLogger(__name__)
PREFIX = '␞FS:'
MAX_ATTEMPTS = 8
MAX_ACTION_ROWS = 16
PERMANENT_FEISHU_CODES = {
    230001, 230002, 230006, 230013, 230027, 230028, 230099,
    99991663, 99991672,
}


class Connection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=()):
        sql = re.sub(r'\btg_(links|updates|drafts|outbox|prompts)\b', r'fsui_\1', sql)
        sql = re.sub(r'\blink_codes\b', 'feishu_link_codes', sql)
        return self.conn.execute(sql, args)

    def __getattr__(self, name):
        return getattr(self.conn, name)


@contextlib.contextmanager
def db_adapter(write=False):
    with db(write=write) as conn:
        yield Connection(conn)


def process(msg, event):
    """Use inbox rowid as a durable update ID; replays are safe."""
    open_id, chat_id = msg['open_id'], msg['chat_id']
    group = event.get('group', False)
    with db(write=True) as conn:
        if group and chat_id not in settings(conn)['feishu_allowed_chats']:
            return
        linked = conn.execute('SELECT user_id FROM feishu_links WHERE open_id=?', (open_id,)).fetchone()
        if not linked:
            if not event.get('action_token'):
                conn.execute(
                    'INSERT INTO feishu_outbox(chat_id,open_id,user_id,text,message_uuid) VALUES (?,?,?,?,?)',
                    (chat_id, open_id, None,
                     '请先登录 Web「机器人与 AI」生成飞书绑定码，再私聊发送「绑定 绑定码」。',
                     str(uuid.uuid4())),
                )
            return
        conn.execute('INSERT OR IGNORE INTO fsui_identities(open_id) VALUES (?)', (open_id,))
        actor = str(conn.execute('SELECT id FROM fsui_identities WHERE open_id=?', (open_id,)).fetchone()['id'])
        conn.execute('DELETE FROM fsui_links WHERE user_id=? AND telegram_id<>?', (linked['user_id'], actor))
        conn.execute('INSERT OR REPLACE INTO fsui_links VALUES (?,?)', (actor, linked['user_id']))
        parent_row = None
        for ref_id in (event.get('parent'), event.get('root')):
            if ref_id:
                parent_row = conn.execute(
                    'SELECT id FROM fsui_messages WHERE feishu_id=? AND chat_id=?',
                    (ref_id, chat_id),
                ).fetchone()
                if parent_row:
                    break
        callback = None
        if event.get('action_token'):
            callback = conn.execute(
                'SELECT action,group_mode FROM fsui_actions WHERE token=? AND telegram_id=? AND chat_id=? AND expires>?',
                (event['action_token'], actor, chat_id, int(time.time())),
            ).fetchone()
            if not callback:
                return
            group = bool(callback['group_mode'])
            if group and chat_id not in settings(conn)['feishu_allowed_chats']:
                return
    message = {
        'message_id': msg['rowid'],
        'chat': {'id': chat_id, 'type': 'group' if group else 'private'},
        'from': {'id': int(actor), 'is_bot': False},
        'text': event.get('text', ''),
    }
    if event.get('open_message_id'):
        message['feishu_message_id'] = event['open_message_id']
    if parent_row:
        message['reply_to_message'] = {'message_id': parent_row['id']}
    if callback:
        update = {
            'update_id': msg['rowid'],
            'callback_query': {
                'id': msg['message_id'],
                'from': message['from'],
                'message': message,
                'data': callback['action'],
            },
        }
    else:
        update = {'update_id': msg['rowid'], 'message': message}
    bot.process_update(update, db_provider=db_adapter, platform='feishu')


def _card_template(text, is_callback_error=False):
    first = text.strip().splitlines()[0] if text.strip() else ''
    if is_callback_error:
        return 'orange'
    if '永久删除' in first or '失败' in first:
        return 'red'
    if first.startswith('⚠️') or '确认' in first:
        return 'orange'
    if first.startswith('✅'):
        return 'green'
    if first.startswith(('🧮', '📊', '🤖')):
        return 'wathet'
    return 'grey'


def _button_type(label, callback_data=''):
    if callback_data.startswith(('deleteyes:', 'delete:', 'void:')) or '永久删除' in label or '作废' in label:
        return 'danger'
    if (callback_data.endswith((':save', ':commit', ':go'))
            or label.startswith('✅')
            or label.startswith('确认')
            or '一键报销' in label
            or label == '记入账本'):
        return 'primary'
    return 'default'


def _build_card(text, actions, is_callback_error=False):
    raw = str(text or '').strip()[:8000]
    lines = raw.splitlines()
    if len(lines) > 1 and 0 < len(lines[0].strip()) <= 60:
        title = lines[0].strip()
        body = '\n'.join(lines[1:]).strip()
    else:
        title = '操作提示' if is_callback_error else '一起记 · 团队账本'
        body = raw
    elements = []
    if body:
        elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': body[:8000]}})
    if actions:
        elements.append({'tag': 'hr'})
        elements.extend(actions[:MAX_ACTION_ROWS])
    return {
        'config': {'wide_screen_mode': True, 'enable_forward': True},
        'header': {
            'title': {'tag': 'plain_text', 'content': title[:60]},
            'template': _card_template(raw, is_callback_error=is_callback_error),
        },
        'elements': elements or [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': title}}],
    }


def _prepare(conn, row):
    payload = json.loads(row['payload'])
    payload.setdefault('chat_id', payload.get('_fs_chat'))
    actor = str(payload.get('_fs_actor') or '')
    identity = conn.execute(
        'SELECT i.open_id,l.user_id FROM fsui_identities i '
        'JOIN fsui_links l ON l.telegram_id=CAST(i.id AS TEXT) '
        'JOIN feishu_links f ON f.open_id=i.open_id AND f.user_id=l.user_id '
        'JOIN users u ON u.id=l.user_id AND u.active=1 WHERE i.id=?',
        (actor,),
    ).fetchone()
    if not identity or (payload.get('_fs_group') and str(payload.get('chat_id')) not in settings(conn)['feishu_allowed_chats']):
        return None
    if not payload.get('_fs_card') or payload.get('_fs_expires', 0) <= time.time():
        conn.execute('DELETE FROM fsui_actions WHERE expires<?', (int(time.time()),))
        buttons = (payload.get('reply_markup') or {}).get('inline_keyboard') or []
        actions = []
        for row_buttons in buttons:
            row_items = []
            for button in row_buttons:
                if button.get('callback_data'):
                    token = secrets.token_urlsafe(18)
                    conn.execute(
                        'INSERT INTO fsui_actions VALUES (?,?,?,?,?,?)',
                        (token, actor, str(payload['chat_id']), button['callback_data'],
                         int(time.time()) + 3600, int(bool(payload.get('_fs_group')))),
                    )
                    label = str(button['text'])[:100]
                    row_items.append({
                        'tag': 'button',
                        'text': {'tag': 'plain_text', 'content': label},
                        'type': _button_type(label, button['callback_data']),
                        'value': {'token': token},
                    })
                elif str(button.get('url', '')).startswith(('https://', 'http://')):
                    url = button['url']
                    row_items.append({
                        'tag': 'button',
                        'text': {'tag': 'plain_text', 'content': str(button['text'])[:100]},
                        'type': 'default',
                        'url': url,
                        'multi_url': {'url': url, 'pc_url': url, 'android_url': url, 'ios_url': url},
                    })
            for start in range(0, len(row_items), 3):
                actions.append({'tag': 'action', 'actions': row_items[start:start + 3]})
        card = _build_card(
            payload.get('text', ''),
            actions,
            is_callback_error=row.get('method') == 'answerCallbackQuery',
        )
        payload['_fs_card'] = card
        payload['_fs_uuid'] = str(uuid.uuid4())
        payload['_fs_expires'] = int(time.time()) + 3600
        conn.execute('UPDATE fsui_outbox SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), row['id']))
    return payload


def _try_patch(client, message_id, card_json):
    if not message_id or not hasattr(client.im.v1.message, 'patch'):
        return False
    try:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        body = PatchMessageRequestBody.builder().content(card_json).build()
        req = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = client.im.v1.message.patch(req)
        return bool(response and response.success())
    except Exception:
        LOG.debug('Feishu card patch failed; falling back to create', exc_info=True)
        return False


def send_one(client):
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    with db(write=True) as conn:
        row = conn.execute(
            'SELECT * FROM fsui_outbox WHERE sent=0 AND attempts<? ORDER BY id LIMIT 1',
            (MAX_ATTEMPTS,),
        ).fetchone()
        if not row:
            return False
        row = dict(row)
        payload = json.loads(row['payload'])
        if row['method'] == 'answerCallbackQuery' and not payload.get('text'):
            conn.execute('UPDATE fsui_outbox SET sent=1,payload=? WHERE id=?', ('{}', row['id']))
            return True
        payload = _prepare(conn, row)
        if payload is None:
            conn.execute('UPDATE fsui_outbox SET sent=1,payload=? WHERE id=?', ('{}', row['id']))
            return True
    card_json = json.dumps(payload['_fs_card'], ensure_ascii=False)
    fs_message_id = None
    patched = False
    if row['method'] == 'editMessageText' and payload.get('_fs_message_id') and not payload.get('_prompt'):
        patched = _try_patch(client, payload['_fs_message_id'], card_json)
        if patched:
            fs_message_id = payload['_fs_message_id']
    if not patched:
        body = (CreateMessageRequestBody.builder()
                .receive_id(str(payload['chat_id']))
                .msg_type('interactive')
                .content(card_json)
                .uuid(payload['_fs_uuid'])
                .build())
        req = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(body).build()
        response = client.im.v1.message.create(req)
        if not response.success():
            code = getattr(response, 'code', None)
            with db(write=True) as conn:
                if code in PERMANENT_FEISHU_CODES:
                    conn.execute("UPDATE fsui_outbox SET attempts=?,payload='{}' WHERE id=?", (MAX_ATTEMPTS, row['id']))
                    LOG.warning('Dropping permanent Feishu card failure (code %s, row %s)', code, row['id'])
                    return True
                next_attempts = int(row.get('attempts', 0)) + 1
                if next_attempts >= MAX_ATTEMPTS:
                    conn.execute("UPDATE fsui_outbox SET attempts=?,payload='{}' WHERE id=?", (next_attempts, row['id']))
                else:
                    conn.execute('UPDATE fsui_outbox SET attempts=? WHERE id=?', (next_attempts, row['id']))
            raise RuntimeError(f'Feishu card send failed (code {code})')
        fs_message_id = getattr(response.data, 'message_id', None)
    with db(write=True) as conn:
        if fs_message_id:
            conn.execute('INSERT OR IGNORE INTO fsui_messages(feishu_id,chat_id) VALUES (?,?)',
                         (fs_message_id, str(payload['chat_id'])))
            row_id = conn.execute('SELECT id FROM fsui_messages WHERE feishu_id=?', (fs_message_id,)).fetchone()['id']
            prompt = payload.get('_prompt')
            if prompt:
                conn.execute('INSERT OR REPLACE INTO fsui_prompts VALUES (?,?,?,?,?,?)',
                             (str(payload['chat_id']), row_id, prompt['telegram_id'],
                              prompt['token'], prompt['step'], int(time.time()) + 3600))
        conn.execute('UPDATE fsui_outbox SET sent=1,payload=? WHERE id=?', ('{}', row['id']))
    return True
