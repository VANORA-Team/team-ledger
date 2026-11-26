import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from .db import db, settings
from .domain import currency, fail, minor_amount, signed_minor_amount
from .config import today
from . import reimbursement_plan


def fetch_rates(base, codes):
    url = os.getenv('FX_API_URL', 'https://api.frankfurter.app/latest')
    is_v2 = '/v2/' in url
    query = urllib.parse.urlencode({'base' if is_v2 else 'from': base, 'quotes' if is_v2 else 'to': ','.join(sorted(codes))})
    request = urllib.request.Request(url + ('&' if '?' in url else '?') + query, headers={'User-Agent': 'TogetherLedger/1.0'})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read(1_000_000), parse_float=Decimal)
    if isinstance(payload, dict) and payload.get('base') == base and isinstance(payload.get('rates'), dict):
        payload = [{'base':base,'quote':quote,'rate':rate,'date':payload.get('date')} for quote,rate in payload['rates'].items()]
    if not isinstance(payload, list):
        raise ValueError('Invalid exchange-rate response')
    result = {}
    for row in payload:
        if row.get('base') == base and row.get('quote') in codes:
            rate = Decimal(str(row['rate']))
            if not rate.is_finite() or rate <= 0:
                raise ValueError('Invalid exchange rate')
            from datetime import date
            rate_date = date.fromisoformat(str(row['date'])).isoformat()
            result[row['quote']] = {'rate': str(Decimal(1) / rate), 'date': rate_date}
    if set(result) != set(codes):
        raise ValueError('Some currencies are unavailable')
    return result


def cached_rates(base, codes):
    """Read-only rates for use inside an open write transaction (never fetches)."""
    codes = set(codes) - {base}
    result = {'base': base, 'rates': {base: {'rate': '1', 'date': None}},
              'source': 'Frankfurter · 每日参考汇率', 'stale': False, 'missing': [], 'warning': None}
    if not codes:
        return result
    with db() as conn:
        row = conn.execute('SELECT * FROM fx_cache WHERE base=?', (base,)).fetchone()
    cached = json.loads(row['payload']) if row else {}
    fresh = row and time.time() - row['fetched_at'] < float(os.getenv('FX_CACHE_HOURS', '12')) * 3600
    result['rates'].update({c: cached[c] for c in codes if c in cached})
    result['missing'] = sorted(codes - set(cached))
    result['stale'] = not fresh
    if not fresh:
        result['warning'] = '汇率缓存已过期或尚未获取，跨币种金额仅供参考。'
    elif result['missing']:
        result['warning'] = '汇率缓存缺少部分币种，跨币种金额暂不可用。'
    return result


def rates_for(base, codes, force=False):
    codes = set(codes) - {base}
    result = {'base': base, 'rates': {base: {'rate': '1', 'date': None}},
              'source': 'Frankfurter · 每日参考汇率', 'stale': False, 'missing': [], 'warning': None}
    if not codes:
        return result
    with db() as conn:
        row = conn.execute('SELECT * FROM fx_cache WHERE base=?', (base,)).fetchone()
    cached = json.loads(row['payload']) if row else {}
    fresh = row and time.time() - row['fetched_at'] < float(os.getenv('FX_CACHE_HOURS', '12')) * 3600
    if fresh and codes <= set(cached) and not force:
        result['rates'].update({c: cached[c] for c in codes})
        return result
    try:
        rates = fetch_rates(base, codes)
        # A cache record represents a single fetch; do not refresh timestamps on old pairs.
        with db(write=True) as conn:
            conn.execute('INSERT OR REPLACE INTO fx_cache VALUES (?,?,?)', (base, json.dumps(rates), int(time.time())))
        result['rates'].update(rates)
    except Exception:
        result['stale'] = bool(cached)
        result['rates'].update({c: cached[c] for c in codes if c in cached})
        result['missing'] = sorted(codes - set(cached))
        result['warning'] = '汇率接口暂不可用，正在使用上次缓存的参考汇率。' if cached else '汇率接口暂不可用，跨币种计算暂停。原币种账目仍可正常记录。'
    return result


