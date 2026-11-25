"""Tests for flexible transaction fees (percentage rate or manual fixed amount) on income and expenses."""
import io
import json
import re
import secrets
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app import ai_context, bot, config, feishu, fx
from app.db import db, init_db
from app.funds_plan import build_plan
from tests import test_bot_ui as ui, test_feishu as feishu_fixtures, test_ledger as base


class FeeMechanismTest(unittest.TestCase):
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
    # 1. Core API: percentage & manual fixed fees for income & expenses
    # ------------------------------------------------------------------

    def test_percentage_and_manual_fees_for_income_and_expenses(self):
        # Operating income with arbitrary percentage fee (2.9% on 33.33 USD -> 0.96657 -> 0.97 USD)
        inc_rate = self.entry(
            kind='income', income_type='operating', amount='33.33', currency='USD',
            fee_mode='rate', fee_rate='2.90', description='Stripe 收款',
        )
        self.assertEqual(inc_rate['gross_amount'], '33.33')
        self.assertEqual(inc_rate['fee_mode'], 'rate')
        self.assertEqual(inc_rate['fee_rate'], '2.9')
        self.assertEqual(inc_rate['fee_amount'], '0.97')
        self.assertEqual(inc_rate['fee_minor'], 97)
        self.assertEqual(inc_rate['amount'], '32.36')
        self.assertEqual(inc_rate['amount_minor'], 3236)

        # Capital income (公户转入) with manual fixed fee
        inc_fixed = self.entry(
            kind='income', income_type='capital', amount='5000', currency='CNY',
            fee_mode='fixed', fee_amount='20.50', description='公户转入电汇',
        )
        self.assertEqual(inc_fixed['gross_amount'], '5000.00')
        self.assertEqual(inc_fixed['fee_mode'], 'fixed')
        self.assertEqual(inc_fixed['fee_rate'], '')
        self.assertEqual(inc_fixed['fee_amount'], '20.50')
        self.assertEqual(inc_fixed['amount'], '4979.50')

        # Public expense with arbitrary percentage fee (0.6% on 1000 CNY -> 6.00 CNY -> total 1006.00 CNY)
        pub_rate = self.entry(
            kind='expense', expense_type='public', amount='1000', currency='CNY',
            fee_mode='rate', fee_rate='0.6', description='公账对外付款',
        )
        self.assertEqual(pub_rate['gross_amount'], '1000.00')
        self.assertEqual(pub_rate['fee_mode'], 'rate')
        self.assertEqual(pub_rate['fee_rate'], '0.6')
        self.assertEqual(pub_rate['fee_amount'], '6.00')
        self.assertEqual(pub_rate['amount'], '1006.00')

        # Member advance with manual fixed fee (1000 + 15 = 1015 reimbursable)
        adv_fixed = self.entry(
            kind='expense', expense_type='advance', amount='1000', currency='CNY',
            fee_mode='fixed', fee_amount='15', description='机票垫付',
        )
        self.assertEqual(adv_fixed['gross_amount'], '1000.00')
        self.assertEqual(adv_fixed['fee_mode'], 'fixed')
        self.assertEqual(adv_fixed['fee_amount'], '15.00')
        self.assertEqual(adv_fixed['amount'], '1015.00')
        self.assertEqual(adv_fixed['remaining_amount'], '1015.00')

        # Zero-decimal currency (JPY) HALF_UP rounding: 1000 * 3.55% = 35.5 -> 36 JPY
        jpy_exp = self.entry(
            kind='expense', expense_type='advance', amount='1000', currency='JPY',
            fee_mode='rate', fee_rate='3.55', description='日元消费',
        )
        self.assertEqual((jpy_exp['gross_amount'], jpy_exp['fee_amount'], jpy_exp['amount']), ('1000', '36', '1036'))

        # Omitted fee_mode infers rate or fixed automatically; zero normalizes to none
        inferred_rate = self.entry(kind='income', amount='200', currency='CNY', fee_rate='1.25')
        self.assertEqual((inferred_rate['fee_mode'], inferred_rate['fee_rate'], inferred_rate['fee_amount'], inferred_rate['amount']),
                         ('rate', '1.25', '2.50', '197.50'))
        inferred_fixed = self.entry(kind='expense', amount='200', currency='CNY', fee_amount='5.5')
        self.assertEqual((inferred_fixed['fee_mode'], inferred_fixed['fee_amount'], inferred_fixed['amount']),
                         ('fixed', '5.50', '205.50'))
        zero_rate = self.entry(kind='income', amount='100', currency='CNY', fee_mode='rate', fee_rate='0')
        self.assertEqual((zero_rate['fee_mode'], zero_rate['fee_rate'], zero_rate['fee_minor'], zero_rate['amount']),
                         ('none', '', 0, '100.00'))
        zero_fixed = self.entry(kind='expense', amount='100', currency='CNY', fee_mode='fixed', fee_amount='0.00')
        self.assertEqual((zero_fixed['fee_mode'], zero_fixed['fee_minor'], zero_fixed['amount']),
                         ('none', 0, '100.00'))

        # Fee defaults to 0 when omitted or when rate/fixed value is left empty
        for extra in (
            {},
            {'fee_mode': 'none'},
            {'fee_mode': 'rate'},
            {'fee_mode': 'rate', 'fee_rate': ''},
            {'fee_mode': 'fixed'},
            {'fee_mode': 'fixed', 'fee_amount': ''},
        ):
            with self.subTest(default_zero=extra):
                created = self.entry(kind='income', amount='100', currency='CNY', **extra)
                self.assertEqual(
                    (created['fee_mode'], created['fee_rate'], created['fee_minor'], created['fee_amount'], created['gross_amount'], created['amount']),
                    ('none', '', 0, '0.00', '100.00', '100.00'),
                )

    def test_fee_validation_errors_and_idempotency_conflict(self):
        bad_cases = [
            dict(kind='income', amount='100', fee_mode='bad'),
            dict(kind='income', amount='100', fee_mode='rate', fee_rate='-1'),
            dict(kind='income', amount='100', fee_mode='rate', fee_rate='100'),
            dict(kind='income', amount='100', fee_mode='rate', fee_rate='1.23456'),
            dict(kind='income', amount='100', fee_mode='fixed', fee_amount='-5'),
            dict(kind='income', amount='100', fee_mode='fixed', fee_amount='100'),
            dict(kind='income', amount='100', fee_mode='fixed', fee_amount='120'),
            dict(kind='expense', amount='100', fee_mode='rate', fee_rate='105'),
            dict(kind='expense', amount='100', fee_mode='fixed', fee_amount='1.234'),
            dict(kind='expense', amount='100', fee_rate='1', fee_amount='5'),
            dict(kind='expense', amount='100', currency='JPY', fee_mode='fixed', fee_amount='1.5'),
        ]
        for extra in bad_cases:
            with self.subTest(extra=extra):
                payload = {'currency': 'CNY', 'date': config.today(), **extra}
                res = self.client.post(
                    '/api/entries',
                    json=payload,
                    headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
                )
                self.assertEqual(res.status_code, 400, f'{extra} -> {res.status_code} {res.text}')

        # Idempotent replay matches exact fee; changing fee on same key returns 409
        body = {'kind': 'income', 'amount': '500', 'currency': 'CNY', 'date': config.today(), 'fee_mode': 'rate', 'fee_rate': '2'}
        headers = {**self.headers, 'Idempotency-Key': secrets.token_hex(16)}
        first = self.client.post('/api/entries', json=body, headers=headers)
        replay = self.client.post('/api/entries', json=body, headers=headers)
        conflict = self.client.post('/api/entries', json={**body, 'fee_rate': '3'}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.json()['id'], first.json()['id'])
        self.assertEqual(conflict.status_code, 409)

    def test_edit_preserves_or_updates_fees_without_double_application(self):
        inc = self.entry(kind='income', amount='1000', fee_mode='rate', fee_rate='3', description='客户首款')
        self.assertEqual((inc['gross_amount'], inc['fee_amount'], inc['amount']), ('1000.00', '30.00', '970.00'))

        # Legacy-style edit copying entry fields without fee keys must NOT double-deduct 3%!
        legacy_body = {k: inc[k] for k in ('kind', 'amount', 'currency', 'date', 'description', 'note', 'payer_id')}
        res = self.client.put(f'/api/entries/{inc["id"]}', json={**legacy_body, 'description': '改说明'}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        edited = res.json()
        self.assertEqual((edited['gross_amount'], edited['fee_mode'], edited['fee_rate'], edited['fee_amount'], edited['amount']),
                         ('1000.00', 'rate', '3', '30.00', '970.00'))

        # Updating gross amount recalculates rate fee
        res2 = self.client.put(f'/api/entries/{inc["id"]}', json={**legacy_body, 'amount': '2000', 'fee_mode': 'rate', 'fee_rate': '3'}, headers=self.headers)
        self.assertEqual(res2.status_code, 200, res2.text)
        self.assertEqual((res2.json()['gross_amount'], res2.json()['fee_amount'], res2.json()['amount']),
                         ('2000.00', '60.00', '1940.00'))

        # Switch rate -> fixed
        res3 = self.client.put(f'/api/entries/{inc["id"]}', json={**legacy_body, 'amount': '2000', 'fee_mode': 'fixed', 'fee_amount': '25'}, headers=self.headers)
        self.assertEqual(res3.status_code, 200, res3.text)
        self.assertEqual((res3.json()['fee_mode'], res3.json()['fee_rate'], res3.json()['fee_amount'], res3.json()['amount']),
                         ('fixed', '', '25.00', '1975.00'))

        # Clear fee -> none
        res4 = self.client.put(f'/api/entries/{inc["id"]}', json={**legacy_body, 'amount': '2000', 'fee_mode': 'none'}, headers=self.headers)
        self.assertEqual(res4.status_code, 200, res4.text)
        self.assertEqual((res4.json()['fee_mode'], res4.json()['fee_amount'], res4.json()['amount']),
                         ('none', '0.00', '2000.00'))

        # Spread-and-override PUT {**entry, 'amount': ...} updates gross amount properly (with or without fee)
        plain = res4.json()
        res5 = self.client.put(f'/api/entries/{plain["id"]}', json={**plain, 'amount': '1500'}, headers=self.headers)
        self.assertEqual(res5.status_code, 200, res5.text)
        self.assertEqual((res5.json()['gross_amount'], res5.json()['amount']), ('1500.00', '1500.00'))

        # Spread-and-override with existing rate fee recalculates on new gross, while unchanged amount preserves fee
        with_fee = self.entry(kind='expense', expense_type='public', amount='100', fee_mode='rate', fee_rate='5', description='公账')
        res6 = self.client.put(f'/api/entries/{with_fee["id"]}', json={**with_fee, 'description': '改名'}, headers=self.headers)
        self.assertEqual((res6.json()['gross_amount'], res6.json()['fee_amount'], res6.json()['amount']), ('100.00', '5.00', '105.00'))
        res7 = self.client.put(f'/api/entries/{with_fee["id"]}', json={**res6.json(), 'amount': '200'}, headers=self.headers)
        self.assertEqual((res7.json()['gross_amount'], res7.json()['fee_amount'], res7.json()['amount']), ('200.00', '10.00', '210.00'))

    # ------------------------------------------------------------------
    # 2. Reimbursement, Public Balance, Reporting & DB Migration
    # ------------------------------------------------------------------

    def test_reimbursement_public_balance_and_funds_plan_with_fees(self):
        # Operating income: 1000 CNY, 3% fee (30) -> net 970 CNY
        self.entry(kind='income', income_type='operating', amount='1000', currency='CNY', fee_mode='rate', fee_rate='3', description='营收')
        # Capital income: 100 USD, manual 2 USD fee -> net 98 USD (= 686 CNY @ 7)
        self.entry(kind='income', income_type='capital', amount='100', currency='USD', fee_mode='fixed', fee_amount='2', description='美元转入')
        # Public expense: 200 CNY, manual 10 CNY fee -> total 210 CNY
        self.entry(kind='expense', expense_type='public', amount='200', currency='CNY', fee_mode='fixed', fee_amount='10', description='服务器')
        # Member advance: 300 CNY, 2% fee (6) -> total reimbursable 306 CNY
        adv = self.entry(kind='expense', expense_type='advance', amount='300', currency='CNY', fee_mode='rate', fee_rate='2', description='差旅')

        # Partial reimbursement 106 CNY -> remaining 200 CNY
        self.assertEqual(self.action(adv, 'pay', '首次报销', amount='106').status_code, 200)
        adv_now = next(e for e in self.client.get('/api/entries').json() if e['id'] == adv['id'])
        self.assertEqual((adv_now['reimbursed_amount'], adv_now['remaining_amount'], adv_now['status']),
                         ('106.00', '200.00', 'pending'))

        rep = self.public_balance()
        # CNY: op 970 - pub 210 - paid 106 = 654, due 200, after_due 454, fees = 30 + 10 + 6 = 46
        # USD: cap 98 (= 686 CNY), fees = 2 USD (= 14 CNY)
        # Consolidated CNY: income 1656, outflow 316, balance 1340, due 200, after_due 1140
        # Fees consolidated: income_fees = 30 + 14 = 44, public_fees = 10, advance_fees = 6, total_fees = 60
        self.assertEqual(rep['operating_income'], '970.00')
        self.assertEqual(rep['capital_income'], '686.00')
        self.assertEqual(rep['public_paid'], '210.00')
        self.assertEqual(rep['paid'], '106.00')
        self.assertEqual(rep['public_balance'], '1340.00')
        self.assertEqual(rep['due'], '200.00')
        self.assertEqual(rep['actual_net_after_due'], '1140.00')
        self.assertEqual(rep['income_fees'], '44.00')
        self.assertEqual(rep['public_fees'], '10.00')
        self.assertEqual(rep['advance_fees'], '6.00')
        self.assertEqual(rep['total_fees'], '60.00')

        by_cur = {a['code']: a for a in rep['currency_accounts']}
        self.assertEqual((by_cur['CNY']['balance'], by_cur['CNY']['total_fees']), ('654.00', '46.00'))
        self.assertEqual((by_cur['USD']['balance'], by_cur['USD']['total_fees']), ('98.00', '2.00'))

        # Bank reconciliation against exact net CNY balance (654.00) matches!
        rec = self.public_balance({'bank_actual': {'amount': '654', 'currency': 'CNY', 'scope': 'currency'}})['reconciliation']
        self.assertEqual((rec['status'], rec['difference']), ('matched', '0.00'))

        # Batch plan settles remaining 200 CNY cleanly (total reimbursed becomes 306 == amount_minor)
        conf = self.client.get('/api/settings').json()
        rates = fx.rates_for(conf['reference_currency'], {'CNY', 'USD'})
        with db(write=True) as conn:
            plan = build_plan(conn, self.user, rates, 'original')
        self.assertEqual(plan['items'][0]['amount'], '200.00')
        self.assertEqual(self.action(adv, 'pay', '结清余款', amount='200').status_code, 200)
        adv_done = next(e for e in self.client.get('/api/entries').json() if e['id'] == adv['id'])
        self.assertEqual((adv_done['status'], adv_done['reimbursed_amount'], adv_done['remaining_amount']),
                         ('reimbursed', '306.00', '0.00'))

    def test_legacy_db_without_fee_columns_migrates_idempotently(self):
        e = self.entry(kind='income', amount='250', description='旧账目')
        with db(write=True) as conn:
            for col in ('gross_amount_minor', 'fee_mode', 'fee_rate', 'fee_minor'):
                conn.execute(f'ALTER TABLE entries DROP COLUMN {col}')
        init_db()
        init_db()
        row = next(x for x in self.client.get('/api/entries').json() if x['id'] == e['id'])
        self.assertEqual((row['gross_amount'], row['amount'], row['fee_mode'], row['fee_rate'], row['fee_amount'], row['fee_minor']),
                         ('250.00', '250.00', 'none', '', '0.00', 0))

    # ------------------------------------------------------------------
    # 3. Excel Export with Fees
    # ------------------------------------------------------------------

    def test_excel_export_includes_fee_columns_and_totals(self):
        inc = self.entry(kind='income', amount='1000', fee_mode='rate', fee_rate='2.5', description='含费营收')
        adv = self.entry(kind='expense', expense_type='advance', amount='400', fee_mode='fixed', fee_amount='12', description='含费垫付')

        resp = self.client.get('/api/export.xlsx')
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))

        # Sheet 1 KPIs & currency breakdown
        ws_sum = wb['资金汇总']
        kpis = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(1, ws_sum.max_row + 1) if ws_sum.cell(r, 1).value}
        self.assertEqual(float(kpis['营业收入']), 975.0)
        self.assertEqual(float(kpis['待报销总额']), 412.0)
        self.assertEqual(float(kpis['累计手续费']), 37.0)

        # Sheet 2: 收支明细
        ws_entries = wb['收支明细']
        headers = [c.value for c in ws_entries[1]]
        rows = [dict(zip(headers, [c.value for c in r])) for r in ws_entries.iter_rows(min_row=2)]
        by_id = {r['编号']: r for r in rows}
        self.assertEqual(float(by_id[inc['id']]['金额（原币种）']), 975.0)
        self.assertEqual(float(by_id[inc['id']]['原金额（原币种）']), 1000.0)
        self.assertEqual(by_id[inc['id']]['手续费方式'], '按比例 2.5%')
        self.assertEqual(float(by_id[inc['id']]['手续费（原币种）']), 25.0)

        self.assertEqual(float(by_id[adv['id']]['金额（原币种）']), 412.0)
        self.assertEqual(float(by_id[adv['id']]['原金额（原币种）']), 400.0)
        self.assertEqual(by_id[adv['id']]['手续费方式'], '手动金额')
        self.assertEqual(float(by_id[adv['id']]['手续费（原币种）']), 12.0)

        # Sheet 4: 成员待报销
        ws_pending = wb['成员待报销']
        p_headers = [c.value for c in ws_pending[1]]
        p_rows = [dict(zip(p_headers, [c.value for c in r])) for r in ws_pending.iter_rows(min_row=2)]
        self.assertEqual(float(p_rows[0]['原金额']), 412.0)
        self.assertEqual(float(p_rows[0]['其中手续费']), 12.0)

    # ------------------------------------------------------------------
    # 4. AI Assistant, Telegram Bot & Feishu Bot
    # ------------------------------------------------------------------

    def test_ai_plan_create_edit_and_context_with_fees(self):
        raw = {'reply': '记两笔含手续费账目。', 'questions': [], 'actions': [
            {'type': 'create_entry', 'kind': 'income', 'amount': '1000', 'currency': 'CNY',
             'fee_mode': 'rate', 'fee_rate': '2.9%', 'description': '线上回款'},
            {'type': 'create_entry', 'kind': 'expense', 'expense_type': 'public', 'amount': '500', 'currency': 'CNY',
             'fee_mode': 'fixed', 'fee_amount': '15', 'description': '跨境付款'},
        ]}
        with patch('app.ai.plan', return_value=raw):
            plan = self.post('/ai/plan', {'text': '回款1000手续费2.9%，公账付500手续费15'}).json()
        self.assertFalse(plan['actions'][0]['errors'], plan)
        self.assertFalse(plan['actions'][1]['errors'], plan)
        self.assertEqual(plan['actions'][0]['resolved']['amount'], '971.00')
        self.assertEqual(plan['actions'][1]['resolved']['amount'], '515.00')
        self.assertIn('2.9%', plan['actions'][0]['summary'])
        self.assertIn('手动手续费', plan['actions'][1]['summary'])

        exec_res = self.client.post(
            '/api/ai/execute',
            json={'plan_id': plan['plan_id'], 'actions': [0, 1]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(exec_res.status_code, 200, exec_res.text)
        by_desc = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual((by_desc['线上回款']['gross_amount'], by_desc['线上回款']['fee_amount'], by_desc['线上回款']['amount']),
                         ('1000.00', '29.00', '971.00'))
        self.assertEqual((by_desc['跨境付款']['gross_amount'], by_desc['跨境付款']['fee_amount'], by_desc['跨境付款']['amount']),
                         ('500.00', '15.00', '515.00'))

        # Context includes fee details
        with db() as conn:
            ctx_entries = ai_context.entries(conn)
        ctx_by_desc = {item['description']: item for item in ctx_entries}
        self.assertEqual(ctx_by_desc['线上回款']['fee_rate'], '2.9')
        self.assertEqual(ctx_by_desc['线上回款']['gross_amount'], '1000.00')

        # AI edit to switch fee mode and clear fee
        edit_raw = {'reply': '改手续费。', 'questions': [], 'actions': [
            {'type': 'edit_entry', 'id': by_desc['线上回款']['id'], 'fee_mode': 'fixed', 'fee_amount': '10'},
            {'type': 'edit_entry', 'id': by_desc['跨境付款']['id'], 'fee_mode': 'none'},
        ]}
        with patch('app.ai.plan', return_value=edit_raw):
            e_plan = self.post('/ai/plan', {'text': '修改手续费'}).json()
        self.assertFalse(e_plan['actions'][0]['errors'], e_plan)
        self.assertFalse(e_plan['actions'][1]['errors'], e_plan)
        exec2 = self.client.post(
            '/api/ai/execute',
            json={'plan_id': e_plan['plan_id'], 'actions': [0, 1]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(exec2.status_code, 200, exec2.text)
        by_desc2 = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual((by_desc2['线上回款']['fee_mode'], by_desc2['线上回款']['fee_amount'], by_desc2['线上回款']['amount']),
                         ('fixed', '10.00', '990.00'))
        self.assertEqual((by_desc2['跨境付款']['fee_mode'], by_desc2['跨境付款']['fee_amount'], by_desc2['跨境付款']['amount']),
                         ('none', '0.00', '500.00'))

    def test_telegram_and_feishu_fee_flows(self):
        self.link_bot()
        # Telegram /income -> CNY -> 1000 -> edit fee -> rate -> 1.5% -> save
        bot.process_update(self.message(10, '/income'))
        token = self.draft()['token']
        self.click(11, f'd:{token}:cur:CNY')
        bot.process_update(self.message(12, '1000'))
        self.click(13, f'd:{token}:edit:fee')
        self.click(14, f'd:{token}:fee:rate')
        bot.process_update(self.message(15, '1.5%'))
        preview_txt = self.last_message()['text']
        self.assertIn('原金额：¥1,000.00 CNY', preview_txt)
        self.assertIn('按比例 1.5%', preview_txt)
        self.assertIn('实收金额：¥985.00 CNY', preview_txt)
        self.click(16, f'd:{token}:save')

        # Telegram /book -> CNY -> 200 -> edit fee -> fixed -> 8 -> save
        bot.process_update(self.message(20, '/book'))
        token2 = self.draft()['token']
        self.click(21, f'd:{token2}:cur:CNY')
        bot.process_update(self.message(22, '200'))
        self.click(23, f'd:{token2}:edit:fee')
        self.click(24, f'd:{token2}:fee:fixed')
        bot.process_update(self.message(25, '8'))
        self.assertIn('实付总额：¥208.00 CNY', self.last_message()['text'])
        self.click(26, f'd:{token2}:save')

        entries = sorted(self.client.get('/api/entries').json(), key=lambda x: x['id'])
        self.assertEqual((entries[0]['amount'], entries[0]['fee_amount']), ('985.00', '15.00'))
        self.assertEqual((entries[1]['amount'], entries[1]['fee_amount']), ('208.00', '8.00'))

        # Telegram detail & /funds show fee summary
        self.click(27, f'e:{entries[0]["id"]}')
        self.assertIn('手续费：按比例 1.5%', self.last_message()['text'])
        bot.process_update(self.message(28, '/funds'))
        self.assertIn('累计手续费：¥23.00 CNY', self.last_message()['text'])

        # Editing entry via Telegram preserves gross amount (no double fee!)
        self.click(29, f'edit:{entries[0]["id"]}')
        edit_tok = self.draft()['token']
        self.assertIn('原金额：¥1,000.00 CNY', self.last_message()['text'])
        self.click(30, f'd:{edit_tok}:save')
        refreshed = next(x for x in self.client.get('/api/entries').json() if x['id'] == entries[0]['id'])
        self.assertEqual((refreshed['gross_amount'], refreshed['fee_amount'], refreshed['amount']),
                         ('1000.00', '15.00', '985.00'))

        # Feishu quick text commands with rate & fixed fee
        link = self.post('/feishu/link-code').json()['code']
        self.feishu_message('bind', '绑定 ' + link)
        self.assertIn('绑定成功', self.feishu_process())

        self.feishu_message(1, '营业收入 1000 CNY 客户款 手续费 2.5%')
        p1 = self.feishu_process()
        self.assertIn('2.5%', p1)
        self.assertIn('975.00', p1)
        code1 = re.search(r'确认 ([A-F0-9]{8})', p1)[1]
        self.feishu_message(2, '确认 ' + code1)
        self.assertIn('已完成', self.feishu_process())

        self.feishu_message(3, '公账支出 400 CNY 网关费 手续费 12')
        p2 = self.feishu_process()
        self.assertIn('手动手续费', p2)
        self.assertIn('412.00', p2)
        code2 = re.search(r'确认 ([A-F0-9]{8})', p2)[1]
        self.feishu_message(4, '确认 ' + code2)
        self.assertIn('已完成', self.feishu_process())

        by_desc = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual((by_desc['客户款']['gross_amount'], by_desc['客户款']['fee_amount'], by_desc['客户款']['amount']),
                         ('1000.00', '25.00', '975.00'))
        self.assertEqual((by_desc['网关费']['gross_amount'], by_desc['网关费']['fee_amount'], by_desc['网关费']['amount']),
                         ('400.00', '12.00', '412.00'))


if __name__ == '__main__':
    unittest.main()
