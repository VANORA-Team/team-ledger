"""Feishu private-chat worker. One worker must own a database's Feishu inbox."""
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

from fastapi import HTTPException
from . import ai, ai_context, ai_plan, config, feishu_ui
from .bot_ui import ai_query_line, amount_text
from .db import db, init_db, settings

LOG = logging.getLogger(__name__)
BOT_OPEN_ID = None
HELP = ('发送「绑定 绑定码」绑定账号。绑定后可发送「支出 35 CNY 打车」或「收入 100 CNY 收款」；'
        '也可以发送「最近账目」「余额」「待报销」。配置 AI 后可用自然语言记账和查询。'
        '新增、修改、作废、报销和分红等操作需要回复预览中的确认码。仅支持私聊。')


def accept_event(data):
    """Persist a validated SDK event before acknowledging it to Feishu."""
    event = data.event
    msg, sender = event.message, event.sender
    if (getattr(msg, 'chat_type', None) not in ('p2p', 'group') or
            getattr(msg, 'message_type', None) != 'text' or
            getattr(sender, 'sender_type', None) != 'user'):
        return
    sender_id = getattr(sender, 'sender_id', None)
    open_id = getattr(sender_id, 'open_id', None)
    message_id, chat_id = getattr(msg, 'message_id', None), getattr(msg, 'chat_id', None)
    if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in (open_id, message_id, chat_id)):
        return
    try:
        text = json.loads(msg.content).get('text', '').strip()
    except (ValueError, TypeError, AttributeError):
        return
    if not isinstance(text, str) or not text or len(text) > 4000:
        return
    if msg.chat_type == 'group':
        mentions = getattr(msg, 'mentions', None) or []
        bot_mentions = [m for m in mentions if BOT_OPEN_ID and
                        getattr(getattr(m, 'id', None), 'open_id', None) == BOT_OPEN_ID]
        if not bot_mentions:
            return
        for mention in bot_mentions:
            text = text.replace(mention.key, '')
        text = text.strip()
        if not text:
            text = '/start'
        text = feishu_ui.PREFIX + json.dumps({'group': True, 'text': text,
                              'parent': getattr(msg, 'parent_id', None)}, ensure_ascii=False)
    with db(write=True) as conn:
        conn.execute('INSERT OR IGNORE INTO feishu_inbox(message_id,open_id,chat_id,text,created_at) VALUES (?,?,?,?,?)',
                     (message_id, open_id, chat_id, text, config.now()))


def accept_card(data):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    event = data.event
    token = (getattr(event.action, 'value', None) or {}).get('token')
    open_id = getattr(event.operator, 'open_id', None)
    chat_id = getattr(event.context, 'open_chat_id', None)
    event_id = getattr(getattr(data, 'header', None), 'event_id', None)
    accepted = False
    if all(isinstance(x, str) and 0 < len(x) <= 256 for x in (token, open_id, chat_id, event_id)):
        with db(write=True) as conn:
            action = conn.execute(
                'SELECT a.group_mode FROM fsui_actions a JOIN fsui_identities i ON CAST(i.id AS TEXT)=a.telegram_id '
                'JOIN feishu_links l ON l.open_id=i.open_id JOIN fsui_links f ON f.telegram_id=a.telegram_id AND f.user_id=l.user_id '
                'JOIN users u ON u.id=l.user_id AND u.active=1 '
                'WHERE a.token=? AND i.open_id=? AND a.chat_id=? AND a.expires>?',
                (token, open_id, chat_id, int(time.time()))).fetchone()
            if action and (not action['group_mode'] or chat_id in settings(conn)['feishu_allowed_chats']):
                value = feishu_ui.PREFIX + json.dumps({'action_token': token, 'group': bool(action['group_mode'])})
                conn.execute('INSERT OR IGNORE INTO feishu_inbox(message_id,open_id,chat_id,text,created_at) VALUES (?,?,?,?,?)',
                             ('card:' + event_id, open_id, chat_id, value, config.now()))
                accepted = True
    return P2CardActionTriggerResponse({'toast': {
        'type': 'info' if accepted else 'warning',
        'content': '已收到' if accepted else '按钮已过期或不属于你，请重新打开菜单。'}})


def enqueue(conn, chat_id, open_id, text):
    owner = conn.execute('SELECT user_id FROM feishu_links WHERE open_id=?', (open_id,)).fetchone()
    conn.execute('INSERT INTO feishu_outbox(chat_id,open_id,user_id,text,message_uuid) VALUES (?,?,?,?,?)',
                 (chat_id, open_id, owner['user_id'] if owner else None, text[:12000], str(uuid.uuid4())))