def manual_rate_for(item, parent_entry=None):
    """Return (Decimal_rate, fx_currency) if item (or parent entry for same-currency payment) has a manual FX rate."""
    if parent_entry is not None:
        if item.get('currency') == parent_entry.get('currency') and parent_entry.get('fx_rate') and parent_entry.get('fx_currency'):
            return Decimal(str(parent_entry['fx_rate'])), parent_entry['fx_currency']
        return None, ''
    if item.get('fx_rate') and item.get('fx_currency'):
        return Decimal(str(item['fx_rate'])), item['fx_currency']
    return None, ''


def effective_rate(item, base, rates_map, *, parent_entry=None):
    """Resolve conversion rate from item's currency to base. Returns (Decimal_rate_or_None, is_manual)."""
    code = item['currency']
    if code == base:
        return Decimal(1), False
    m_rate, m_cur = manual_rate_for(item, parent_entry=parent_entry)
    if m_rate is not None:
        if m_cur == base:
            return m_rate, True
        bridge = (rates_map or {}).get(m_cur)
        if bridge:
            return m_rate * Decimal(str(bridge['rate'])), True
        return None, True
    row = (rates_map or {}).get(code)
    if row:
        return Decimal(str(row['rate'])), False
    return None, False


def required_codes(entries, payments, base, extra_codes=None):
    """Return the set of currency codes that need external FX rates to convert into base."""
    entry_by_id = {e['id']: e for e in entries}
    codes = set(extra_codes or ())
    for e in entries:
        if e['status'] not in ('posted', 'pending', 'reimbursed'):
            continue
        if e['currency'] == base:
            continue
        m_rate, m_cur = manual_rate_for(e)
        if m_rate is not None:
            if m_cur != base:
                codes.add(m_cur)
        else:
            codes.add(e['currency'])
    for p in payments or ():
        if p['currency'] == base:
            continue
        parent = entry_by_id.get(p.get('entry_id'))
        m_rate, m_cur = manual_rate_for(p, parent_entry=parent)
        if m_rate is not None:
            if m_cur != base:
                codes.add(m_cur)
        else:
            codes.add(p['currency'])
    codes.discard(base)
    return codes


def _income_type(entry):
    return 'capital' if entry.get('income_type') == 'capital' else 'operating'


def _fmt_dec(value, decimals):
    quantum = Decimal(1).scaleb(-decimals)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f'.{decimals}f')


