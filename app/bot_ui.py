"""Telegram menus, editable drafts and confirmed ledger operations."""
import hashlib
import json
import os
import time
import secrets
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from . import ai_plan, config, excel_export
from .db import settings
from .domain import (add_entry, admin, can_manage_entry, compute_fee, currency, delete_entry, edit_entry, entry_view, fail,
                     is_finance, minor_amount, normalized_entry, require_entry_access, text_value,
                     transition, make_payment_quote, payment_preview)

DELIVERY = ContextVar('telegram_delivery', default={})
PLATFORM = ContextVar('bot_platform', default='telegram')
PAGE_SIZE = 5
STATUS = {'pending': '待报销', 'reimbursed': '已报销', 'draft': '草稿', 'posted': '已入账', 'voided': '已作废'}
COMMANDS = [('start', '打开团队账本菜单'), ('book', '记录成员垫付'), ('expense', '记录公账支出'), ('income', '记录营业收入'),
            ('capital', '记录公户转入（不计入利润）'), ('ai', '自然语言记账'), ('recent', '最近账目与详情'), ('summary', '我的待报销明细'),
            ('team', '团队待报销明细'), ('funds', '公户结余、资金计算与一键报销'), ('today', '今日收支'),
            ('month', '本月收支'), ('export', '导出 Excel 账本报表'), ('cancel', '取消当前填写'), ('help', '使用帮助')]


def queue(conn, method, payload):
    if PLATFORM.get() == 'feishu':
        payload = dict(payload)
        delivery = DELIVERY.get()
        payload['_fs_actor'] = delivery.get('actor')
        payload['_fs_group'] = delivery.get('group', False)
        payload['_fs_chat'] = delivery.get('chat_id')
        if delivery.get('feishu_message_id'):
            payload['_fs_message_id'] = delivery.get('feishu_message_id')
    conn.execute('INSERT INTO tg_outbox(method,payload) VALUES (?,?)', (method, json.dumps(payload, ensure_ascii=False)))


def button(text, data):
    return {'text': text, 'callback_data': data}


def nav():
    return [button('🏠 主菜单', 'home'), button('＋ 再记一笔', 'new')]


def web_button(path='', label='打开 Web 账本'):
    url = os.getenv('PUBLIC_URL', '').rstrip('/')
    return {'text': label, 'url': url + path} if url.startswith(('https://', 'http://')) else None


def say(conn, chat_id, text, buttons=None, edit=False, prompt=None):
    ctx = DELIVERY.get()
    payload, method = {'chat_id': chat_id, 'text': text}, 'sendMessage'
    if edit and ctx.get('callback') and ctx.get('message_id'):
        method = 'editMessageText'
        payload['message_id'] = ctx['message_id']
        payload['_thread_id'] = ctx.get('thread_id')
    elif ctx.get('thread_id'):
        payload['message_thread_id'] = ctx['thread_id']
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    if prompt:
        payload['reply_markup'] = {'force_reply': True, 'selective': True, 'input_field_placeholder': '回复此消息填写，/cancel 取消'}
        if ctx.get('user_message_id'):
            payload['reply_parameters'] = {'message_id': ctx['user_message_id'], 'allow_sending_without_reply': True}
        payload['_prompt'] = prompt
    queue(conn, method, payload)


def amount_text(value, code, conf):
    cur = currency(conf, code)
    value = Decimal(str(value)).quantize(Decimal(1).scaleb(-cur['decimals']), rounding=ROUND_HALF_UP)
    return f'{cur["symbol"]}{value:,.{cur["decimals"]}f} {code}'


def original(entry, conf):
    return amount_text(Decimal(entry['amount_minor']) / (10 ** entry['decimals']), entry['currency'], conf)


def entry_status(entry):
    return '部分报销' if entry['status'] == 'pending' and entry['reimbursed_minor'] else STATUS[entry['status']]


def entry_description(entry):
    return entry.get('description') or '未填写说明'


def entry_type_label(entry):
    if entry.get('kind') == 'income':
        return '公户转入' if entry.get('income_type') == 'capital' else '营业收入'
    return '公账支出' if entry.get('expense_type') == 'public' else '成员垫付'


def payment_totals(entry, conf):
    return f'累计已报：{amount_text(entry["reimbursed_amount"],entry["currency"],conf)}\n剩余待报：{amount_text(entry["remaining_amount"],entry["currency"],conf)}'


def reference(value, code, conf, rates):
    base = conf['reference_currency']
    if code == base:
        return ''
    row = (rates or {}).get('rates', {}).get(code)
    if not row:
        return f'结算参考：{base} 汇率暂不可用'
    converted = amount_text(Decimal(str(value)) * Decimal(row['rate']), base, conf)
    suffix = ' · 缓存' if rates.get('stale') else ''
    return f'≈ {converted} · {row["date"]}{suffix}'


def menu(conn, user, chat_id):
    conf = settings(conn)
    count = conn.execute("SELECT COUNT(*) FROM entries WHERE payer_id=? AND status='pending'", (user['id'],)).fetchone()[0]
    bottom_row = [button('📥 导出 Excel', 'export'), button('使用帮助', 'help')] if is_finance(user) else [button('使用帮助', 'help')]
    rows = [[button('💸 成员垫付', 'new'), button('本月收支', 'period:month')],
            [button('✨ AI 记账', 'ai'), button('📋 最近账目', 'list:recent:0')],
            [button(f'🙋 我的待报销 · {count}', 'list:mine:0'), button('👥 团队待报销', 'list:team:0')],
            [button('📊 资金与付款项目', 'funds'), button('📅 今日收支', 'period:today')],
            bottom_row]
    if is_finance(user):
        rows.insert(0, [button('🏦 公账支出', 'newpublic'), button('💰 营业收入', 'newincome'), button('🏛️ 公户转入', 'newcapital')])
    draft = conn.execute("SELECT token FROM tg_drafts WHERE user_id=? AND chat_id=? AND expires>? AND step<>'funds_receipt'",
                         (user['id'], chat_id, int(time.time()))).fetchone()
    if draft:
        rows.insert(1, [button('继续当前填写', f'd:{draft["token"]}:resume'), button('取消当前填写', f'd:{draft["token"]}:cancel')])
    if web_button():
        rows.append([web_button()])
    say(conn, chat_id, f'📒 {conf["team_name"]}\n{user["name"]}，你的团队账本已连接。\n\n'
        f'默认币种 {conf["default_currency"]} · 结算参考 {conf["reference_currency"]}\n'
        '公账支出直接扣款；成员垫付记账后待报销，付款后标记已报销。\n选择下方功能，或私聊直接描述一笔收支。', rows, edit=True)


def help_menu(conn, chat_id):
    group_help = ('群聊：@机器人发起命令或菜单，填写金额等内容时请回复机器人提示并 @机器人，普通聊天不会触发记账。成员草稿互相独立。\n'
                  if PLATFORM.get() == 'feishu' else
                  '群聊：请回复机器人弹出的输入提示，普通聊天不会触发记账。成员草稿互相独立。\n')
    say(conn, chat_id, '📖 使用帮助\n\n'
        '记账：点公账支出 / 成员垫付 / 营业收入 / 公户转入，选币种、输入金额即可预览；用途、备注选填。收入预览支持一键切换营业收入或公户转入（不计利润），编辑用途或备注时发送 - 清空。\n'
        '🤖 AI：私聊直接说「昨天打车花了35元」，群里使用 /ai 记账内容；也可以一次说一段话，核对清单后才入账。\n'
        '查账：最近账目、个人与团队待报销均可翻页，点项目查看详情。\n'
        '账本所有者和财务管理员可登记公账支出、营业收入、公户转入与报销，并使用 /export 导出含收据的 Excel（.xlsx）报表（支持按月、待报销或自定义日期范围）。/funds 可查看公户结余、经营利润、模拟进账与具体报销项目。\n'
        '一键报销：选择记账 / 结算货币，核对全部项目后确认已转账。模拟收入不会自动入账，也不会用于实际付款。\n'
        + group_help +
        '取消：/cancel；返回菜单：/start。凭证文件请到 Web 上传。', [nav()], edit=True)


def show_export_menu(conn, user, chat_id):
    admin(user)
    say(
        conn,
        chat_id,
        '📥 导出 Excel 报表（.xlsx）\n\n'
        '报表包含：资金汇总、收支明细（含收据预览）、逐次报销付款、成员待报销及收据大图工作表。\n'
        '选择下方导出范围，或直接发送：\n'
        '/export 2026-11\n'
        '/export 2026-11-01 2026-11-22',
        [
            [button('📅 本月账目', 'x:month'), button('📆 上月账目', 'x:last_month')],
            [button('📋 全部有效账目', 'x:all'), button('🧾 全部待报销', 'x:pending')],
            [button('🗂️ 全部（含已作废）', 'x:all_voided'), button('🔍 自定义日期范围', 'x:range')],
            [button('🏠 主菜单', 'home')],
        ],
        edit=True,
    )


