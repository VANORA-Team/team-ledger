"""Compact, privacy-safe ledger context for the AI planner."""
import json
from decimal import Decimal
from . import ai, config, fx

DEFAULT_LIMIT = ai.DEFAULT_CONTEXT_ENTRIES
DEFAULT_CHARS = ai.DEFAULT_CONTEXT_CHARS


def _limit(conn=None):
    return ai.load_config(conn)['context_entries']


def _char_cap(conn=None):
    return ai.load_config(conn)['context_chars']


def _amount(row, units):
    return format(Decimal(units) / (10 ** row['decimals']), f'.{row["decimals"]}f')


def _compact(row):
    item = {
        'id': row['id'], 'date': row['date'], 'kind': row['kind'], 'expense_type': row['expense_type'],
        'income_type': row['income_type'],
        'amount': _amount(row, row['amount_minor']), 'currency': row['currency'],
        'description': row['description'][:120], 'payer': row['payer_name'],
        'status': row['status'],
    }
    fee_mode = row['fee_mode'] or 'none'
    fee_minor = row['fee_minor'] or 0
    if fee_mode != 'none' or fee_minor > 0:
        gross_minor = row['gross_amount_minor'] or row['amount_minor']
        item['gross_amount'] = _amount(row, gross_minor)
        item['fee_mode'] = fee_mode
        if row['fee_rate']:
            item['fee_rate'] = row['fee_rate']
        item['fee_amount'] = _amount(row, fee_minor)
    if row['kind'] == 'expense' and row['expense_type'] == 'advance':
        item['remaining'] = _amount(row, row['amount_minor'] - row['reimbursed_minor'])
        if row['reimbursed_minor']:
            item['reimbursed'] = _amount(row, row['reimbursed_minor'])
    if row['note']:
        item['note'] = row['note'][:100]
    return item


def _rows(conn, where='', params=(), limit=None):
    sql = ('SELECT e.id,e.kind,e.expense_type,e.income_type,e.amount_minor,e.gross_amount_minor,'
           'e.fee_mode,e.fee_rate,e.fee_minor,e.currency,e.decimals,e.description,e.date,'
           'e.status,e.reimbursed_minor,e.note,u.name payer_name '
           'FROM entries e JOIN users u ON u.id=e.payer_id ' + where +
           ' ORDER BY e.date DESC,e.id DESC')
    values = list(params)
    if limit:
        sql += ' LIMIT ?'
        values.append(limit)
    return conn.execute(sql, values).fetchall()


def entries(conn, limit=None):
    limit = limit or _limit(conn)
    # Every actionable row can be targeted by edit/void/pay; recent
    # history makes conversational references ("the taxi one") resolvable.
    actionable = _rows(conn, "WHERE e.status IN ('draft','pending','posted')", limit=300)
    recent = _rows(conn, '', limit=limit)
    merged, seen = [], set()
    for row in list(actionable) + list(recent):
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        merged.append(_compact(row))
    return merged


def members(conn):
    return [dict(r) for r in conn.execute('SELECT id,name FROM users WHERE active=1 ORDER BY id')]


def currencies(conf):
    return [{k: c[k] for k in ('code', 'name', 'symbol', 'decimals')} for c in conf['currencies']]


def funds(conf):
    try:
        report = fx.report()
    except Exception:
        return {'complete': False, 'warning': '汇率暂不可用'}
    if not report.get('complete'):
        return {'complete': False, 'base': report.get('base'),
                'warning': report.get('fx', {}).get('warning') or '汇率不完整'}
    keys = ('base', 'income', 'operating_income', 'capital_income', 'paid', 'public_paid',
            'total_outflow', 'income_fees', 'public_fees', 'advance_fees', 'total_fees',
            'public_balance', 'cash', 'due', 'net_after_due', 'operating_net',
            'operating_net_after_due', 'operating_profit', 'operating_profit_after_due',
            'reserve', 'payable', 'remaining', 'shortage', 'balances')
    return {key: report.get(key) for key in keys}


def build(conn, conf, user, limit=None):
    ai_cfg = ai.load_config(conn)
    context = {
        'timezone': config.TIMEZONE,
        'current_user': {'id': user['id'], 'name': user['name']},
        'currencies': currencies(conf),
        'members': members(conn),
        'entries': entries(conn, limit or ai_cfg['context_entries']),
        'funds': funds(conf),
    }
    cap = ai_cfg['context_chars']
    while context['entries'] and len(json.dumps(context, ensure_ascii=False)) > cap:
        context['entries'].pop()
    return context
