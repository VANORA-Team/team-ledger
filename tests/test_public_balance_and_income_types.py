"""Tests for income type separation (operating vs capital) and corporate public-balance calculation."""
import io
import json
import re
import secrets
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app import bot, config, excel_export, feishu, fx
from app.db import db, init_db
from app.funds_plan import build_plan
from tests import test_bot_ui as ui, test_feishu as feishu_fixtures, test_ledger as base


class PublicBalanceAndIncomeTypesTest(unittest.TestCase):
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
    # 1. Income types: validation, defaults, idempotency, editing, RBAC
    # ------------------------------------------------------------------

    def test_income_types_default_validation_and_idempotency_conflict(self):
        default_inc = self.entry(kind='income', amount='100', description='默认营业收入')
        self.assertEqual((default_inc['income_type'], default_inc['expense_type'], default_inc['status']),
                         ('operating', '', 'posted'))

        cap_inc = self.entry(kind='income', income_type='capital', amount='5000', description='首期出资')
        self.assertEqual((cap_inc['income_type'], cap_inc['expense_type'], cap_inc['status']),
                         ('capital', '', 'posted'))

        adv = self.entry(amount='20', description='普通垫付')
        self.assertEqual(adv['income_type'], '')

        # Invalid income_type values rejected with 400
        for bad in ('unknown', '', None, 123, {}, []):
            with self.subTest(bad=bad):
                res = self.client.post(
                    '/api/entries',
                    json=dict(kind='income', income_type=bad, amount='10', currency='CNY', date=config.today()),
                    headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
                )
                self.assertEqual(res.status_code, 400, res.text)

        # Idempotent replay matches same income_type; mismatched income_type returns 409
        body = dict(kind='income', income_type='capital', amount='200', currency='CNY', date=config.today())
        headers = {**self.headers, 'Idempotency-Key': secrets.token_hex(16)}
        first = self.client.post('/api/entries', json=body, headers=headers)
        replay = self.client.post('/api/entries', json=body, headers=headers)
        conflict = self.client.post('/api/entries', json={**body, 'income_type': 'operating'}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.json()['id'], first.json()['id'])
        self.assertEqual(conflict.status_code, 409)

    def test_posted_income_can_be_reclassified_and_voided_income_cannot_be_edited(self):
        inc = self.entry(kind='income', income_type='operating', amount='1000', description='待归类入账')
        self.assertEqual((self.report()['operating_income'], self.report()['capital_income']), ('1000.00', '0.00'))

        # Switch operating -> capital
        body = {k: inc[k] for k in ('kind', 'amount', 'currency', 'date', 'description', 'note', 'payer_id')}
        res = self.client.put(f'/api/entries/{inc["id"]}', json={**body, 'income_type': 'capital'}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['income_type'], 'capital')
        rep = self.report()
        self.assertEqual((rep['operating_income'], rep['capital_income'], rep['public_balance']),
                         ('0.00', '1000.00', '1000.00'))

        # Preserves income_type when omitted in edit payload
        res2 = self.client.put(f'/api/entries/{inc["id"]}', json={**body, 'amount': '1200'}, headers=self.headers)
        self.assertEqual(res2.status_code, 200, res2.text)
        self.assertEqual((res2.json()['income_type'], res2.json()['amount']), ('capital', '1200.00'))

        # Cannot change kind from income to expense
        bad_kind = self.client.put(f'/api/entries/{inc["id"]}', json={**body, 'kind': 'expense'}, headers=self.headers)
        self.assertEqual(bad_kind.status_code, 400)

        # Voided income cannot be edited
        self.assertEqual(self.action(inc, 'void', '误录').status_code, 200)
        after_void = self.client.put(f'/api/entries/{inc["id"]}', json={**body, 'income_type': 'operating'}, headers=self.headers)
        self.assertEqual(after_void.status_code, 409)

    def test_member_cannot_create_or_edit_capital_or_operating_income(self):
        from fastapi.testclient import TestClient
        from app.main import app

        self.new_member(sub='vanora-mem-inc', username='meminc', name='普通成员', role='member')
        inc = self.entry(kind='income', income_type='capital', amount='500')
        with TestClient(app) as client:
            login = self.login_as(client, 'vanora-mem-inc', preferred_username='meminc', name='普通成员')
            headers = {'X-CSRF-Token': login['csrf']}
            for itype in ('operating', 'capital'):
                res = client.post(
                    '/api/entries',
                    json=dict(kind='income', income_type=itype, amount='100', currency='CNY', date=config.today()),
                    headers={**headers, 'Idempotency-Key': secrets.token_hex(16)},
                )
                self.assertEqual(res.status_code, 403)
            edit_res = client.put(
                f'/api/entries/{inc["id"]}',
                json=dict(kind='income', income_type='operating', amount='500', currency='CNY', date=config.today()),
                headers=headers,
            )
            self.assertEqual(edit_res.status_code, 403)
            # Members CAN view / calculate public balance (read-only)
            self.assertEqual(client.get('/api/public-balance').status_code, 200)
            self.assertEqual(client.post('/api/public-balance/calculate', json={}, headers=headers).status_code, 200)

    def test_legacy_db_without_income_type_migrates_idempotently(self):
        inc = self.entry(kind='income', amount='300', description='旧收入')
        exp = self.entry(amount='50', description='旧支出')
        with db(write=True) as conn:
            conn.execute('ALTER TABLE entries DROP COLUMN income_type')
        init_db()
        init_db()
        rows = {e['id']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual(rows[inc['id']]['income_type'], 'operating')
        self.assertEqual(rows[exp['id']]['income_type'], '')
        self.assertEqual(self.report()['operating_income'], '300.00')

    # ------------------------------------------------------------------
    # 2. Public balance calculation, operating net, shareholders, as_of,
    #    simulation adjustments, bank reconciliation
    # ------------------------------------------------------------------

    def test_public_balance_equations_shareholders_and_multicurrency(self):
        partner = self.new_member(sub='vanora-partner', username='partner', name='合伙人王', role='finance')
        # Shareholder capital injections (Alice 3000 CNY + Partner 1000 USD = 7000 CNY => total 10000 CNY)
        self.entry(kind='income', income_type='capital', amount='3000', currency='CNY', date='2026-10-01', description='A轮注资-小林')
        self.entry(kind='income', income_type='capital', amount='1000', currency='USD', date='2026-10-02', payer_id=partner['id'], description='A轮注资-王')
        # Operating revenue: 2000 CNY
        self.entry(kind='income', income_type='operating', amount='2000', currency='CNY', date='2026-10-05', description='客户首款')
        # Public expenses: 1500 CNY + 100 USD (700 CNY) = 2200 CNY
        self.entry(expense_type='public', amount='1500', currency='CNY', date='2026-10-06', description='办公房租')
        self.entry(expense_type='public', amount='100', currency='USD', date='2026-10-07', description='海外云服务')
        # Member advance: 1000 CNY, partially reimbursed 400 CNY (remaining 600 CNY)
        adv = self.entry(amount='1000', currency='CNY', date='2026-10-08', description='展会差旅')
        self.assertEqual(self.action(adv, 'pay', '首次报销', amount='400').status_code, 200)

        rep = self.public_balance()
        # Total income = 2000 + 10000 = 12000; outflow = 2200 + 400 = 2600; public_balance = 9400
        self.assertEqual(rep['operating_income'], '2000.00')
        self.assertEqual(rep['capital_income'], '10000.00')
        self.assertEqual(rep['income'], '12000.00')
        self.assertEqual(rep['public_paid'], '2200.00')
        self.assertEqual(rep['paid'], '400.00')
        self.assertEqual(rep['total_outflow'], '2600.00')
        self.assertEqual(rep['public_balance'], '9400.00')
        self.assertEqual(rep['actual_cash'], '9400.00')
        self.assertEqual(rep['due'], '600.00')
        self.assertEqual(rep['actual_net_after_due'], '8800.00')
        # Operating profit strictly excludes 公户转入 (capital): 2000 - 2600 = -600; after due = -1200
        self.assertEqual(rep['operating_net'], '-600.00')
        self.assertEqual(rep['operating_net_after_due'], '-1200.00')
        self.assertEqual(rep['operating_profit'], '-600.00')
        self.assertEqual(rep['operating_profit_after_due'], '-1200.00')

        # Exact per-currency corporate accounts & operating profit (excluding 公户转入)
        by_cur = {a['code']: a for a in rep['currency_accounts']}
        self.assertEqual(by_cur['CNY']['operating_income'], '2000.00')
        self.assertEqual(by_cur['CNY']['capital_income'], '3000.00')
        self.assertEqual(by_cur['CNY']['public_paid'], '1500.00')
        self.assertEqual(by_cur['CNY']['paid'], '400.00')
        self.assertEqual(by_cur['CNY']['balance'], '3100.00')
        self.assertEqual(by_cur['CNY']['due'], '600.00')
        self.assertEqual(by_cur['CNY']['net_after_due'], '2500.00')
        self.assertEqual(by_cur['CNY']['operating_profit'], '100.00')
        self.assertEqual(by_cur['CNY']['operating_profit_after_due'], '-500.00')
        self.assertEqual(by_cur['USD']['capital_income'], '1000.00')
        self.assertEqual(by_cur['USD']['public_paid'], '100.00')
        self.assertEqual(by_cur['USD']['balance'], '900.00')
        self.assertEqual(by_cur['USD']['operating_profit'], '-100.00')
        self.assertEqual(by_cur['USD']['operating_profit_after_due'], '-100.00')

        # Shareholder contributions sorted descending by converted total
        sh = rep['shareholder_contributions']
        self.assertEqual(len(sh), 2)
        self.assertEqual((sh[0]['name'], sh[0]['total'], sh[0]['share'], sh[0]['by_currency']),
                         ('合伙人王', '7000.00', '70.0', {'USD': '1000.00'}))
        self.assertEqual((sh[1]['name'], sh[1]['total'], sh[1]['share'], sh[1]['by_currency']),
                         ('小林', '3000.00', '30.0', {'CNY': '3000.00'}))

        # Both capital and operating incomes fund reimbursement batches
        conf = self.client.get('/api/settings').json()
        rates = fx.rates_for(conf['reference_currency'], {'CNY', 'USD'})
        with db(write=True) as conn:
            plan = build_plan(conn, self.user, rates, 'original')
        self.assertEqual(plan['items'][0]['amount'], '600.00')

    def test_public_balance_as_of_cutoff_reconstructs_historical_balance_and_due(self):
        self.entry(kind='income', income_type='capital', amount='1000', date='2026-10-01')
        self.entry(kind='income', income_type='operating', amount='500', date='2026-10-20')
        self.entry(expense_type='public', amount='200', date='2026-10-10')
        adv = self.entry(amount='300', date='2026-10-05', description='早先垫付')
        self.assertEqual(self.action(adv, 'pay', '十月下旬报销').status_code, 200)
        # Set payment created_at explicitly to 2026-10-25
        with db(write=True) as conn:
            conn.execute("UPDATE payments SET created_at='2026-10-25T10:00:00+08:00'")

        # Snapshot as of 2026-10-15: only capital 1000, public 200, payment NOT happened yet -> balance 800, due 300
        mid = self.public_balance({'as_of': '2026-10-15'})
        self.assertEqual(mid['as_of'], '2026-10-15')
        self.assertEqual((mid['capital_income'], mid['operating_income'], mid['public_paid'], mid['paid']),
                         ('1000.00', '0.00', '200.00', '0.00'))
        self.assertEqual((mid['public_balance'], mid['due'], mid['actual_net_after_due']),
                         ('800.00', '300.00', '500.00'))

        # Full current snapshot: capital 1000 + op 500 - pub 200 - paid 300 = 1000, due 0
        now_rep = self.public_balance({})
        self.assertEqual((now_rep['public_balance'], now_rep['paid'], now_rep['due']),
                         ('1000.00', '300.00', '0.00'))

    def test_public_balance_simulation_and_bank_reconciliation(self):
        self.entry(kind='income', income_type='capital', amount='1000', currency='CNY')
        self.entry(kind='income', income_type='operating', amount='100', currency='USD')  # 700 CNY
        self.entry(expense_type='public', amount='300', currency='CNY')
        self.entry(amount='500', currency='CNY', description='待报销')
        before_entries = self.client.get('/api/entries').json()

        # Simulate +50 USD operating (350 CNY), +200 CNY capital, -150 CNY public expense => delta +400 CNY
        res = self.public_balance({
            'base': 'CNY',
            'include_pending': True,
            'adjustments': {
                'operating_income': {'amount': '50', 'currency': 'USD'},
                'capital_income': {'amount': '200', 'currency': 'CNY'},
                'public_expense': {'amount': '150', 'currency': 'CNY'},
            },
            'bank_actual': {'amount': '750', 'currency': 'CNY', 'scope': 'currency'},
        })
        # Book CNY balance = 1000 - 300 = 700; USD = 100 (700 CNY); consolidated book = 1400 CNY
        sim = res['simulation']
        self.assertTrue(sim['has_adjustments'])
        self.assertEqual((sim['sim_operating'], sim['sim_capital'], sim['sim_public_expense'], sim['sim_net_delta']),
                         ('350.00', '200.00', '150.00', '400.00'))
        self.assertEqual((sim['book_balance'], sim['projected_balance'], sim['projected_after_due'], sim['final_balance']),
                         ('1400.00', '1800.00', '1300.00', '1300.00'))
        # Projected operating net = (700 + 350) - (300 + 150) = 600; after due 500 = 100
        self.assertEqual((sim['projected_operating_net'], sim['projected_operating_net_after_due']),
                         ('600.00', '100.00'))

        # Per-currency simulated balances
        by_cur = {a['code']: a for a in res['currency_accounts']}
        self.assertEqual((by_cur['CNY']['balance'], by_cur['CNY']['projected_balance']), ('700.00', '750.00'))
        self.assertEqual((by_cur['USD']['balance'], by_cur['USD']['projected_balance']), ('100.00', '150.00'))

        # Bank reconciliation (currency scope): bank 750 CNY vs book 700 CNY => +50 surplus
        rec = res['reconciliation']
        self.assertEqual((rec['status'], rec['bank_balance'], rec['book_balance'], rec['difference']),
                         ('surplus', '750.00', '700.00', '50.00'))

        # Exact match and deficit cases
        matched = self.public_balance({'bank_actual': {'amount': '700', 'currency': 'CNY', 'scope': 'currency'}})
        self.assertEqual((matched['reconciliation']['status'], matched['reconciliation']['difference']), ('matched', '0.00'))

        deficit = self.public_balance({'bank_actual': {'amount': '1300', 'currency': 'CNY', 'scope': 'consolidated'}})
        self.assertEqual((deficit['reconciliation']['status'], deficit['reconciliation']['difference']), ('deficit', '-100.00'))

        # Offline FX: currency-scope reconciliation & currency_accounts STILL work!
        with db(write=True) as conn:
            conn.execute('DELETE FROM fx_cache')
        with patch('app.fx.fetch_rates', side_effect=OSError('offline')):
            offline = self.public_balance({'bank_actual': {'amount': '700', 'currency': 'CNY', 'scope': 'currency'}})
            self.assertFalse(offline['complete'])
            self.assertEqual(offline['reconciliation']['status'], 'matched')
            self.assertEqual({a['code']: a['balance'] for a in offline['currency_accounts']}, {'CNY': '700.00', 'USD': '100.00'})
            offline_cons = self.public_balance({'bank_actual': {'amount': '1400', 'currency': 'CNY', 'scope': 'consolidated'}})
            self.assertEqual(offline_cons['reconciliation']['status'], 'unavailable')

        # Simulation never mutates ledger
        self.assertEqual(self.client.get('/api/entries').json(), before_entries)

        # Validation failures
        self.assertEqual(self.post('/public-balance/calculate', {'as_of': 'bad-date'}).status_code, 400)
        self.assertEqual(self.post('/public-balance/calculate', {
            'base': 'CNY', 'bank_actual': {'amount': '100', 'currency': 'USD', 'scope': 'consolidated'}
        }).status_code, 400)
        self.assertEqual(self.post('/public-balance/calculate', {
            'adjustments': {'operating_income': {'amount': '-10', 'currency': 'CNY'}}
        }).status_code, 400)

    # ------------------------------------------------------------------
    # 3. Excel Export with income types & public balance summary
    # ------------------------------------------------------------------

    def test_excel_export_separates_income_types_and_filters(self):
        op = self.entry(kind='income', income_type='operating', amount='2000', description='项目营收')
        cap = self.entry(kind='income', income_type='capital', amount='5000', description='资金注资')
        pub = self.entry(expense_type='public', amount='600', description='公账支出项')
        self.entry(amount='400', description='垫付项')

        resp = self.client.get('/api/export')
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        ws_entries = wb['收支明细']
        headers = [c.value for c in ws_entries[1]]
        rows = [dict(zip(headers, [c.value for c in r])) for r in ws_entries.iter_rows(min_row=2)]
        by_desc = {r['用途 / 说明']: r for r in rows}
        self.assertEqual(by_desc['项目营收']['业务类型'], '营业收入')
        self.assertEqual(by_desc['资金注资']['业务类型'], '公户转入')
        self.assertEqual(by_desc['公账支出项']['业务类型'], '公账支出')

        # Summary sheet contains operating_income, capital_income, public balance, and profit excluding 公户转入
        ws_sum = wb['资金汇总']
        kpis = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(1, ws_sum.max_row + 1) if ws_sum.cell(r, 1).value}
        self.assertEqual(float(kpis['营业收入']), 2000.0)
        self.assertEqual(float(kpis['公户转入（不计入利润）']), 5000.0)
        self.assertEqual(float(kpis['公户结余（可用资金）']), 6400.0)
        self.assertEqual(float(kpis['扣除全部待报销后结余']), 6000.0)
        self.assertEqual(float(kpis['经营利润（实付口径，不含公户转入）']), 1400.0)
        self.assertEqual(float(kpis['经营净利润（含待报销，不含公户转入）']), 1000.0)

        # Filtering by operating / capital / income
        for q, expected in [('?kind=operating', [op['id']]), ('?kind=capital', [cap['id']]), ('?kind=income', [op['id'], cap['id']])]:
            wb_f = load_workbook(io.BytesIO(self.client.get('/api/export' + q).content))
            ws_f = wb_f['收支明细']
            h = [c.value for c in ws_f[1]]
            ids = [dict(zip(h, [c.value for c in r]))['编号'] for r in ws_f.iter_rows(min_row=2)]
            self.assertEqual(ids, expected, q)

        self.assertEqual(excel_export.parse_export_shortcut('营业收入')['kind'], 'operating')
        self.assertEqual(excel_export.parse_export_shortcut('公户转入')['kind'], 'capital')
        self.assertEqual(excel_export.parse_export_shortcut('股东出资')['kind'], 'capital')

    # ------------------------------------------------------------------
    # 4. AI Assistant: create/edit/query income types & public balance
    # ------------------------------------------------------------------

    def test_ai_plan_supports_income_types_and_public_balance_queries(self):
        raw = {'reply': '记两笔收入。', 'questions': [], 'actions': [
            {'type': 'create_entry', 'kind': 'income', 'income_type': 'operating', 'amount': '800', 'currency': 'CNY', 'description': '客户回款'},
            {'type': 'create_entry', 'kind': 'income', 'income_type': 'capital', 'amount': '5000', 'currency': 'CNY', 'description': '公户调拨打款'},
        ]}
        with patch('app.ai.plan', return_value=raw):
            plan = self.post('/ai/plan', {'text': '收到客户回款800，公户转入5000'}).json()
        self.assertEqual(plan['actions'][0]['label'], '营业收入')
        self.assertEqual(plan['actions'][1]['label'], '公户转入')
        exec_res = self.client.post(
            '/api/ai/execute',
            json={'plan_id': plan['plan_id'], 'actions': [0, 1]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(exec_res.status_code, 200, exec_res.text)
        by_desc = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual(by_desc['客户回款']['income_type'], 'operating')
        self.assertEqual(by_desc['公户调拨打款']['income_type'], 'capital')

        # AI query filtering by income_type & balance query with public_balance fields
        q_raw = {'reply': '查询结果', 'questions': [], 'actions': [
            {'type': 'query', 'query': 'list_entries', 'filter_income_type': 'capital'},
            {'type': 'query', 'query': 'summary'},
            {'type': 'query', 'query': 'balance'},
        ]}
        with patch('app.ai.plan', return_value=q_raw):
            q_plan = self.post('/ai/plan', {'text': '查公户转入和公户结余'}).json()
        cards = [r['card'] for r in q_plan['results']]
        self.assertEqual([e['description'] for e in cards[0]['entries']], ['公户调拨打款'])
        self.assertEqual(cards[1]['by_income_type']['capital'][0]['amount'], '5000.00')
        self.assertEqual(cards[1]['by_income_type']['operating'][0]['amount'], '800.00')
        self.assertEqual((cards[2]['public_balance'], cards[2]['operating_income'], cards[2]['capital_income'], cards[2]['operating_profit_after_due']),
                         ('5800.00', '800.00', '5000.00', '800.00'))

        # AI edit posted income to switch income_type
        e_raw = {'reply': '修改归类', 'questions': [], 'actions': [
            {'type': 'edit_entry', 'id': by_desc['客户回款']['id'], 'income_type': 'capital'},
        ]}
        with patch('app.ai.plan', return_value=e_raw):
            e_plan = self.post('/ai/plan', {'text': '把客户回款改成公户转入'}).json()
        self.assertFalse(e_plan['actions'][0]['errors'], e_plan)
        self.assertEqual(
            self.client.post(
                '/api/ai/execute',
                json={'plan_id': e_plan['plan_id'], 'actions': [0]},
                headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
            ).status_code,
            200,
        )
        updated = next(e for e in self.client.get('/api/entries').json() if e['id'] == by_desc['客户回款']['id'])
        self.assertEqual(updated['income_type'], 'capital')

    # ------------------------------------------------------------------
    # 5. Telegram & Feishu Bots
    # ------------------------------------------------------------------

    def test_telegram_capital_command_toggle_and_funds_public_balance(self):
        self.link_bot()
        # /capital creates capital income
        bot.process_update(self.message(2, '/capital'))
        token = self.draft()['token']
        self.click(3, f'd:{token}:cur:CNY')
        bot.process_update(self.message(4, '5000'))
        self.assertIn('公户转入', self.last_message()['text'])
        # Toggle to operating and back to capital on preview card
        self.click(5, f'd:{token}:inctype')
        self.assertIn('营业收入', self.last_message()['text'])
        self.click(6, f'd:{token}:inctype')
        self.assertIn('公户转入', self.last_message()['text'])
        self.click(7, f'd:{token}:save')

        self.entry(kind='income', income_type='operating', amount='1200', description='服务费')
        self.entry(expense_type='public', amount='300', description='服务器')
        self.entry(amount='400', description='垫付')

        # /today separates operating vs capital
        bot.process_update(self.message(8, '/today'))
        today_text = self.last_message()['text']
        self.assertIn('营业收入 · 1 笔', today_text)
        self.assertIn('公户转入（不计利润） · 1 笔', today_text)

        # /funds displays corporate balance & operating profit excluding 公户转入
        bot.process_update(self.message(9, '/funds'))
        funds_text = self.last_message()['text']
        self.assertIn('累计营业收入：¥1,200.00 CNY', funds_text)
        self.assertIn('累计公户转入（不计利润）：¥5,000.00 CNY', funds_text)
        self.assertIn('扣全额待报销后公户结余：¥5,500.00 CNY', funds_text)
        self.assertIn('经营利润（实付，不含公户转入）：¥900.00 CNY', funds_text)
        self.assertIn('经营净利润（含待报，不含公户转入）：¥500.00 CNY', funds_text)

    def test_feishu_shortcuts_for_operating_capital_and_public_balance(self):
        response = self.post('/feishu/link-code')
        self.assertEqual(response.status_code, 200)
        self.feishu_message('bind', '绑定 ' + response.json()['code'])
        self.assertIn('绑定成功', self.feishu_process())

        self.feishu_message(1, '公户转入 3000 CNY 备用金注资')
        preview = self.feishu_process()
        self.assertIn('公户转入', preview)
        code = re.search(r'确认 ([A-F0-9]{8})', preview)[1]
        self.feishu_message(2, '确认 ' + code)
        self.assertIn('已完成', self.feishu_process())

        self.feishu_message(3, '营业收入 800 CNY 客户回款')
        preview2 = self.feishu_process()
        self.assertIn('营业收入', preview2)
        code2 = re.search(r'确认 ([A-F0-9]{8})', preview2)[1]
        self.feishu_message(4, '确认 ' + code2)
        self.assertIn('已完成', self.feishu_process())

        rows = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual(rows['备用金注资']['income_type'], 'capital')
        self.assertEqual(rows['客户回款']['income_type'], 'operating')

        self.feishu_message(5, '公户结余')
        bal_reply = self.feishu_process()
        self.assertIn('公户结余', bal_reply)
        self.assertIn('营业收入', bal_reply)
        self.assertIn('公户转入', bal_reply)
        self.assertIn('经营净利润（不含公户转入）', bal_reply)


if __name__ == '__main__':
    unittest.main()