def _currency_accounts(entries, payments, conf, sim_deltas=None):
    cur_meta = {c['code']: c for c in conf['currencies']}
    conf_order = {c['code']: idx for idx, c in enumerate(conf['currencies'])}
    sim_deltas = sim_deltas or {}
    buckets = {}

    def slot(code, decimals=None):
        if code not in buckets:
            dec = decimals if decimals is not None else cur_meta.get(code, {}).get('decimals', 2)
            buckets[code] = {
                'code': code,
                'decimals': dec,
                'operating_income': Decimal(0),
                'capital_income': Decimal(0),
                'public_paid': Decimal(0),
                'paid': Decimal(0),
                'due': Decimal(0),
                'income_fees': Decimal(0),
                'public_fees': Decimal(0),
                'advance_fees': Decimal(0),
                'operating_count': 0,
                'capital_count': 0,
                'public_count': 0,
                'payment_count': 0,
                'due_count': 0,
            }
        elif decimals is not None:
            buckets[code]['decimals'] = decimals
        return buckets[code]

    for e in entries:
        if e['status'] in ('draft', 'voided'):
            continue
        amt = Decimal(e['amount_minor']) / (10 ** e['decimals'])
        fee = Decimal(e.get('fee_minor') or 0) / (10 ** e['decimals'])
        if e['kind'] == 'income' and e['status'] == 'posted':
            b = slot(e['currency'], e['decimals'])
            b['income_fees'] += fee
            if _income_type(e) == 'capital':
                b['capital_income'] += amt
                b['capital_count'] += 1
            else:
                b['operating_income'] += amt
                b['operating_count'] += 1
        elif e['kind'] == 'expense' and e.get('expense_type') == 'public' and e['status'] == 'posted':
            b = slot(e['currency'], e['decimals'])
            b['public_paid'] += amt
            b['public_fees'] += fee
            b['public_count'] += 1
        elif e['kind'] == 'expense' and e.get('expense_type', 'advance') == 'advance':
            if fee > 0:
                b = slot(e['currency'], e['decimals'])
                b['advance_fees'] += fee
            if e['status'] == 'pending':
                rem = Decimal(e['amount_minor'] - e['reimbursed_minor']) / (10 ** e['decimals'])
                if rem > 0:
                    b = slot(e['currency'], e['decimals'])
                    b['due'] += rem
                    b['due_count'] += 1

    for p in payments:
        b = slot(p['currency'], p['decimals'])
        b['paid'] += Decimal(p['amount_minor']) / (10 ** p['decimals'])
        b['payment_count'] += 1

    for code, delta in sim_deltas.items():
        if any(delta.get(k, Decimal(0)) != 0 for k in ('operating', 'capital', 'public_expense')):
            slot(code)

    ordered_codes = sorted(buckets.keys(), key=lambda c: (conf_order.get(c, 999), c))
    rows = []
    for code in ordered_codes:
        b = buckets[code]
        dec = b['decimals']
        income = b['operating_income'] + b['capital_income']
        outflow = b['public_paid'] + b['paid']
        balance = income - outflow
        operating_net = b['operating_income'] - outflow
        net_after_due = balance - b['due']
        operating_net_after_due = operating_net - b['due']
        delta = sim_deltas.get(code) or {}
        sim_op = delta.get('operating', Decimal(0))
        sim_cap = delta.get('capital', Decimal(0))
        sim_pub = delta.get('public_expense', Decimal(0))
        projected_balance = balance + sim_op + sim_cap - sim_pub
        projected_after_due = projected_balance - b['due']
        projected_operating_net = operating_net + sim_op - sim_pub
        projected_operating_net_after_due = projected_operating_net - b['due']
        total_fees = b['income_fees'] + b['public_fees'] + b['advance_fees']
        rows.append({
            'code': code,
            'decimals': dec,
            'operating_income': _fmt_dec(b['operating_income'], dec),
            'capital_income': _fmt_dec(b['capital_income'], dec),
            'income': _fmt_dec(income, dec),
            'public_paid': _fmt_dec(b['public_paid'], dec),
            'paid': _fmt_dec(b['paid'], dec),
            'outflow': _fmt_dec(outflow, dec),
            'balance': _fmt_dec(balance, dec),
            'operating_net': _fmt_dec(operating_net, dec),
            'operating_profit': _fmt_dec(operating_net, dec),
            'due': _fmt_dec(b['due'], dec),
            'net_after_due': _fmt_dec(net_after_due, dec),
            'operating_net_after_due': _fmt_dec(operating_net_after_due, dec),
            'operating_profit_after_due': _fmt_dec(operating_net_after_due, dec),
            'projected_balance': _fmt_dec(projected_balance, dec),
            'projected_after_due': _fmt_dec(projected_after_due, dec),
            'projected_operating_profit': _fmt_dec(projected_operating_net, dec),
            'projected_operating_profit_after_due': _fmt_dec(projected_operating_net_after_due, dec),
            'income_fees': _fmt_dec(b['income_fees'], dec),
            'public_fees': _fmt_dec(b['public_fees'], dec),
            'advance_fees': _fmt_dec(b['advance_fees'], dec),
            'total_fees': _fmt_dec(total_fees, dec),
            'operating_count': b['operating_count'],
            'capital_count': b['capital_count'],
            'public_count': b['public_count'],
            'payment_count': b['payment_count'],
            'due_count': b['due_count'],
        })
    return rows


