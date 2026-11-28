"""Validation, dry-run previews and atomic execution for AI bookkeeping plans."""
import json
import re
import secrets
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fastapi import HTTPException
from . import config, fx
from .db import settings
from .domain import (add_entry, admin, currency, delete_entry, edit_entry, entry_view, fail, has_reimbursements,
                     make_payment_quote, minor_amount, normalized_entry, payment_preview,
                     require_entry_access, require_entry_edit_access, text_value, transition)
from .funds_plan import build_plan as build_funds_plan, snapshot as funds_snapshot

PLAN_TTL = 1800
MAX_RESOLVED = 50


class PlanQuestion(Exception):
    """The action needs one more detail from the user; do not create a card."""


def _card(kind, label):
    return {'index': None, 'type': kind, 'writable': True, 'selected': True,
            'label': label, 'summary': '', 'resolved': {}, 'errors': [], 'warnings': []}


def _error(card, exc):
    card['errors'].append(str(exc.detail) if isinstance(exc, HTTPException) else str(exc))


def _text(value, limit, default=''):
    try:
        return text_value(value if value is not None else default, '内容', limit, False)
    except HTTPException as exc:
        raise PlanQuestion(exc.detail)


def _date_value(value, default=None):
    """Accept ISO plus common Chinese/relative forms; return ISO or default."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    text = str(value).strip()
    today = date.fromisoformat(config.today())
    words = {'今天': 0, '今日': 0, '昨天': -1, '昨日': -1, '前天': -2, '明天': 1, '明日': 1, '后天': 2}
    if text in words:
        return (today + timedelta(days=words[text])).isoformat()
    weekdays = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
    match = re.fullmatch(r'(上|本|这)(?:周|星期)([一二三四五六日天])', text)
    if match:
        monday = today - timedelta(days=today.weekday())
        target = monday + timedelta(days=weekdays[match.group(2)])
        if match.group(1) == '上':
            target -= timedelta(days=7)
        return target.isoformat()
    match = re.fullmatch(r'(?:周|星期)([一二三四五六日天])', text)
    if match:
        monday = today - timedelta(days=today.weekday())
        return (monday + timedelta(days=weekdays[match.group(1)])).isoformat()
    text = text.replace('年', '-').replace('月', '-').replace('日', '').replace('号', '')
    text = text.replace('/', '-').replace('.', '-').strip('-')
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return default
    match = re.fullmatch(r'(\d{1,2})-(\d{1,2})', text)
    if match:
        try:
            candidate = date(today.year, int(match.group(1)), int(match.group(2)))
            if candidate > today + timedelta(days=31):
                candidate = date(today.year - 1, candidate.month, candidate.day)
            return candidate.isoformat()
        except ValueError:
            return default
    return default


def _strict_date(value, label):
    if value in (None, ''):
        return None
    parsed = _date_value(value)
    if not parsed:
        raise PlanQuestion(f'{label}格式不正确，请使用 YYYY-MM-DD。')
    return parsed


def _amount_value(value):
    """Return a plain decimal string, 'FULL' for full-amount requests, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = format(Decimal(str(value)), 'f')
    else:
        text = str(value).strip()
        text = text.translate(str.maketrans('０１２３４５６７８９．，', '0123456789.,'))
        lowered = text.lower()
        if any(word in lowered for word in ('full', 'all remaining', '全部', '全额', '结清', '剩余全部')):
            return 'FULL'
        unit = Decimal(10000) if '万' in text else Decimal(1000) if ('千' in text or re.search(r'\d\s*[kK]\b', text)) else Decimal(1)
        text = text.replace(',', '')
        match = re.search(r'-?\d+(?:\.\d+)?', text)
        if not match:
            return None
        try:
            number = Decimal(match.group(0)) * unit
        except InvalidOperation:
            return None
        text = format(number, 'f')
    return text or None