def queue_excel_export(conn, user, chat_id, filters_spec):
    admin(user)
    validated = excel_export.parse_export_filters(conn, **filters_spec)
    scope_label = excel_export._build_scope_label(validated)
    ctx = DELIVERY.get()
    say(
        conn,
        chat_id,
        f'📊 正在生成并发送 Excel 报表\n范围：{scope_label}\n\n'
        '包含资金汇总、收支明细、逐次报销付款、成员待报销及收据凭证，文件将随后发送到当前会话。',
        [[button('📥 导出其他范围', 'export'), button('🏠 主菜单', 'home')]],
        edit=True,
    )
    payload = {
        'chat_id': chat_id,
        'user_id': user['id'],
        'export': 'excel',
        'filters': filters_spec,
    }
    if ctx.get('thread_id'):
        payload['message_thread_id'] = ctx['thread_id']
    queue(conn, 'sendDocument', payload)


def export_callback(conn, user, telegram_id, chat_id, data):
    admin(user)
    parts = data.split(':', 1)
    preset = parts[1] if len(parts) == 2 else ''
    if preset == 'range':
        new_draft(conn, user, telegram_id, chat_id, payload={'_flow': 'export'}, step='export_range')
        ask(
            conn,
            chat_id,
            draft_for(conn, telegram_id, chat_id),
            'export_range',
            '请输入导出日期范围：\n• 单月：2026-11\n• 区间：2026-11-01 2026-11-22\n• 单日：2026-11-22',
        )
        return
    if preset in ('month', 'last_month', 'all', 'pending', 'all_voided'):
        filters_spec = excel_export.parse_export_shortcut(preset)
        queue_excel_export(conn, user, chat_id, filters_spec)
        return
    fail('导出选项无效，请重新选择')


def new_draft(conn, user, telegram_id, chat_id, payload=None, step=None):
    conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
    token = secrets.token_hex(5)
    step = step or ('confirm' if payload else 'currency')
    payload = dict(payload) if payload else {'kind': 'expense', 'date': config.today(), 'note': '', 'status': 'pending'}
    if payload.get('kind') == 'expense':
        payload.setdefault('expense_type', 'advance')
    elif payload.get('kind') == 'income':
        payload.setdefault('income_type', 'operating')
    payload.setdefault('description', '')
    payload.setdefault('payer_id', user['id'])
    conn.execute('INSERT INTO tg_drafts VALUES (?,?,?,?,?,?,?)',
                 (token, telegram_id, chat_id, user['id'], step, json.dumps(payload, ensure_ascii=False), int(time.time()) + 3600))
    return token, payload


def draft_for(conn, telegram_id, chat_id):
    return conn.execute('SELECT * FROM tg_drafts WHERE telegram_id=? AND chat_id=? AND expires>?',
                        (telegram_id, chat_id, int(time.time()))).fetchone()


def persist(conn, token, data, step):
    conn.execute('UPDATE tg_drafts SET step=?,payload=?,expires=? WHERE token=?',
                 (step, json.dumps(data, ensure_ascii=False), int(time.time()) + 3600, token))


