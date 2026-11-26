"""Telegram long-polling worker with durable replies. One worker per database."""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from fastapi import HTTPException
from . import ai, ai_context, ai_plan, config, excel_export, fx, permissions_sync
from .db import db, init_db, settings, set_setting
from .bot_ui import (COMMANDS, DELIVERY, PLATFORM, accepts_text, command_text, draft_for, handle,
                     queue, say, nav)


def process_update(update, db_provider=None, platform='telegram'):
    token = PLATFORM.set(platform)
    try:
        return _process_update(update, db_provider or db, platform)
    finally:
        PLATFORM.reset(token)


def _process_update(update, db_provider, platform):
    update_id = update.get('update_id')
    if type(update_id) is not int:
        return
    callback = update.get('callback_query')
    msg = callback.get('message', {}) if callback else update.get('message', {})
    sender = callback.get('from', {}) if callback else msg.get('from', {})
    telegram_id, chat_id = str(sender.get('id', '')), str(msg.get('chat', {}).get('id', ''))
    text = str(msg.get('text', '')).strip()[:4000] if not callback else ''
    with db_provider() as conn:
        if conn.execute('SELECT 1 FROM tg_updates WHERE update_id=?', (update_id,)).fetchone():
            return
        conf = settings(conn)
        command, argument = command_text(text, conf)
        authorized_chat = msg.get('chat', {}).get('type') == 'private' or chat_id in conf['feishu_allowed_chats' if platform == 'feishu' else 'telegram_allowed_chats']
        linked_any = conn.execute('SELECT u.* FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=?', (telegram_id,)).fetchone()
        linked = linked_any if (linked_any and linked_any['active']) else None
        draft = draft_for(conn, telegram_id, chat_id)
        accepted = accepts_text(conn, msg, telegram_id, chat_id, draft)
        callback_data = callback.get('data', '') if callback else ''
        payload = json.loads(draft['payload']) if draft else {}
        followup_history = payload.get('history') if draft and draft['step'] == 'ai_plan' and payload.get('_flow') == 'ai_plan' else None
        funds_flow = command == '/funds' or callback_data == 'funds' or callback_data.startswith('f:') or (payload.get('_flow') == 'funds' and ((not callback and not command and accepted) or callback_data == f'd:{draft["token"]}:resume'))
        codes = {r['currency'] for r in conn.execute('SELECT DISTINCT currency FROM entries')}
        payment_code = None
        if callback and draft:
            parts = callback.get('data', '').split(':')
            payload = json.loads(draft['payload'])
            if len(parts) == 4 and parts[:3] == ['d', draft['token'], 'paycur'] and payload.get('_action') == 'pay':
                row = conn.execute('SELECT currency,fx_rate,fx_currency FROM entries WHERE id=?', (payload['_entry_id'],)).fetchone()
                if row and not (row['fx_rate'] and row['fx_currency'] == parts[3]):
                    payment_code = row['currency']
    if linked_any and linked_any['oidc_sub']:
        if permissions_sync.sync_member(linked_any['oidc_sub'], force=False, db_provider=db_provider):
            with db_provider() as conn:
                linked = conn.execute('SELECT u.* FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=? AND u.active=1', (telegram_id,)).fetchone()
                draft = draft_for(conn, telegram_id, chat_id)
                payload = json.loads(draft['payload']) if draft else {}
    result, error, rates = None, None, None
    authorized = bool(linked and authorized_chat and command != 'foreign' and not sender.get('is_bot'))
    if authorized and (callback_data.endswith(':edit:payer') or (not callback and text)):
        permissions_sync.sync_all_members(force=False, db_provider=db_provider)
    wants_ai = not callback and (
        (command == '/ai' and bool(argument))
        or (not command and text and accepted and (
            not draft or draft['step'] == 'ai_text'
            or (draft['step'] == 'ai_plan' and payload.get('_flow') == 'ai_plan')
        ))
    )
    # External calls happen before taking the SQLite write lock.
    progress_stop = progress_message = ledger_context = None
    if authorized and wants_ai:
        # Immediate feedback first: context assembly may also touch the FX cache.
        if platform == 'telegram':
            progress_stop, progress_message = progress_start(chat_id, msg.get('message_thread_id'))
        try:
            try:
                with db_provider() as conn:
                    ledger_context = ai_context.build(conn, conf, dict(linked))
            except Exception:
                ledger_context = None
            try:
                result = ai.parse(argument if command == '/ai' else text, conf,
                                  context=ledger_context, history=followup_history)
            except HTTPException as exc:
                error = exc.detail
        finally:
            if progress_stop:
                progress_stop.set()
    if authorized:
        codes.add(conf['default_currency'])
        if draft:
            codes.add(json.loads(draft['payload']).get('currency', conf['default_currency']))
        if result:
            codes.add(result.get('currency', conf['default_currency']))
        if funds_flow:
            # Only posted income, outstanding expenses and actual payments not covered by manual rates need external FX.
            with db_provider() as conn:
                fund_entries = [dict(r) for r in conn.execute("SELECT id,currency,status,fx_rate,fx_currency FROM entries WHERE status IN ('posted','pending','reimbursed')")]
                fund_payments = [dict(r) for r in conn.execute('SELECT entry_id,currency FROM payments')]
            extra = set()
            fund_action = callback_data.split(':')[2] if callback_data.startswith('f:') and len(callback_data.split(':')) > 2 else ''
            if payload.get('scenario') and fund_action not in ('refresh', 'cancel', 'batch', 'mode') and command != '/funds' and callback_data != 'funds':
                extra.add(payload['scenario']['currency'])
            if payload.get('scenario_currency') and draft and draft['step'] == 'funds_amount' and not callback:
                extra.add(payload['scenario_currency'])
            codes = fx.required_codes(fund_entries, fund_payments, conf['reference_currency'], extra)
        if payment_code:
            # A payment quote only needs this entry's pair. Other ledger/default
            # currencies must not block a supported settlement-currency payment.
            codes = {payment_code}
        wants_rates = callback and (callback_data.startswith(('list:', 'e:', 'd:', 'edit:')) or callback_data in ('summary', 'funds'))
        frozen_funds = funds_flow and draft and payload.get('_flow') == 'funds' and (
            callback_data.startswith(f'f:{draft["token"]}:') and fund_action in ('plan', 'receipt', 'note', 'commit')
            or callback_data == f'd:{draft["token"]}:resume' and draft['step'] in ('funds_confirm', 'funds_note', 'funds_receipt')
            or not callback and not command and draft['step'] == 'funds_note')
        if not frozen_funds and (funds_flow or wants_rates or command in ('/summary', '/team', '/recent', '/funds') or result or (draft and draft['step'].startswith(('edit_', 'description', 'amount')))):
            try:
                rates = fx.rates_for(conf['reference_currency'], codes)
            except Exception:
                rates = None
        if result and isinstance(result, dict) and isinstance(result.get('actions'), list):
            # AI plans may need batch/query rates beyond the chat's usual currencies.
            try:
                with db_provider() as conn:
                    plan_rates = ai_plan.needed_rates(conn, conf, result)
                if plan_rates:
                    rates = plan_rates
            except Exception:
                pass
    context = DELIVERY.set({'callback': bool(callback), 'message_id': msg.get('message_id'),
                            'feishu_message_id': msg.get('feishu_message_id'),
                            'user_message_id': msg.get('message_id') if not callback else None,
                            'thread_id': msg.get('message_thread_id'),
                            'actor': telegram_id, 'chat_id': chat_id,
                            'group': msg.get('chat', {}).get('type') != 'private'})
    try:
        with db_provider(write=True) as conn:
            if conn.execute('SELECT 1 FROM tg_updates WHERE update_id=?', (update_id,)).fetchone():
                return
            conn.execute('SAVEPOINT handling')
            try:
                handle(conn, update, result, error, rates, ledger_context)
            except HTTPException as exc:
                conn.execute('ROLLBACK TO handling')
                if callback:
                    queue(conn, 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': str(exc.detail)[:150], 'show_alert': True})
                elif chat_id:
                    say(conn, chat_id, str(exc.detail), [nav()])
            conn.execute('RELEASE handling')
            conn.execute('INSERT INTO tg_updates VALUES (?,?)', (update_id, config.now()))
            if platform == 'telegram':
                set_setting(conn, 'bot_offset', update_id + 1)
                set_setting(conn, 'bot_heartbeat', int(time.time()))
            if progress_message:
                # Remove the transient "thinking" bubble after the real reply is queued.
                queue(conn, 'deleteMessage', {'chat_id': chat_id, 'message_id': progress_message})
    finally:
        DELIVERY.reset(context)


class TelegramError(RuntimeError):
    def __init__(self, code=0, description=''):
        super().__init__(f'Telegram operation failed ({code})')
        self.code, self.description = code, description


def _tg_opener():
    proxy = os.getenv('TELEGRAM_PROXY_URL')
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({'https': proxy}) if proxy else urllib.request.ProxyHandler()
    )