def _shareholder_contributions(capital_incomes, conf, fx_data=None, complete=False, base_decimals=2, total_capital=Decimal(0)):
    cur_meta = {c['code']: c for c in conf['currencies']}
    groups = {}
    for e in capital_incomes:
        uid = e['payer_id']
        if uid not in groups:
            groups[uid] = {
                'id': uid,
                'name': e.get('payer_name') or f'成员 #{uid}',
                'count': 0,
                'currencies': {},
                'total_dec': Decimal(0),
            }
        g = groups[uid]
        g['count'] += 1
        amt = Decimal(e['amount_minor']) / (10 ** e['decimals'])
        g['currencies'][e['currency']] = g['currencies'].get(e['currency'], Decimal(0)) + amt
        if complete and fx_data:
            rate, _ = effective_rate(e, fx_data['base'], fx_data['rates'])
            if rate is not None:
                g['total_dec'] += amt * rate

    items = []
    for g in groups.values():
        by_currency = {
            code: _fmt_dec(val, cur_meta.get(code, {}).get('decimals', 2))
            for code, val in sorted(g['currencies'].items())
        }
        share = None
        total_str = None
        if complete:
            total_str = _fmt_dec(g['total_dec'], base_decimals)
            if total_capital > 0:
                pct = (g['total_dec'] / total_capital * Decimal(100)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                share = format(pct, '.1f')
        items.append({
            'id': g['id'],
            'name': g['name'],
            'count': g['count'],
            'by_currency': by_currency,
            'total': total_str,
            'share': share,
            '_sort': g['total_dec'] if complete else Decimal(g['count']),
        })
    items.sort(key=lambda x: (-x['_sort'], x['id']))
    for item in items:
        item.pop('_sort', None)
    return items


def calculate(entries, conf, fx, scenario=None, payments=None, reimbursement=None, extra_codes=None):
    base = fx['base']
    base_decimals = currency(conf, base)['decimals']
    quantum = Decimal(1).scaleb(-base_decimals)

    def formatted(value):
        return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f'.{base_decimals}f')

    payments = payments or []
    entry_by_id = {e['id']: e for e in entries}
    all_extra = set(extra_codes or ())
    if scenario:
        all_extra.add(scenario['currency'])
    needed = required_codes(entries, payments, base, all_extra)
    actual_missing = sorted(needed - set(fx['rates'].keys()))
    manual_fx_count = sum(
        1 for e in entries
        if e['status'] in ('posted', 'pending', 'reimbursed')
        and e['currency'] != base and e.get('fx_rate') and e.get('fx_currency')
    )

    def convert(entry, units=None):
        rate, _ = effective_rate(entry, base, fx['rates'])
        return Decimal(entry['amount_minor'] if units is None else units) / (10 ** entry['decimals']) * rate

    def convert_payment(payment):
        rate, _ = effective_rate(payment, base, fx['rates'], parent_entry=entry_by_id.get(payment.get('entry_id')))
        return Decimal(payment['amount_minor']) / (10 ** payment['decimals']) * rate

    posted_incomes = [e for e in entries if e['kind'] == 'income' and e['status'] == 'posted']
    operating_incomes = [e for e in posted_incomes if _income_type(e) == 'operating']
    capital_incomes = [e for e in posted_incomes if _income_type(e) == 'capital']
    public_expenses = [e for e in entries if e['kind'] == 'expense' and e.get('expense_type') == 'public' and e['status'] == 'posted']
    balances = {}
    for entry, factor in [(e, 1) for e in posted_incomes] + [(p, -1) for p in payments + public_expenses]:
        key = entry['currency']
        balances[key] = balances.get(key, Decimal(0)) + factor * Decimal(entry['amount_minor']) / (10 ** entry['decimals'])
    choice = reimbursement_plan.normalize(entries, conf, base, reimbursement)
    currency_accounts = _currency_accounts(entries, payments, conf)
    common = {'reimbursement': choice, 'base': base, 'fx': fx, 'manual_fx_count': manual_fx_count,
              'balances': {k: str(v) for k, v in balances.items()},
              'currency_accounts': currency_accounts,
              'complete': not actual_missing, 'scenario': scenario, 'members': [],
              'shareholder_contributions': _shareholder_contributions(capital_incomes, conf)}
    if actual_missing:
        return common
    operating_income = sum((convert(e) for e in operating_incomes), Decimal(0))
    capital_income = sum((convert(e) for e in capital_incomes), Decimal(0))
    income = operating_income + capital_income
    today_str = today()
    today_operating_income = sum((convert(e) for e in operating_incomes if e['date'] == today_str), Decimal(0))
    today_capital_income = sum((convert(e) for e in capital_incomes if e['date'] == today_str), Decimal(0))
    today_income = today_operating_income + today_capital_income
    paid = sum((convert_payment(p) for p in payments), Decimal(0))
    public_paid = sum((convert(e) for e in public_expenses), Decimal(0))
    total_outflow = paid + public_paid
    due_entries = sorted((e for e in entries if e['kind'] == 'expense' and e.get('expense_type', 'advance') == 'advance' and e['status'] == 'pending'), key=lambda e: (e['date'], e['id']))
    active_advances = [e for e in entries if e['kind'] == 'expense' and e.get('expense_type', 'advance') == 'advance' and e['status'] in ('pending', 'reimbursed')]
    income_fees = sum((convert(e, e.get('fee_minor') or 0) for e in posted_incomes), Decimal(0))
    public_fees = sum((convert(e, e.get('fee_minor') or 0) for e in public_expenses), Decimal(0))
    advance_fees = sum((convert(e, e.get('fee_minor') or 0) for e in active_advances), Decimal(0))
    expense_fees = public_fees + advance_fees
    total_fees = income_fees + expense_fees
    due = sum((convert(e, e['amount_minor'] - e['reimbursed_minor']) for e in due_entries), Decimal(0))
    extra = Decimal(0)
    if scenario:
        cur = currency(conf, scenario['currency'])
        units = minor_amount(scenario['amount'], cur['decimals'])
        extra = Decimal(units) / (10 ** cur['decimals']) * Decimal(fx['rates'][cur['code']]['rate'])
    actual_cash = income - total_outflow
    public_balance = actual_cash
    operating_net = operating_income - total_outflow
    operating_net_after_due = operating_net - due
    cash = actual_cash + extra
    actual_net_after_due = actual_cash - due
    net_after_due = cash - due
    available = max(cash, Decimal(0))
    selected_ids = set(choice['entry_ids']) if choice['mode'] == 'manual' else {e['id'] for e in due_entries}
    selected_due = sum((convert(e, e['amount_minor'] - e['reimbursed_minor']) for e in due_entries if e['id'] in selected_ids), Decimal(0))
    reserve = reimbursement_plan.limit(choice, available, selected_due)
    payable = reserve
    groups = {}
    left = payable
    # Unselected projects stay visible and outstanding, with no allocation.
    for entry in due_entries:
        key = entry['payer_id']
        if key not in groups:
            groups[key] = {'id': key, 'name': entry['payer_name'], 'due': Decimal(0), 'suggested': Decimal(0), 'entries': []}
        amount = convert(entry, entry['amount_minor'] - entry['reimbursed_minor'])
        groups[key]['due'] += amount
        selected = entry['id'] in selected_ids
        portion = min(left, amount) if selected else Decimal(0)
        groups[key]['suggested'] += portion
        left -= portion
        original = Decimal(entry['amount_minor'] - entry['reimbursed_minor']) / (10 ** entry['decimals'])
        gross_minor = entry.get('gross_amount_minor') or entry['amount_minor']
        fee_minor = entry.get('fee_minor') or 0
        groups[key]['entries'].append({
            'id': entry['id'],
            'selected': selected,
            'description': entry['description'],
            'date': entry['date'],
            'currency': entry['currency'],
            'amount': format(original, f'.{entry["decimals"]}f'),
            'original_amount': format(Decimal(entry['amount_minor']) / (10 ** entry['decimals']), f'.{entry["decimals"]}f'),
            'gross_amount': format(Decimal(gross_minor) / (10 ** entry['decimals']), f'.{entry["decimals"]}f'),
            'fee_mode': entry.get('fee_mode') or 'none',
            'fee_rate': entry.get('fee_rate') or '',
            'fee_minor': fee_minor,
            'fee_amount': format(Decimal(fee_minor) / (10 ** entry['decimals']), f'.{entry["decimals"]}f'),
            'fx_mode': 'manual' if (entry.get('fx_rate') and entry.get('fx_currency')) else 'auto',
            'fx_rate': entry.get('fx_rate') or '',
            'fx_currency': entry.get('fx_currency') or '',
            'reimbursed_amount': format(Decimal(entry['reimbursed_minor']) / (10 ** entry['decimals']), f'.{entry["decimals"]}f'),
            'settlement': formatted(amount),
            'suggested': formatted(portion),
            'fully_covered': portion >= amount,
        })
    common.update({k: formatted(v) for k, v in dict(
        income=income, operating_income=operating_income, capital_income=capital_income,
        today_income=today_income, today_operating_income=today_operating_income, today_capital_income=today_capital_income,
        paid=paid, public_paid=public_paid, total_outflow=total_outflow,
        income_fees=income_fees, public_fees=public_fees, advance_fees=advance_fees,
        expense_fees=expense_fees, total_fees=total_fees,
        actual_cash=actual_cash, public_balance=public_balance, cash=cash,
        due=due, selected_due=selected_due, reserve=reserve, payable=payable,
        remaining=cash - reserve, unplanned=due - reserve,
        actual_net_after_due=actual_net_after_due, net_after_due=net_after_due,
        operating_net=operating_net, operating_net_after_due=operating_net_after_due,
        operating_profit=operating_net, operating_profit_after_due=operating_net_after_due,
        shortage=max(due - available, Decimal(0)), extra=extra).items()})
    common['members'] = [{**g, **{k: formatted(g[k]) for k in ('due', 'suggested')}} for g in groups.values()]
    common['shareholder_contributions'] = _shareholder_contributions(
        capital_incomes, conf, fx, complete=True, base_decimals=base_decimals, total_capital=capital_income
    )
    return common