def ask(conn, chat_id, draft, step, text):
    persist(conn, draft['token'], json.loads(draft['payload']), step)
    name = conn.execute('SELECT name FROM users WHERE id=?', (draft['user_id'],)).fetchone()['name']
    group_hint = '群聊中请回复这条提示并 @机器人。' if PLATFORM.get() == 'feishu' else '群聊中请回复这条提示。'
    say(conn, chat_id, f'{name}，{text}\n\n{group_hint}/cancel 可取消。',
        prompt={'token': draft['token'], 'telegram_id': draft['telegram_id'], 'step': step})
    if PLATFORM.get() != 'feishu':
        row = conn.execute('SELECT id,payload FROM tg_outbox ORDER BY id DESC LIMIT 1').fetchone()
        payload = json.loads(row['payload'])
        payload['entities'] = [{'type': 'text_mention', 'offset': 0, 'length': len(name.encode('utf-16-le')) // 2,
                                'user': {'id': int(draft['telegram_id']), 'is_bot': False, 'first_name': name}}]
        conn.execute('UPDATE tg_outbox SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), row['id']))


def show_currencies(conn, chat_id, token, conf):
    buttons = [button(c['code'] + ' ' + c['name'], f'd:{token}:cur:{c["code"]}') for c in conf['currencies']]
    say(conn, chat_id, '选择币种，输入金额后即可预览；用途、备注选填。', [buttons[i:i+2] for i in range(0, len(buttons), 2)] + [[button('取消', f'd:{token}:cancel')]], edit=True)


def fee_label(entry, conf):
    mode = entry.get('fee_mode') or 'none'
    fee_minor = entry.get('fee_minor') or 0
    if mode == 'none' and not fee_minor:
        return ''
    fee_txt = amount_text(Decimal(fee_minor) / (10 ** entry['decimals']), entry['currency'], conf)
    if mode == 'rate' and entry.get('fee_rate'):
        return f'按比例 {entry["fee_rate"]}%（{fee_txt}）'
    return f'手动 {fee_txt}'


def preview(conn, user, chat_id, token, data, rates=None):
    conf = settings(conn)
    payer = conn.execute('SELECT name FROM users WHERE id=?', (data.get('payer_id'),)).fetchone()
    kind = ('公户转入 · 计入公户/不计利润' if data.get('income_type') == 'capital' else '营业收入 · 计入公户与利润') if data['kind'] == 'income' else '公账支出 · 直接扣款' if data.get('expense_type') == 'public' else '成员垫付 · 待报销'
    member_label = '转入成员' if (data['kind'] == 'income' and data.get('income_type') == 'capital') else '成员'
    cur = currency(conf, data['currency'])
    gross_minor = minor_amount(data['amount'], cur['decimals'])
    fee_mode, fee_rate, fee_minor, effective_minor = compute_fee(data['kind'], gross_minor, cur['decimals'], data)
    effective_dec = Decimal(effective_minor) / (10 ** cur['decimals'])
    effective_str = format(effective_dec, f'.{cur["decimals"]}f')
    lines = [f'🧾 确认{kind}', f'用途：{entry_description(data)}']
    if fee_mode != 'none' or fee_minor > 0:
        fee_txt = amount_text(Decimal(fee_minor) / (10 ** cur['decimals']), data['currency'], conf)
        mode_txt = f'按比例 {fee_rate}%（{fee_txt}）' if fee_mode == 'rate' else f'手动 {fee_txt}'
        eff_label = '实收金额' if data['kind'] == 'income' else '实付总额'
        lines += [
            f'原金额：{amount_text(data["amount"], data["currency"], conf)}',
            f'手续费：{mode_txt}',
            f'{eff_label}：{amount_text(effective_str, data["currency"], conf)}',
        ]
    else:
        lines.append(f'金额：{amount_text(data["amount"], data["currency"], conf)}')
    converted = reference(effective_str, data['currency'], conf, rates)
    if converted:
        lines.append(converted)
    lines += [f'日期：{data["date"]}', f'{member_label}：{payer["name"] if payer else "当前成员"}', f'备注：{data.get("note") or "未填写"}', '\n核对后确认才入账。']
    rows = [
        [button('✅ 保存修改' if data.get('_edit_id') else '✅ 确认入账', f'd:{token}:save')],
        [button('金额', f'd:{token}:edit:amount'), button('币种', f'd:{token}:edit:currency'), button('手续费', f'd:{token}:edit:fee')],
        [button('用途', f'd:{token}:edit:description'), button('日期', f'd:{token}:edit:date'), button('备注', f'd:{token}:edit:note')]
        + ([button('成员', f'd:{token}:edit:payer')] if is_finance(user) else []),
    ]
    if data['kind'] == 'income':
        rows.append([button('切换为营业收入' if data.get('income_type') == 'capital' else '切换为公户转入', f'd:{token}:inctype')])
    rows.append([button('取消', f'd:{token}:cancel'), button('主菜单', 'home')])
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def list_entries(conn, user, chat_id, scope='recent', page=0, rates=None):
    if scope not in ('recent', 'mine', 'team', 'plan', 'voided'):
        fail('列表不存在')
    params, where = [], ''
    if scope == 'mine':
        where, params = "WHERE payer_id=? AND status='pending'", [user['id']]
    elif scope == 'voided':
        where = "WHERE status='voided'"
    elif scope in ('team', 'plan'):
        where = "WHERE status='pending'"
    else:
        where = "WHERE status<>'voided'"
    count = conn.execute('SELECT COUNT(*) FROM entries ' + where, params).fetchone()[0]
    page = min(max(page, 0), max((count - 1) // PAGE_SIZE, 0))
    order = 'date,id' if scope == 'plan' else 'date DESC,id DESC'
    ids = conn.execute(f'SELECT id FROM entries {where} ORDER BY {order} LIMIT ? OFFSET ?', params + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    conf = settings(conn)
    title = {'recent': '📋 最近账目 · 不含已作废', 'mine': '🙋 我的待报销', 'team': '👥 团队待报销',
             'plan': '📌 待报销项目 · 最早日期优先', 'voided': '🗑 已作废账目'}[scope]
    lines = [f'{title}\n第 {page + 1} / {max((count + PAGE_SIZE - 1) // PAGE_SIZE, 1)} 页 · 共 {count} 笔']
    if scope in ('mine', 'team'):
        sums = conn.execute('SELECT currency,decimals,SUM(amount_minor-reimbursed_minor) amount FROM entries ' + where + ' GROUP BY currency,decimals', params).fetchall()
        # Totals are bounded so teams with many currencies stay under Telegram limits.
        lines.append('合计：' + (' / '.join(amount_text(Decimal(r['amount']) / (10 ** r['decimals']), r['currency'], conf) for r in sums[:8]) or '暂无'))
    rows = []
    for row in ids:
        entry = entry_view(conn, row['id'])
        converted = reference(entry['amount'], entry['currency'], conf, rates)
        lines.append(f'\n#{entry["id"]} {entry_description(entry)}\n{original(entry,conf)} · {entry_status(entry)}\n'
                     f'{entry["payer_name"]} · {entry["date"]}' + ('\n' + converted if converted else ''))
        if entry['reimbursed_minor']:
            lines.append(payment_totals(entry, conf))
        rows.append([button(f'查看 #{entry["id"]} · {entry_description(entry)[:24]}', f'e:{entry["id"]}')])
    if not ids:
        lines.append('\n这里还没有账目。')
    paging = []
    if page:
        paging.append(button('← 上一页', f'list:{scope}:{page-1}'))
    if (page + 1) * PAGE_SIZE < count:
        paging.append(button('下一页 →', f'list:{scope}:{page+1}'))
    if paging:
        rows.append(paging)
    if scope == 'recent':
        voided = conn.execute("SELECT COUNT(*) FROM entries WHERE status='voided'").fetchone()[0]
        if voided:
            rows.append([button(f'🗑 已作废账目 · {voided}', 'list:voided:0')])
    rows.append(nav())
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def detail(conn, user, chat_id, entry_id, rates=None):
    conf, entry = settings(conn), entry_view(conn, entry_id)
    lines = [f'🧾 #{entry_id} · {entry_status(entry)} · {entry_type_label(entry)}', entry_description(entry), original(entry, conf)]
    if entry.get('fee_minor') or (entry.get('fee_mode') and entry.get('fee_mode') != 'none'):
        lines.append(f'原金额：{amount_text(entry["gross_amount"], entry["currency"], conf)} · 手续费：{fee_label(entry, conf)}')
    converted = reference(entry['amount'], entry['currency'], conf, rates)
    if converted:
        lines.append(converted)
    if entry['kind'] == 'expense' and entry['expense_type'] == 'advance':
        lines.append(payment_totals(entry, conf))
        remaining_reference = reference(entry['remaining_amount'], entry['currency'], conf, rates)
        if remaining_reference:
            lines.append('剩余待报 ' + remaining_reference)
    member_label = '转入成员' if (entry['kind'] == 'income' and entry.get('income_type') == 'capital') else '成员'
    lines += [f'{member_label}：{entry["payer_name"]}', f'日期：{entry["date"]}', f'备注：{entry["note"] or "未填写"}',
              f'凭证：{"已上传，Web 可查看" if entry["receipt"] else "未上传"}']
    if entry['review_note']:
        lines.append('处理备注：' + entry['review_note'])
    if entry['payments']:
        lines.append('\n最近付款（完整历史见 Web）：')
        for payment in entry['payments'][-3:]:
            lines.append(f'{payment["created_at"][:16]} · {amount_text(payment["amount"],payment["currency"],conf)} · {payment["name"]}\n'
                         f'抵扣 {amount_text(payment["entry_amount"],entry["currency"],conf)} · {payment["note"][:60]}')
    rows = []
    manageable = can_manage_entry(user, entry)
    editable_income = entry['kind'] == 'income' and entry['status'] == 'posted'
    editable_expense = (entry['kind'] == 'expense' and entry['status'] in (('draft', 'posted') if entry['expense_type'] == 'public' else ('pending', 'draft')) and not entry['reimbursed_minor'])
    if manageable and (editable_income or editable_expense):
        rows.append([button('✏️ 修改账目', f'edit:{entry_id}')])
    if entry['status'] == 'pending' and is_finance(user):
        rows.append([button('💳 继续报销' if entry['reimbursed_minor'] else '💳 登记已付款', f'pay:{entry_id}')])
    if entry['status'] == 'draft' and manageable:
        rows.append([button('记入账本', f'post:{entry_id}')])
    if manageable and entry['status'] not in ('reimbursed', 'voided') and not entry['reimbursed_minor']:
        rows.append([button('作废此账目', f'void:{entry_id}')])
    if entry['status'] == 'voided' and is_finance(user):
        rows.append([button('🗑 永久删除此账目', f'delete:{entry_id}')])
    web_detail = web_button(f'/#entry/{entry_id}', '在 Web 查看 / 传凭证' if manageable else '在 Web 查看')
    if web_detail:
        rows.append([web_detail])
    rows += [[button('📋 最近账目', 'list:recent:0')], nav()]
    say(conn, chat_id, '\n'.join(lines), rows, edit=True)


def period_summary(conn, chat_id, period):
    if period not in ('today', 'month'):
        fail('时间范围无效')
    conf, today = settings(conn), config.today()
    start = today if period == 'today' else today[:7] + '-01'
    rows = conn.execute("SELECT kind,expense_type,income_type,currency,decimals,SUM(amount_minor) amount,COUNT(*) count FROM entries WHERE date>=? AND date<=? AND status NOT IN ('draft','voided') GROUP BY kind,expense_type,income_type,currency,decimals", (start, today)).fetchall()
    lines = [f'📅 {"今日" if period == "today" else "本月"}收支 · {start} 至 {today}', '按发生日期统计，原币种分别列示。']
    for kind, expense_type, income_type, label in [
        ('income', '', 'operating', '营业收入'),
        ('income', '', 'capital', '公户转入（不计利润）'),
        ('expense', 'public', '', '公账支出'),
        ('expense', 'advance', '', '成员垫付'),
    ]:
        selected = [
            r for r in rows
            if r['kind'] == kind and r['expense_type'] == expense_type
            and (kind != 'income' or (r['income_type'] or 'operating') == income_type)
        ]
        lines.append(f'\n{label} · {sum(r["count"] for r in selected)} 笔\n' +
                     ('\n'.join(amount_text(Decimal(r['amount']) / (10 ** r['decimals']), r['currency'], conf) for r in selected) or '暂无'))
    say(conn, chat_id, '\n'.join(lines), [[button('今日', 'period:today'), button('本月', 'period:month')], nav()], edit=True)


def command_text(text, conf):
    if not text.startswith('/'):
        return '', text
    first, *rest = text.split(maxsplit=1)
    parts = first.split('@', 1)
    if len(parts) == 2 and parts[1].lower() != str(conf.get('bot_username', '')).lower():
        return 'foreign', ''
    return parts[0].lower(), rest[0] if rest else ''


def accepts_text(conn, msg, telegram_id, chat_id, draft):
    if msg.get('chat', {}).get('type') == 'private':
        return True
    reply_id = msg.get('reply_to_message', {}).get('message_id')
    if not reply_id or not draft:
        return False
    return bool(conn.execute('SELECT 1 FROM tg_prompts WHERE chat_id=? AND message_id=? AND telegram_id=? AND token=? AND step=? AND expires>?',
                             (chat_id, reply_id, telegram_id, draft['token'], draft['step'], int(time.time()))).fetchone())


def entry_snapshot(conn, entry_id):
    entry = dict(conn.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone())
    event_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE entry_id=?', (entry_id,)).fetchone()[0]
    return hashlib.sha256(json.dumps([entry, event_id], sort_keys=True).encode()).hexdigest()


def show_field(conn, chat_id, draft, field):
    data, token, conf = json.loads(draft['payload']), draft['token'], settings(conn)
    if field == 'currency':
        persist(conn, token, data, 'edit_currency')
        show_currencies(conn, chat_id, token, conf)
    elif field == 'payer':
        persist(conn, token, data, 'edit_payer')
        members = conn.execute('SELECT id,name FROM users WHERE active=1 ORDER BY id').fetchall()
        say(conn, chat_id, '选择垫付 / 经手成员：', [[button(m['name'], f'd:{token}:payer:{m["id"]}')] for m in members] + [[button('返回预览', f'd:{token}:back')]], edit=True)
    elif field == 'date':
        persist(conn, token, data, 'edit_date')
        say(conn, chat_id, '选择日期：', [[button('今天', f'd:{token}:day:0'), button('昨天', f'd:{token}:day:1')],
            [button('自定义日期', f'd:{token}:dateinput'), button('返回预览', f'd:{token}:back')]], edit=True)
    elif field == 'fee':
        persist(conn, token, data, 'edit_fee')
        say(conn, chat_id, '选择手续费方式（默认 0）：\n支持填写任意比例（%），或手动填写固定手续费金额。',
            [[button('按比例（%）', f'd:{token}:fee:rate'), button('手动金额', f'd:{token}:fee:fixed')],
             [button('无手续费（0）', f'd:{token}:fee:none'), button('返回预览', f'd:{token}:back')]], edit=True)
    elif field in ('amount', 'description', 'note'):
        hints = {'amount': f'请输入金额（{data["currency"]}）。', 'description': '用途 / 收入说明选填，发送 - 跳过 / 清空。', 'note': '请输入备注，发送 - 清空。'}
        ask(conn, chat_id, draft, 'edit_' + field, hints[field])
    else:
        fail('不支持修改该字段')


def draft_callback(conn, user, telegram_id, chat_id, callback_data, rates):
    parts = callback_data.split(':')
    if len(parts) < 3 or parts[0] != 'd':
        fail('此按钮已过期，请返回主菜单。')
    draft = conn.execute('SELECT * FROM tg_drafts WHERE token=? AND telegram_id=? AND chat_id=? AND user_id=? AND expires>?',
                         (parts[1], telegram_id, chat_id, user['id'], int(time.time()))).fetchone()
    if not draft:
        fail('这不是你当前的草稿，或它已确认 / 过期，请重新开始。')
    payload, step, token, conf = json.loads(draft['payload']), draft['step'], draft['token'], settings(conn)
    action = parts[2]
    if action == 'cancel':
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        say(conn, chat_id, '已关闭报销结果。' if payload.get('_receipt') else '已取消，此次填写没有入账。', [nav()], edit=True)
    elif action == 'edit' and len(parts) == 4 and step == 'confirm':
        if parts[3] == 'payer':
            admin(user)
        show_field(conn, chat_id, draft, parts[3])
    elif action == 'inctype' and step == 'confirm' and payload.get('kind') == 'income':
        admin(user)
        payload['income_type'] = 'operating' if payload.get('income_type') == 'capital' else 'capital'
        persist(conn, token, payload, 'confirm')
        preview(conn, user, chat_id, token, payload, rates)
    elif action == 'fee' and len(parts) == 4 and step == 'edit_fee':
        mode = parts[3]
        if mode == 'none':
            payload['fee_mode'] = 'none'
            payload.pop('fee_rate', None)
            payload.pop('fee_amount', None)
            persist(conn, token, payload, 'confirm')
            preview(conn, user, chat_id, token, payload, rates)
        elif mode == 'rate':
            ask(conn, chat_id, draft, 'edit_fee_rate',
                '请输入手续费比例（%），例如 0.6 或 2.9（默认 0，输入 0 或 - 清零）。')
        elif mode == 'fixed':
            ask(conn, chat_id, draft, 'edit_fee_amount',
                f'请输入手动手续费金额（{payload["currency"]}，默认 0，输入 0 或 - 清零）。')
        else:
            fail('手续费选项无效')
    elif action == 'resume':
        if payload.get('_flow') == 'funds':
            from . import bot_funds
            bot_funds.resume(conn, user, chat_id, draft, rates)
        elif payload.get('_flow') == 'ai_plan':
            ai_plan_show(conn, chat_id, token, payload)
        elif step == 'confirm':
            preview(conn, user, chat_id, token, payload, rates)
        elif step == 'action_confirm':
            if payload.get('_action') == 'pay' and not payload.get('_quote'):
                show_payment_currencies(conn, chat_id, token, payload)
            else:
                action_preview(conn, chat_id, token, payload)
        elif step == 'pay_currency':
            show_payment_currencies(conn, chat_id, token, payload)
        elif step in ('currency', 'edit_currency'):
            show_currencies(conn, chat_id, token, conf)
        elif step in ('edit_date', 'edit_payer', 'edit_fee'):
            show_field(conn, chat_id, draft, step[5:])
        else:
            hints = {'amount': '请输入金额。', 'edit_amount': '请输入金额。',
                     'edit_fee_rate': '请输入手续费比例（%），例如 0.6 或 2.9（默认 0，输入 0 或 - 清零）。',
                     'edit_fee_amount': f'请输入手动手续费金额（{payload.get("currency","")}，默认 0，输入 0 或 - 清零）。',
                     'description': '用途 / 收入说明选填，发送 - 跳过 / 清空。', 'edit_description': '用途 / 收入说明选填，发送 - 跳过 / 清空。',
                     'edit_note': '请输入备注，发送 - 清空。', 'pay_note': '请输入付款方式 / 付款参考号。',
                     'pay_amount': f'请输入本次实际已报销金额（{payload.get("_quote",{}).get("currency","")}），发送 全额 报销剩余全部。',
                     'void_note': '请输入作废原因。', 'ai_text': '🤖 请描述收支，可以一次说一段话。',
                     'export_range': '请输入导出日期范围（如 2026-11 或 2026-11-01 2026-11-22）。'}
            ask(conn, chat_id, draft, step, hints.get(step, '请继续填写。'))
    elif action == 'paycur' and len(parts) == 4 and step == 'pay_currency':
        payload['_quote'] = make_payment_quote(conn, user, payload['_entry_id'], parts[3], rates)
        persist(conn, token, payload, 'pay_amount')
        quote = payload['_quote']
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'pay_amount',
            f'请填写本次实际已报销金额（{quote["currency"]}）。\n剩余全额：{quote["maximum_amount"]} {quote["currency"]}。\n'
            + (f'1 {quote["entry_currency"]} = {quote["rate"]} {quote["currency"]} · {quote["rate_date"]}\n' if quote['currency'] != quote['entry_currency'] else '')
            + '可以只报一部分；发送 全额 报销剩余全部。换算确认有效 30 分钟，请先完成真实转账。')
    elif action == 'paychange' and payload.get('_action') == 'pay':
        show_payment_currencies(conn, chat_id, token, payload)
    elif action == 'back' and step.startswith('edit_'):
        persist(conn, token, payload, 'confirm')
        preview(conn, user, chat_id, token, payload, rates)
    elif action == 'cur' and len(parts) == 4 and step in ('currency', 'edit_currency'):
        cur_obj = currency(conf, parts[3])
        payload['currency'] = parts[3]
        if payload.get('fee_mode') == 'fixed' and payload.get('fee_amount'):
            try:
                minor_amount(payload['fee_amount'], cur_obj['decimals'], allow_zero=True)
            except HTTPException:
                payload['fee_mode'] = 'none'
                payload.pop('fee_amount', None)
        next_step = 'edit_amount' if step == 'edit_currency' else 'amount'
        persist(conn, token, payload, next_step)
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), next_step,
            f'币种 {parts[3]}，请{"重新" if step == "edit_currency" else ""}填写金额。')
    elif action == 'payer' and len(parts) == 4 and step == 'edit_payer' and parts[3].isdigit():
        admin(user)
        payload['payer_id'] = int(parts[3])
        normalized_entry(conn, user, payload)
        persist(conn, token, payload, 'confirm')
        preview(conn, user, chat_id, token, payload, rates)
    elif action == 'day' and len(parts) == 4 and step == 'edit_date' and parts[3] in ('0', '1'):
        payload['date'] = (date.fromisoformat(config.today()) - timedelta(days=int(parts[3]))).isoformat()
        persist(conn, token, payload, 'confirm')
        preview(conn, user, chat_id, token, payload, rates)
    elif action == 'dateinput' and step == 'edit_date':
        ask(conn, chat_id, draft, 'edit_date', '请输入日期 YYYY-MM-DD。')
    elif action == 'save' and step == 'confirm':
        if payload.get('_edit_id'):
            if entry_snapshot(conn, payload['_edit_id']) != payload['_snapshot']:
                fail('账目已更新，请重新打开详情后修改。')
            entry = edit_entry(conn, user, payload['_edit_id'], payload)
        else:
            source = PLATFORM.get()
            entry = add_entry(conn, user, payload, source,
                              f'{"tg" if source == "telegram" else source}:draft:{token}')
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        say(conn, chat_id, f'✅ 已{"保存修改" if payload.get("_edit_id") else "入账"} #{entry["id"]}\n{entry_description(entry)}\n{original(entry,conf)}\n' +
            ('已加入待报销，付款后可直接标记已报销。' if entry['status'] == 'pending' else STATUS[entry['status']]),
            [[button('查看详情', f'e:{entry["id"]}'), button('我的待报销', 'summary')], nav()], edit=True)
    elif action == 'commit' and step == 'action_confirm':
        entry_id = payload['_entry_id']
        if entry_snapshot(conn, entry_id) != payload['_snapshot']:
            fail('账目已被其他操作更新，请重新打开详情核对。')
        if payload['_action'] == 'pay' and not payload.get('_quote'):
            show_payment_currencies(conn, chat_id, token, payload)
            return
        source_prefix = 'feishu' if PLATFORM.get() == 'feishu' else 'tg'
        transition(conn, user, entry_id, payload['_action'], payload['note'],
                   **({'amount': payload['_pay_amount'], 'payment_key': f'pay:{source_prefix}:{token}', 'quote_id': payload['_quote']['id']} if payload['_action'] == 'pay' else {}))
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        detail(conn, user, chat_id, entry_id, rates)
    else:
        fail('此按钮不属于当前步骤，请继续填写或用 /cancel 重新开始。')


def show_payment_currencies(conn, chat_id, token, payload):
    entry, conf = entry_view(conn, payload['_entry_id']), settings(conn)
    payload.pop('_quote', None)
    payload.pop('_pay_amount', None)
    persist(conn, token, payload, 'pay_currency')
    rows = [[button('按记账货币 · ' + entry['currency'], f'd:{token}:paycur:{entry["currency"]}')]]
    if conf['reference_currency'] != entry['currency']:
        rows.append([button('按结算货币 · ' + conf['reference_currency'], f'd:{token}:paycur:{conf["reference_currency"]}')])
    rows.append([button('取消', f'd:{token}:cancel')])
    say(conn, chat_id, f'💳 #{entry["id"]} {entry_description(entry)}\n{payment_totals(entry,conf)}\n\n选择本次实际付款的币种，再填写已报销金额。', rows, edit=True)


def action_preview(conn, chat_id, token, payload):
    entry, conf = entry_view(conn, payload['_entry_id']), settings(conn)
    action = payload['_action']
    label = {'pay': '登记已付款', 'void': '作废账目', 'submit': '记入账本'}[action]
    description = {'pay': '请确认上述本次金额已经实际转账。系统只登记本次付款，不会发起转账。',
                   'void': '账目保留历史，但不再计入有效收支。', 'submit': '公账支出入账后直接扣减公账余额。' if entry['expense_type'] == 'public' else '草稿将进入待报销，金额不会从团队余额扣除。'}[action]
    payment_text = ''
    rows = [[button('确认' + label, f'd:{token}:commit')]]
    if action == 'pay':
        user = {'id': conn.execute('SELECT user_id FROM tg_drafts WHERE token=?', (token,)).fetchone()[0]}
        _, quote, units, allocation = payment_preview(conn, user, entry['id'], payload['_pay_amount'], payload['_quote']['id'])
        paid = entry['reimbursed_minor'] + allocation
        payment_text = f'\n本次实际付款：{amount_text(Decimal(units)/(10**quote["decimals"]),quote["currency"],conf)}\n'
        payment_text += f'抵扣记账金额：{amount_text(Decimal(allocation)/(10**entry["decimals"]),entry["currency"],conf)}\n'
        payment_text += f'登记后累计已报：{amount_text(Decimal(paid)/(10**entry["decimals"]),entry["currency"],conf)}\n剩余待报：{amount_text(Decimal(entry["amount_minor"]-paid)/(10**entry["decimals"]),entry["currency"],conf)}\n'
        if quote['currency'] != entry['currency']:
            payment_text += f'汇率：1 {entry["currency"]} = {quote["rate"]} {quote["currency"]} · {quote["rate_date"]}\n'
        rows.append([button('修改币种 / 金额', f'd:{token}:paychange')])
    say(conn, chat_id, f'⚠️ 确认{label}\n#{entry["id"]} {entry_description(entry)}\n原金额 {original(entry,conf)}\n' + payment_text +
        f'成员：{entry["payer_name"]}\n备注：{payload["note"]}\n\n' +
        description,
        rows + [[button('取消', f'd:{token}:cancel')]], edit=True)


def handle(conn, update, ai_result=None, ai_error=None, rates=None, ai_context=None):
    conf = settings(conn)
    cb = update.get('callback_query')
    msg = cb.get('message', {}) if cb else update.get('message', {})
    sender = cb.get('from', {}) if cb else msg.get('from', {})
    telegram_id, chat_id = str(sender.get('id', '')), str(msg.get('chat', {}).get('id', ''))
    private = msg.get('chat', {}).get('type') == 'private'
    if not telegram_id or not chat_id or sender.get('is_bot'):
        return
    if cb:
        queue(conn, 'answerCallbackQuery', {'callback_query_id': cb['id']})
    if not private and chat_id not in conf['feishu_allowed_chats' if PLATFORM.get() == 'feishu' else 'telegram_allowed_chats']:
        return
    text = str(msg.get('text', '')).strip()[:2500] if not cb else ''
    command, argument = command_text(text, conf)
    if command == 'foreign':
        return
    if command == '/start' and argument:
        if not private:
            say(conn, chat_id, '绑定码请只发送给机器人私聊。')
            return
        row = conn.execute('SELECT l.*,u.active FROM link_codes l JOIN users u ON u.id=l.user_id WHERE code_hash=? AND expires>?',
                           (hashlib.sha256(argument.encode()).hexdigest(), int(time.time()))).fetchone()
        if not row or not row['active']:
            say(conn, chat_id, '绑定码无效或已过期，请到 Web「机器人与 AI」重新生成。')
            return
        conn.execute('DELETE FROM tg_drafts WHERE user_id=? OR telegram_id=?', (row['user_id'], telegram_id))
        conn.execute('DELETE FROM tg_links WHERE user_id=? OR telegram_id=?', (row['user_id'], telegram_id))
        conn.execute('INSERT INTO tg_links VALUES (?,?)', (telegram_id, row['user_id']))
        conn.execute('DELETE FROM link_codes WHERE user_id=?', (row['user_id'],))
    linked = conn.execute('SELECT u.* FROM tg_links t JOIN users u ON u.id=t.user_id WHERE t.telegram_id=? AND u.active=1', (telegram_id,)).fetchone()
    if not linked:
        if private or cb or command:
            hint = ('请先登录 Web，在「机器人与 AI」生成飞书绑定码，私聊发送「绑定 绑定码」。'
                    if PLATFORM.get() == 'feishu' else
                    '请先登录 Web，在「机器人与 AI」生成绑定码，私聊发送 /start 绑定码。')
            say(conn, chat_id, hint, [[web_button()]] if web_button() else None)
        return
    user, data = dict(linked), str(cb.get('data', '')) if cb else ''
    conn.execute('DELETE FROM tg_drafts WHERE expires<?', (int(time.time()),))
    conn.execute('DELETE FROM tg_prompts WHERE expires<?', (int(time.time()),))
    if command in ('/start', '/menu') or data == 'home':
        menu(conn, user, chat_id)
    elif command == '/help' or data == 'help':
        help_menu(conn, chat_id)
    elif command == '/cancel' or data == 'cancel':
        conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
        say(conn, chat_id, '已取消当前填写。', [nav()], edit=True)
    elif command in ('/recent', '/summary', '/team') or data == 'summary' or data.startswith('list:'):
        scope, page = {'/recent': 'recent', '/summary': 'mine', '/team': 'team'}.get(command, 'mine'), 0
        if data.startswith('list:'):
            parts = data.split(':')
            if len(parts) != 3 or not parts[2].isdigit():
                fail('分页参数无效')
            scope, page = parts[1], int(parts[2])
        list_entries(conn, user, chat_id, scope, page, rates)
    elif command in ('/today', '/month') or data.startswith('period:'):
        period_summary(conn, chat_id, command[1:] if command else data.split(':', 1)[1])
    elif command == '/funds' or data == 'funds':
        from . import bot_funds
        bot_funds.start(conn, user, telegram_id, chat_id, rates)
    elif command == '/export' or data == 'export':
        admin(user)
        if command == '/export' and argument:
            filters_spec = excel_export.parse_export_shortcut(argument)
            queue_excel_export(conn, user, chat_id, filters_spec)
        else:
            show_export_menu(conn, user, chat_id)
    elif data.startswith('x:'):
        export_callback(conn, user, telegram_id, chat_id, data)
    elif command in ('/book', '/income', '/capital', '/expense') or data in ('new', 'newincome', 'newcapital', 'newpublic'):
        if command in ('/income', '/capital', '/expense') or data in ('newincome', 'newcapital', 'newpublic'):
            admin(user)
        token, payload = new_draft(conn, user, telegram_id, chat_id)
        if command == '/income' or data == 'newincome':
            payload.update(kind='income', income_type='operating', status='posted')
            persist(conn, token, payload, 'currency')
        elif command == '/capital' or data == 'newcapital':
            payload.update(kind='income', income_type='capital', status='posted')
            persist(conn, token, payload, 'currency')
        elif command == '/expense' or data == 'newpublic':
            payload.update(expense_type='public', status='posted')
            persist(conn, token, payload, 'currency')
        show_currencies(conn, chat_id, token, conf)
    elif data == 'ai' or command == '/ai' and not argument:
        new_draft(conn, user, telegram_id, chat_id, step='ai_text')
        ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'ai_text',
            '🤖 请描述收支，可以一次说一段话，例如「昨天打车35元，老王垫的聚餐240元，收到尾款2000美元」。')
    elif data.startswith(('e:', 'edit:', 'pay:', 'void:', 'post:', 'delete:', 'deleteyes:')):
        entry_action(conn, user, telegram_id, chat_id, data, rates)
    elif data.startswith('a:'):
        ai_plan_callback(conn, user, telegram_id, chat_id, data, rates)
    elif data.startswith('f:'):
        from . import bot_funds
        bot_funds.callback(conn, user, telegram_id, chat_id, data, rates)
    elif cb:
        draft_callback(conn, user, telegram_id, chat_id, data, rates)
    elif command and command != '/ai':
        help_menu(conn, chat_id)
    else:
        text_input(conn, user, msg, telegram_id, chat_id, text, command, ai_result, ai_error, rates, ai_context)


