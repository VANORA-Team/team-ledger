"""Tests for manual foreign-currency exchange rates at bookkeeping time."""
import io
import re
import secrets
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app import ai_context, backups, bot, config, fx
from app.db import db, init_db
from app.funds_plan import build_plan
from tests import test_bot_ui as ui, test_feishu as feishu_fixtures, test_ledger as base


class ManualFxRateTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    action = base.LedgerTest.action
    new_member = base.LedgerTest.new_member
    login_as = base.LedgerTest.login_as
    link_bot = base.LedgerTest.link_bot
    message = staticmethod(base.LedgerTest.message)
    callback = staticmethod(base.LedgerTest.callback)
    click = ui.TelegramUiTest.click
    draft = ui.TelegramUiTest.draft
    last_message = ui.TelegramUiTest.last_message
    feishu_message = feishu_fixtures.FeishuTest.message
    feishu_process = feishu_fixtures.FeishuTest.process

    def report(self, query=''):
        response = self.client.get('/api/report' + query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def public_balance(self, body=None):
        if body is None:
            response = self.client.get('/api/public-balance')
        else:
            response = self.post('/public-balance/calculate', body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ------------------------------------------------------------------
    # 1. Core API: CRUD, validation, fees interaction & idempotency
    # ------------------------------------------------------------------

    def test_manual_fx_rate_create_normalize_and_fee_interaction(self):
        # Explicit manual mode with trailing zeros normalized
        adv = self.entry(
            kind='expense', expense_type='advance', amount='100', currency='USD',
            fx_mode='manual', fx_rate='7.2500', description='境外云服务',
        )
        self.assertEqual(adv['fx_mode'], 'manual')
        self.assertEqual(adv['fx_rate'], '7.25')
        self.assertEqual(adv['fx_currency'], 'CNY')
        self.assertEqual(adv['fx_amount'], '725.00')

        # Inferred manual mode (fx_mode omitted) + up to 8 decimals for JPY
        jpy = self.entry(
            kind='expense', expense_type='public', amount='10000', currency='JPY',
            fx_rate='0.04725000', description='日本交通',
        )
        self.assertEqual((jpy['fx_mode'], jpy['fx_rate'], jpy['fx_currency'], jpy['fx_amount']),
                         ('manual', '0.04725', 'CNY', '472.50'))

        # Integer rate formatting removes .00
        eur = self.entry(
            kind='income', income_type='capital', amount='50', currency='EUR',
            fx_rate='8.00', description='欧元注资',
        )
        self.assertEqual((eur['fx_mode'], eur['fx_rate'], eur['fx_currency'], eur['fx_amount']),
                         ('manual', '8', 'CNY', '400.00'))

        # Combined fee + manual FX: fx_amount uses net effective amount after fee
        # Income: 100 USD - 3% (3 USD) = 97 USD * 7.20 = 698.40 CNY
        inc_fee = self.entry(
            kind='income', income_type='operating', amount='100', currency='USD',
            fee_mode='rate', fee_rate='3', fx_mode='manual', fx_rate='7.2', description='Stripe 回款',
        )
        self.assertEqual((inc_fee['gross_amount'], inc_fee['fee_amount'], inc_fee['amount'], inc_fee['fx_amount']),
                         ('100.00', '3.00', '97.00', '698.40'))

        # Default auto mode when omitted
        auto_entry = self.entry(kind='expense', amount='50', currency='USD', description='自动汇率')
        self.assertEqual((auto_entry['fx_mode'], auto_entry['fx_rate'], auto_entry['fx_currency'], auto_entry['fx_amount']),
                         ('auto', '', '', ''))

    def test_manual_fx_validation_errors_and_idempotency_conflict(self):
        bad_cases = [
            dict(currency='USD', fx_mode='bad', fx_rate='7.2'),
            dict(currency='USD', fx_mode='manual', fx_rate=''),
            dict(currency='USD', fx_mode='manual'),
            dict(currency='USD', fx_rate='0'),
            dict(currency='USD', fx_rate='-7.25'),
            dict(currency='USD', fx_rate='7.123456789'),
            dict(currency='USD', fx_rate='1e2'),
            dict(currency='USD', fx_rate='1,000'),
            dict(currency='USD', fx_rate='1000000001'),
            dict(currency='USD', fx_rate='7.2', fx_currency='USD'),
            dict(currency='USD', fx_rate='7.2', fx_currency='XXX'),
            dict(currency='CNY', fx_mode='manual', fx_rate='7.2'),
            dict(currency='CNY', fx_rate='1'),
        ]
        for extra in bad_cases:
            with self.subTest(extra=extra):
                payload = {'kind': 'expense', 'amount': '100', 'date': config.today(), **extra}
                res = self.client.post(
                    '/api/entries',
                    json=payload,
                    headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
                )
                self.assertEqual(res.status_code, 400, f'{extra} -> {res.status_code} {res.text}')

        # Idempotent replay matches exact fx_rate; changing fx_rate on same key returns 409
        body = {'kind': 'expense', 'amount': '100', 'currency': 'USD', 'date': config.today(), 'fx_mode': 'manual', 'fx_rate': '7.25'}
        headers = {**self.headers, 'Idempotency-Key': secrets.token_hex(16)}
        first = self.client.post('/api/entries', json=body, headers=headers)
        replay = self.client.post('/api/entries', json=body, headers=headers)
        conflict = self.client.post('/api/entries', json={**body, 'fx_rate': '7.30'}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.json()['id'], first.json()['id'])
        self.assertEqual(conflict.status_code, 409)

    def test_edit_preserves_updates_and_clears_manual_fx_rate(self):
        e = self.entry(kind='expense', amount='100', currency='USD', fx_rate='7.25', description='初始')
        self.assertEqual((e['fx_mode'], e['fx_rate'], e['fx_amount']), ('manual', '7.25', '725.00'))

        # Editing description without fx keys preserves manual rate
        legacy = {k: e[k] for k in ('kind', 'expense_type', 'amount', 'currency', 'date', 'description', 'note', 'payer_id')}
        r1 = self.client.put(f'/api/entries/{e["id"]}', json={**legacy, 'description': '改描述'}, headers=self.headers)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual((r1.json()['fx_mode'], r1.json()['fx_rate'], r1.json()['fx_amount']), ('manual', '7.25', '725.00'))

        # Updating amount recalculates fx_amount
        r2 = self.client.put(f'/api/entries/{e["id"]}', json={**legacy, 'amount': '200'}, headers=self.headers)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual((r2.json()['amount'], r2.json()['fx_rate'], r2.json()['fx_amount']), ('200.00', '7.25', '1450.00'))

        # Updating manual rate via spread payload
        r3 = self.client.put(f'/api/entries/{e["id"]}', json={**r2.json(), 'fx_rate': '7.30'}, headers=self.headers)
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual((r3.json()['fx_mode'], r3.json()['fx_rate'], r3.json()['fx_amount']), ('manual', '7.3', '1460.00'))

        # Clearing manual rate via fx_mode='auto'
        r4 = self.client.put(f'/api/entries/{e["id"]}', json={**r3.json(), 'fx_mode': 'auto'}, headers=self.headers)
        self.assertEqual(r4.status_code, 200, r4.text)
        self.assertEqual((r4.json()['fx_mode'], r4.json()['fx_rate'], r4.json()['fx_currency'], r4.json()['fx_amount']),
                         ('auto', '', '', ''))

        # Setting manual rate on auto entry via spread {**entry, 'fx_rate': '7.4'}
        r5 = self.client.put(f'/api/entries/{e["id"]}', json={**r4.json(), 'fx_rate': '7.4'}, headers=self.headers)
        self.assertEqual(r5.status_code, 200, r5.text)
        self.assertEqual((r5.json()['fx_mode'], r5.json()['fx_rate'], r5.json()['fx_amount']), ('manual', '7.4', '1480.00'))

        # Switching currency clears old manual rate (both via legacy payload and spread payload)
        r6 = self.client.put(f'/api/entries/{e["id"]}', json={**r5.json(), 'currency': 'CNY'}, headers=self.headers)
        self.assertEqual(r6.status_code, 200, r6.text)
        self.assertEqual((r6.json()['currency'], r6.json()['fx_mode'], r6.json()['fx_rate']), ('CNY', 'auto', ''))

        # Switching USD -> EUR without new rate also clears old USD rate
        e2 = self.entry(kind='expense', amount='50', currency='USD', fx_rate='7.25', description='换币种')
        r7 = self.client.put(
            f'/api/entries/{e2["id"]}',
            json={'kind': 'expense', 'expense_type': 'advance', 'amount': '50', 'currency': 'EUR', 'date': config.today()},
            headers=self.headers,
        )
        self.assertEqual(r7.status_code, 200, r7.text)
        self.assertEqual((r7.json()['currency'], r7.json()['fx_mode'], r7.json()['fx_rate']), ('EUR', 'auto', ''))

    # ------------------------------------------------------------------
    # 2. Reports, Triangulation, Payments & Offline / Custom Currencies
    # ------------------------------------------------------------------

    def test_reports_public_balance_shareholders_and_triangulation(self):
        # Market rates from fixture: USD=7, EUR=8, HKD=0.9, CNY=1
        # Entry 1: operating income 100 USD, fee 2 USD -> net 98 USD @ manual 7.25 = 710.50 CNY; fee = 14.50 CNY
        self.entry(kind='income', income_type='operating', amount='100', currency='USD',
                   fee_mode='fixed', fee_amount='2', fx_rate='7.25', description='手动汇率营收')
        # Entry 2: operating income 100 USD @ auto 7.00 = 700.00 CNY
        self.entry(kind='income', income_type='operating', amount='100', currency='USD', description='自动汇率营收')
        # Entry 3: capital income 100 USD @ manual 7.50 = 750.00 CNY
        self.entry(kind='income', income_type='capital', amount='100', currency='USD',
                   fx_rate='7.5', description='手动汇率注资')
        # Entry 4: public expense 50 USD @ manual 7.20 = 360.00 CNY
        self.entry(kind='expense', expense_type='public', amount='50', currency='USD',
                   fx_rate='7.2', description='手动汇率公账')
        # Entry 5: member advance 100 USD @ manual 7.30 = 730.00 CNY
        adv = self.entry(kind='expense', expense_type='advance', amount='100', currency='USD',
                         fx_rate='7.3', description='手动汇率垫付')

        rep = self.public_balance()
        # operating = 710.50 + 700.00 = 1410.50; capital = 750.00; income = 2160.50
        # public_paid = 360.00; actual_cash = 1800.50; due = 730.00; after_due = 1070.50
        # operating_net = 1410.50 - 360.00 = 1050.50; operating_net_after_due = 320.50
        self.assertEqual(rep['operating_income'], '1410.50')
        self.assertEqual(rep['capital_income'], '750.00')
        self.assertEqual(rep['public_paid'], '360.00')
        self.assertEqual(rep['public_balance'], '1800.50')
        self.assertEqual(rep['due'], '730.00')
        self.assertEqual(rep['actual_net_after_due'], '1070.50')
        self.assertEqual(rep['operating_net_after_due'], '320.50')
        self.assertEqual(rep['income_fees'], '14.50')
        self.assertEqual(rep['manual_fx_count'], 4)
        self.assertEqual(rep['shareholder_contributions'][0]['total'], '750.00')
        self.assertEqual(rep['shareholder_contributions'][0]['share'], '100.0')
        self.assertEqual(rep['members'][0]['entries'][0]['fx_rate'], '7.3')
        self.assertEqual(rep['members'][0]['entries'][0]['settlement'], '730.00')

        # Switching report base to USD: USD entries use rate=1 (original USD amounts)
        usd_rep = self.report('?base=USD')
        self.assertEqual(usd_rep['operating_income'], '198.00')
        self.assertEqual(usd_rep['capital_income'], '100.00')
        self.assertEqual(usd_rep['public_paid'], '50.00')
        self.assertEqual(usd_rep['due'], '100.00')

        # Switching report base to HKD: manual USD->CNY entries triangulate via CNY->HKD
        # Mock rate for base=HKD: CNY->HKD = 1.1, USD->HKD = 7.8
        with db(write=True) as conn:
            conn.execute('DELETE FROM fx_cache')
        with patch('app.fx.fetch_rates', side_effect=lambda base, codes: {
            c: {'rate': {'CNY': '1.1', 'USD': '7.8'}[c], 'date': '2026-10-16'} for c in codes
        }):
            hkd_rep = self.report('?base=HKD')
        # operating: 98*7.25*1.1 (781.55) + 100*7.8 (780.00) = 1561.55 HKD
        # capital: 100*7.5*1.1 = 825.00 HKD
        # due: 100*7.3*1.1 = 803.00 HKD
        self.assertEqual(hkd_rep['operating_income'], '1561.55')
        self.assertEqual(hkd_rep['capital_income'], '825.00')
        self.assertEqual(hkd_rep['due'], '803.00')

        # Settlement payment on adv uses manual 7.30 rate (not market 7.00)!
        quote = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'CNY'}).json()[0]
        self.assertEqual((quote['rate'], quote['rate_date'], quote['rate_source'], quote['maximum_amount']),
                         ('7.3', None, 'manual', '730.00'))
        # Partial CNY payment: 292 CNY credits 40 USD, leaving 60 USD (= 438 CNY due)
        pay1 = self.client.post(
            '/api/entries/action',
            json={'action': 'pay', 'ids': [adv['id']], 'note': '首次报销',
                  'payments': [{'id': adv['id'], 'amount': '292', 'quote_id': quote['id']}]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(pay1.status_code, 200, pay1.text)
        self.assertEqual((pay1.json()[0]['reimbursed_amount'], pay1.json()[0]['remaining_amount']), ('40.00', '60.00'))

        # Pay remaining 60 USD in original currency USD -> paid in CNY inherits manual 7.30 rate (60*7.3 = 438)
        orig_quote = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'original'}).json()[0]
        pay2 = self.client.post(
            '/api/entries/action',
            json={'action': 'pay', 'ids': [adv['id']], 'note': '美元尾款',
                  'payments': [{'id': adv['id'], 'amount': '60', 'quote_id': orig_quote['id']}]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(pay2.status_code, 200, pay2.text)
        rep_after = self.report()
        # Total paid for adv: 292 + 438 = 730.00 CNY; due = 0.00 CNY; net_after_due unchanged (1070.50)
        self.assertEqual((rep_after['paid'], rep_after['due'], rep_after['actual_net_after_due']),
                         ('730.00', '0.00', '1070.50'))

    def test_offline_and_custom_currency_work_fully_with_manual_fx(self):
        # Add custom currency USDT (not supported by Frankfurter)
        conf = self.client.get('/api/settings').json()
        conf['currencies'].append({'code': 'USDT', 'name': '泰达币', 'symbol': '₮', 'decimals': 2})
        # Save settings (TWD/USDT 3-letter ISO code rule -> use TWD as 3-letter custom currency)
        conf['currencies'][-1] = {'code': 'TWD', 'name': '新台币', 'symbol': 'NT$', 'decimals': 2}
        res = self.client.put('/api/settings', json=conf, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)

        self.entry(kind='income', amount='1000', currency='TWD', fx_rate='0.225', description='台币收款')
        adv = self.entry(kind='expense', expense_type='advance', amount='400', currency='TWD',
                         fx_rate='0.225', description='台币垫付')

        with db(write=True) as conn:
            conn.execute('DELETE FROM fx_cache')

        # Even with external FX completely offline, report, public balance, payment quote & batch plan ALL succeed!
        with patch('app.fx.fetch_rates', side_effect=OSError('offline')):
            rep = self.report()
            self.assertTrue(rep['complete'])
            self.assertEqual((rep['income'], rep['due'], rep['actual_net_after_due']), ('225.00', '90.00', '135.00'))

            pb = self.public_balance()
            self.assertTrue(pb['complete'])
            self.assertEqual(pb['public_balance'], '225.00')

            # Offline public balance with an unrated simulated foreign adjustment gracefully degrades (no KeyError)
            pb_adj = self.public_balance({'adjustments': {'operating_income': {'amount': '50', 'currency': 'TWD'}}})
            self.assertFalse(pb_adj['complete'])
            self.assertIn('TWD', pb_adj['fx']['missing'])

            q = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'CNY'})
            self.assertEqual(q.status_code, 200, q.text)
            self.assertEqual((q.json()[0]['rate'], q.json()[0]['maximum_amount']), ('0.225', '90.00'))

            with db(write=True) as conn:
                plan_settle = build_plan(conn, self.user, None, 'settlement')
                plan_orig = build_plan(conn, self.user, None, 'original')
            self.assertEqual((plan_settle['items'][0]['amount'], plan_settle['items'][0]['currency']), ('90.00', 'CNY'))
            self.assertEqual((plan_orig['items'][0]['amount'], plan_orig['items'][0]['currency']), ('400.00', 'TWD'))

        # Cannot remove CNY from currencies since TWD entries reference fx_currency='CNY'
        conf2 = self.client.get('/api/settings').json()
        conf2['currencies'] = [c for c in conf2['currencies'] if c['code'] != 'CNY']
        conf2['default_currency'] = 'USD'
        conf2['reference_currency'] = 'USD'
        self.assertEqual(self.client.put('/api/settings', json=conf2, headers=self.headers).status_code, 400)

    def test_legacy_db_migration_and_backup_restore_compatibility(self):
        e = self.entry(kind='expense', amount='50', currency='USD', description='旧库账目')
        # Simulate pre-feature backup zip without fx_rate / fx_currency columns
        with db(write=True) as conn:
            conn.execute('ALTER TABLE entries DROP COLUMN fx_rate')
            conn.execute('ALTER TABLE entries DROP COLUMN fx_currency')
        created = backups.create_backup(['local'])
        # Restoring that older-schema backup validates cleanly and runs init_db() migration!
        backups.restore_backup('local', created['name'])
        init_db()
        # Restore clears sessions (ephemeral); re-login to verify API output cleanly.
        session = self.login_as(self.client, 'vanora-owner-alice', preferred_username='alice', name='小林')
        self.headers = {'X-CSRF-Token': session['csrf']}
        row = next(x for x in self.client.get('/api/entries').json() if x['id'] == e['id'])
        self.assertEqual((row['fx_mode'], row['fx_rate'], row['fx_currency'], row['fx_amount']), ('auto', '', '', ''))

    # ------------------------------------------------------------------
    # 3. Excel Export
    # ------------------------------------------------------------------

    def test_excel_export_uses_manual_fx_and_displays_columns(self):
        inc = self.entry(kind='income', amount='100', currency='USD', fx_rate='7.25', description='手动汇率收入')
        adv = self.entry(kind='expense', expense_type='advance', amount='50', currency='USD', fx_rate='7.40', description='手动汇率垫付')
        auto_e = self.entry(kind='expense', expense_type='public', amount='10', currency='USD', description='自动汇率公账')
        local_e = self.entry(kind='expense', expense_type='public', amount='20', currency='CNY', description='人民币公账')

        # Reimburse 20 USD of adv in CNY (uses manual 7.40 -> 148 CNY)
        q = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'CNY'}).json()[0]
        self.assertEqual(
            self.client.post(
                '/api/entries/action',
                json={'action': 'pay', 'ids': [adv['id']], 'note': '手动汇率付款',
                      'payments': [{'id': adv['id'], 'amount': '148', 'quote_id': q['id']}]},
                headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
            ).status_code,
            200,
        )

        resp = self.client.get('/api/export.xlsx')
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))

        # Sheet 1: KPIs reflect manual rates
        ws_sum = wb['资金汇总']
        kpis = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(1, ws_sum.max_row + 1) if ws_sum.cell(r, 1).value}
        self.assertEqual(float(kpis['营业收入']), 725.0)
        self.assertEqual(float(kpis['公账支出']), 90.0)  # 10*7 + 20
        self.assertEqual(float(kpis['已报销付款']), 148.0)
        self.assertEqual(float(kpis['待报销总额']), 222.0)  # 30*7.40
        all_sum_text = ' '.join(str(ws_sum.cell(r, 1).value or '') for r in range(1, ws_sum.max_row + 1))
        self.assertIn('手动汇率说明', all_sum_text)

        # Sheet 2: 收支明细
        ws_entries = wb['收支明细']
        headers = [c.value for c in ws_entries[1]]
        self.assertEqual(headers[13], '凭证文件')  # Column 14 unchanged
        rows = [dict(zip(headers, [c.value for c in r])) for r in ws_entries.iter_rows(min_row=2)]
        by_id = {r['编号']: r for r in rows}
        self.assertEqual(float(by_id[inc['id']]['参考换算（CNY）']), 725.0)
        self.assertEqual(by_id[inc['id']]['汇率方式'], '手动指定（CNY）')
        self.assertEqual(float(by_id[inc['id']]['指定汇率']), 7.25)
        self.assertEqual(float(by_id[auto_e['id']]['参考换算（CNY）']), 70.0)
        self.assertEqual(by_id[auto_e['id']]['汇率方式'], '自动参考')
        self.assertIsNone(by_id[auto_e['id']]['指定汇率'])
        self.assertEqual(by_id[local_e['id']]['汇率方式'], '—')

        # Sheet 3: 逐次报销付款 shows 手动汇率 for rate date
        ws_pay = wb['逐次报销付款']
        p_headers = [c.value for c in ws_pay[1]]
        p_row = dict(zip(p_headers, [c.value for c in ws_pay[2]]))
        self.assertEqual(float(p_row['锁定汇率']), 7.4)
        self.assertEqual(p_row['汇率日期'], '手动汇率')

        # Sheet 4: 成员待报销 uses manual rate (30 * 7.40 = 222.00)
        ws_pend = wb['成员待报销']
        pend_headers = [c.value for c in ws_pend[1]]
        pend_row = dict(zip(pend_headers, [c.value for c in ws_pend[2]]))
        self.assertEqual(float(pend_row['参考换算（CNY）']), 222.0)

    # ------------------------------------------------------------------
    # 4. AI Assistant, Telegram Bot & Feishu Bot
    # ------------------------------------------------------------------

    def test_ai_plan_create_edit_and_context_with_manual_fx(self):
        raw = {'reply': '记一笔指定汇率美元账目。', 'questions': [], 'actions': [
            {'type': 'create_entry', 'kind': 'expense', 'expense_type': 'advance',
             'amount': '100', 'currency': 'USD', 'fx_rate': '７．２５', 'description': '海外域名'},
        ]}
        with patch('app.ai.plan', return_value=raw):
            plan = self.post('/ai/plan', {'text': '我垫了100美元买海外域名，汇率7.25'}).json()
        self.assertFalse(plan['actions'][0]['errors'], plan)
        res_act = plan['actions'][0]['resolved']
        self.assertEqual((res_act['fx_mode'], res_act['fx_rate'], res_act['fx_currency'], res_act['fx_amount']),
                         ('manual', '7.25', 'CNY', '725.00'))
        self.assertIn('手动汇率 1 USD = 7.25 CNY', plan['actions'][0]['summary'])

        exec_res = self.client.post(
            '/api/ai/execute',
            json={'plan_id': plan['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(exec_res.status_code, 200, exec_res.text)
        entry = self.client.get('/api/entries').json()[0]
        self.assertEqual((entry['fx_mode'], entry['fx_rate'], entry['fx_currency'], entry['fx_amount']),
                         ('manual', '7.25', 'CNY', '725.00'))

        # Context includes manual FX
        with db() as conn:
            ctx = ai_context.entries(conn)
        self.assertEqual((ctx[0]['fx_rate'], ctx[0]['fx_currency']), ('7.25', 'CNY'))

        # AI edit updates rate, then clears rate with fx_mode='auto'
        edit_raw1 = {'reply': '改汇率。', 'questions': [], 'actions': [
            {'type': 'edit_entry', 'id': entry['id'], 'fx_rate': '7.35'},
        ]}
        with patch('app.ai.plan', return_value=edit_raw1):
            p1 = self.post('/ai/plan', {'text': '把汇率改成7.35'}).json()
        self.assertFalse(p1['actions'][0]['errors'], p1)
        self.client.post(
            '/api/ai/execute',
            json={'plan_id': p1['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(self.client.get('/api/entries').json()[0]['fx_rate'], '7.35')

        edit_raw2 = {'reply': '恢复自动汇率。', 'questions': [], 'actions': [
            {'type': 'edit_entry', 'id': entry['id'], 'fx_mode': 'auto'},
        ]}
        with patch('app.ai.plan', return_value=edit_raw2):
            p2 = self.post('/ai/plan', {'text': '取消手动汇率'}).json()
        self.assertFalse(p2['actions'][0]['errors'], p2)
        self.client.post(
            '/api/ai/execute',
            json={'plan_id': p2['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        refreshed = self.client.get('/api/entries').json()[0]
        self.assertEqual((refreshed['fx_mode'], refreshed['fx_rate'], refreshed['fx_currency']), ('auto', '', ''))

    def test_telegram_and_feishu_manual_fx_flows(self):
        self.link_bot()
        # Telegram /book -> USD -> 100 -> edit:fx -> fx:manual -> 7.25 -> save
        bot.process_update(self.message(10, '/book'))
        token = self.draft()['token']
        self.click(11, f'd:{token}:cur:USD')
        bot.process_update(self.message(12, '100'))
        # Preview for USD includes 汇率 button
        buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertIn(f'd:{token}:edit:fx', buttons)
        self.click(13, f'd:{token}:edit:fx')
        self.click(14, f'd:{token}:fx:manual')
        bot.process_update(self.message(15, '7.25'))
        preview_txt = self.last_message()['text']
        self.assertIn('手动汇率 1 USD = 7.25 CNY', preview_txt)
        self.assertIn('¥725.00 CNY', preview_txt)
        self.click(16, f'd:{token}:save')

        entry = self.client.get('/api/entries').json()[0]
        self.assertEqual((entry['fx_mode'], entry['fx_rate'], entry['fx_amount']), ('manual', '7.25', '725.00'))

        # Telegram detail shows manual rate
        self.click(17, f'e:{entry["id"]}')
        self.assertIn('手动汇率 1 USD = 7.25 CNY', self.last_message()['text'])

        # Telegram edit preserves manual rate, and switching back to auto clears it
        self.click(18, f'edit:{entry["id"]}')
        edit_tok = self.draft()['token']
        self.assertIn('手动汇率 1 USD = 7.25 CNY', self.last_message()['text'])
        self.click(19, f'd:{edit_tok}:edit:fx')
        self.click(20, f'd:{edit_tok}:fx:auto')
        self.assertNotIn('手动汇率', self.last_message()['text'])
        self.click(21, f'd:{edit_tok}:save')
        self.assertEqual(self.client.get('/api/entries').json()[0]['fx_mode'], 'auto')

        # Telegram /book -> CNY does NOT show 汇率 button
        bot.process_update(self.message(30, '/book'))
        tok_cny = self.draft()['token']
        self.click(31, f'd:{tok_cny}:cur:CNY')
        bot.process_update(self.message(32, '50'))
        cny_buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertNotIn(f'd:{tok_cny}:edit:fx', cny_buttons)

        # Feishu quick text commands with 汇率 and 手续费 in both orders
        link = self.post('/feishu/link-code').json()['code']
        self.feishu_message('bind', '绑定 ' + link)
        self.assertIn('绑定成功', self.feishu_process())

        self.feishu_message(1, '公账支出 100 USD 买域名 汇率 7.25')
        p1 = self.feishu_process()
        self.assertIn('手动汇率 1 USD = 7.25 CNY', p1)
        code1 = re.search(r'确认 ([A-F0-9]{8})', p1)[1]
        self.feishu_message(2, '确认 ' + code1)
        self.assertIn('已完成', self.feishu_process())

        self.feishu_message(3, '营业收入 100 USD 客户款 汇率 7.20 手续费 2.5%')
        p2 = self.feishu_process()
        self.assertIn('2.5%', p2)
        self.assertIn('手动汇率 1 USD = 7.2 CNY', p2)
        code2 = re.search(r'确认 ([A-F0-9]{8})', p2)[1]
        self.feishu_message(4, '确认 ' + code2)
        self.assertIn('已完成', self.feishu_process())

        by_desc = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual((by_desc['买域名']['fx_rate'], by_desc['买域名']['fx_amount']), ('7.25', '725.00'))
        # 100 - 2.5 = 97.50 USD * 7.2 = 702.00 CNY
        self.assertEqual((by_desc['客户款']['amount'], by_desc['客户款']['fx_rate'], by_desc['客户款']['fx_amount']),
                         ('97.50', '7.2', '702.00'))


if __name__ == '__main__':
    unittest.main()