def _raw_input(text, conf):
    match = re.fullmatch(r'(支出|收入)\s+(\d+(?:\.\d+)?)\s+([A-Za-z]{3})(?:\s+(.{1,200}))?', text)
    if match:
        return {'reply': '', 'questions': [], 'actions': [{'type': 'create_entry',
                'kind': 'expense' if match[1] == '支出' else 'income', 'amount': match[2],
                'currency': match[3].upper(), 'description': match[4] or '', 'date': config.today()}]}
    queries = {'最近账目': 'list_entries', '余额': 'balance', '待报销': 'due'}
    if text in queries:
        return {'reply': '', 'questions': [], 'actions': [{'type': 'query', 'query': queries[text]}]}
    return None


def _format_plan(plan, conf, confirmation=None):
    lines = []
    if plan.get('reply'):
        lines.append(str(plan['reply']))
    lines.extend('❓ ' + str(q) for q in plan.get('questions', []))
    for result in plan.get('results', []):
        result_text = ai_query_line(result.get('card', {}), conf)
        if result_text:
            lines.append(result_text)
    for card in plan.get('actions', []):
        lines.append(('⚠️ ' if card.get('errors') else '📝 ') +
                     str(card.get('label') or '账目') + '：' + str(card.get('summary') or ''))
        note = card.get('resolved', {}).get('note')
        if note:
            lines.append('备注：' + str(note))
        if card.get('type') == 'batch_pay':
            for item in card.get('resolved', {}).get('items', []):
                lines.append(f'  #{item["id"]} {item.get("description") or "未填写用途"} · '
                             f'{amount_text(item["amount"], item["currency"], conf)} → 抵扣 '
                             f'{amount_text(item["credited"], item["entry_currency"], conf)}')
        lines.extend('⚠️ ' + str(err) for err in card.get('errors', []))
        lines.extend('提示：' + str(warning) for warning in card.get('warnings', []))
    if confirmation:
        lines.append(f'请核对全部有效操作，回复「确认 {confirmation}」执行，或回复「取消 {confirmation}」。清单 30 分钟内有效。')
    return '\n'.join(lines) or '没有可执行的操作，请补充描述。'