def entry_action(conn, user, telegram_id, chat_id, data, rates):
    action, value = data.split(':', 1)
    if not value.isdigit():
        fail('账目编号无效')
    entry = entry_view(conn, int(value))
    if action == 'e':
        detail(conn, user, chat_id, entry['id'], rates)
        return
    if action in ('pay', 'delete', 'deleteyes'):
        admin(user)
    else:
        require_entry_access(user, entry)
    if action in ('delete', 'deleteyes'):
        if entry['status'] != 'voided':
            fail('仅已作废账目可以永久删除')
        if action == 'delete':
            say(conn, chat_id, f'⚠️ 永久删除 #{entry["id"]}「{entry_description(entry)}」\n'
                '账目、操作记录和凭证都会删除，无法恢复。', [
                    [button('确认永久删除', f'deleteyes:{entry["id"]}'), button('取消', f'e:{entry["id"]}')]
                ], edit=True)
            return
        receipt = delete_entry(conn, user, entry['id'])
        if receipt:
            (config.DATA / 'receipts' / os.path.basename(receipt)).unlink(missing_ok=True)
        say(conn, chat_id, f'🗑 已永久删除 #{entry["id"]}「{entry_description(entry)}」\n账目、操作记录和凭证均已删除。',
            [[button('📋 最近账目', 'list:recent:0')], nav()], edit=True)
        return
    if action == 'edit':
        if entry['kind'] == 'income':
            if entry['status'] != 'posted':
                fail('仅已入账的收入可以修改')
        elif entry['status'] not in (('draft', 'posted') if entry['expense_type'] == 'public' else ('draft', 'pending')) or entry['reimbursed_minor']:
            fail('只可修改没有报销记录的支出')
        body = {k: entry[k] for k in ('kind', 'expense_type', 'income_type', 'currency', 'description', 'date', 'note', 'payer_id', 'status')}
        body['amount'] = entry['gross_amount']
        body['fee_mode'] = entry.get('fee_mode') or 'none'
        if body['fee_mode'] == 'rate':
            body['fee_rate'] = entry.get('fee_rate') or ''
        elif body['fee_mode'] == 'fixed':
            body['fee_amount'] = entry.get('fee_amount') or ''
        body.update(_edit_id=entry['id'], _snapshot=entry_snapshot(conn, entry['id']))
        token, body = new_draft(conn, user, telegram_id, chat_id, body)
        preview(conn, user, chat_id, token, body, rates)
        return
    if action == 'pay' and entry['status'] != 'pending':
        fail('只有待报销账目可以登记付款')
    if action == 'void' and (entry['status'] in ('reimbursed', 'voided') or entry['reimbursed_minor']):
        fail('已报销或已作废账目不能作废')
    if action == 'post':
        if entry['kind'] != 'expense' or entry['status'] != 'draft':
            fail('仅草稿可以记入账本')
        body = {'_action': 'submit', '_entry_id': entry['id'], '_snapshot': entry_snapshot(conn, entry['id']), 'note': ''}
        token, body = new_draft(conn, user, telegram_id, chat_id, body, step='action_confirm')
        action_preview(conn, chat_id, token, body)
        return
    body = {'_action': action, '_entry_id': entry['id'], '_snapshot': entry_snapshot(conn, entry['id'])}
    if action == 'pay':
        token, body = new_draft(conn, user, telegram_id, chat_id, body, step='pay_currency')
        show_payment_currencies(conn, chat_id, token, body)
        return
    new_draft(conn, user, telegram_id, chat_id, body, step=action + '_note')
    ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), action + '_note',
        f'处理 #{entry["id"]}「{entry_description(entry)}」\n' + ('请填写付款方式 / 付款参考号，请先完成真实转账。' if action == 'pay' else '请填写作废原因。'))


