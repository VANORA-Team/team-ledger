import re
import secrets
import time
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fastapi import HTTPException
from .config import now
from .db import settings, set_setting


def fail(message, status=400):
    raise HTTPException(status, message)


def receipt_media_type(filename):
    suffix = (filename or '').rsplit('.', 1)[-1].lower()
    return {'png': 'image/png', 'jpg': 'image/jpeg', 'pdf': 'application/pdf'}.get(suffix)


def text_value(value, label, limit=200, required=True):
    if not isinstance(value, str):
        fail(f'{label}格式不正确')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        fail(f'{label}不能为空且最多 {limit} 个字符' if required else f'{label}最多 {limit} 个字符')
    return value


def is_finance(user):
    return bool(user['active'] and user['role'] in ('owner', 'finance'))


def admin(user):
    if not is_finance(user):
        fail('此操作需要财务管理员权限', 403)


def owner(user):
    if not user['active'] or user['role'] != 'owner':
        fail('此操作需要账本所有者权限', 403)


def can_manage_entry(user, entry):
    return is_finance(user) or (
        user['active'] and entry['kind'] == 'expense' and
        entry['expense_type'] == 'advance' and entry['payer_id'] == user['id']
    )


def require_entry_access(user, entry):
    if not can_manage_entry(user, entry):
        fail('仅财务管理员可管理这笔账目', 403)


def public_user(user):
    data = {k: user[k] for k in ('id', 'username', 'name', 'role', 'active')}
    sub = user['oidc_sub'] if 'oidc_sub' in user.keys() else None
    email = user['oidc_email'] if 'oidc_email' in user.keys() else ''
    data['oidc_linked'] = bool(sub)
    data['oidc_email'] = email or ''
    return data


def currency(conf, code):
    match = next((c for c in conf['currencies'] if c['code'] == code), None)
    if not match:
        fail('请选择团队已启用的币种')
    return match


def minor_amount(value, decimals, *, allow_zero=False, label='金额'):
    value = str(value).strip()
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,4})?', value):
        fail(f'{label}必须为{"非负数" if (allow_zero and label != "金额") else "正数"}，不支持千分位或科学计数法')
    try:
        amount = Decimal(value)
        scaled = amount * (10 ** decimals)
        if amount < 0 or (amount == 0 and not allow_zero) or scaled != scaled.to_integral_value() or scaled > 9_000_000_000_000:
            fail(f'{label}超出范围或小数超过 {decimals} 位')
        return int(scaled)
    except InvalidOperation:
        fail(f'{label}格式不正确')


INCOME_TYPES = ('operating', 'capital')
FEE_MODES = ('none', 'rate', 'fixed')
FX_MODES = ('auto', 'manual')


def income_type_label(income_type):
    return '公户转入' if income_type == 'capital' else '营业收入'