def _rate_value(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return format(Decimal(str(value)), 'f')
    text = str(value).strip().translate(str.maketrans('０１２３４５６７８９．％', '0123456789.%'))
    if not text:
        return ''
    text = text.rstrip('%').strip()
    return text


def _fx_rate_value(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return format(Decimal(str(value)), 'f')
    text = str(value).strip().translate(str.maketrans('０１２３４５６７８９．', '0123456789.'))
    return text


def _fx_converted_amount(amount_minor, decimals, fx_rate, fx_currency, conf):
    if not fx_rate or not fx_currency:
        return ''
    target_dec = next((c['decimals'] for c in conf['currencies'] if c['code'] == fx_currency), 2)
    val = (Decimal(amount_minor) / (10 ** decimals) * Decimal(str(fx_rate))).quantize(
        Decimal(1).scaleb(-target_dec), rounding=ROUND_HALF_UP
    )
    return format(val, f'.{target_dec}f')


def _fx_summary_text(fx_rate, fx_currency, fx_amount, code, conf):
    if not fx_rate or not fx_currency:
        return ''
    conv = f' ≈ {_money(fx_amount, fx_currency, conf)}' if fx_amount else ''
    return f'（手动汇率 1 {code} = {fx_rate} {fx_currency}{conv}）'


def _merchant_summary_text(merchant_currency, merchant_amount, conf):
    if not merchant_currency:
        return ''
    if merchant_amount:
        return f'（商家标价 {_money(merchant_amount, merchant_currency, conf)}）'
    return f'（商家货币 {merchant_currency}）'


def _fee_summary_text(kind, fee_mode, fee_rate, fee_amount, effective_amount, code, conf):
    if fee_mode == 'rate' and fee_rate:
        label = '实收' if kind == 'income' else '实付'
        return f'（手续费 {fee_rate}% = {_money(fee_amount, code, conf)}，{label} {_money(effective_amount, code, conf)}）'
    if fee_mode == 'fixed' and Decimal(str(fee_amount or 0)) > 0:
        label = '实收' if kind == 'income' else '实付'
        return f'（手动手续费 {_money(fee_amount, code, conf)}，{label} {_money(effective_amount, code, conf)}）'
    return ''


def _money(amount, code, conf):
    cur = next((c for c in conf['currencies'] if c['code'] == code), None)
    if not cur:
        return f'{amount} {code}'
    try:
        value = Decimal(str(amount)).quantize(Decimal(1).scaleb(-cur['decimals']))
    except (InvalidOperation, ValueError):
        return f'{amount} {code}'
    return f'{cur["symbol"]}{value:,.{cur["decimals"]}f} {code}'


def _entry_desc(entry):
    return entry.get('description') or '未填写说明'


def _members(conn):
    return [dict(r) for r in conn.execute('SELECT id,name,username FROM users WHERE active=1 ORDER BY id')]


def _member(conn, value, user):
    if value in (None, '', '我', '本人', '自己'):
        return user['id'], user['name']
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        row = conn.execute('SELECT id,name FROM users WHERE id=? AND active=1', (int(value),)).fetchone()
        if row:
            return row['id'], row['name']
        raise PlanQuestion(f'没有找到成员 #{value}，请确认。')
    text = str(value).strip().lstrip('@')
    rows = _members(conn)
    exact = [r for r in rows if text in (r['name'], r['username'])]
    if len(exact) == 1:
        return exact[0]['id'], exact[0]['name']
    lowered = text.casefold()
    exact = [r for r in rows if lowered in (r['name'].casefold(), r['username'].casefold())]
    if len(exact) == 1:
        return exact[0]['id'], exact[0]['name']
    partial = [r for r in rows if lowered in r['name'].casefold() or lowered in r['username'].casefold()]
    if len(partial) == 1:
        return partial[0]['id'], partial[0]['name']
    if len(partial) > 1:
        names = '、'.join(r['name'] for r in partial[:5])
        raise PlanQuestion(f'成员「{text}」可能指 {names}，请写全名。')
    raise PlanQuestion(f'没有找到成员「{text}」，请确认姓名。')


def _target(conn, value, context_ids):
    try:
        entry_id = int(value)
    except (TypeError, ValueError):
        raise PlanQuestion('请指明要处理的账目编号。')
    try:
        entry = entry_view(conn, entry_id)
    except HTTPException:
        raise PlanQuestion(f'没有找到账目 #{entry_id}，请核对编号。')
    warning = None if entry_id in context_ids else f'#{entry_id} 不在最近摘要中，请核对后再确认。'
    return entry, warning


def _ids(action):
    values = action.get('ids')
    if isinstance(values, list):
        result = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        if result:
            return list(dict.fromkeys(result))
    if action.get('id') is not None:
        try:
            return [int(action['id'])]
        except (TypeError, ValueError):
            return []
    return []


def _append(actions, card):
    if len(actions) >= MAX_RESOLVED:
        fail(f'一次最多处理 {MAX_RESOLVED} 项操作，请分成几段描述。')
    actions.append(card)


# --- individual action resolvers -------------------------------------------

def _resolve_create(conn, user, conf, action):
    card = _card('create_entry', '新增账目')
    kind = action.get('kind') if action.get('kind') in ('expense', 'income') else 'expense'
    if action.get('kind') and action.get('kind') not in ('expense', 'income'):
        card['errors'].append('账目类型只能是支出或收入')
    raw_cur = action.get('currency')
    raw_pay_cur = action.get('payment_currency')
    if raw_cur and raw_pay_cur and str(raw_cur).strip().upper() != str(raw_pay_cur).strip().upper():
        card['errors'].append('付款货币与币种参数不一致')
    code = str(raw_cur or raw_pay_cur or conf['default_currency']).strip().upper()
    try:
        cur = currency(conf, code)
        code = cur['code']
    except HTTPException as exc:
        _error(card, exc)
        cur = currency(conf, conf['default_currency'])
        code = cur['code']
    amount = _amount_value(action.get('amount'))
    if amount == 'FULL':
        card['errors'].append('新增账目需要明确金额')
        amount = None
    if amount is None:
        card['errors'].append('缺少金额')
    else:
        try:
            minor_amount(amount, cur['decimals'])
        except HTTPException as exc:
            _error(card, exc)
    description = str(action.get('description') or '').strip()
    if len(description) > 120:
        card['errors'].append('用途最多 120 个字符')
        description = description[:120]
    if action.get('date') in (None, ''):
        entry_date = config.today()
    else:
        entry_date = _date_value(action['date'], None)
        if not entry_date:
            card['errors'].append('日期格式不正确，请使用 YYYY-MM-DD。')
            entry_date = config.today()
    payer_id, payer_name = _member(conn, action.get('payer'), user)
    expense_type = action.get('expense_type', 'advance') if kind == 'expense' else ''
    if kind == 'expense' and expense_type not in ('public', 'advance'):
        card['errors'].append('支出请选择公账支出或成员垫付')
    raw_income_type = action.get('income_type')
    income_type = ('operating' if raw_income_type in (None, '') else raw_income_type) if kind == 'income' else ''
    if kind == 'income' and income_type not in ('operating', 'capital'):
        card['errors'].append('收入请选择营业收入或公户转入')
    requested_status = action.get('status')
    if kind == 'income':
        status = 'posted'
    else:
        allowed = (None, '', 'draft', 'posted', 'pending') if expense_type == 'public' else (None, '', 'draft', 'pending')
        if requested_status not in allowed:
            card['errors'].append('支出状态与支出方式不一致')
        status = 'draft' if requested_status == 'draft' else 'posted' if expense_type == 'public' else 'pending'
    try:
        note = _text(action.get('note'), 1000)
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = ''
    raw_fee_mode = action.get('fee_mode')
    fee_rate = _rate_value(action.get('fee_rate')) if action.get('fee_rate') not in (None, '') else ''
    raw_fee_amount = action.get('fee_amount')
    fee_amount_in = ''
    if raw_fee_amount not in (None, ''):
        parsed_fee = _amount_value(raw_fee_amount)
        if parsed_fee in (None, 'FULL'):
            card['errors'].append('手续费金额格式不正确')
        else:
            fee_amount_in = parsed_fee
    raw_fx_mode = action.get('fx_mode')
    fx_rate_in = _fx_rate_value(action.get('fx_rate')) if action.get('fx_rate') not in (None, '') else ''
    raw_merchant_mode = action.get('merchant_mode')
    raw_merchant_cur = action.get('merchant_currency')
    merchant_cur_in = str(raw_merchant_cur).strip().upper() if raw_merchant_cur not in (None, '') else ''
    raw_merchant_amt = action.get('merchant_amount')
    merchant_amt_in = ''
    if raw_merchant_amt not in (None, ''):
        parsed_m_amt = _amount_value(raw_merchant_amt)
        if parsed_m_amt in (None, 'FULL'):
            card['errors'].append('商家金额格式不正确')
        else:
            merchant_amt_in = parsed_m_amt
    body = {'kind': kind, 'expense_type': expense_type, 'income_type': income_type, 'amount': amount or '', 'currency': code, 'description': description,
            'date': entry_date, 'payer_id': payer_id, 'status': status, 'note': note}
    if raw_fee_mode not in (None, ''):
        body['fee_mode'] = raw_fee_mode
    if fee_rate != '':
        body['fee_rate'] = fee_rate
    if fee_amount_in != '':
        body['fee_amount'] = fee_amount_in
    if raw_fx_mode not in (None, ''):
        body['fx_mode'] = raw_fx_mode
    if fx_rate_in != '':
        body['fx_rate'] = fx_rate_in
    if raw_merchant_mode not in (None, ''):
        body['merchant_mode'] = raw_merchant_mode
    if merchant_cur_in != '':
        body['merchant_currency'] = merchant_cur_in
    if merchant_amt_in != '':
        body['merchant_amount'] = merchant_amt_in
    norm = None
    if not card['errors']:
        try:
            norm = normalized_entry(conn, user, body)
        except HTTPException as exc:
            _error(card, exc)
    eff_fee_mode = norm['fee_mode'] if norm else (raw_fee_mode or 'none')
    eff_fee_rate = norm['fee_rate'] if norm else fee_rate
    eff_fee_amount = format(Decimal(norm['fee_minor']) / (10 ** cur['decimals']), f'.{cur["decimals"]}f') if norm else fee_amount_in
    eff_amount = format(Decimal(norm['amount_minor']) / (10 ** cur['decimals']), f'.{cur["decimals"]}f') if norm else (amount or '')
    eff_fx_rate = norm['fx_rate'] if norm else fx_rate_in
    eff_fx_currency = norm['fx_currency'] if norm else (conf['reference_currency'] if fx_rate_in else '')
    eff_fx_mode = 'manual' if (eff_fx_rate and eff_fx_currency) else 'auto'
    eff_fx_amount = _fx_converted_amount(norm['amount_minor'], cur['decimals'], eff_fx_rate, eff_fx_currency, conf) if norm else ''
    eff_m_cur = norm['merchant_currency'] if norm else merchant_cur_in
    eff_m_dec = norm['merchant_decimals'] if norm else 0
    eff_m_minor = norm['merchant_amount_minor'] if norm else 0
    eff_m_mode = 'different' if eff_m_cur else 'same'
    eff_m_amt = format(Decimal(eff_m_minor) / (10 ** eff_m_dec), f'.{eff_m_dec}f') if (norm and eff_m_cur and eff_m_minor > 0) else (merchant_amt_in if (not norm and eff_m_cur) else '')
    resolved_amount = eff_amount if eff_fee_mode != 'none' else (amount or '')
    label = ('公户转入' if income_type == 'capital' else '营业收入') if kind == 'income' else ('新增草稿' if status == 'draft' else '公账支出' if expense_type == 'public' else '成员垫付')
    card['label'] = label
    card['resolved'] = {'kind': kind, 'expense_type': expense_type, 'income_type': income_type,
                        'amount': resolved_amount, 'gross_amount': amount or '',
                        'fee_mode': eff_fee_mode, 'fee_rate': eff_fee_rate, 'fee_amount': eff_fee_amount if eff_fee_mode != 'none' else '',
                        'fx_mode': eff_fx_mode, 'fx_rate': eff_fx_rate, 'fx_currency': eff_fx_currency, 'fx_amount': eff_fx_amount,
                        'merchant_mode': eff_m_mode, 'merchant_currency': eff_m_cur, 'merchant_amount': eff_m_amt,
                        'currency': code, 'payment_currency': code, 'description': description,
                        'date': entry_date, 'payer_id': payer_id, 'payer_name': payer_name,
                        'status': status, 'note': note}
    state = '草稿' if status == 'draft' else ('公户转入 · 计入公户/不计利润' if income_type == 'capital' else '营业收入 · 计入公户与利润') if kind == 'income' else '公账支出 · 已扣款' if expense_type == 'public' else '成员垫付 · 待报销'
    fee_text = _fee_summary_text(kind, eff_fee_mode, eff_fee_rate, eff_fee_amount, eff_amount, code, conf) if norm else ''
    fx_text = _fx_summary_text(eff_fx_rate, eff_fx_currency, eff_fx_amount, code, conf) if norm else ''
    merchant_text = _merchant_summary_text(eff_m_cur, eff_m_amt, conf) if norm else ''
    card['summary'] = f'{description or "未填写说明"} · {_money(amount or 0, code, conf)}{merchant_text}{fee_text}{fx_text} · {entry_date} · {payer_name} · {state}'
    return card


def _resolve_edit(conn, user, conf, action, context_ids):
    card = _card('edit_entry', '修改账目')
    entry, warning = _target(conn, action.get('id'), context_ids)
    if warning:
        card['warnings'].append(warning)
    try:
        require_entry_edit_access(user, entry)
    except HTTPException as exc:
        card['errors'].append(str(exc.detail))
        card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · {exc.detail}'
        card['resolved'] = {'id': entry['id'], 'version': entry['version']}
        return card
    reimbursed_flow = has_reimbursements(entry)
    if reimbursed_flow:
        card['label'] = '修改历史账目'
        card['warnings'].append(
            f'此账目已有报销记录（累计已报 {_money(entry["reimbursed_amount"], entry["currency"], conf)}），历史付款记录将保留。'
        )
    if action.get('kind') and action.get('kind') != entry['kind']:
        card['errors'].append('不支持更改账目类型')
    body = {'kind': entry['kind'], 'amount': entry['gross_amount'], 'currency': entry['currency'],
            'expense_type': entry['expense_type'], 'income_type': entry.get('income_type', '') if entry['kind'] == 'income' else '',
            'fee_mode': entry.get('fee_mode') or 'none',
            'fee_rate': entry.get('fee_rate') or '',
            'fee_amount': entry['fee_amount'] if entry.get('fee_mode') == 'fixed' else '',
            'fx_mode': entry.get('fx_mode') or ('manual' if entry.get('fx_rate') else 'auto'),
            'fx_rate': entry.get('fx_rate') or '',
            'fx_currency': entry.get('fx_currency') or '',
            'merchant_mode': entry.get('merchant_mode') or ('different' if entry.get('merchant_currency') else 'same'),
            'merchant_currency': entry.get('merchant_currency') or '',
            'merchant_amount': entry.get('merchant_amount') or '',
            'description': entry['description'], 'date': entry['date'], 'payer_id': entry['payer_id'],
            'note': entry['note'], 'status': 'pending' if reimbursed_flow else entry['status']}
    if reimbursed_flow and action.get('status') == 'draft':
        card['errors'].append('已有报销记录的账目不能转为草稿')
    if entry['kind'] == 'expense' and 'expense_type' in action:
        if reimbursed_flow and action['expense_type'] not in (None, '', entry['expense_type']):
            card['errors'].append('已有报销记录的支出不能更改支出方式')
        elif not reimbursed_flow:
            body['expense_type'] = action['expense_type']
            if body['status'] != 'draft':
                body['status'] = 'posted' if action['expense_type'] == 'public' else 'pending'
    if entry['kind'] == 'income' and 'income_type' in action and action['income_type'] not in (None, ''):
        body['income_type'] = action['income_type']
    if 'amount' in action:
        amount = _amount_value(action.get('amount'))
        if amount == 'FULL' or amount is None:
            card['errors'].append('修改金额需要明确数字')
        else:
            body['amount'] = amount
    currency_changed = False
    raw_cur_edit = action.get('currency') or action.get('payment_currency')
    if action.get('currency') and action.get('payment_currency') and str(action['currency']).strip().upper() != str(action['payment_currency']).strip().upper():
        card['errors'].append('付款货币与币种参数不一致')
    if raw_cur_edit:
        try:
            next_code = currency(conf, str(raw_cur_edit).strip().upper())['code']
            currency_changed = next_code != body['currency']
            if reimbursed_flow and currency_changed:
                card['errors'].append('已有报销记录的账目不能修改记账币种')
            else:
                body['currency'] = next_code
        except HTTPException as exc:
            _error(card, exc)
    has_m_mode = 'merchant_mode' in action and action.get('merchant_mode') not in (None, '')
    has_m_cur = 'merchant_currency' in action and action.get('merchant_currency') is not None
    has_m_amt = 'merchant_amount' in action and action.get('merchant_amount') is not None
    if has_m_mode or has_m_cur or has_m_amt:
        if action.get('merchant_mode') == 'same':
            body['merchant_mode'] = 'same'
            body['merchant_currency'] = ''
            body['merchant_amount'] = ''
        else:
            next_m_cur = str(action.get('merchant_currency') or '').strip().upper() if has_m_cur else body['merchant_currency']
            if has_m_cur and not next_m_cur and not has_m_mode and not has_m_amt:
                body['merchant_mode'] = 'same'
                body['merchant_currency'] = ''
                body['merchant_amount'] = ''
            else:
                m_cur_changed = has_m_cur and next_m_cur != body['merchant_currency']
                body['merchant_mode'] = action.get('merchant_mode') if has_m_mode else 'different'
                body['merchant_currency'] = next_m_cur
                if has_m_amt:
                    raw_ma = action.get('merchant_amount')
                    if raw_ma in ('', None):
                        body['merchant_amount'] = ''
                    else:
                        parsed_ma = _amount_value(raw_ma)
                        if parsed_ma in (None, 'FULL'):
                            card['errors'].append('商家金额格式不正确')
                        else:
                            body['merchant_amount'] = parsed_ma
                elif m_cur_changed:
                    body['merchant_amount'] = ''
    elif currency_changed and body['currency'] == body['merchant_currency']:
        body['merchant_mode'] = 'same'
        body['merchant_currency'] = ''
        body['merchant_amount'] = ''
    has_fx_mode = 'fx_mode' in action and action.get('fx_mode') not in (None, '')
    has_fx_rate = 'fx_rate' in action and action.get('fx_rate') is not None
    if has_fx_mode or has_fx_rate:
        if action.get('fx_mode') == 'auto':
            body['fx_mode'] = 'auto'
            body['fx_rate'] = ''
            body['fx_currency'] = ''
        else:
            parsed_fx = _fx_rate_value(action.get('fx_rate')) if has_fx_rate else body['fx_rate']
            if parsed_fx in (None, '') and not has_fx_mode:
                body['fx_mode'] = 'auto'
                body['fx_rate'] = ''
                body['fx_currency'] = ''
            else:
                body['fx_mode'] = action.get('fx_mode') if has_fx_mode else 'manual'
                body['fx_rate'] = parsed_fx or ''
                body['fx_currency'] = conf['reference_currency'] if (has_fx_rate or currency_changed or not body['fx_currency']) else body['fx_currency']
    elif currency_changed:
        body['fx_mode'] = 'auto'
        body['fx_rate'] = ''
        body['fx_currency'] = ''
    has_mode = 'fee_mode' in action and action.get('fee_mode') not in (None, '')
    has_rate = 'fee_rate' in action and action.get('fee_rate') not in (None, '')
    has_famt = 'fee_amount' in action and action.get('fee_amount') not in (None, '')
    if has_mode or has_rate or has_famt:
        next_mode = action.get('fee_mode') if has_mode else ('rate' if has_rate and not has_famt else ('fixed' if has_famt and not has_rate else body['fee_mode']))
        body['fee_mode'] = next_mode
        if next_mode == 'none':
            body['fee_rate'] = ''
            body['fee_amount'] = ''
        elif next_mode == 'rate':
            if has_rate:
                body['fee_rate'] = _rate_value(action.get('fee_rate'))
            body['fee_amount'] = ''
        elif next_mode == 'fixed':
            if has_famt:
                parsed_fee = _amount_value(action.get('fee_amount'))
                if parsed_fee in (None, 'FULL'):
                    card['errors'].append('手续费金额格式不正确')
                else:
                    body['fee_amount'] = parsed_fee
            body['fee_rate'] = ''
    if 'description' in action:
        description = str(action.get('description') or '').strip()
        if len(description) > 120:
            card['errors'].append('用途最多 120 个字符')
        else:
            body['description'] = description
    if 'date' in action and action.get('date'):
        body['date'] = _date_value(action['date'], '')
        if not body['date']:
            card['errors'].append('日期格式不正确')
    payer_name = entry['payer_name']
    if 'payer' in action and action.get('payer') is not None:
        payer_id, payer_name = _member(conn, action['payer'], user)
        body['payer_id'] = payer_id
    if 'note' in action:
        body['note'] = str(action.get('note') or '')[:1000]
    norm = None
    if not card['errors']:
        try:
            norm = normalized_entry(conn, user, body, existing_payer_id=entry['payer_id'])
        except HTTPException as exc:
            _error(card, exc)
    if norm and reimbursed_flow:
        if norm['amount_minor'] < entry['reimbursed_minor']:
            card['errors'].append(
                f'修改后的实付总额不能低于累计已报销金额（{_money(entry["reimbursed_amount"], entry["currency"], conf)}）'
            )
        else:
            next_status = 'reimbursed' if norm['amount_minor'] == entry['reimbursed_minor'] else 'pending'
            body['status'] = next_status
            if next_status != entry['status']:
                if next_status == 'pending':
                    card['warnings'].append('修改后实付总额高于累计已报销金额，状态将由「已报销」转为「部分报销」，差额可继续报销。')
                else:
                    card['warnings'].append('修改后实付总额等于累计已报销金额，状态将由「部分报销」自动结清为「已报销」。')
    cur_obj = next((c for c in conf['currencies'] if c['code'] == body['currency']), {'decimals': 2})
    eff_fee_mode = norm['fee_mode'] if norm else body['fee_mode']
    eff_fee_rate = norm['fee_rate'] if norm else body['fee_rate']
    eff_fee_amount = format(Decimal(norm['fee_minor']) / (10 ** cur_obj['decimals']), f'.{cur_obj["decimals"]}f') if norm else body['fee_amount']
    eff_amount = format(Decimal(norm['amount_minor']) / (10 ** cur_obj['decimals']), f'.{cur_obj["decimals"]}f') if norm else body['amount']
    eff_fx_rate = norm['fx_rate'] if norm else body['fx_rate']
    eff_fx_currency = norm['fx_currency'] if norm else body['fx_currency']
    eff_fx_mode = 'manual' if (eff_fx_rate and eff_fx_currency) else 'auto'
    eff_fx_amount = _fx_converted_amount(norm['amount_minor'], cur_obj['decimals'], eff_fx_rate, eff_fx_currency, conf) if norm else ''
    eff_m_cur = norm['merchant_currency'] if norm else body['merchant_currency']
    eff_m_dec = norm['merchant_decimals'] if norm else 0
    eff_m_minor = norm['merchant_amount_minor'] if norm else 0
    eff_m_mode = 'different' if eff_m_cur else 'same'
    eff_m_amt = format(Decimal(eff_m_minor) / (10 ** eff_m_dec), f'.{eff_m_dec}f') if (norm and eff_m_cur and eff_m_minor > 0) else (body['merchant_amount'] if (not norm and eff_m_cur) else '')
    resolved_body = {
        **body,
        'gross_amount': body['amount'],
        'amount': eff_amount if eff_fee_mode != 'none' else body['amount'],
        'fee_mode': eff_fee_mode,
        'fee_rate': eff_fee_rate,
        'fee_amount': eff_fee_amount if eff_fee_mode != 'none' else '',
        'fx_mode': eff_fx_mode,
        'fx_rate': eff_fx_rate,
        'fx_currency': eff_fx_currency,
        'fx_amount': eff_fx_amount,
        'merchant_mode': eff_m_mode,
        'merchant_currency': eff_m_cur,
        'merchant_amount': eff_m_amt,
        'payment_currency': body['currency'],
    }
    card['resolved'] = {'id': entry['id'], 'version': entry['version'], **resolved_body, 'payer_name': payer_name}
    before = f'{_money(entry["amount"], entry["currency"], conf)} · {entry["description"] or "未填写说明"}'
    fee_text = _fee_summary_text(entry['kind'], eff_fee_mode, eff_fee_rate, eff_fee_amount, eff_amount, body['currency'], conf) if norm else ''
    fx_text = _fx_summary_text(eff_fx_rate, eff_fx_currency, eff_fx_amount, body['currency'], conf) if norm else ''
    merchant_text = _merchant_summary_text(eff_m_cur, eff_m_amt, conf) if norm else ''
    after = f'{_money(body["amount"], body["currency"], conf)}{merchant_text}{fee_text}{fx_text} · {body["description"] or "未填写说明"}'
    card['summary'] = f'#{entry["id"]} 修改：{before} → {after} · {body["date"]}'
    return card


def _resolve_transition(conn, user, conf, action, context_ids):
    kind = action['type']
    labels = {'void_entry': '作废账目', 'submit_entry': '草稿入账'}
    card = _card(kind, labels[kind])
    entry_id = action.get('id')
    entry, warning = _target(conn, entry_id, context_ids)
    require_entry_access(user, entry)
    if warning:
        card['warnings'].append(warning)
    if kind == 'void_entry':
        if entry['reimbursed_minor'] or entry['status'] in ('reimbursed', 'voided'):
            card['errors'].append('已有报销记录或已作废的账目不能作废')
        note = str(action.get('note') or '').strip()[:500] or '通过 AI 助手作废'
        action_name = 'void'
    elif kind == 'submit_entry':
        if entry['kind'] != 'expense' or entry['status'] != 'draft':
            card['errors'].append('只有草稿支出可以记入账本')
        note = str(action.get('note') or '').strip()[:500]
        action_name = 'submit'
    card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'action': action_name, 'note': note}
    card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · {_money(entry["amount"], entry["currency"], conf)} · {labels[kind]}'
    if note:
        card['summary'] += f' · 备注：{note[:60]}'
    return card


def _resolve_delete(conn, user, conf, action, context_ids):
    admin(user)
    card = _card('delete_entry', '永久删除账目')
    entry, warning = _target(conn, action.get('id'), context_ids)
    if warning:
        card['warnings'].append(warning)
    if entry['status'] != 'voided':
        card['errors'].append('仅已作废账目可以永久删除；请先作废。')
    card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'description': entry['description'],
                        'amount': entry['amount'], 'currency': entry['currency']}
    card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · {_money(entry["amount"], entry["currency"], conf)} · 永久删除'
    card['warnings'].append('永久删除会清除账目、操作记录、付款记录和凭证，无法恢复。')
    return card


