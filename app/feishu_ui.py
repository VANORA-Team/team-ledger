"""Isolated Feishu adapter for the shared interactive ledger flows."""
import contextlib
import json
import re
import secrets
import time
import uuid

from . import bot
from .db import db, settings

PREFIX = '␞FS:'


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
            return
        conn.execute('INSERT OR IGNORE INTO fsui_identities(open_id) VALUES (?)', (open_id,))
        actor = str(conn.execute('SELECT id FROM fsui_identities WHERE open_id=?', (open_id,)).fetchone()['id'])
        conn.execute('DELETE FROM fsui_links WHERE user_id=? AND telegram_id<>?', (linked['user_id'], actor))
        conn.execute('INSERT OR REPLACE INTO fsui_links VALUES (?,?)', (actor, linked['user_id']))
        parent = event.get('parent')
        parent_row = conn.execute('SELECT id FROM fsui_messages WHERE feishu_id=? AND chat_id=?', (parent, chat_id)).fetchone() if parent else None
        callback = None
        if event.get('action_token'):
            callback = conn.execute('SELECT action,group_mode FROM fsui_actions WHERE token=? AND telegram_id=? AND chat_id=? AND expires>?',
                                    (event['action_token'], actor, chat_id, int(time.time()))).fetchone()
            if not callback:
                return
            group = bool(callback['group_mode'])
            if group and chat_id not in settings(conn)['feishu_allowed_chats']:
                return
    message = {'message_id': msg['rowid'], 'chat': {'id': chat_id, 'type': 'group' if group else 'private'},
               'from': {'id': int(actor), 'is_bot': False}, 'text': event.get('text', '')}
    if parent_row:
        message['reply_to_message'] = {'message_id': parent_row['id']}
    if callback:
        update = {'update_id': msg['rowid'], 'callback_query': {'id': msg['message_id'], 'from': message['from'],
                  'message': message, 'data': callback['action']}}
    else:
        update = {'update_id': msg['rowid'], 'message': message}
    bot.process_update(update, db_provider=db_adapter, platform='feishu')


def _prepare(conn, row):
    payload = json.loads(row['payload'])
    payload.setdefault('chat_id', payload.get('_fs_chat'))
    actor = str(payload.get('_fs_actor') or '')
    identity = conn.execute('SELECT i.open_id,l.user_id FROM fsui_identities i JOIN fsui_links l ON l.telegram_id=CAST(i.id AS TEXT) JOIN feishu_links f ON f.open_id=i.open_id AND f.user_id=l.user_id JOIN users u ON u.id=l.user_id AND u.active=1 WHERE i.id=?', (actor,)).fetchone()
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
                    conn.execute('INSERT INTO fsui_actions VALUES (?,?,?,?,?,?)',
                                 (token, actor, str(payload['chat_id']), button['callback_data'], int(time.time()) + 3600, int(bool(payload.get('_fs_group')))))
                    row_items.append({'tag':'button','text':{'tag':'plain_text','content':button['text'][:100]},
                                      'type':'default','value':{'token':token}})
                elif button.get('url', '').startswith('https://'):
                    row_items.append({'tag':'button','text':{'tag':'plain_text','content':button['text'][:100]},
                                      'type':'default','url':button['url']})
            for start in range(0, len(row_items), 5):
                actions.append({'tag':'action','actions':row_items[start:start + 5]})
        card = {'config':{'wide_screen_mode':True},
                'elements':[{'tag':'div','text':{'tag':'plain_text','content':str(payload.get('text',''))[:8000]}}] + actions}
        payload['_fs_card'] = card
        payload['_fs_uuid'] = str(uuid.uuid4())
        payload['_fs_expires'] = int(time.time()) + 3600
        conn.execute('UPDATE fsui_outbox SET payload=? WHERE id=?', (json.dumps(payload,ensure_ascii=False),row['id']))
    return payload


def send_one(client):
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    with db(write=True) as conn:
        row = conn.execute('SELECT * FROM fsui_outbox WHERE sent=0 ORDER BY id LIMIT 1').fetchone()
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
    body = (CreateMessageRequestBody.builder().receive_id(str(payload['chat_id'])).msg_type('interactive')
            .content(json.dumps(payload['_fs_card'],ensure_ascii=False)).uuid(payload['_fs_uuid']).build())
    req = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(body).build()
    response = client.im.v1.message.create(req)
    if not response.success():
        raise RuntimeError(f'Feishu card send failed (code {response.code})')
    fs_message_id = getattr(response.data, 'message_id', None)
    with db(write=True) as conn:
        if fs_message_id:
            conn.execute('INSERT OR IGNORE INTO fsui_messages(feishu_id,chat_id) VALUES (?,?)', (fs_message_id,str(payload['chat_id'])))
            row_id = conn.execute('SELECT id FROM fsui_messages WHERE feishu_id=?', (fs_message_id,)).fetchone()['id']
            prompt = payload.get('_prompt')
            if prompt:
                conn.execute('INSERT OR REPLACE INTO fsui_prompts VALUES (?,?,?,?,?,?)',
                    (str(payload['chat_id']),row_id,prompt['telegram_id'],prompt['token'],prompt['step'],int(time.time())+3600))
        conn.execute('UPDATE fsui_outbox SET sent=1,payload=? WHERE id=?', ('{}', row['id']))
    return True