def text_input(conn, user, msg, telegram_id, chat_id, text, command, ai_result, ai_error, rates, ai_context=None):
    draft, conf = draft_for(conn, telegram_id, chat_id), settings(conn)
    prior_history = None
    if draft:
        try:
            prior_payload = json.loads(draft['payload'])
            if prior_payload.get('_flow') == 'ai_plan':
                prior_history = prior_payload.get('history')
        except (ValueError, TypeError):
            pass
    if draft and command != '/ai' and accepts_text(conn, msg, telegram_id, chat_id, draft):
        step, payload = draft['step'], json.loads(draft['payload'])
        if payload.get('_flow') == 'funds':
            from . import bot_funds
            bot_funds.text_input(conn, user, chat_id, draft, text, rates)
            return
        if payload.get('_flow') == 'ai_plan':
            if ai_error or (isinstance(ai_result, dict) and isinstance(ai_result.get('actions'), list)):
                conn.execute('DELETE FROM tg_drafts WHERE token=?', (draft['token'],))
                if ai_error:
                    say(conn, chat_id, '⚠️ ' + ai_error, [[button('改用按钮记账', 'new')], nav()])
                else:
                    ai_plan_handle_result(conn, user, telegram_id, chat_id, conf, ai_result, ai_context, rates, text, prior_history)
                return
            say(conn, chat_id, '🤖 当前操作清单还在等你确认。\n点上方 ✅ 执行；也可以直接补充或修改需求，我会重新整理清单；发送 /cancel 取消。',
                [[button('取消本次清单', 'cancel')], nav()])
            return
        if step == 'ai_text':
            pass
        elif step == 'export_range':
            admin(user)
            filters_spec = excel_export.parse_export_shortcut(text)
            conn.execute('DELETE FROM tg_drafts WHERE token=?', (draft['token'],))
            queue_excel_export(conn, user, chat_id, filters_spec)
            return
        elif step == 'pay_amount':
            amount = payload['_quote']['maximum_amount'] if text == '全额' else text
            payment_preview(conn, user, payload['_entry_id'], amount, payload['_quote']['id'])
            payload['_pay_amount'] = amount
            persist(conn, draft['token'], payload, 'pay_note')
            ask(conn, chat_id, draft_for(conn, telegram_id, chat_id), 'pay_note', '请输入付款方式 / 付款参考号。下一步会展示金额与剩余款，确认后才登记。')
            return
        elif step in ('pay_note', 'void_note'):
            payload['note'] = text_value(text, '付款备注 / 作废原因', 500)
            if step == 'pay_note' and not payload.get('_quote'):
                show_payment_currencies(conn, chat_id, draft['token'], payload)
                return
            persist(conn, draft['token'], payload, 'action_confirm')
            action_preview(conn, chat_id, draft['token'], payload)
            return
        elif step in ('amount', 'description', 'edit_amount', 'edit_description', 'edit_note', 'edit_date', 'edit_fee_rate', 'edit_fee_amount'):
            if step in ('amount', 'edit_amount'):
                minor_amount(text, currency(conf, payload['currency'])['decimals'])
                payload['amount'] = text
            elif step == 'edit_fee_rate':
                cleaned_rate = text.strip().rstrip('%％').strip()
                if cleaned_rate in ('', '-'):
                    payload['fee_mode'] = 'none'
                    payload.pop('fee_rate', None)
                    payload.pop('fee_amount', None)
                else:
                    payload['fee_mode'] = 'rate'
                    payload['fee_rate'] = cleaned_rate
                    payload.pop('fee_amount', None)
            elif step == 'edit_fee_amount':
                cleaned_fee = text.strip()
                if cleaned_fee in ('', '-'):
                    payload['fee_mode'] = 'none'
                    payload.pop('fee_rate', None)
                    payload.pop('fee_amount', None)
                else:
                    payload['fee_mode'] = 'fixed'
                    payload['fee_amount'] = cleaned_fee
                    payload.pop('fee_rate', None)
            elif step in ('description', 'edit_description'):
                payload['description'] = '' if text == '-' else text_value(text, '用途', 120, False)
            elif step == 'edit_note':
                payload['note'] = '' if text == '-' else text_value(text, '备注', 1000, False)
            else:
                try:
                    payload['date'] = date.fromisoformat(text).isoformat()
                except ValueError:
                    fail('日期请使用 YYYY-MM-DD 格式。')
            payload.setdefault('description', '')
            norm = normalized_entry(conn, user, payload)
            payload['fee_mode'] = norm['fee_mode']
            if norm['fee_mode'] == 'rate':
                payload['fee_rate'] = norm['fee_rate']
                payload.pop('fee_amount', None)
            elif norm['fee_mode'] == 'fixed':
                payload.pop('fee_rate', None)
            else:
                payload.pop('fee_rate', None)
                payload.pop('fee_amount', None)
            persist(conn, draft['token'], payload, 'confirm')
            preview(conn, user, chat_id, draft['token'], payload, rates)
            return
        else:
            say(conn, chat_id, '请点击当前草稿的按钮继续，或发送 /cancel。', [nav()])
            return
    if ai_error:
        say(conn, chat_id, '⚠️ ' + ai_error, [[button('改用按钮记账', 'new')], nav()])
    elif ai_result:
        if isinstance(ai_result, dict) and isinstance(ai_result.get('actions'), list):
            ai_plan_handle_result(conn, user, telegram_id, chat_id, conf, ai_result, ai_context, rates, text, prior_history)
        else:
            normalized_entry(conn, user, ai_result)
            token, payload = new_draft(conn, user, telegram_id, chat_id, ai_result)
            preview(conn, user, chat_id, token, payload, rates)
    elif msg.get('chat', {}).get('type') == 'private' and text:
        say(conn, chat_id, '请使用菜单继续，或发送 /help 查看用法。', [nav()])