def _resolve_pay(conn, user, conf, action, context_ids, rates):
    admin(user)
    card = _card('pay_entry', '登记报销')
    entry, warning = _target(conn, action.get('id'), context_ids)
    if warning:
        card['warnings'].append(warning)
    if entry['kind'] != 'expense' or entry['status'] != 'pending' or not entry['remaining_minor']:
        card['errors'].append('只有尚有剩余金额的待报销支出可以登记付款')
        card['resolved'] = {'id': entry['id'], 'version': entry['version']}
        card['summary'] = f'#{entry["id"]} {_entry_desc(entry)} · 当前状态不允许登记付款'
        return card
    requested = action.get('settlement_currency') or 'original'
    code = entry['currency'] if requested in ('original', '', None) else str(requested).upper()
    if code not in (entry['currency'], conf['reference_currency']):
        card['errors'].append('付款币种请选择记账货币或团队结算货币')
    raw_amount = action.get('amount')
    amount = _amount_value(raw_amount)
    if raw_amount not in (None, '') and amount is None:
        card['errors'].append('付款金额格式不正确')
    try:
        note = _text(action.get('note'), 500) or '通过 AI 助手登记付款'
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = '通过 AI 助手登记付款'
    raw_fx_mode = action.get('fx_mode')
    fx_rate_in = _fx_rate_value(action.get('fx_rate')) if action.get('fx_rate') not in (None, '') else ''
    quote = None
    if not card['errors']:
        try:
            quote = make_payment_quote(
                conn, user, entry['id'], code, rates,
                manual_fx_rate=fx_rate_in or None,
                fx_mode=raw_fx_mode,
            )
            if amount in (None, 'FULL'):
                amount = quote['maximum_amount']
            entry_after, _, units, allocation = payment_preview(conn, user, entry['id'], amount, quote['id'])
        except HTTPException as exc:
            _error(card, exc)
    if quote and not card['errors']:
        pay_amount = format(Decimal(units) / (10 ** quote['decimals']), f'.{quote["decimals"]}f')
        allocation_text = format(Decimal(allocation) / (10 ** entry['decimals']), f'.{entry["decimals"]}f')
        remaining = entry['remaining_minor'] - allocation
        remaining_text = format(Decimal(remaining) / (10 ** entry['decimals']), f'.{entry["decimals"]}f')
        card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'amount': pay_amount,
                            'currency': quote['currency'], 'quote_id': quote['id'], 'note': note,
                            'entry_amount': allocation_text, 'entry_currency': entry['currency'],
                            'remaining_amount': remaining_text,
                            'fx_rate': quote['rate'] if quote['currency'] != entry['currency'] else '',
                            'fx_date': quote['rate_date']}
        rate_part = ''
        if quote['currency'] != entry['currency']:
            rate_tag = quote['rate_date'] or '手动汇率'
            rate_part = f'（1 {entry["currency"]} = {quote["rate"]} {quote["currency"]} · {rate_tag}）'
        card['summary'] = (f'#{entry["id"]} {_entry_desc(entry)} · 本次付款 {_money(pay_amount, quote["currency"], conf)}{rate_part}'
                           f' · 抵扣 {_money(allocation_text, entry["currency"], conf)} · 剩余 {_money(remaining_text, entry["currency"], conf)}')
        card['warnings'].append('请确认这笔转账已经实际完成；系统只登记，不会发起付款。')
    elif quote:
        card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'amount': amount or '',
                            'currency': quote['currency'], 'quote_id': quote['id'], 'note': note}
    else:
        card['resolved'] = {'id': entry['id'], 'version': entry['version'], 'amount': amount or '',
                            'currency': code, 'note': note}
    return card