def process_one():
    receipts = []
    with db() as conn:
        msg = conn.execute('SELECT rowid AS rowid,* FROM feishu_inbox WHERE done=0 ORDER BY rowid LIMIT 1').fetchone()
        if not msg:
            return False
        msg = dict(msg)
        user = conn.execute('SELECT u.* FROM feishu_links l JOIN users u ON u.id=l.user_id WHERE l.open_id=? AND u.active=1', (msg['open_id'],)).fetchone()
        user = dict(user) if user else None
        conf = settings(conn)
    text, open_id, chat_id = msg['text'], msg['open_id'], msg['chat_id']
    binding = re.fullmatch(r'(?:绑定|/start)\s+([A-Za-z0-9_-]{1,100})', text)
    ui_event = None
    if text.startswith(feishu_ui.PREFIX):
        ui_event = json.loads(text[len(feishu_ui.PREFIX):])
    elif user and not binding:
        with db() as conn:
            identity = conn.execute('SELECT id FROM fsui_identities WHERE open_id=?', (open_id,)).fetchone()
            active_draft = identity and conn.execute(
                'SELECT 1 FROM fsui_drafts WHERE telegram_id=? AND chat_id=? AND expires>?',
                (str(identity['id']), chat_id, int(time.time()))).fetchone()
        if text.startswith('/') or active_draft:
            ui_event = {'text': text, 'group': False}
    if ui_event is not None:
        if ui_event.get('group') and ui_event.get('text') == '/groupid':
            with db(write=True) as conn:
                enqueue(conn, chat_id, open_id, '本群 ID：' + chat_id + '\n请在 Web「团队设置」中添加此 ID。')
                conn.execute("UPDATE feishu_inbox SET done=1,text='' WHERE message_id=?", (msg['message_id'],))
            return True
        feishu_ui.process(msg, ui_event)
        with db(write=True) as conn:
            conn.execute("UPDATE feishu_inbox SET done=1,text='' WHERE message_id=?", (msg['message_id'],))
        return True
    confirm = re.fullmatch(r'(确认|取消)\s+([A-Fa-f0-9]{8})', text)
    prepared = context = rates = error = None
    if user and not binding and not confirm and text not in ('帮助', '/start', '/help'):
        try:
            prepared = _raw_input(text, conf)
            if prepared is None:
                with db() as conn:
                    context = ai_context.build(conn, conf, user)
                prepared = ai.parse(text, conf, context=context)
            with db() as conn:
                rates = ai_plan.needed_rates(conn, conf, prepared)
        except HTTPException as exc:
            error = str(exc.detail)
        except Exception:
            LOG.exception('Feishu planning failed')
            error = '暂时无法处理，请稍后重试。'
    with db(write=True) as conn:
        current = conn.execute('SELECT done FROM feishu_inbox WHERE message_id=?', (msg['message_id'],)).fetchone()
        if not current or current['done']:
            return True
        linked = conn.execute('SELECT u.* FROM feishu_links l JOIN users u ON u.id=l.user_id WHERE l.open_id=? AND u.active=1', (open_id,)).fetchone()
        reply = None
        if binding:
            code_hash = hashlib.sha256(binding[1].encode()).hexdigest()
            row = conn.execute('SELECT c.user_id FROM feishu_link_codes c JOIN users u ON u.id=c.user_id WHERE c.code_hash=? AND c.expires>? AND u.active=1', (code_hash, int(time.time()))).fetchone()
            if row:
                old_id = conn.execute('SELECT id FROM fsui_identities WHERE open_id=?', (open_id,)).fetchone()
                if old_id:
                    conn.execute('DELETE FROM fsui_actions WHERE telegram_id=?', (str(old_id['id']),))
                    conn.execute('DELETE FROM fsui_drafts WHERE telegram_id=?', (str(old_id['id']),))
                conn.execute('DELETE FROM fsui_drafts WHERE user_id=?', (row['user_id'],))
                conn.execute('DELETE FROM fsui_links WHERE user_id=? OR telegram_id=?',
                             (row['user_id'], str(old_id['id']) if old_id else ''))
                conn.execute('DELETE FROM feishu_pending WHERE open_id=? OR user_id=?', (open_id, row['user_id']))
                conn.execute('DELETE FROM feishu_links WHERE open_id=? OR user_id=?', (open_id, row['user_id']))
                conn.execute('INSERT INTO feishu_links VALUES (?,?)', (open_id, row['user_id']))
                conn.execute('DELETE FROM feishu_link_codes WHERE user_id=?', (row['user_id'],))
                reply = '绑定成功。发送 /start 打开按钮菜单。' + HELP
            else:
                reply = '绑定码无效或已过期，请在 Web「机器人与 AI」重新生成。'
        elif not linked:
            reply = '请先登录 Web「机器人与 AI」生成飞书绑定码，再在此私聊发送「绑定 绑定码」。'
        elif confirm:
            pending = conn.execute('SELECT * FROM feishu_pending WHERE open_id=? AND user_id=? AND confirmation=? AND expires>?',
                                   (open_id, linked['id'], confirm[2].upper(), int(time.time()))).fetchone()
            if not pending:
                reply = '确认码无效或已过期，请重新发送记账内容。'
            elif confirm[1] == '取消':
                conn.execute('DELETE FROM feishu_pending WHERE open_id=?', (open_id,))
                reply = '已取消这份清单。'
            else:
                conn.execute('SAVEPOINT feishu_execute')
                try:
                    row, plan = ai_plan.load(conn, linked, pending['plan_id'])
                    selected = [a['index'] for a in plan['actions'] if not a.get('errors')]
                    result = ai_plan.execute(conn, linked, pending['plan_id'], selected)
                    receipts = result.pop('receipts', [])
                    labels = {'create_entry': '新增', 'edit_entry': '修改', 'void_entry': '作废',
                              'submit_entry': '入账', 'delete_entry': '永久删除', 'pay_entry': '报销',
                              'dividend': '分红', 'batch_pay': '批量报销'}
                    lines = []
                    for item in result['results']:
                        target = (f'#{item["entry_id"]}' if 'entry_id' in item else
                                  f'D{item["dividend_id"]}' if 'dividend_id' in item else
                                  '、'.join(f'#{n}' for n in item.get('entry_ids', [])))
                        details = (f' · {item["amount"]} {item["currency"]}'
                                   if item.get('amount') and item.get('currency') else '')
                        lines.append(f'{labels.get(item["type"], "操作")} {target}{details}')
                    reply = '✅ 已完成：\n' + '\n'.join(lines)
                    conn.execute('DELETE FROM feishu_pending WHERE open_id=?', (open_id,))
                except HTTPException as exc:
                    conn.execute('ROLLBACK TO feishu_execute')
                    receipts = []
                    reply = str(exc.detail)
                conn.execute('RELEASE feishu_execute')
        elif text in ('帮助', '/start', '/help'):
            reply = HELP
        elif not user or user['id'] != linked['id']:
            reply = '账号绑定已变化，请重新发送。'
        elif error:
            reply = error
        else:
            conn.execute('SAVEPOINT feishu_plan')
            try:
                plan = ai_plan.resolve(conn, linked, settings(conn), prepared, context or {'entries': []}, rates)
                plan = ai_plan.store(conn, linked, plan, 'feishu')
                confirmation = None
                if plan.get('plan_id'):
                    confirmation = secrets.token_hex(4).upper()
                reply = _format_plan(plan, settings(conn), confirmation)
                if len(reply) > 12000:
                    if plan.get('plan_id'):
                        conn.execute('DELETE FROM ai_plans WHERE id=?', (plan['plan_id'],))
                    reply = '结果太长，无法完整展示，请把需求分成几次发送。'
                elif confirmation:
                    conn.execute('INSERT OR REPLACE INTO feishu_pending VALUES (?,?,?,?,?)',
                                 (open_id, linked['id'], plan['plan_id'], confirmation, plan['expires']))
            except HTTPException as exc:
                conn.execute('ROLLBACK TO feishu_plan')
                reply = str(exc.detail)
            conn.execute('RELEASE feishu_plan')
        enqueue(conn, chat_id, open_id, reply)
        # The input text can contain one-time binding codes and financial data.
        conn.execute("UPDATE feishu_inbox SET done=1,text='' WHERE message_id=?", (msg['message_id'],))
    for name in receipts:
        (config.DATA / 'receipts' / os.path.basename(name)).unlink(missing_ok=True)
    return True