def _tg_execute(request, timeout=40):
    opener = _tg_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read(4_000_000))
        if not body.get('ok'):
            raise TelegramError(body.get('error_code', 0), body.get('description', ''))
        return body.get('result')
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(20000))
        except Exception:
            body = {}
        raise TelegramError(exc.code, body.get('description', '')) from None
    except TelegramError:
        raise
    except Exception:
        # Request URLs contain the token. Never log the original urllib exception.
        raise TelegramError() from None


def api(method, payload):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
    )
    return _tg_execute(request, timeout=40)


def _encode_multipart(fields, file_field, filename, data, content_type):
    boundary = '----VanoraLedger' + secrets.token_hex(12)
    chunks = []
    for key, value in fields.items():
        if value is None or value == '':
            continue
        chunks.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f'{value}\r\n'.encode('utf-8')
        )
    safe_name = str(filename or 'team-ledger.xlsx').replace('"', '_').replace('\r', '').replace('\n', '')
    encoded_name = urllib.parse.quote(safe_name, safe='')
    chunks.append(
        (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe_name}"; filename*=UTF-8\'\'{encoded_name}\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode('utf-8')
    )
    chunks.append(data)
    chunks.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def send_document(chat_id, filename, content, caption='', thread_id=None):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    fields = {'chat_id': str(chat_id)}
    if caption:
        fields['caption'] = str(caption)[:1024]
    if thread_id:
        fields['message_thread_id'] = str(thread_id)
    body, content_type = _encode_multipart(
        fields,
        'document',
        filename,
        content,
        excel_export.EXCEL_MEDIA_TYPE,
    )
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendDocument',
        data=body,
        headers={'Content-Type': content_type},
    )
    return _tg_execute(request, timeout=60)