def _resolve_batch(conn, user, conf, action, rates, context_ids):
    admin(user)
    card = _card('batch_pay', '批量登记报销')
    requested = action.get('settlement_currency') or 'original'
    mode = 'original' if requested in ('original', '', None) else 'settlement'
    if mode == 'settlement' and str(requested).upper() != conf['reference_currency']:
        card['errors'].append(f'批量报销的结算币种只能是 {conf["reference_currency"]}')
    payer_id, payer_name = None, ''
    if action.get('member'):
        payer_id, payer_name = _member(conn, action['member'], user)
    try:
        date_from = _strict_date(action.get('date_from'), '开始日期')
        date_to = _strict_date(action.get('date_to'), '结束日期')
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        date_from = date_to = None
    entry_ids, id_warnings = None, []
    if action.get('ids') is not None:
        entry_ids = []
        if not isinstance(action['ids'], list):
            card['errors'].append('账目编号列表格式不正确')
    if isinstance(action.get('ids'), list):
        for value in action['ids']:
            if type(value) is not int or value <= 0:
                card['errors'].append('账目编号无效')
            else:
                entry_ids.append(value)
    for entry_id in entry_ids or []:
        try:
            entry = entry_view(conn, entry_id)
        except HTTPException:
            raise PlanQuestion(f'没有找到账目 #{entry_id}，请核对编号。')
        if entry['kind'] != 'expense' or entry['status'] != 'pending':
            card['errors'].append(f'#{entry_id} 不是待报销支出，不能登记付款。')
        if entry_id not in context_ids:
            id_warnings.append(f'#{entry_id} 不在最近摘要中，请核对后再确认。')
    try:
        note = _text(action.get('note'), 500) or '通过 AI 助手批量登记付款'
    except PlanQuestion as exc:
        card['errors'].append(str(exc))
        note = '通过 AI 助手批量登记付款'
    plan = None
    reimbursement = {'mode': 'budget', 'budget': action['budget']} if action.get('budget') is not None else None
    if not card['errors']:
        try:
            plan = build_funds_plan(conn, user, rates, mode, payer_id=payer_id,
                                    entry_ids=entry_ids, date_from=date_from, date_to=date_to, note=note, reimbursement=reimbursement)
        except HTTPException as exc:
            _error(card, exc)
    if plan:
        totals = ' / '.join(_money(value, code, conf) for code, value in list(plan['totals'].items())[:6])
        card['resolved'] = {'mode': mode, 'payer_id': payer_id, 'payer_name': payer_name, 'entry_ids': entry_ids,
                            'date_from': date_from, 'date_to': date_to, 'note': note,
                            'budget': (plan.get('reimbursement') or {}).get('budget'), 'base': plan['base'],
                            'snapshot': plan['snapshot'], 'expires': plan['expires'],
                            'items': plan['items'], 'totals': plan['totals'],
                            'cost': plan['cost'], 'left': plan['left']}
        card['summary'] = f'批量登记 {len(plan["items"])} 笔报销 · 实际付款 {totals} · 折合 {_money(plan["cost"], plan["base"], conf)}'
        card['warnings'].append('请确认这些转账已经实际完成；系统只登记，不会发起付款。')
    else:
        card['resolved'] = {'mode': mode, 'payer_id': payer_id, 'payer_name': payer_name, 'entry_ids': entry_ids,
                            'date_from': date_from, 'date_to': date_to, 'note': note}
    card['warnings'].extend(id_warnings)
    return card