AI_ICONS = {'create_entry': '📝', 'edit_entry': '✏️', 'void_entry': '🗑️', 'submit_entry': '📥',
            'pay_entry': '💳', 'batch_pay': '📦', 'delete_entry': '🔥'}
AI_LABELS = {'create_entry': '新增', 'edit_entry': '修改', 'void_entry': '作废', 'submit_entry': '入账',
             'pay_entry': '报销', 'batch_pay': '批量报销', 'delete_entry': '永久删除'}


def ai_query_line(card, conf):
    kind = card.get('kind') if isinstance(card, dict) else None
    if kind == 'entries':
        totals = ' / '.join(amount_text(value, code, conf) for code, value in card.get('totals', {}).items())
        lines = [f'🔎 查到 {card.get("total", 0)} 笔' + (f' · {totals}' if totals else '')]
        for entry in card.get('entries', [])[:5]:
            lines.append(f'#{entry["id"]} {entry_description(entry)[:40]} · '
                         f'{amount_text(entry["amount"], entry["currency"], conf)} · {entry_status(entry)}')
        if card.get('total', 0) > 5:
            lines.append(f'…另有 {card["total"] - 5} 笔，可在 Web 收支明细查看。')
        return '\n'.join(lines)
    if kind == 'summary':
        parts = []
        for code, values in card.get('totals', {}).items():
            try:
                expense, income = Decimal(str(values.get('expense', '0'))), Decimal(str(values.get('income', '0')))
            except Exception:
                continue
            if expense > 0:
                parts.append('支出 ' + amount_text(expense, code, conf))
            if income > 0:
                parts.append('收入 ' + amount_text(income, code, conf))
        return f'📊 共 {card.get("count", 0)} 笔' + (f' · {" / ".join(parts)}' if parts else '')
    if kind in ('balance', 'due', 'funds'):
        if not card.get('complete'):
            lines = ['📊 ' + (card.get('warning') or '资金数据暂不可用')]
            accounts = card.get('currency_accounts') or []
            if accounts:
                lines.append('各币种公户结余：' + ' / '.join(amount_text(a['balance'], a['code'], conf) for a in accounts[:6]))
            return '\n'.join(lines)
        lines = [f'💰 公户结余（可用）{amount_text(card["cash"], card["base"], conf)}'
                 f' · 待报销 {amount_text(card["due"], card["base"], conf)}'
                 f' · 可安排 {amount_text(card["payable"], card["base"], conf)}'
                 f' · 公账支出 {amount_text(card["public_paid"], card["base"], conf)}']
        if card.get('operating_income') is not None and card.get('capital_income') is not None:
            lines.append(f'📥 营业收入 {amount_text(card["operating_income"], card["base"], conf)}'
                         f' · 公户转入 {amount_text(card["capital_income"], card["base"], conf)}'
                         f' · 扣全额待报后 {amount_text(card.get("net_after_due", card["remaining"]), card["base"], conf)}')
        if card.get('operating_net_after_due') is not None:
            lines.append(f'📈 经营净利润（不含公户转入）{amount_text(card["operating_net_after_due"], card["base"], conf)}')
        accounts = card.get('currency_accounts') or []
        if len(accounts) > 1:
            lines.append('💱 各币种结余：' + ' / '.join(amount_text(a['balance'], a['code'], conf) for a in accounts[:6]))
        for member in (card.get('members') or [])[:5]:
            if Decimal(str(member.get('due', '0'))) > 0:
                lines.append(f'🙋 {member["name"]} · 待报销 {amount_text(member["due"], card["base"], conf)}'
                             f' · 建议 {amount_text(member["suggested"], card["base"], conf)}')
        if card.get('warning'):
            lines.append('💡 ' + card['warning'])
        return '\n'.join(lines)
    if kind == 'simulation':
        if not card.get('complete'):
            return '⚗️ ' + (card.get('warning') or '模拟暂不可用')
        text = (f'⚗️ 模拟进账后可用 {amount_text(card["cash"], card["base"], conf)}'
                f' · 可安排 {amount_text(card["payable"], card["base"], conf)}'
                f' · 待报销 {amount_text(card["due"], card["base"], conf)}')
        if card.get('warning'):
            text += '\n💡 ' + card['warning']
        return text
    if kind == 'entry':
        entry = card['entry']
        return (f'🧾 #{entry["id"]} {entry_description(entry)} · '
                f'{amount_text(entry["amount"], entry["currency"], conf)} · {entry_status(entry)}')
    if kind == 'error':
        return '⚠️ ' + str(card.get('error') or '查询失败')
    return ''