def progress_start(chat_id, thread_id=None):
    """Best-effort "AI is thinking" feedback. Never blocks or breaks processing."""
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        return None, None
    payload = {'chat_id': chat_id,
               'text': '🤖 收到，正在理解你的描述…\n☕ 内容较多时可能要十几秒，请稍等。'}
    if thread_id:
        payload['message_thread_id'] = thread_id
    message_id = None
    try:
        sent = api('sendMessage', payload)
        message_id = sent.get('message_id') if isinstance(sent, dict) else None
    except Exception:
        pass
    stop = threading.Event()

    def keep_typing():
        ticks = 0
        while not stop.wait(4):
            ticks += 1
            try:
                action = {'chat_id': chat_id, 'action': 'typing'}
                if thread_id:
                    action['message_thread_id'] = thread_id
                api('sendChatAction', action)
            except Exception:
                return
            if ticks == 4 and message_id:
                # Reassure the user when a large paragraph takes longer.
                try:
                    api('editMessageText', {'chat_id': chat_id, 'message_id': message_id,
                                            'text': '🤖 还在整理这段描述，马上就好…\n⏳ 已收到，请不要重复发送。'})
                except Exception:
                    pass

    threading.Thread(target=keep_typing, daemon=True).start()
    return stop, message_id


def _deliver_excel_document(row, payload):
    user_id = payload.get('user_id')
    with db() as conn:
        user_row = conn.execute('SELECT oidc_sub FROM users WHERE id=?', (user_id,)).fetchone() if user_id else None
        linked_sub = user_row['oidc_sub'] if user_row else None
    if linked_sub:
        permissions_sync.sync_member(linked_sub, force=False)
    with db() as conn:
        authorized = bool(user_id and conn.execute(
            "SELECT 1 FROM tg_links l JOIN users u ON u.id=l.user_id "
            "WHERE l.user_id=? AND u.active=1 AND u.role IN ('owner','finance')",
            (user_id,),
        ).fetchone())
        conf = settings(conn)
        chat_str = str(payload.get('chat_id', ''))
        if authorized and chat_str.startswith('-') and chat_str not in conf['telegram_allowed_chats']:
            authorized = False
    if not authorized:
        with db(write=True) as conn:
            conn.execute('UPDATE tg_outbox SET sent=1 WHERE id=?', (row['id'],))
        return True, None
    try:
        exported = excel_export.build_excel_export(**(payload.get('filters') or {}))
    except HTTPException:
        with db(write=True) as conn:
            conn.execute('UPDATE tg_outbox SET attempts=8 WHERE id=?', (row['id'],))
        return True, None
    caption = excel_export.format_export_caption(exported, conf.get('team_name', ''))
    return False, send_document(
        payload['chat_id'],
        exported['filename'],
        exported['content'],
        caption=caption,
        thread_id=payload.get('message_thread_id'),
    )