# --- read-only queries ------------------------------------------------------

def _query_clauses(conn, user, conf, action, default_status=True):
    clauses, params = [], []
    scope = action.get('scope')
    if scope == 'mine':
        clauses.append('e.payer_id=?')
        params.append(user['id'])
    kind = action.get('filter_kind')
    if kind in ('expense', 'income'):
        clauses.append('e.kind=?')
        params.append(kind)
    expense_type = action.get('filter_expense_type')
    if expense_type in ('public', 'advance'):
        clauses.append("e.kind='expense' AND e.expense_type=?")
        params.append(expense_type)
    income_type = action.get('filter_income_type')
    if income_type in ('operating', 'capital'):
        clauses.append("e.kind='income' AND e.income_type=?")
        params.append(income_type)
    status = action.get('filter_status')
    if status == 'partial':
        clauses.append("e.status='pending' AND e.reimbursed_minor>0")
    elif status in ('draft', 'pending', 'reimbursed', 'posted', 'voided'):
        clauses.append('e.status=?')
        params.append(status)
    elif default_status:
        clauses.append("e.status NOT IN ('draft','voided')")
    code = action.get('filter_currency')
    if code:
        try:
            clauses.append('e.currency=?')
            params.append(currency(conf, str(code).upper())['code'])
        except HTTPException:
            raise PlanQuestion(f'没有启用币种 {code}。')
    m_code = action.get('filter_merchant_currency')
    if m_code:
        try:
            clauses.append('e.merchant_currency=?')
            params.append(currency(conf, str(m_code).upper())['code'])
        except HTTPException:
            raise PlanQuestion(f'没有启用币种 {m_code}。')
    if action.get('filter_payer'):
        payer_id, _ = _member(conn, action['filter_payer'], user)
        clauses.append('e.payer_id=?')
        params.append(payer_id)
    if action.get('text'):
        needle = '%' + str(action['text']).strip()[:60] + '%'
        clauses.append('(e.description LIKE ? OR e.note LIKE ? OR u.name LIKE ? OR e.merchant_currency LIKE ?)')
        params += [needle, needle, needle, needle]
    date_from = _strict_date(action.get('date_from'), '开始日期')
    date_to = _strict_date(action.get('date_to'), '结束日期')
    if date_from:
        clauses.append('e.date>=?')
        params.append(date_from)
    if date_to:
        clauses.append('e.date<=?')
        params.append(date_to)
    return clauses, params