def parse_fx_rate(value):
    if isinstance(value, bool) or value is None:
        fail('外币汇率格式不正确')
    raw = str(value).strip()
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,8})?', raw):
        fail('外币汇率需为正数，最多保留 8 位小数，不支持千分位或科学计数法')
    try:
        rate = Decimal(raw)
    except InvalidOperation:
        fail('外币汇率格式不正确')
    if not rate.is_finite() or rate <= 0 or rate > Decimal('1000000000'):
        fail('外币汇率需大于 0 且不超过 1,000,000,000')
    text = format(rate, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return rate, text


def compute_fx(cur_code, conf, body):
    raw_mode = body.get('fx_mode', None)
    raw_rate = body.get('fx_rate', None)
    raw_target = body.get('fx_currency', None)
    has_rate = raw_rate is not None and str(raw_rate).strip() != ''

    if raw_mode is not None and raw_mode != '':
        if not isinstance(raw_mode, str) or raw_mode not in FX_MODES:
            fail('汇率方式请选择自动参考汇率或手动指定')
        if raw_mode == 'auto':
            return '', ''
        if not has_rate:
            fail('请填写外币汇率，或切换为自动参考汇率')
    elif not has_rate:
        return '', ''

    if raw_target not in (None, ''):
        if not isinstance(raw_target, str):
            fail('汇率目标币种格式不正确')
        target_code = currency(conf, raw_target.strip().upper())['code']
    else:
        target_code = conf['reference_currency']

    if target_code == cur_code:
        fail('记账币种与参考币种一致，无需手动指定外币汇率')

    _, rate_str = parse_fx_rate(raw_rate)
    return rate_str, target_code


def parse_fee_rate(value):
    if isinstance(value, bool) or value is None:
        fail('手续费比例格式不正确')
    raw = str(value).strip()
    if not re.fullmatch(r'\d{1,3}(?:\.\d{1,4})?', raw):
        fail('手续费比例需为非负数，最多保留 4 位小数（例如 0.6 或 2.9）')
    try:
        rate = Decimal(raw)
    except InvalidOperation:
        fail('手续费比例格式不正确')
    if rate < 0 or rate > Decimal(100):
        fail('手续费比例需在 0%–100% 之间')
    text = format(rate, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return rate, text


def compute_fee(kind, gross_minor, decimals, body):
    raw_mode = body.get('fee_mode', None)
    raw_rate = body.get('fee_rate', None)
    raw_fee = body.get('fee_amount', None)
    has_rate = raw_rate is not None and str(raw_rate).strip() != ''
    has_fee = raw_fee is not None and str(raw_fee).strip() != ''

    if raw_mode is None or raw_mode == '':
        if has_rate and has_fee:
            fail('按比例手续费与手动手续费只能选择一种')
        mode = 'rate' if has_rate else ('fixed' if has_fee else 'none')
    else:
        if not isinstance(raw_mode, str) or raw_mode not in FEE_MODES:
            fail('手续费方式请选择无手续费、按比例或手动金额')
        mode = raw_mode

    if mode == 'none':
        return 'none', '', 0, gross_minor

    if mode == 'rate':
        if not has_rate:
            return 'none', '', 0, gross_minor
        rate_dec, rate_str = parse_fee_rate(raw_rate)
        if rate_dec == 0:
            return 'none', '', 0, gross_minor
        if kind == 'income' and rate_dec >= Decimal(100):
            fail('收入手续费比例必须小于 100%')
        fee_minor = int((Decimal(gross_minor) * rate_dec / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    else:
        if not has_fee:
            return 'none', '', 0, gross_minor
        fee_minor = minor_amount(raw_fee, decimals, allow_zero=True, label='手续费金额')
        if fee_minor == 0:
            return 'none', '', 0, gross_minor
        rate_str = ''

    if kind == 'income':
        effective_minor = gross_minor - fee_minor
        if effective_minor <= 0:
            fail('收入手续费必须小于原金额，实际入账金额需大于 0')
    else:
        effective_minor = gross_minor + fee_minor
        if effective_minor > 9_000_000_000_000:
            fail('含手续费后的实际金额超出范围')

    return mode, rate_str, fee_minor, effective_minor


def signed_minor_amount(value, decimals):
    value = str(value).strip()
    if not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,4})?', value):
        fail('金额格式不正确，不支持千分位或科学计数法')
    try:
        amount = Decimal(value)
        scaled = amount * (10 ** decimals)
        if scaled != scaled.to_integral_value() or abs(scaled) > 9_000_000_000_000:
            fail(f'金额超出范围或小数超过 {decimals} 位')
        return int(scaled)
    except InvalidOperation:
        fail('金额格式不正确')


def normalized_entry(conn, user, body):
    conf = settings(conn)
    kind = body.get('kind', 'expense')
    if kind not in ('expense', 'income'):
        fail('账目类型不正确')
    expense_type = body.get('expense_type', 'advance') if kind == 'expense' else ''
    if kind == 'expense' and expense_type not in ('public', 'advance'):
        fail('支出请选择公账支出或成员垫付')
    income_type = body.get('income_type', 'operating') if kind == 'income' else ''
    if kind == 'income' and income_type not in INCOME_TYPES:
        fail('收入请选择营业收入或公户转入')
    if kind == 'income' or expense_type == 'public':
        admin(user)
    cur = currency(conf, body.get('currency', conf['default_currency']))
    gross_raw = body['gross_amount'] if body.get('gross_amount') not in (None, '') else body.get('amount', '')
    gross_minor = minor_amount(gross_raw, cur['decimals'])
    fee_mode, fee_rate, fee_minor, amount = compute_fee(kind, gross_minor, cur['decimals'], body)
    fx_rate, fx_currency = compute_fx(cur['code'], conf, body)
    description = text_value(body.get('description', ''), '用途 / 收入说明', 120, False)
    try:
        entry_date = date.fromisoformat(body.get('date', ''))
        if entry_date.year < 2000 or entry_date.year > 2100:
            raise ValueError()
    except (ValueError, TypeError):
        fail('日期格式不正确')
    payer_id = body.get('payer_id', user['id'])
    if type(payer_id) is not int:
        fail('成员格式不正确')
    if payer_id != user['id']:
        admin(user)
    if not conn.execute('SELECT id FROM users WHERE id=? AND active=1', (payer_id,)).fetchone():
        fail('成员不存在或已停用')
    direct = kind == 'income' or expense_type == 'public'
    status = body.get('status', 'posted' if direct else 'pending')
    if direct and status in ('pending', 'submitted'):
        status = 'posted'  # Existing forms used pending for income.
    if status == 'submitted':
        status = 'pending'  # Older open forms and persisted Telegram drafts remain usable.
    allowed = ('posted',) if kind == 'income' else ('draft', 'posted') if direct else ('draft', 'pending')
    if status not in allowed:
        fail('成员垫付请选择草稿或待报销；公账支出请选择草稿或已入账；收入正常入账')
    return {'kind': kind, 'expense_type': expense_type, 'income_type': income_type,
            'amount_minor': amount, 'gross_amount_minor': gross_minor,
            'fee_mode': fee_mode, 'fee_rate': fee_rate, 'fee_minor': fee_minor,
            'currency': cur['code'], 'decimals': cur['decimals'],
            'fx_rate': fx_rate, 'fx_currency': fx_currency,
            'description': description, 'category': '', 'date': entry_date.isoformat(),
            'payer_id': payer_id, 'status': status, 'note': text_value(body.get('note', ''), '备注', 1000, False)}


def entry_view(conn, entry_id):
    row = conn.execute('SELECT e.*,u.name payer_name,c.name creator_name FROM entries e JOIN users u ON u.id=e.payer_id JOIN users c ON c.id=e.created_by WHERE e.id=?', (entry_id,)).fetchone()
    if not row:
        fail('账目不存在', 404)
    result = dict(row)
    def amount(units):
        return format(Decimal(units) / (10 ** row['decimals']), f'.{row["decimals"]}f')
    gross_minor = row['gross_amount_minor'] if row['gross_amount_minor'] else row['amount_minor']
    fee_minor = row['fee_minor'] or 0
    result['gross_amount_minor'] = gross_minor
    result['gross_amount'] = amount(gross_minor)
    result['fee_mode'] = row['fee_mode'] or 'none'
    result['fee_rate'] = row['fee_rate'] or ''
    result['fee_minor'] = fee_minor
    result['fee_amount'] = amount(fee_minor)
    result['fx_rate'] = row['fx_rate'] or ''
    result['fx_currency'] = row['fx_currency'] or ''
    result['fx_mode'] = 'manual' if (result['fx_rate'] and result['fx_currency']) else 'auto'
    if result['fx_mode'] == 'manual':
        conf = settings(conn)
        target_dec = next((c['decimals'] for c in conf['currencies'] if c['code'] == result['fx_currency']), 2)
        fx_val = (Decimal(row['amount_minor']) / (10 ** row['decimals']) * Decimal(result['fx_rate'])).quantize(
            Decimal(1).scaleb(-target_dec), rounding=ROUND_HALF_UP
        )
        result['fx_amount'] = format(fx_val, f'.{target_dec}f')
    else:
        result['fx_rate'] = ''
        result['fx_currency'] = ''
        result['fx_amount'] = ''
    result['amount'] = amount(row['amount_minor'])
    result['reimbursed_amount'] = amount(row['reimbursed_minor'])
    result['remaining_minor'] = row['amount_minor'] - row['reimbursed_minor'] if row['kind'] == 'expense' and row['expense_type'] == 'advance' else 0
    result['remaining_amount'] = amount(result['remaining_minor'])
    result['version'] = conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE entry_id=?', (entry_id,)).fetchone()[0]
    result['payments'] = []
    for payment in conn.execute('SELECT p.*,u.name FROM payments p JOIN users u ON u.id=p.user_id WHERE entry_id=? ORDER BY p.id', (entry_id,)):
        item = {k: payment[k] for k in ('id', 'user_id', 'name', 'currency', 'decimals', 'amount_minor', 'entry_amount_minor', 'fx_rate', 'fx_date', 'note', 'created_at')}
        item['amount'] = format(Decimal(payment['amount_minor']) / (10 ** payment['decimals']), f'.{payment["decimals"]}f')
        item['entry_amount'] = amount(payment['entry_amount_minor'])
        result['payments'].append(item)
    result['receipt_type'] = receipt_media_type(result['receipt'])
    result['receipt'] = bool(result['receipt'])
    result.pop('category', None)  # Retained database column only for compatibility with earlier local builds.
    return result


def audit(conn, entry_id, user_id, action, detail=''):
    conn.execute('INSERT INTO events(entry_id,user_id,action,detail,created_at) VALUES (?,?,?,?,?)',
                 (entry_id, user_id, action, detail, now()))


def add_entry(conn, user, body, source='web', source_key=None):
    data = normalized_entry(conn, user, body)
    if not source_key or len(source_key) > 200:
        fail('缺少有效的防重复提交标识')
    existing = conn.execute('SELECT id,created_by FROM entries WHERE source_key=?', (source_key,)).fetchone()
    if existing:
        if existing['created_by'] != user['id']:
            fail('重复提交标识冲突', 409)
        previous = entry_view(conn, existing['id'])
        if any(previous[k] != v for k, v in data.items() if k not in ('status', 'category')):
            fail('同一提交标识不能用于不同账目', 409)
        return previous
    timestamp = now()
    cols = list(data) + ['created_by', 'source', 'source_key', 'created_at', 'updated_at']
    values = list(data.values()) + [user['id'], source, source_key, timestamp, timestamp]
    cursor = conn.execute(f'INSERT INTO entries({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})', values)
    audit(conn, cursor.lastrowid, user['id'], 'created', f'通过 {source} 创建')
    return entry_view(conn, cursor.lastrowid)


def make_payment_quote(conn, user, entry_id, code, fx=None):
    admin(user)
    entry, conf = entry_view(conn, entry_id), settings(conn)
    if entry['kind'] != 'expense' or entry['expense_type'] != 'advance' or entry['status'] != 'pending':
        fail('只有待报销的成员垫付可以登记付款', 409)
    code = entry['currency'] if code == 'original' else code
    if code not in (entry['currency'], conf['reference_currency']):
        fail('请选择记账货币或团队结算货币')
    cur = currency(conf, code)
    rate, rate_date, rate_source = Decimal(1), None, 'same'
    if code != entry['currency']:
        if entry.get('fx_rate') and entry.get('fx_currency') == code:
            rate, rate_date, rate_source = Decimal(entry['fx_rate']), None, 'manual'
        else:
            row = (fx or {}).get('rates', {}).get(entry['currency'])
            if not row or fx.get('base') != code or fx.get('stale') or fx.get('missing'):
                fail('暂无有效汇率，不能按结算货币登记，请刷新汇率或选择记账货币')
            rate, rate_date, rate_source = Decimal(row['rate']), row['date'], 'reference'
    maximum = int((Decimal(entry['remaining_minor']) / (10 ** entry['decimals']) * rate * (10 ** cur['decimals'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if maximum <= 0:
        fail('剩余金额小于该币种最小单位，请按记账货币报销')
    token, expires = secrets.token_urlsafe(24), int(time.time()) + 1800
    conn.execute('DELETE FROM payment_quotes WHERE expires<?', (int(time.time()) - 86400,))
    conn.execute('INSERT INTO payment_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (token, entry_id, user['id'], entry['version'], code, cur['decimals'], str(rate), rate_date, entry['remaining_minor'], maximum, expires))
    return {'id': token, 'entry_id': entry_id, 'version': entry['version'], 'currency': code, 'decimals': cur['decimals'],
            'rate': str(rate), 'rate_date': rate_date, 'rate_source': rate_source,
            'remaining_amount': entry['remaining_amount'], 'entry_currency': entry['currency'],
            'maximum_amount': format(Decimal(maximum) / (10 ** cur['decimals']), f'.{cur["decimals"]}f'), 'expires': expires}


def payment_preview(conn, user, entry_id, amount, quote_id):
    entry = entry_view(conn, entry_id)
    quote = conn.execute('SELECT * FROM payment_quotes WHERE id=? AND entry_id=? AND user_id=?', (quote_id, entry_id, user['id'])).fetchone()
    if not quote or quote['expires'] <= time.time():
        fail('付款换算已过期，请重新选择付款币种', 409)
    if entry['kind'] != 'expense' or entry['expense_type'] != 'advance' or entry['status'] != 'pending' or entry['version'] != quote['version']:
        fail('账目或已报销金额已变化，请刷新后核对再登记', 409)
    units = minor_amount(amount, quote['decimals'])
    if units > quote['maximum_minor']:
        fail('本次报销金额不能超过所选币种的剩余待报销金额')
    original_units = entry['remaining_minor'] if units == quote['maximum_minor'] else int((Decimal(units) / (10 ** quote['decimals']) / Decimal(quote['rate']) * (10 ** entry['decimals'])).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if not 0 < original_units <= entry['remaining_minor']:
        fail('折算金额小于记账货币最小单位，请调整金额或选择记账货币')
    if original_units == entry['remaining_minor'] and units != quote['maximum_minor']:
        fail('金额接近结清，请填写剩余全额或调低本次报销金额')
    return entry, dict(quote), units, original_units


def record_payment(conn, user, entry_id, amount, note, payment_key, quote_id):
    admin(user)
    note = text_value(note, '付款方式 / 付款参考号', 500)
    if not isinstance(payment_key, str) or not 16 <= len(payment_key) <= 200:
        fail('缺少有效的付款防重复标识，请刷新后重试')
    previous = conn.execute('SELECT * FROM payments WHERE source_key=?', (payment_key,)).fetchone()
    if previous:
        units = minor_amount(amount, previous['decimals'])
        if (previous['entry_id'], previous['user_id'], previous['amount_minor'], previous['note'], previous['quote_id']) != (entry_id, user['id'], units, note, quote_id):
            fail('同一付款标识不能用于不同的付款内容', 409)
        return entry_view(conn, entry_id)
    entry, quote, units, original_units = payment_preview(conn, user, entry_id, amount, quote_id)
    paid, timestamp = entry['reimbursed_minor'] + original_units, now()
    conn.execute('INSERT INTO payments(entry_id,user_id,amount_minor,currency,decimals,note,entry_amount_minor,fx_rate,fx_date,quote_id,source_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (entry_id, user['id'], units, quote['currency'], quote['decimals'], note, original_units, quote['rate'], quote['rate_date'], quote_id, payment_key, timestamp))
    conn.execute('UPDATE entries SET reimbursed_minor=?,status=?,review_note=?,paid_at=?,updated_at=? WHERE id=?',
                 (paid, 'reimbursed' if paid == entry['amount_minor'] else 'pending', note, timestamp, timestamp, entry_id))
    actual = format(Decimal(units) / (10 ** quote['decimals']), f'.{quote["decimals"]}f')
    allocation = format(Decimal(original_units) / (10 ** entry['decimals']), f'.{entry["decimals"]}f')
    detail = f'本次报销 {actual} {quote["currency"]}'
    if quote['currency'] != entry['currency']:
        rate_tag = quote['rate_date'] or '手动汇率'
        detail += f'，抵扣 {allocation} {entry["currency"]}（1 {entry["currency"]} = {quote["rate"]} {quote["currency"]} · {rate_tag}）'
    audit(conn, entry_id, user['id'], 'pay', detail + ' · ' + note)
    return entry_view(conn, entry_id)


def transition(conn, user, entry_id, action, note='', *, amount=None, payment_key=None, quote_id=None):
    entry = entry_view(conn, entry_id)
    if action == 'void':
        require_entry_access(user, entry)
        if entry['reimbursed_minor'] or entry['status'] in ('reimbursed', 'voided'):
            fail('已有报销记录或已作废的账目不能作废', 409)
        note = text_value(note, '作废原因', 500)
        conn.execute('UPDATE entries SET status=?,review_note=?,updated_at=? WHERE id=?', ('voided', note, now(), entry_id))
        audit(conn, entry_id, user['id'], 'void', note)
        return entry_view(conn, entry_id)
    if entry['kind'] != 'expense':
        fail('收入无需报销')
    if action == 'pay':
        return record_payment(conn, user, entry_id, amount, note, payment_key, quote_id)
    note = text_value(note, '处理备注', 500, False)
    transitions = {'submit': (('draft',), 'posted' if entry['expense_type'] == 'public' else 'pending')}
    if action not in transitions:
        fail('操作不正确')
    require_entry_access(user, entry)
    allowed, target = transitions[action]
    if entry['status'] not in allowed:
        fail('账目状态已变化，请刷新后重试', 409)
    conn.execute('UPDATE entries SET status=?,review_note=?,paid_at=?,updated_at=? WHERE id=?',
                 (target, note, None, now(), entry_id))
    audit(conn, entry_id, user['id'], action, note)
    return entry_view(conn, entry_id)


def delete_entry(conn, user, entry_id):
    admin(user)
    row = conn.execute('SELECT id,status,receipt FROM entries WHERE id=?', (entry_id,)).fetchone()
    if not row:
        fail('账目不存在', 404)
    if row['status'] != 'voided':
        fail('仅已作废账目可以永久删除', 409)
    # Remove all related rows; callers delete the receipt file after commit.
    conn.execute('DELETE FROM payment_quotes WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM payments WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM events WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM entries WHERE id=?', (entry_id,))
    return row['receipt']


def edit_entry(conn, user, entry_id, body):
    old = entry_view(conn, entry_id)
    require_entry_access(user, old)
    if old['kind'] == 'income':
        if old['status'] != 'posted':
            fail('仅已入账的收入可以修改', 409)
    else:
        allowed = ('draft', 'posted') if old['expense_type'] == 'public' else ('draft', 'pending')
        if old['status'] not in allowed or old['reimbursed_minor']:
            fail('仅没有报销记录的支出可以修改', 409)
    merged = dict(body)
    if old['kind'] == 'expense':
        merged['expense_type'] = body.get('expense_type', old['expense_type'])
    else:
        merged['income_type'] = body.get('income_type', old.get('income_type') or 'operating')
    has_fee_input = any(k in body for k in ('fee_mode', 'fee_rate', 'fee_amount'))
    if not has_fee_input:
        merged['fee_mode'] = old['fee_mode']
        if old['fee_mode'] == 'rate':
            merged['fee_rate'] = old['fee_rate']
        elif old['fee_mode'] == 'fixed':
            merged['fee_amount'] = old['fee_amount']
    new_currency = merged.get('currency', old['currency'])
    currency_changed = new_currency != old['currency']
    has_fx_input = any(k in body for k in ('fx_mode', 'fx_rate', 'fx_currency'))
    if not has_fx_input:
        if not currency_changed and old['fx_rate'] and old['fx_currency']:
            merged['fx_mode'] = 'manual'
            merged['fx_rate'] = old['fx_rate']
            merged['fx_currency'] = old['fx_currency']
        else:
            merged['fx_mode'] = 'auto'
            merged['fx_rate'] = ''
            merged['fx_currency'] = ''
    else:
        raw_fx_mode = body.get('fx_mode')
        raw_fx_rate = body.get('fx_rate')
        if raw_fx_mode == 'auto' and old.get('fx_mode') == 'auto' and raw_fx_rate not in (None, ''):
            # Support callers doing PUT {**entry, 'fx_rate': '7.25'} without flipping fx_mode.
            merged['fx_mode'] = 'manual'
        elif (currency_changed and raw_fx_mode == old.get('fx_mode') and raw_fx_rate == old.get('fx_rate')
              and body.get('fx_currency') == old.get('fx_currency')):
            # Support callers doing PUT {**entry, 'currency': 'CNY'} without explicitly clearing old manual FX.
            merged['fx_mode'] = 'auto'
            merged['fx_rate'] = ''
            merged['fx_currency'] = ''
    has_gross = body.get('gross_amount') not in (None, '')
    has_amount = body.get('amount') not in (None, '')
    if not has_gross and not has_amount:
        merged['gross_amount'] = old['gross_amount']
    elif not has_gross and has_amount:
        if not has_fee_input and str(body['amount']).strip() == old['amount']:
            merged['gross_amount'] = old['gross_amount']
        else:
            merged['gross_amount'] = body['amount']
    elif has_gross and has_amount:
        gross_str = str(body['gross_amount']).strip()
        amt_str = str(body['amount']).strip()
        if gross_str == old['gross_amount'] and amt_str != old['amount']:
            # Support callers doing PUT {**entry, 'amount': '99'} while preserving explicit gross+effective payloads.
            keep_gross = False
            try:
                cur_obj = currency(settings(conn), merged.get('currency', old['currency']))
                g_minor = minor_amount(gross_str, cur_obj['decimals'])
                _, _, _, eff_minor = compute_fee(old['kind'], g_minor, cur_obj['decimals'], merged)
                a_minor = minor_amount(amt_str, cur_obj['decimals'])
                keep_gross = (a_minor == eff_minor)
            except HTTPException:
                keep_gross = False
            if not keep_gross:
                merged['gross_amount'] = body['amount']
    data = normalized_entry(conn, user, merged)
    if data['kind'] != old['kind']:
        fail('不支持更改账目类型')
    conn.execute('UPDATE entries SET ' + ','.join(f'{k}=?' for k in data) + ',review_note=?,updated_at=? WHERE id=?',
                 list(data.values()) + ['', now(), entry_id])
    audit(conn, entry_id, user['id'], 'edited')
    return entry_view(conn, entry_id)


def save_settings(conn, user, body):
    owner(user)
    conf = settings(conn)
    name = text_value(body.get('team_name', conf['team_name']), '团队名称', 40)
    currencies = body.get('currencies', conf['currencies'])
    if not isinstance(currencies, list) or not 1 <= len(currencies) <= 30:
        fail('请配置 1–30 个币种')
    codes = set()
    clean = []
    for cur in currencies:
        if not isinstance(cur, dict) or not re.fullmatch('[A-Z]{3}', str(cur.get('code', ''))):
            fail('币种代码必须为 3 位大写英文字母')
        code = cur['code']
        decimals = cur.get('decimals', 2)
        if type(decimals) is not int or decimals not in (0, 2, 3, 4) or code in codes:
            fail('币种不能重复，小数位支持 0、2、3、4')
        used = conn.execute('SELECT decimals FROM entries WHERE currency=? UNION ALL SELECT decimals FROM payments WHERE currency=? LIMIT 1', (code, code)).fetchone()
        if used and used['decimals'] != decimals:
            fail(f'{code} 已有账目，不能修改其小数位')
        codes.add(code)
        clean.append({'code': code, 'decimals': decimals, 'name': text_value(cur.get('name', code), '币种名称', 20),
                      'symbol': text_value(cur.get('symbol', code), '货币符号', 8)})
    for row in conn.execute("SELECT currency FROM entries UNION SELECT currency FROM payments UNION SELECT fx_currency FROM entries WHERE fx_currency<>''"):
        if row['currency'] not in codes:
            fail(f'{row["currency"]} 已有账目，不能移除')
    default = body.get('default_currency', conf['default_currency'])
    reference = body.get('reference_currency', conf['reference_currency'])
    if default not in codes or reference not in codes:
        fail('默认币种和汇总币种必须在启用币种中')
    chats = body.get('telegram_allowed_chats', conf['telegram_allowed_chats'])
    if not isinstance(chats, list) or len(chats) > 30 or any(not re.fullmatch(r'-?\d{1,20}', str(c)) for c in chats):
        fail('Telegram 群组 ID 格式不正确')
    feishu_chats = body.get('feishu_allowed_chats', conf['feishu_allowed_chats'])
    if not isinstance(feishu_chats, list) or len(feishu_chats) > 30 or any(
            not isinstance(c, str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,100}', c) for c in feishu_chats):
        fail('飞书群组 ID 格式不正确')
    values = dict(team_name=name, currencies=clean, default_currency=default, reference_currency=reference,
                  telegram_allowed_chats=list(dict.fromkeys(str(c) for c in chats)),
                  feishu_allowed_chats=list(dict.fromkeys(feishu_chats)))
    for key, value in values.items():
        set_setting(conn, key, value)
    return settings(conn)