def _parse_as_of(value):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        fail('截止日期格式不正确，请使用 YYYY-MM-DD')
    if parsed.year < 2000 or parsed.year > 2100:
        fail('截止日期超出允许范围')
    return parsed.isoformat()


def _snapshot_as_of(entries, payments, as_of):
    if not as_of:
        return entries, payments
    filtered_payments = [p for p in payments if str(p.get('created_at') or '')[:10] <= as_of]
    paid_by_entry = {}
    for p in filtered_payments:
        eid = p['entry_id']
        paid_by_entry[eid] = paid_by_entry.get(eid, 0) + int(p['entry_amount_minor'])
    filtered_entries = []
    for e in entries:
        if e['date'] > as_of:
            continue
        item = dict(e)
        if item['kind'] == 'expense' and item.get('expense_type', 'advance') == 'advance' and item['status'] in ('pending', 'reimbursed'):
            reimb = min(paid_by_entry.get(item['id'], 0), item['amount_minor'])
            item['reimbursed_minor'] = reimb
            item['status'] = 'reimbursed' if reimb >= item['amount_minor'] else 'pending'
        filtered_entries.append(item)
    return filtered_entries, filtered_payments


def _parse_adjustment(conf, raw, label):
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        fail(f'{label}格式不正确')
    amount_raw = raw.get('amount', '')
    if amount_raw in (None, ''):
        return None
    code = str(raw.get('currency') or conf['default_currency']).strip().upper()
    cur = currency(conf, code)
    units = minor_amount(amount_raw, cur['decimals'], allow_zero=True)
    if units == 0:
        return None
    dec = Decimal(units) / (10 ** cur['decimals'])
    return {'amount': _fmt_dec(dec, cur['decimals']), 'currency': cur['code'], 'decimals': cur['decimals'], 'decimal': dec}