def _filtered_rows(conn, user, conf, action, default_status=True):
    clauses, params = _query_clauses(conn, user, conf, action, default_status)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = conn.execute('SELECT e.id FROM entries e JOIN users u ON u.id=e.payer_id' + where +
                        ' ORDER BY e.date DESC,e.id DESC', params).fetchall()
    return rows, where, params


def _list_query(conn, user, conf, action):
    rows, where, params = _filtered_rows(conn, user, conf, action)
    limit = action.get('limit')
    try:
        limit = min(max(int(limit), 1), 50) if limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    ids = [row['id'] for row in rows[:limit]]
    entries = [entry_view(conn, entry_id) for entry_id in ids]
    totals = {}
    for row in conn.execute(
            'SELECT e.currency,e.decimals,SUM(e.amount_minor) amount FROM entries e JOIN users u ON u.id=e.payer_id' +
            where + ' GROUP BY e.currency,e.decimals', params):
        totals[row['currency']] = format(Decimal(row['amount']) / (10 ** row['decimals']), f'.{row["decimals"]}f')
    return {'kind': 'entries', 'entries': entries, 'total': len(rows), 'shown': len(entries), 'totals': totals}


def _summary_query(conn, user, conf, action):
    clauses, params = _query_clauses(conn, user, conf, action)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = conn.execute(
        'SELECT e.kind,e.income_type,e.currency,e.decimals,e.amount_minor,e.payer_id,u.name payer_name '
        'FROM entries e JOIN users u ON u.id=e.payer_id' + where + ' ORDER BY e.date,e.id', params).fetchall()
    by_kind, by_member, by_income_type = {}, {}, {}
    for row in rows:
        amount = Decimal(row['amount_minor']) / (10 ** row['decimals'])
        slot = by_kind.setdefault(row['kind'], {}).setdefault(
            row['currency'], {'currency': row['currency'], 'decimals': row['decimals'], 'amount': Decimal(0), 'count': 0})
        slot['amount'] += amount
        slot['count'] += 1
        if row['kind'] == 'income':
            it = 'capital' if row['income_type'] == 'capital' else 'operating'
            islot = by_income_type.setdefault(it, {}).setdefault(
                row['currency'], {'currency': row['currency'], 'decimals': row['decimals'], 'amount': Decimal(0), 'count': 0})
            islot['amount'] += amount
            islot['count'] += 1
        person = by_member.setdefault((row['payer_id'], row['currency'], row['kind']), {
            'id': row['payer_id'], 'name': row['payer_name'], 'currency': row['currency'],
            'decimals': row['decimals'], 'kind': row['kind'], 'amount': Decimal(0), 'count': 0})
        person['amount'] += amount
        person['count'] += 1

    def format_slots(slots):
        cleaned = []
        for slot in slots:
            item = {**slot, 'amount': format(slot['amount'], f'.{slot["decimals"]}f')}
            item.pop('decimals', None)
            cleaned.append(item)
        return sorted(cleaned, key=lambda item: (item['currency'], item.get('kind', ''), item.get('name', '')))

    card = {'kind': 'summary', 'from': action.get('date_from'), 'to': action.get('date_to'), 'count': len(rows),
            'by_kind': {kind: format_slots(slots.values()) for kind, slots in by_kind.items()},
            'by_income_type': {it: format_slots(slots.values()) for it, slots in by_income_type.items()},
            'by_member': format_slots(by_member.values())}
    total = {}
    for kind, slots in card['by_kind'].items():
        for slot in slots:
            total.setdefault(slot['currency'], {'expense': '0', 'income': '0'})
            total[slot['currency']][kind] = slot['amount']
    card['totals'] = total
    return card


def _report_with_conn(conn, base=None, scenario=None):
    """Like fx.report, but reads the caller's connection (sees uncommitted writes)."""
    conf = settings(conn)
    base = base or conf['reference_currency']
    currency(conf, base)
    entries = [dict(row) for row in conn.execute(
        'SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id')]
    payments = [dict(row) for row in conn.execute('SELECT * FROM payments')]
    extra_codes = set()
    if scenario:
        cur = currency(conf, scenario['currency'])
        minor_amount(scenario['amount'], cur['decimals'])
        extra_codes.add(cur['code'])
    codes = fx.required_codes(entries, payments, base, extra_codes)
    return fx.calculate(entries, conf, fx.cached_rates(base, codes), scenario, payments)


def _funds_card(kind, conf, action, conn):
    base = str(action.get('base') or conf['reference_currency']).upper()
    try:
        currency(conf, base)
    except HTTPException:
        base = conf['reference_currency']
    try:
        report = _report_with_conn(conn, base)
    except Exception:
        report = None
    if not report:
        return {'kind': kind, 'complete': False, 'base': base, 'warning': '汇率或资金数据暂不可用，请稍后重试。'}
    card = {'kind': kind, 'complete': bool(report.get('complete')), 'base': base,
            'cash': report.get('cash'), 'public_balance': report.get('public_balance'),
            'income': report.get('income'), 'operating_income': report.get('operating_income'),
            'capital_income': report.get('capital_income'), 'paid': report.get('paid'),
            'public_paid': report.get('public_paid'), 'total_outflow': report.get('total_outflow'),
            'due': report.get('due'), 'net_after_due': report.get('net_after_due'),
            'actual_net_after_due': report.get('actual_net_after_due'),
            'operating_net': report.get('operating_net'),
            'operating_net_after_due': report.get('operating_net_after_due'),
            'operating_profit': report.get('operating_profit'),
            'operating_profit_after_due': report.get('operating_profit_after_due'),
            'income_fees': report.get('income_fees'), 'public_fees': report.get('public_fees'),
            'advance_fees': report.get('advance_fees'), 'total_fees': report.get('total_fees'),
            'reserve': report.get('reserve'), 'payable': report.get('payable'),
            'remaining': report.get('remaining'), 'shortage': report.get('shortage'),
            'balances': report.get('balances', {}),
            'currency_accounts': report.get('currency_accounts', []),
            'shareholder_contributions': report.get('shareholder_contributions', [])}
    if not report.get('complete'):
        card['warning'] = report.get('fx', {}).get('warning') or '汇率不完整，跨币种金额暂不可用。'
    elif report.get('fx', {}).get('stale'):
        card['warning'] = report.get('fx', {}).get('warning') or '正在使用过期的汇率缓存，跨币种金额仅供参考。'
    if kind == 'due':
        card['members'] = report.get('members', [])
    elif kind == 'funds':
        card['members'] = report.get('members', [])
        card['fx'] = {'base': report.get('fx', {}).get('base'),
                      'source': report.get('fx', {}).get('source'),
                      'stale': report.get('fx', {}).get('stale'),
                      'missing': report.get('fx', {}).get('missing', [])}
    return card