def drain_outbox():
    with db() as conn:
        pending = [dict(r) for r in conn.execute('SELECT * FROM tg_outbox WHERE sent=0 AND attempts<8 ORDER BY id LIMIT 30')]
    for row in pending:
        payload = json.loads(row['payload'])
        prompt, thread_id = payload.pop('_prompt', None), payload.pop('_thread_id', None)
        method = row['method']
        try:
            try:
                if method == 'sendDocument' and payload.get('export') == 'excel':
                    already_finalized, result = _deliver_excel_document(row, payload)
                    if already_finalized:
                        continue
                else:
                    result = api(method, payload)
            except TelegramError as exc:
                if method == 'editMessageText' and exc.code == 400 and 'message is not modified' in exc.description.lower():
                    result = None
                elif method == 'editMessageText' and exc.code == 400:
                    payload.pop('message_id', None)
                    if thread_id:
                        payload['message_thread_id'] = thread_id
                    result = api('sendMessage', payload)
                elif method == 'answerCallbackQuery' and exc.code == 400:
                    result = None
                elif exc.code in (400, 403):
                    with db(write=True) as conn:
                        conn.execute('UPDATE tg_outbox SET attempts=8 WHERE id=?', (row['id'],))
                    continue
                else:
                    raise
            with db(write=True) as conn:
                conn.execute('UPDATE tg_outbox SET sent=1 WHERE id=?', (row['id'],))
                if prompt and result and result.get('message_id'):
                    conn.execute('INSERT OR REPLACE INTO tg_prompts VALUES (?,?,?,?,?,?)',
                                 (str(payload['chat_id']), result['message_id'], prompt['telegram_id'], prompt['token'], prompt['step'], int(time.time()) + 3600))
        except RuntimeError:
            with db(write=True) as conn:
                conn.execute('UPDATE tg_outbox SET attempts=attempts+1 WHERE id=?', (row['id'],))
            break


def configure_commands():
    api('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in COMMANDS]})
    api('setChatMenuButton', {'menu_button': {'type': 'commands'}})


def main():
    init_db()
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print('TELEGRAM_BOT_TOKEN 未配置。请填写 .env 后重启。')
        return
    try:
        me = api('getMe', {})
        webhook = api('getWebhookInfo', {})
        if webhook.get('url'):
            print('已配置 Webhook，请先在原服务停用再启动长轮询。')
            return
        with db(write=True) as conn:
            set_setting(conn, 'bot_username', me.get('username'))
            set_setting(conn, 'bot_id', me['id'])
        configure_commands()
        print('Telegram 团队账本已启动。')
    except RuntimeError:
        print('机器人初始化失败，请检查网络与配置。')
        return
    while True:
        try:
            drain_outbox()
            with db() as conn:
                offset = settings(conn).get('bot_offset', 0)
            updates = api('getUpdates', {'offset': offset, 'timeout': 20, 'allowed_updates': ['message', 'callback_query']})
            for update in updates:
                callback = update.get('callback_query')
                if callback:
                    try:
                        api('answerCallbackQuery', {'callback_query_id': callback['id']})
                    except RuntimeError:
                        pass
                process_update(update)
                drain_outbox()
            with db(write=True) as conn:
                set_setting(conn, 'bot_heartbeat', int(time.time()))
        except KeyboardInterrupt:
            break
        except Exception:
            print('机器人连接或处理失败，5 秒后重试。')
            time.sleep(5)


if __name__ == '__main__':
    main()