def send_one(client):
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    with db() as conn:
        row = conn.execute('SELECT * FROM feishu_outbox WHERE sent=0 ORDER BY id LIMIT 1').fetchone()
        if not row:
            return False
        row = dict(row)
        authorized = row['user_id'] is None or conn.execute(
            'SELECT 1 FROM feishu_links l JOIN users u ON u.id=l.user_id '
            'WHERE l.open_id=? AND l.user_id=? AND u.active=1',
            (row['open_id'], row['user_id'])).fetchone()
    if not authorized:
        with db(write=True) as conn:
            conn.execute("UPDATE feishu_outbox SET sent=1,text='' WHERE id=?", (row['id'],))
        return True
    body = (CreateMessageRequestBody.builder().receive_id(row['chat_id']).msg_type('text')
            .content(json.dumps({'text': row['text']}, ensure_ascii=False)).uuid(row['message_uuid']).build())
    request = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(body).build()
    response = client.im.v1.message.create(request)
    if not response.success():
        raise RuntimeError(f'Feishu send failed (code {response.code})')
    with db(write=True) as conn:
        conn.execute("UPDATE feishu_outbox SET sent=1,text='' WHERE id=?", (row['id'],))
    return True


def credentials():
    with db() as conn:
        row = conn.execute('SELECT app_id,app_secret,version FROM feishu_config WHERE id=1').fetchone()
    if row:
        return row['app_id'], row['app_secret'], str(row['version'])
    return os.getenv('FEISHU_APP_ID', ''), os.getenv('FEISHU_APP_SECRET', ''), 'environment'


def run_worker():
    import lark_oapi as lark
    app_id, app_secret, _ = credentials()
    if not app_id or not app_secret:
        raise SystemExit('请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET。')
    init_db()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def refresh_bot_id():
        global BOT_OPEN_ID
        try:
            request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.GET)
                       .uri('/open-apis/bot/v3/info').token_types({lark.AccessTokenType.TENANT}).build())
            response = client.request(request)
            payload = json.loads(response.raw.content)
            BOT_OPEN_ID = payload.get('bot', {}).get('open_id') if response.success() else None
        except Exception:
            BOT_OPEN_ID = None
        if not BOT_OPEN_ID:
            LOG.warning('Unable to obtain the Feishu bot identity; group messages are disabled.')

    def work():
        next_identity_check = 0
        while True:
            try:
                if not BOT_OPEN_ID and time.monotonic() >= next_identity_check:
                    refresh_bot_id()
                    next_identity_check = time.monotonic() + 60
                handled = process_one()
                sent = send_one(client)
                ui_sent = feishu_ui.send_one(client)
                if not handled and not sent and not ui_sent:
                    time.sleep(1)
            except Exception:
                LOG.exception('Feishu worker failed; retrying')
                time.sleep(5)
    threading.Thread(target=work, daemon=True).start()
    handler = (lark.EventDispatcherHandler.builder('', '')
               .register_p2_im_message_receive_v1(accept_event)
               .register_p2_card_action_trigger(accept_card).build())
    from .feishu_ws import Client
    Client(app_id, app_secret, event_handler=handler).start()


def main():
    init_db()
    child = None
    running_version = None
    try:
        while True:
            app_id, app_secret, version = credentials()
            if child and (child.poll() is not None or version != running_version):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                child = None
            if not child and app_id and app_secret:
                child = subprocess.Popen([sys.executable, '-m', 'app.feishu', '--worker'])
                running_version = version
            time.sleep(5)
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


if __name__ == '__main__':
    if sys.argv[1:] == ['--worker']:
        init_db()
        run_worker()
    else:
        main()