def _entry_query(conn, user, conf, action):
    try:
        entry = entry_view(conn, int(action.get('id')))
    except (TypeError, ValueError):
        raise PlanQuestion('请提供要查看的账目编号。')
    except HTTPException:
        raise PlanQuestion(f'没有找到账目 #{action.get("id")}。')
    events = [dict(row) for row in conn.execute(
        'SELECT e.id,e.action,e.detail,e.created_at,u.name FROM events e JOIN users u ON u.id=e.user_id WHERE entry_id=? ORDER BY e.id',
        (entry['id'],))]
    return {'kind': 'entry', 'entry': entry, 'events': events}


def run_query(conn, user, conf, action):
    query = action.get('query')
    try:
        if query == 'list_entries':
            return _list_query(conn, user, conf, action)
        if query == 'entry_detail':
            return _entry_query(conn, user, conf, action)
        if query == 'summary':
            return _summary_query(conn, user, conf, action)
        if query in ('balance', 'due', 'funds'):
            return _funds_card(query, conf, action, conn)
        if query == 'simulate_income':
            base = str(action.get('base') or conf['reference_currency']).upper()
            cur = currency(conf, str(action.get('simulate_currency') or conf['reference_currency']).upper())
            amount = _amount_value(action.get('simulate_amount'))
            if amount in (None, 'FULL'):
                return {'kind': 'simulation', 'complete': False, 'base': base, 'warning': '请提供模拟进账金额。'}
            minor_amount(amount, cur['decimals'])
            report = _report_with_conn(conn, base, {'amount': amount, 'currency': cur['code']})
            return {'kind': 'simulation', 'complete': bool(report.get('complete')), 'base': report.get('base'),
                    'cash': report.get('cash'), 'due': report.get('due'), 'reserve': report.get('reserve'),
                    'payable': report.get('payable'), 'remaining': report.get('remaining'),
                    'shortage': report.get('shortage'), 'extra': report.get('extra'),
                    'public_paid': report.get('public_paid'),
                    'members': report.get('members', []),
                    'warning': report.get('fx', {}).get('warning')
                    if (not report.get('complete') or report.get('fx', {}).get('stale')) else None}
        return {'kind': 'error', 'error': f'不支持的查询：{query}'}
    except PlanQuestion as exc:
        return {'kind': 'error', 'error': str(exc)}
    except HTTPException as exc:
        return {'kind': 'error', 'error': str(exc.detail)}


# --- plan assembly, storage and execution ----------------------------------

def resolve(conn, user, conf, raw, context, rates=None):
    actions, results = [], []
    raw_questions = raw.get('questions') if isinstance(raw.get('questions'), list) else []
    questions = list(raw_questions)
    raw_actions = raw.get('actions') if isinstance(raw.get('actions'), list) else []
    context_ids = {item['id'] for item in context.get('entries', [])}
    seen_write, targeted = False, set()

    def guard_targets(ids):
        duplicates = sorted(set(ids) & targeted)
        if duplicates:
            questions.append('账目 ' + '、'.join(f'#{entry_id}' for entry_id in duplicates) +
                             ' 在同一份清单里被多次操作，请分成两步确认。')
            return False
        targeted.update(ids)
        return True

    for action in raw_actions:
        action_type = action.get('type')
        try:
            if action_type == 'query':
                results.append({'index': len(actions) + len(results), 'type': 'query',
                                'query': action.get('query'), 'action': action,
                                'card': run_query(conn, user, conf, action)})
            elif action_type == 'create_entry':
                _append(actions, _resolve_create(conn, user, conf, action))
                seen_write = True
            elif action_type == 'edit_entry':
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要修改的账目编号。')
                if guard_targets(ids[:1]):
                    if action.get('id') is None:
                        action = {**action, 'id': ids[0]}
                    _append(actions, _resolve_edit(conn, user, conf, action, context_ids))
                    seen_write = True
            elif action_type in ('void_entry', 'submit_entry'):
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要处理的账目编号。')
                if guard_targets(ids):
                    for entry_id in ids:
                        single = dict(action)
                        single['id'] = entry_id
                        single.pop('ids', None)
                        _append(actions, _resolve_transition(conn, user, conf, single, context_ids))
                    seen_write = True
            elif action_type == 'pay_entry':
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要登记付款的账目编号。')
                if guard_targets(ids[:1]):
                    if action.get('id') is None:
                        action = {**action, 'id': ids[0]}
                    _append(actions, _resolve_pay(conn, user, conf, action, context_ids, rates))
                    seen_write = True
            elif action_type == 'delete_entry':
                ids = _ids(action)
                if not ids:
                    raise PlanQuestion('请指明要永久删除的账目编号。')
                if guard_targets(ids):
                    for entry_id in ids:
                        single = dict(action)
                        single['id'] = entry_id
                        single.pop('ids', None)
                        _append(actions, _resolve_delete(conn, user, conf, single, context_ids))
                    seen_write = True
            elif action_type == 'batch_pay':
                if seen_write:
                    questions.append('批量报销需要基于最新账本。请先确认前面的操作，再单独发起批量报销。')
                else:
                    _append(actions, _resolve_batch(conn, user, conf, action, rates, context_ids))
                    seen_write = True
            else:
                questions.append(f'无法识别的操作类型：{action_type}')
        except PlanQuestion as exc:
            questions.append(str(exc))
        except HTTPException as exc:
            card = _card(action_type or 'unknown', '无法执行')
            card['errors'].append(str(exc.detail))
            _append(actions, card)
            seen_write = True
    # Keep indexes stable and unique for the client.
    counter = 0
    for card in actions:
        card['index'] = counter
        counter += 1
    for result in results:
        result['index'] = counter
        counter += 1
    deduped = []
    for question in questions:
        if isinstance(question, str) and question.strip() and question.strip() not in deduped:
            deduped.append(question.strip()[:200])
    return {'reply': raw.get('reply', ''), 'questions': deduped[:5],
            'actions': actions, 'results': results,
            'executable': any(not card['errors'] for card in actions)}


def store(conn, user, plan, source):
    payload = {'reply': plan['reply'], 'questions': plan['questions'],
               'actions': plan['actions'], 'results': plan['results']}
    writable = [card for card in plan['actions'] if not card['errors']]
    if not writable:
        plan['plan_id'] = None
        plan['expires'] = None
        return plan
    plan_id = secrets.token_urlsafe(24)
    expires = int(time.time()) + PLAN_TTL
    conn.execute('INSERT INTO ai_plans(id,user_id,source,payload,created_at,expires) VALUES (?,?,?,?,?,?)',
                 (plan_id, user['id'], source, json.dumps(payload, ensure_ascii=False), config.now(), expires))
    conn.execute('DELETE FROM ai_plans WHERE expires<?', (int(time.time()) - 86400,))
    plan['plan_id'] = plan_id
    plan['expires'] = expires
    return plan


def load(conn, user, plan_id):
    row = conn.execute('SELECT * FROM ai_plans WHERE id=? AND user_id=? AND expires>?',
                       (str(plan_id or ''), user['id'], int(time.time()))).fetchone()
    if not row:
        fail('操作清单不存在或已过期，请重新描述需求。', 409)
    return row, json.loads(row['payload'])


