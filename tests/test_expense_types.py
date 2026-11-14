"""Public expenses affect cash immediately; member advances require reimbursement."""
import csv
import io
import json
import secrets
import unittest
from unittest.mock import patch

from app import bot, config, fx
from app.db import db, init_db
from app.funds_plan import build_plan
from tests import test_ledger as base, test_bot_ui as ui


class ExpenseTypesTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    action = base.LedgerTest.action
    link_bot = base.LedgerTest.link_bot
    message = staticmethod(base.LedgerTest.message)
    callback = staticmethod(base.LedgerTest.callback)
    click = ui.TelegramUiTest.click
    draft = ui.TelegramUiTest.draft
    last_message = ui.TelegramUiTest.last_message

    def report(self):
        response = self.client.get('/api/report')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_public_and_advance_cash_effect_and_partial_payment(self):
        self.entry(kind='income', amount='1000')
        public = self.entry(expense_type='public', amount='200')
        advance = self.entry(expense_type='advance', amount='300')
        self.assertEqual((public['status'], public['remaining_minor'], public['payments']), ('posted', 0, []))
        self.assertEqual(advance['status'], 'pending')
        report = self.report()
        self.assertEqual((report['cash'], report['public_paid'], report['paid'], report['due']),
                         ('800.00', '200.00', '0.00', '300.00'))
        self.assertEqual(self.action(advance, 'pay', '已实际转账', amount='100').status_code, 200)
        report = self.report()
        self.assertEqual((report['cash'], report['due'], report['public_paid'], report['paid']),
                         ('700.00', '200.00', '200.00', '100.00'))
        self.assertEqual([e['id'] for m in report['members'] for e in m['entries']], [advance['id']])

    def test_public_cannot_be_reimbursed_or_selected_for_reimbursement(self):
        public = self.entry(expense_type='public')
        self.assertEqual(self.action(public, 'pay', '不能重复扣款').status_code, 409)
        response = self.post('/calculate', {'reimbursement': {'mode': 'manual', 'entry_ids': [public['id']]}})
        self.assertIn(response.status_code, (400, 409))
        self.assertEqual(self.report()['paid'], '0.00')

    def test_public_draft_only_deducts_on_submit_and_void_restores_cash(self):
        self.entry(kind='income', amount='100')
        public = self.entry(expense_type='public', status='draft', amount='30')
        self.assertEqual(self.report()['cash'], '100.00')
        submitted = self.action(public, 'submit')
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()[0]['status'], 'posted')
        self.assertEqual(self.report()['cash'], '70.00')
        self.assertEqual(self.action(public, 'submit').status_code, 409)
        self.assertEqual(self.action(public, 'void', '重复支出').status_code, 200)
        self.assertEqual(self.report()['cash'], '100.00')
        self.assertEqual(self.report()['public_paid'], '0.00')

    def test_edit_public_preserves_type_and_updates_cash(self):
        self.entry(kind='income', amount='100')
        public = self.entry(expense_type='public', amount='20')
        body = {key: public[key] for key in ('kind', 'currency', 'date', 'description', 'note', 'payer_id', 'status')}
        response = self.client.put(f'/api/entries/{public["id"]}', json={**body, 'amount': '35'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['expense_type'], 'public')
        self.assertEqual(self.report()['cash'], '65.00')

    def test_unpaid_advance_can_be_changed_to_public_and_back(self):
        self.entry(kind='income', amount='100')
        entry = self.entry(amount='20')
        body = {key: entry[key] for key in ('kind', 'amount', 'currency', 'date', 'description', 'note', 'payer_id')}
        response = self.client.put(f'/api/entries/{entry["id"]}', json={**body, 'expense_type': 'public'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.report()['cash'], self.report()['due']), ('80.00', '0.00'))
        response = self.client.put(f'/api/entries/{entry["id"]}', json={**body, 'expense_type': 'advance'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.report()['cash'], self.report()['due']), ('100.00', '20.00'))
        self.assertEqual(self.action(entry, 'pay', '部分报销', amount='5').status_code, 200)
        response = self.client.put(f'/api/entries/{entry["id"]}', json={**body, 'expense_type': 'public'}, headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_public_create_replay_and_cross_type_conflict(self):
        body = dict(kind='expense', expense_type='public', amount='10', currency='CNY', date=config.today())
        headers = {**self.headers, 'Idempotency-Key': secrets.token_hex(16)}
        first = self.client.post('/api/entries', json=body, headers=headers)
        replay = self.client.post('/api/entries', json=body, headers=headers)
        conflict = self.client.post('/api/entries', json={**body, 'expense_type': 'advance'}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.json()['id'], first.json()['id'])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.report()['cash'], '-10.00')

    def test_old_clients_default_to_advance_and_invalid_type_is_rejected(self):
        self.assertEqual(self.entry()['expense_type'], 'advance')
        for value in ('unknown', '', None, {}, 1):
            response = self.client.post('/api/entries', json=dict(kind='expense', expense_type=value, amount='10',
                                        currency='CNY', date=config.today()),
                                        headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)})
            self.assertEqual(response.status_code, 400, response.text)

    def test_public_mixed_currencies_and_missing_rate(self):
        self.entry(kind='income', amount='100', currency='USD')
        self.entry(expense_type='public', amount='10', currency='USD')
        self.entry(expense_type='public', amount='50', currency='CNY')
        self.entry(amount='20', currency='CNY')
        report = self.report()
        self.assertEqual((report['cash'], report['public_paid'], report['due']), ('580.00', '120.00', '20.00'))
        self.assertEqual(report['balances'], {'USD': '90', 'CNY': '-50'})
        with db(write=True) as conn:
            conn.execute('DELETE FROM fx_cache')
        with patch('app.fx.fetch_rates', side_effect=OSError('offline')):
            report = self.report()
        self.assertFalse(report['complete'])
        self.assertEqual(report['balances'], {'USD': '90', 'CNY': '-50'})

    def test_batch_plan_deducts_public_expenses_and_excludes_them(self):
        self.entry(kind='income', amount='100')
        public = self.entry(expense_type='public', amount='70')
        advance = self.entry(amount='80')
        conf = self.client.get('/api/settings').json()
        rates = fx.rates_for(conf['reference_currency'], {'CNY'})
        with db(write=True) as conn:
            plan = build_plan(conn, self.user, rates, 'original')
        self.assertEqual(plan['cash'], '30')
        self.assertEqual([item['id'] for item in plan['items']], [advance['id']])
        self.assertNotEqual(plan['items'][0]['id'], public['id'])
        self.assertEqual(plan['items'][0]['amount'], '30.00')

    def test_csv_distinguishes_expense_types_without_dividend_rows(self):
        self.entry(kind='income', amount='100')
        self.entry(expense_type='public', amount='20', description='公账服务器')
        self.entry(amount='10', description='个人打车')
        rows = list(csv.DictReader(io.StringIO(self.client.get('/api/export').text.lstrip('\ufeff'))))
        by_name = {row['用途']: row for row in rows}
        self.assertEqual(by_name['公账服务器']['支出方式'], '公账支出')
        self.assertEqual(by_name['个人打车']['支出方式'], '成员垫付')
        self.assertEqual(by_name['公账服务器']['剩余待报销（原币种）'], '')
        self.assertTrue(all(row['类型'] != 'dividend' for row in rows))

    def test_removed_dividend_endpoints_and_report_fields(self):
        self.assertEqual(self.client.get('/api/dividends').status_code, 404)
        self.assertEqual(self.post('/dividends/quote', {'currency': 'CNY'}).status_code, 404)
        self.assertEqual(self.post('/dividends', {'amount': '10'}).status_code, 404)
        self.assertFalse(any('dividend' in key for key in self.report()))
        expense = self.entry()
        self.assertEqual(self.action(expense, 'distribute', '旧按钮').status_code, 400)

    def test_telegram_public_entry_requires_confirmation_and_has_no_payment_button(self):
        self.entry(kind='income', amount='100')
        self.link_bot()
        self.click(2, 'newpublic')
        token = self.draft()['token']
        self.click(3, f'd:{token}:cur:CNY')
        bot.process_update(self.message(4, '25'))
        self.assertIn('公账支出', self.last_message()['text'])
        self.assertEqual(self.report()['cash'], '100.00')
        self.click(5, f'd:{token}:save')
        self.click(6, f'd:{token}:save')
        public = next(e for e in self.client.get('/api/entries').json() if e['kind'] == 'expense')
        self.assertEqual(public['expense_type'], 'public')
        self.assertEqual(self.report()['cash'], '75.00')
        self.click(7, f'e:{public["id"]}')
        payload = self.last_message()
        buttons = [b['callback_data'] for row in payload['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertNotIn(f'pay:{public["id"]}', buttons)
        self.assertNotIn('剩余待报', payload['text'])

    def test_telegram_expense_command_and_period_summary(self):
        self.link_bot()
        bot.process_update(self.message(2, '/expense'))
        self.assertEqual(json.loads(self.draft()['payload'])['expense_type'], 'public')
        self.entry(kind='income', amount='100')
        self.entry(expense_type='public', amount='20')
        self.entry(amount='30')
        bot.process_update(self.message(3, '/today'))
        text = self.last_message()['text']
        self.assertIn('公账支出 · 1 笔', text)
        self.assertIn('成员垫付 · 1 笔', text)
        self.assertNotIn('分红', text)

    def test_old_expenses_migrate_to_advances_without_changing_payments(self):
        advance = self.entry(amount='30')
        self.assertEqual(self.action(advance, 'pay', '已报', amount='10').status_code, 200)
        before = self.report()
        with db(write=True) as conn:
            conn.execute('ALTER TABLE entries DROP COLUMN expense_type')
        init_db()
        init_db()
        after = self.report()
        self.assertEqual((after['cash'], after['paid'], after['due']), (before['cash'], before['paid'], before['due']))
        row = next(e for e in self.client.get('/api/entries').json() if e['id'] == advance['id'])
        self.assertEqual(row['expense_type'], 'advance')
        self.assertEqual(len(row['payments']), 1)

    def test_legacy_dividends_migrate_once_and_keep_currency_note_and_fx_history(self):
        self.entry(kind='income', amount='100', currency='USD')
        with db(write=True) as conn:
            conn.execute("""CREATE TABLE dividends (
                id INTEGER PRIMARY KEY,user_id INTEGER,amount_minor INTEGER,currency TEXT,decimals INTEGER,
                note TEXT,reference_currency TEXT,fx_rate TEXT,fx_date TEXT,quote_id TEXT,source_key TEXT,created_at TEXT)""")
            conn.execute("INSERT INTO dividends VALUES (1,?,2000,'USD',2,'原转账备注','CNY','7','2026-10-16','old-quote','old-record','2026-10-16T12:00:00+08:00')", (self.user['id'],))
        init_db()
        init_db()
        rows = self.client.get('/api/entries').json()
        self.assertEqual(len(rows), 2)
        public = next(e for e in rows if e['kind'] == 'expense')
        self.assertEqual((public['expense_type'], public['status'], public['amount'], public['currency'], public['note']),
                         ('public', 'posted', '20.00', 'USD', '原转账备注'))
        self.assertEqual(self.report()['cash'], '560.00')
        events = self.client.get(f'/api/entries/{public["id"]}/events').json()
        self.assertEqual(json.loads(events[0]['detail'])['fx_rate'], '7')
        with db() as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='dividends'").fetchone())

    def test_very_old_distributed_income_migrates_without_changing_cash(self):
        income = self.entry(kind='income', amount='100')
        with db(write=True) as conn:
            conn.execute("UPDATE entries SET status='distributed',review_note='历史转账' WHERE id=?", (income['id'],))
        init_db()
        init_db()
        rows = self.client.get('/api/entries').json()
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.report()['cash'], '0.00')
        public = next(e for e in rows if e['kind'] == 'expense')
        self.assertEqual(public['note'], '历史转账')
        self.assertEqual(public['expense_type'], 'public')