def _parse_bank_actual(conf, base, raw):
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        fail('银行公户实际余额格式不正确')
    amount_raw = raw.get('amount', '')
    if amount_raw in (None, ''):
        return None
    scope = str(raw.get('scope') or 'currency').strip().lower()
    if scope not in ('currency', 'consolidated'):
        fail('对账口径仅支持按单币种或折算汇总')
    default_code = base if scope == 'consolidated' else conf['reference_currency']
    code = str(raw.get('currency') or default_code).strip().upper()
    cur = currency(conf, code)
    if scope == 'consolidated' and cur['code'] != base:
        fail(f'按参考币种汇总对账时，银行余额币种需与汇总参考币种 {base} 一致')
    units = signed_minor_amount(amount_raw, cur['decimals'])
    dec = Decimal(units) / (10 ** cur['decimals'])
    return {'amount': _fmt_dec(dec, cur['decimals']), 'currency': cur['code'], 'decimals': cur['decimals'], 'decimal': dec, 'scope': scope}


def public_balance_report(base=None, *, as_of='', adjustments=None, bank_actual=None, include_pending=True, force=False):
    as_of_iso = _parse_as_of(as_of)
    if not isinstance(include_pending, bool):
        fail('待报销扣除选项格式不正确')
    with db() as conn:
        conf = settings(conn)
        base = (base or conf['reference_currency']).strip().upper()
        base_cur = currency(conf, base)
        entries = [dict(e) for e in conn.execute('SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id')]
        payments = [dict(p) for p in conn.execute('SELECT * FROM payments')]
    if adjustments is not None and not isinstance(adjustments, dict):
        fail('试算参数格式不正确')
    adj_in = adjustments or {}
    parsed_adj = {
        'operating_income': _parse_adjustment(conf, adj_in.get('operating_income'), '模拟营业收入'),
        'capital_income': _parse_adjustment(conf, adj_in.get('capital_income'), '模拟公户转入'),
        'public_expense': _parse_adjustment(conf, adj_in.get('public_expense'), '模拟公账支出'),
    }
    parsed_bank = _parse_bank_actual(conf, base, bank_actual)
    entries, payments = _snapshot_as_of(entries, payments, as_of_iso)
    extra_codes = {item['currency'] for item in parsed_adj.values() if item}
    codes = required_codes(entries, payments, base, extra_codes)
    fx_rates = rates_for(base, codes, force)
    result = calculate(entries, conf, fx_rates, payments=payments, extra_codes=extra_codes)

    sim_deltas = {}
    for key, field in (('operating_income', 'operating'), ('capital_income', 'capital'), ('public_expense', 'public_expense')):
        item = parsed_adj[key]
        if item:
            slot = sim_deltas.setdefault(item['currency'], {'operating': Decimal(0), 'capital': Decimal(0), 'public_expense': Decimal(0)})
            slot[field] += item['decimal']
    result['currency_accounts'] = _currency_accounts(entries, payments, conf, sim_deltas=sim_deltas)

    has_adjustments = any(parsed_adj.values())
    adj_public = {
        key: ({'amount': item['amount'], 'currency': item['currency']} if item else None)
        for key, item in parsed_adj.items()
    }
    simulation = {'has_adjustments': has_adjustments, 'include_pending': include_pending, 'adjustments': adj_public}
    base_dec = base_cur['decimals']
    if result['complete']:
        def conv_adj(item):
            if not item:
                return Decimal(0)
            return item['decimal'] * Decimal(fx_rates['rates'][item['currency']]['rate'])

        sim_op = conv_adj(parsed_adj['operating_income'])
        sim_cap = conv_adj(parsed_adj['capital_income'])
        sim_pub = conv_adj(parsed_adj['public_expense'])
        sim_income = sim_op + sim_cap
        sim_net_delta = sim_income - sim_pub
        book_balance = Decimal(result['actual_cash'])
        due_dec = Decimal(result['due'])
        op_inc_dec = Decimal(result['operating_income'])
        cap_inc_dec = Decimal(result['capital_income'])
        outflow_dec = Decimal(result['total_outflow'])
        projected_balance = book_balance + sim_net_delta
        projected_after_due = projected_balance - due_dec
        final_balance = projected_after_due if include_pending else projected_balance
        projected_shortage = max(due_dec - max(projected_balance, Decimal(0)), Decimal(0))
        projected_op_income = op_inc_dec + sim_op
        projected_cap_income = cap_inc_dec + sim_cap
        projected_op_net = projected_op_income - (outflow_dec + sim_pub)
        projected_op_net_after_due = projected_op_net - due_dec
        simulation.update({
            'sim_operating': _fmt_dec(sim_op, base_dec),
            'sim_capital': _fmt_dec(sim_cap, base_dec),
            'sim_income': _fmt_dec(sim_income, base_dec),
            'sim_public_expense': _fmt_dec(sim_pub, base_dec),
            'sim_net_delta': _fmt_dec(sim_net_delta, base_dec),
            'book_balance': _fmt_dec(book_balance, base_dec),
            'projected_balance': _fmt_dec(projected_balance, base_dec),
            'due': _fmt_dec(due_dec, base_dec),
            'projected_after_due': _fmt_dec(projected_after_due, base_dec),
            'final_balance': _fmt_dec(final_balance, base_dec),
            'projected_shortage': _fmt_dec(projected_shortage, base_dec),
            'projected_operating_income': _fmt_dec(projected_op_income, base_dec),
            'projected_capital_income': _fmt_dec(projected_cap_income, base_dec),
            'projected_operating_net': _fmt_dec(projected_op_net, base_dec),
            'projected_operating_net_after_due': _fmt_dec(projected_op_net_after_due, base_dec),
            'projected_operating_profit': _fmt_dec(projected_op_net, base_dec),
            'projected_operating_profit_after_due': _fmt_dec(projected_op_net_after_due, base_dec),
        })

    reconciliation = None
    if parsed_bank:
        rec_dec = parsed_bank['decimals']
        bank_val = parsed_bank['decimal']
        if parsed_bank['scope'] == 'currency':
            account = next((a for a in result['currency_accounts'] if a['code'] == parsed_bank['currency']), None)
            book_val = Decimal(account['balance']) if account else Decimal(0)
            diff = bank_val - book_val
            status = 'matched' if diff == 0 else ('surplus' if diff > 0 else 'deficit')
            messages = {
                'matched': '账实相符：银行公户实际余额与账面公户结余完全一致。',
                'surplus': '银行余额高于账面结余，可能存在尚未登记的营业收入、公户转入、银行利息或期初结余。',
                'deficit': '银行余额低于账面结余，可能存在尚未登记的公账支出、银行手续费或已付款未登记的报销。',
            }
            reconciliation = {
                'scope': 'currency',
                'currency': parsed_bank['currency'],
                'decimals': rec_dec,
                'bank_balance': _fmt_dec(bank_val, rec_dec),
                'book_balance': _fmt_dec(book_val, rec_dec),
                'difference': _fmt_dec(diff, rec_dec),
                'status': status,
                'message': messages[status],
            }
        else:
            if not result['complete']:
                reconciliation = {
                    'scope': 'consolidated',
                    'currency': base,
                    'decimals': base_dec,
                    'bank_balance': _fmt_dec(bank_val, base_dec),
                    'book_balance': None,
                    'difference': None,
                    'status': 'unavailable',
                    'message': '当前汇率不完整，暂无法按参考币种折算汇总对账；可切换为按单个原币种精确对账。',
                }
            else:
                book_val = Decimal(result['actual_cash'])
                diff = bank_val - book_val
                status = 'matched' if diff == 0 else ('surplus' if diff > 0 else 'deficit')
                messages = {
                    'matched': '折算口径下银行余额与账面公户结余一致（多币种时建议按各原币种分别核对）。',
                    'surplus': '折算口径下银行余额高于账面结余，可能存在未入账营业收入、公户转入、期初结余或汇率差异。',
                    'deficit': '折算口径下银行余额低于账面结余，可能存在未入账公账支出、未登记报销付款或汇率差异。',
                }
                reconciliation = {
                    'scope': 'consolidated',
                    'currency': base,
                    'decimals': base_dec,
                    'bank_balance': _fmt_dec(bank_val, base_dec),
                    'book_balance': _fmt_dec(book_val, base_dec),
                    'difference': _fmt_dec(diff, base_dec),
                    'status': status,
                    'message': messages[status],
                }

    return {
        **result,
        'as_of': as_of_iso,
        'include_pending': include_pending,
        'simulation': simulation,
        'reconciliation': reconciliation,
    }


def report(base=None, scenario=None, force=False, reimbursement=None):
    with db() as conn:
        conf = settings(conn)
        base = base or conf['reference_currency']
        currency(conf, base)
        entries = [dict(e) for e in conn.execute('SELECT e.*,u.name payer_name FROM entries e JOIN users u ON u.id=e.payer_id')]
        payments = [dict(p) for p in conn.execute('SELECT * FROM payments')]
    # Ignore drafts, voided entries and entries covered by manual rates when looking up external rates.
    extra_codes = set()
    if scenario:
        currency(conf, scenario['currency'])
        minor_amount(scenario['amount'], currency(conf, scenario['currency'])['decimals'])
        extra_codes.add(scenario['currency'])
    codes = required_codes(entries, payments, base, extra_codes)
    # Reject invalid choices before requesting external exchange rates.
    reimbursement = reimbursement_plan.normalize(entries, conf, base, reimbursement)
    return calculate(entries, conf, rates_for(base, codes, force), scenario, payments, reimbursement)