def ai_plan_persist(conn, token, payload):
    # Do not extend the draft TTL beyond the stored plan's own 30-minute expiry.
    conn.execute('UPDATE tg_drafts SET payload=? WHERE token=?', (json.dumps(payload, ensure_ascii=False), token))


def ai_plan_deliver(conn, user, telegram_id, chat_id, plan, request_text='', history=None):
    conf = settings(conn)
    valid = [card for card in plan['actions'] if not card['errors']]
    if plan.get('plan_id') and valid:
        conn.execute('DELETE FROM tg_drafts WHERE telegram_id=? AND chat_id=?', (telegram_id, chat_id))
        token = secrets.token_hex(5)
        summary = (plan.get('reply') or '') + '\n' + '\n'.join(
            f'{position + 1}. {card["summary"]}' for position, card in enumerate(plan['actions']))
        turns = list(history or []) + [
            {'role': 'user', 'text': str(request_text)[:900]},
            {'role': 'assistant', 'text': summary[:900]},
        ]
        payload = {'_flow': 'ai_plan', 'plan': plan, 'selected': [card['index'] for card in valid],
                   'page': 0, 'history': turns[-8:]}
        expires = min(int(plan.get('expires') or (time.time() + 1800)), int(time.time()) + 3600)
        conn.execute('INSERT INTO tg_drafts VALUES (?,?,?,?,?,?,?)',
                     (token, telegram_id, chat_id, user['id'], 'ai_plan',
                      json.dumps(payload, ensure_ascii=False), expires))
        ai_plan_show(conn, chat_id, token, payload)
        return
    if plan.get('results'):
        lines = ['🤖 查询结果']
    elif plan.get('questions') and not plan.get('actions'):
        lines = ['🤖 还需要确认']
    elif plan.get('reply') and not plan.get('actions'):
        lines = ['🤖 回答']
    else:
        lines = ['🤖 这次没有可以执行的操作。']
    if plan.get('reply'):
        lines.append(plan['reply'])
    for question in plan.get('questions', []):
        lines.append('❓ ' + question)
    for card in plan.get('actions', []):
        lines.append(f'⚠️ {card.get("summary") or card.get("label")}')
        lines += ['   ⚠️ ' + error for error in card.get('errors', [])]
    for result in plan.get('results', []):
        line = ai_query_line(result.get('card', {}), conf)
        if line:
            lines.append(line)
    say(conn, chat_id, '\n'.join(lines)[:3900],
        [[button('🔄 重新描述', 'ai'), button('🏠 主菜单', 'home')]], edit=True)