def _create_body(resolved):
    body = {key: resolved.get(key) for key in ('kind', 'amount', 'currency', 'description', 'date', 'payer_id', 'status', 'note')}
    if resolved.get('gross_amount') not in (None, ''):
        body['gross_amount'] = resolved['gross_amount']
        body['amount'] = resolved['gross_amount']
    fee_mode = resolved.get('fee_mode')
    if fee_mode not in (None, ''):
        body['fee_mode'] = fee_mode
        if fee_mode == 'rate' and resolved.get('fee_rate') not in (None, ''):
            body['fee_rate'] = resolved['fee_rate']
        elif fee_mode == 'fixed' and resolved.get('fee_amount') not in (None, ''):
            body['fee_amount'] = resolved['fee_amount']
    else:
        if resolved.get('fee_rate') not in (None, ''):
            body['fee_rate'] = resolved['fee_rate']
        if resolved.get('fee_amount') not in (None, ''):
            body['fee_amount'] = resolved['fee_amount']
    fx_mode = resolved.get('fx_mode')
    if fx_mode == 'manual' and resolved.get('fx_rate') not in (None, ''):
        body['fx_mode'] = 'manual'
        body['fx_rate'] = resolved['fx_rate']
        if resolved.get('fx_currency'):
            body['fx_currency'] = resolved['fx_currency']
    elif fx_mode == 'auto':
        body['fx_mode'] = 'auto'
    elif resolved.get('fx_rate') not in (None, ''):
        body['fx_rate'] = resolved['fx_rate']
        if resolved.get('fx_currency'):
            body['fx_currency'] = resolved['fx_currency']
    merchant_mode = resolved.get('merchant_mode')
    if merchant_mode == 'different' and resolved.get('merchant_currency'):
        body['merchant_mode'] = 'different'
        body['merchant_currency'] = resolved['merchant_currency']
        if resolved.get('merchant_amount') not in (None, ''):
            body['merchant_amount'] = resolved['merchant_amount']
    elif merchant_mode == 'same':
        body['merchant_mode'] = 'same'
    elif resolved.get('merchant_currency'):
        body['merchant_currency'] = resolved['merchant_currency']
        if resolved.get('merchant_amount') not in (None, ''):
            body['merchant_amount'] = resolved['merchant_amount']
    body['expense_type'] = resolved.get('expense_type', 'advance') if body['kind'] == 'expense' else ''
    body['income_type'] = (resolved.get('income_type') or 'operating') if body['kind'] == 'income' else ''
    return body


def _check_version(conn, entry_id, version):
    current = entry_view(conn, entry_id)
    if current['version'] != version:
        fail('账目已被其他操作更新，请重新描述并核对。', 409)
    return current


def execute(conn, user, plan_id, selected):
    row, payload = load(conn, user, plan_id)
    if row['executed_at'] and row['result']:
        return json.loads(row['result'])
    cards = {card['index']: card for card in payload.get('actions', [])}
    if not isinstance(selected, list) or not selected or any(type(index) is not int for index in selected) or len(set(selected)) != len(selected):
        fail('请选择要执行的操作。')
    chosen = []
    for index in selected:
        card = cards.get(index)
        if not card or card.get('errors'):
            fail('所选操作不存在或仍有错误，请重新核对。')
        chosen.append(card)
    if not chosen:
        fail('请选择要执行的操作。')
    chosen.sort(key=lambda card: card['index'])
    conf = settings(conn)
    # Validate every selected action before writing anything.
    for card in chosen:
        resolved = card['resolved']
        kind = card['type']
        if kind not in ('create_entry', 'edit_entry', 'void_entry', 'submit_entry', 'delete_entry', 'pay_entry', 'batch_pay'):
            fail('此操作已不再支持，请重新生成清单。', 409)
        try:
            if kind == 'create_entry':
                normalized_entry(conn, user, _create_body(resolved))
            elif kind == 'edit_entry':
                current = _check_version(conn, resolved['id'], resolved['version'])
                require_entry_edit_access(user, current)
            elif kind in ('void_entry', 'submit_entry'):
                _check_version(conn, resolved['id'], resolved['version'])
            elif kind == 'delete_entry':
                current = _check_version(conn, resolved['id'], resolved['version'])
                if current['status'] != 'voided':
                    fail('账目已不是作废状态，请重新描述。', 409)
            elif kind == 'pay_entry':
                _check_version(conn, resolved['id'], resolved['version'])
                payment_preview(conn, user, resolved['id'], resolved['amount'], resolved['quote_id'])
            elif kind == 'batch_pay':
                if time.time() >= resolved['expires'] or funds_snapshot(conn) != resolved['snapshot']:
                    fail('账本已变化或确认已过期，请重新描述批量报销需求。', 409)
                for item in resolved['items']:
                    payment_preview(conn, user, item['id'], item['amount'], item['quote_id'])
        except HTTPException:
            raise
        except Exception:
            fail('操作校验失败，请重新描述。', 409)
    results, receipts = [], []
    for card in chosen:
        resolved, kind = card['resolved'], card['type']
        if kind == 'create_entry':
            entry = add_entry(conn, user, _create_body(resolved),
                              'feishu' if row['source'] == 'feishu' else 'ai',
                              f'ai:{user["id"]}:{plan_id}:{card["index"]}')
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'description': entry['description'], 'amount': entry['amount'], 'currency': entry['currency']})
        elif kind == 'edit_entry':
            entry = edit_entry(conn, user, resolved['id'], _create_body(resolved))
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'description': entry['description'], 'amount': entry['amount'], 'currency': entry['currency']})
        elif kind in ('void_entry', 'submit_entry'):
            action_name = resolved['action']
            entry = transition(conn, user, resolved['id'], action_name, resolved.get('note', ''))
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'status': entry['status']})
        elif kind == 'delete_entry':
            receipt = delete_entry(conn, user, resolved['id'])
            if receipt:
                receipts.append(receipt)
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': resolved['id']})
        elif kind == 'pay_entry':
            entry = transition(conn, user, resolved['id'], 'pay', resolved.get('note', ''),
                               amount=resolved['amount'],
                               payment_key=f'pay:ai:{user["id"]}:{plan_id}:{card["index"]}',
                               quote_id=resolved['quote_id'])
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_id': entry['id'],
                            'status': entry['status'], 'reimbursed_amount': entry['reimbursed_amount'],
                            'remaining_amount': entry['remaining_amount']})
        elif kind == 'batch_pay':
            entry_ids = []
            for item in resolved['items']:
                entry = transition(conn, user, item['id'], 'pay', resolved['note'], amount=item['amount'],
                                   payment_key=f'pay:ai:{user["id"]}:{plan_id}:{card["index"]}:{item["id"]}',
                                   quote_id=item['quote_id'])
                entry_ids.append(entry['id'])
            results.append({'index': card['index'], 'type': kind, 'ok': True, 'entry_ids': entry_ids,
                            'count': len(entry_ids), 'totals': resolved.get('totals', {})})
    queries = []
    for result in payload.get('results', []):
        action = result.get('action') or {'query': result.get('query')}
        queries.append({'index': result.get('index'), 'type': 'query', 'query': action.get('query'),
                        'action': action, 'card': run_query(conn, user, conf, action)})
    executed = {'results': results, 'queries': queries, 'receipts': receipts, 'executed_at': config.now()}
    conn.execute('UPDATE ai_plans SET executed_at=?,result=? WHERE id=?',
                 (executed['executed_at'], json.dumps(executed, ensure_ascii=False), plan_id))
    return executed


def needed_rates(conn, conf, raw):
    """Fetch FX once, outside the write lock, only when the plan needs it."""
    actions = raw.get('actions') if isinstance(raw.get('actions'), list) else []
    need, query_bases = False, set()
    for action in actions:
        if action.get('type') == 'batch_pay':
            need = True
        elif action.get('type') == 'pay_entry' and action.get('settlement_currency') not in (None, '', 'original'):
            if action.get('fx_rate') in (None, '') or action.get('fx_mode') == 'auto':
                need = True
        elif action.get('type') == 'query' and action.get('query') in ('balance', 'due', 'funds', 'simulate_income'):
            base = str(action.get('base') or conf['reference_currency']).upper()
            try:
                currency(conf, base)
                query_bases.add(base)
            except HTTPException:
                pass
    if not need and not query_bases:
        return None
    entries = [dict(row) for row in conn.execute("SELECT id,currency,status,fx_rate,fx_currency FROM entries WHERE status IN ('pending','reimbursed','posted')")]
    payments = [dict(row) for row in conn.execute('SELECT entry_id,currency FROM payments')]
    # Warm query-only bases before the write lock; _report_with_conn then uses cache only.
    for base in query_bases - {conf['reference_currency']}:
        try:
            base_codes = fx.required_codes(entries, payments, base)
            fx.rates_for(base, base_codes)
        except Exception:
            pass
    if not need:
        return None
    try:
        ref_codes = fx.required_codes(entries, payments, conf['reference_currency'])
        return fx.rates_for(conf['reference_currency'], ref_codes)
    except Exception:
        return None
