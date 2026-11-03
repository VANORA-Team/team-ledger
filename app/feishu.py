"""Feishu private-chat worker. One worker must own a database's Feishu inbox."""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid

from fastapi import HTTPException
from . import ai, ai_context, ai_plan, config
from .bot_ui import ai_query_line
from .db import db, init_db, settings

LOG = logging.getLogger(__name__)
HELP = ('发送「绑定 绑定码」绑定账号。绑定后可发送「支出 35 CNY 打车」或「收入 100 CNY 收款」；'
        '也可以发送「最近账目」「余额」「待报销」。配置 AI 后可用自然语言记账和查询。'
        '记账需要回复预览中的确认码。仅支持私聊。')


def accept_event(data):
    """Persist a validated SDK event before acknowledging it to Feishu."""
    event = data.event
    msg, sender = event.message, event.sender
    if (getattr(msg, 'chat_type', None) != 'p2p' or
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
    with db(write=True) as conn:
        conn.execute('INSERT OR IGNORE INTO feishu_inbox(message_id,open_id,chat_id,text,created_at) VALUES (?,?,?,?,?)',
                     (message_id, open_id, chat_id, text, config.now()))


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
        lines.extend('⚠️ ' + str(err) for err in card.get('errors', []))
        lines.extend('提示：' + str(warning) for warning in card.get('warnings', []))
    if confirmation:
        lines.append(f'请核对全部有效账目，回复「确认 {confirmation}」入账，或回复「取消 {confirmation}」。清单 30 分钟内有效。')
    return '\n'.join(lines) or '没有可执行的操作，请补充描述。'


def process_one():
    with db() as conn:
        msg = conn.execute('SELECT * FROM feishu_inbox WHERE done=0 ORDER BY rowid LIMIT 1').fetchone()
        if not msg:
            return False
        msg = dict(msg)
        user = conn.execute('SELECT u.* FROM feishu_links l JOIN users u ON u.id=l.user_id WHERE l.open_id=? AND u.active=1', (msg['open_id'],)).fetchone()
        user = dict(user) if user else None
        conf = settings(conn)
    text, open_id, chat_id = msg['text'], msg['open_id'], msg['chat_id']
    binding = re.fullmatch(r'(?:绑定|/start)\s+([A-Za-z0-9_-]{1,100})', text)
    confirm = re.fullmatch(r'(确认|取消)\s+([A-Fa-f0-9]{8})', text)
    prepared = context = rates = error = None
    if user and not binding and not confirm and text not in ('帮助', '/start', '/help'):
        try:
            prepared = _raw_input(text, conf)
            if prepared is None:
                with db() as conn:
                    context = ai_context.build(conn, conf, user)
                prepared = ai.parse(text, conf, context=context)
                # This integration deliberately supports creation and queries only.
                unsupported = [a for a in prepared.get('actions', []) if a.get('type') not in ('create_entry', 'query')]
                if unsupported:
                    prepared['actions'] = [a for a in prepared['actions'] if a.get('type') in ('create_entry', 'query')]
                    prepared.setdefault('questions', []).append('飞书暂不支持修改、删除、报销或分红，请在 Web 操作。')
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
                conn.execute('DELETE FROM feishu_pending WHERE open_id=? OR user_id=?', (open_id, row['user_id']))
                conn.execute('DELETE FROM feishu_links WHERE open_id=? OR user_id=?', (open_id, row['user_id']))
                conn.execute('INSERT INTO feishu_links VALUES (?,?)', (open_id, row['user_id']))
                conn.execute('DELETE FROM feishu_link_codes WHERE user_id=?', (row['user_id'],))
                reply = '绑定成功。' + HELP
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
                    reply = '✅ 已入账：\n' + '\n'.join(f'#{r["entry_id"]} {r.get("description") or "未填写用途"} · {r["amount"]} {r["currency"]}' for r in result['results'])
                    conn.execute('DELETE FROM feishu_pending WHERE open_id=?', (open_id,))
                except HTTPException as exc:
                    conn.execute('ROLLBACK TO feishu_execute')
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


def main():
    import lark_oapi as lark
    app_id, app_secret = os.getenv('FEISHU_APP_ID'), os.getenv('FEISHU_APP_SECRET')
    if not app_id or not app_secret:
        raise SystemExit('请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET。')
    init_db()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    def work():
        while True:
            try:
                handled = process_one()
                sent = send_one(client)
                if not handled and not sent:
                    time.sleep(1)
            except Exception:
                LOG.exception('Feishu worker failed; retrying')
                time.sleep(5)
    threading.Thread(target=work, daemon=True).start()
    handler = (lark.EventDispatcherHandler.builder('', '')
               .register_p2_im_message_receive_v1(accept_event).build())
    lark.ws.Client(app_id, app_secret, event_handler=handler).start()


if __name__ == '__main__':
    main()