def ai_plan_handle_result(conn, user, telegram_id, chat_id, conf, result, context, rates, request_text='', history=None):
    plan = ai_plan.resolve(conn, user, conf, result, context or {'entries': []}, rates)
    plan = ai_plan.store(conn, user, plan, PLATFORM.get())
    ai_plan_deliver(conn, user, telegram_id, chat_id, plan, request_text, history)


def ai_plan_show(conn, chat_id, token, payload, page=None):
    conf = settings(conn)
    plan = payload['plan']
    selected = set(payload.get('selected') or [])
    actions = plan.get('actions', [])
    total = max((len(actions) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(int(payload.get('page', 0) if page is None else page), 0), total - 1)
    payload['page'] = page
    ai_plan_persist(conn, token, payload)
    chosen = [card['index'] for card in actions if not card['errors'] and card['index'] in selected]
    lines = [f'🤖 我理解了 {len(actions)} 项操作 · 已选 {len(chosen)} 项']
    if plan.get('reply'):
        lines.append(plan['reply'])
    for question in plan.get('questions', []):
        lines.append('❓ ' + question)
    for result in plan.get('results', []):
        line = ai_query_line(result.get('card', {}), conf)
        if line:
            lines.append(line)
    if actions:
        lines.append(f'\n📋 操作清单 · 第 {page + 1}/{total} 页')
    start = page * PAGE_SIZE
    for position, card in enumerate(actions[start:start + PAGE_SIZE], start=start):
        if card['errors']:
            mark = '⚠️'
        else:
            mark = '☑️' if card['index'] in selected else '⬜'
        icon = AI_ICONS.get(card['type'], '•')
        lines.append(f'{position + 1}. {mark} {icon} {card["summary"][:170]}')
        if card['type'] == 'batch_pay':
            items = card.get('resolved', {}).get('items') or []
            for item in items[:5]:
                lines.append(f'   · #{item["id"]} {entry_description(item)[:28]} · '
                             f'{amount_text(item["amount"], item["currency"], conf)} → 抵扣 '
                             f'{amount_text(item["credited"], item["entry_currency"], conf)}')
            if len(items) > 5:
                lines.append(f'   · 另有 {len(items) - 5} 笔，确认后一次登记。')
        if card['errors']:
            lines.append('   ⚠️ ' + '；'.join(card['errors'])[:180])
        elif card['type'] in ('pay_entry', 'batch_pay', 'delete_entry') and card.get('warnings'):
            lines.append('   💡 ' + card['warnings'][0][:160])
    if not actions:
        lines.append('（本次只有查询结果，没有需要确认的写操作。）')
    lines.append('\n⏳ 清单 30 分钟内有效；只有你点确认后才会执行。')
    rows = []
    for card in actions[start:start + PAGE_SIZE]:
        if card['errors']:
            continue
        mark = '☑️' if card['index'] in selected else '⬜'
        rows.append([button(f'{mark} {AI_LABELS.get(card["type"], "操作")} · {card["summary"][:28]}',
                            f'a:{token}:t:{card["index"]}')])
    if any(not card['errors'] for card in actions):
        rows.append([button(f'✅ 确认执行 {len(chosen)} 项', f'a:{token}:go'),
                     button('❌ 取消', f'a:{token}:x')])
    paging = []
    if page:
        paging.append(button('← 上一页', f'a:{token}:p:{page - 1}'))
    if page + 1 < total:
        paging.append(button('下一页 →', f'a:{token}:p:{page + 1}'))
    if paging:
        rows.append(paging)
    rows.append(nav())
    say(conn, chat_id, '\n'.join(lines)[:3900], rows, edit=True)


def ai_plan_receipt(conn, chat_id, executed, conf):
    lines = [f'✅ 已完成 {len(executed.get("results", []))} 项操作']
    for result in executed.get('results', []):
        kind = result.get('type')
        if kind == 'delete_entry':
            lines.append(f'🔥 已永久删除 #{result["entry_id"]}')
            continue
        if kind == 'batch_pay':
            totals = ' / '.join(amount_text(value, code, conf) for code, value in (result.get('totals') or {}).items())
            lines.append(f'📦 已登记 {result.get("count", 0)} 笔报销' + (f' · {totals}' if totals else ''))
            continue
        try:
            entry = entry_view(conn, result['entry_id'])
        except HTTPException:
            entry = None
        if kind in ('create_entry', 'edit_entry'):
            lines.append(f'{AI_ICONS[kind]} #{result["entry_id"]} {entry_description(result)} · '
                         f'{amount_text(result.get("amount"), result.get("currency"), conf)}')
        elif kind == 'pay_entry' and entry:
            lines.append(f'💳 #{entry["id"]} {entry_description(entry)} · 累计已报 '
                         f'{amount_text(entry["reimbursed_amount"], entry["currency"], conf)} · 剩余 '
                         f'{amount_text(entry["remaining_amount"], entry["currency"], conf)}')
        elif kind == 'void_entry' and entry:
            lines.append(f'🗑️ #{entry["id"]} {entry_description(entry)} · 已作废')
        elif kind == 'submit_entry' and entry:
            lines.append(f'📥 #{entry["id"]} {entry_description(entry)} · 已记入账本')
    for result in executed.get('queries', []):
        line = ai_query_line(result.get('card', {}), conf)
        if line:
            lines.append(line)
    lines.append('\n所有改动已同步到 Web 账本。')
    say(conn, chat_id, '\n'.join(lines)[:3900],
        [[button('📋 最近账目', 'list:recent:0')], nav()], edit=True)


def ai_plan_callback(conn, user, telegram_id, chat_id, data, rates):
    parts = data.split(':')
    if len(parts) < 3:
        fail('按钮无效，请重新描述需求。')
    token = parts[1]
    draft = conn.execute('SELECT * FROM tg_drafts WHERE token=? AND telegram_id=? AND chat_id=? AND user_id=? AND expires>?',
                         (token, telegram_id, chat_id, user['id'], int(time.time()))).fetchone()
    if not draft:
        fail('操作清单已过期，请重新描述需求。')
    payload = json.loads(draft['payload'])
    if payload.get('_flow') != 'ai_plan':
        fail('请重新描述需求。')
    plan = payload['plan']
    action = parts[2]
    if action == 'x':
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        say(conn, chat_id, '❌ 已取消，本次没有执行任何操作。', [[button('🏠 主菜单', 'home')]], edit=True)
        return
    if action == 't' and len(parts) == 4 and parts[3].isdigit():
        index = int(parts[3])
        card = next((item for item in plan['actions'] if item['index'] == index), None)
        if not card or card['errors']:
            fail('这项操作当前不可选择。')
        selected = set(payload.get('selected') or [])
        selected.symmetric_difference_update({index})
        payload['selected'] = sorted(selected)
        ai_plan_persist(conn, token, payload)
        ai_plan_show(conn, chat_id, token, payload)
        return
    if action == 'p' and len(parts) == 4 and parts[3].isdigit():
        ai_plan_show(conn, chat_id, token, payload, int(parts[3]))
        return
    if action == 'go':
        selected = [item['index'] for item in plan['actions']
                    if not item['errors'] and item['index'] in set(payload.get('selected') or [])]
        if not selected:
            fail('请先选择至少一项操作。')
        executed = ai_plan.execute(conn, user, plan['plan_id'], selected)
        conn.execute('DELETE FROM tg_drafts WHERE token=?', (token,))
        for name in executed.pop('receipts', []):
            (config.DATA / 'receipts' / os.path.basename(name)).unlink(missing_ok=True)
        ai_plan_receipt(conn, chat_id, executed, settings(conn))
        return
    fail('此按钮已过期，请重新描述需求。')
