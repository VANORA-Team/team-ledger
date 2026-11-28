"""Tests for distinguishing merchant currency and payment currency on expense entries."""
import io
import re
import secrets
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app import ai_context, backups, bot, config, fx
from app.db import db, init_db
from tests import test_bot_ui as ui, test_feishu as feishu_fixtures, test_ledger as base


class MerchantCurrencyTest(unittest.TestCase):
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
    # 1. Core API: CRUD, implied FX rate, fee & manual FX composition
    # ------------------------------------------------------------------

    def test_create_read_implied_rate_and_compositions(self):
        # Advance expense: merchant displays $20 USD, Alipay actually deducts 145.60 CNY
        adv = self.entry(
            kind='expense', expense_type='advance',
            amount='145.60', currency='CNY',
            merchant_mode='different', merchant_currency='USD', merchant_amount='20',
            description='OpenAI 订阅',
        )
        self.assertEqual(adv['currency'], 'CNY')
        self.assertEqual(adv['payment_currency'], 'CNY')
        self.assertEqual(adv['amount'], '145.60')
        self.assertEqual(adv['merchant_mode'], 'different')
        self.assertEqual(adv['merchant_currency'], 'USD')
        self.assertEqual(adv['merchant_decimals'], 2)
        self.assertEqual(adv['merchant_amount_minor'], 2000)
        self.assertEqual(adv['merchant_amount'], '20.00')
        self.assertNotIn('merchant_fx_rate', adv)

        # Public expense with merchant_currency only (merchant_amount omitted) + payment_currency alias
        pub_only = self.entry(
            kind='expense', expense_type='public',
            amount='520', payment_currency='CNY', currency='',
            merchant_currency='JPY',
            description='日本云服务',
        )
        self.assertEqual(pub_only['currency'], 'CNY')
        self.assertEqual(pub_only['merchant_mode'], 'different')
        self.assertEqual(pub_only['merchant_currency'], 'JPY')
        self.assertEqual(pub_only['merchant_decimals'], 0)
        self.assertEqual(pub_only['merchant_amount_minor'], 0)
        self.assertEqual(pub_only['merchant_amount'], '')

        # Full 3-layer composition: merchant EUR (display-only) -> payment USD (with fee in USD) -> reference CNY (manual FX)
        # Merchant: 100 EUR, gross payment: 108.50 USD, fee: 1.50 USD -> effective 110.00 USD * 7.20 = 792.00 CNY
        triple = self.entry(
            kind='expense', expense_type='public',
            amount='108.50', currency='USD',
            merchant_currency='EUR', merchant_amount='100',
            fee_mode='fixed', fee_amount='1.50',
            fx_mode='manual', fx_rate='7.20',
            description='欧洲服务器',
        )
        self.assertEqual(triple['gross_amount'], '108.50')
        self.assertEqual(triple['fee_amount'], '1.50')
        self.assertEqual(triple['amount'], '110.00')
        self.assertEqual(triple['merchant_currency'], 'EUR')
        self.assertEqual(triple['merchant_amount'], '100.00')
        self.assertNotIn('merchant_fx_rate', triple)
        self.assertEqual(triple['fx_mode'], 'manual')
        self.assertEqual(triple['fx_rate'], '7.2')
        self.assertEqual(triple['fx_amount'], '792.00')

        # Default same currency expense
        same = self.entry(kind='expense', amount='35', currency='CNY', description='打车')
        self.assertEqual(
            (same['merchant_mode'], same['merchant_currency'], same['merchant_amount']),
            ('same', '', ''),
        )

        # Same currency passed explicitly without merchant_mode normalizes cleanly to same
        norm_same = self.entry(
            kind='expense', amount='50', currency='CNY',
            merchant_currency='CNY', merchant_amount='50.00',
            description='同币种',
        )
        self.assertEqual((norm_same['merchant_mode'], norm_same['merchant_currency'], norm_same['merchant_amount']),
                         ('same', '', ''))

    def test_validation_errors_and_idempotency(self):
        bad_cases = [
            # Income cannot distinguish merchant currency
            dict(kind='income', income_type='operating', currency='CNY', amount='100', merchant_currency='USD'),
            dict(kind='income', income_type='capital', currency='CNY', amount='100', merchant_mode='different', merchant_currency='USD'),
            # Invalid merchant_mode
            dict(currency='CNY', amount='100', merchant_mode='bad', merchant_currency='USD'),
            # merchant_mode=different without merchant_currency
            dict(currency='CNY', amount='100', merchant_mode='different'),
            # merchant_mode=different with same currency
            dict(currency='CNY', amount='100', merchant_mode='different', merchant_currency='CNY'),
            # merchant_amount without merchant_currency
            dict(currency='CNY', amount='100', merchant_amount='20'),
            # Same currency with mismatched merchant_amount
            dict(currency='CNY', amount='100', merchant_currency='CNY', merchant_amount='20'),
            # Unenabled merchant currency
            dict(currency='CNY', amount='100', merchant_currency='ZZZ', merchant_amount='20'),
            # Invalid merchant_amount values
            dict(currency='CNY', amount='100', merchant_currency='USD', merchant_amount='0'),
            dict(currency='CNY', amount='100', merchant_currency='USD', merchant_amount='-20'),
            dict(currency='CNY', amount='100', merchant_currency='JPY', merchant_amount='100.5'),
            dict(currency='CNY', amount='100', merchant_currency='USD', merchant_amount='1,000'),
            # Conflicting currency and payment_currency
            dict(currency='CNY', payment_currency='USD', amount='100'),
        ]
        for payload in bad_cases:
            body = {'kind': 'expense', 'expense_type': 'advance', 'date': config.today(), 'description': 'bad', **payload}
            resp = self.client.post(
                '/api/entries',
                json=body,
                headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
            )
            self.assertEqual(resp.status_code, 400, f'Expected 400 for {payload}, got {resp.status_code}: {resp.text}')

        # Idempotency conflict if merchant_currency / merchant_amount differs under same key
        key = secrets.token_hex(16)
        first = self.client.post(
            '/api/entries',
            json={'kind': 'expense', 'expense_type': 'advance', 'amount': '145.6', 'currency': 'CNY',
                  'merchant_currency': 'USD', 'merchant_amount': '20', 'date': config.today(), 'description': 'API'},
            headers={**self.headers, 'Idempotency-Key': key},
        )
        self.assertEqual(first.status_code, 200, first.text)
        conflict = self.client.post(
            '/api/entries',
            json={'kind': 'expense', 'expense_type': 'advance', 'amount': '145.6', 'currency': 'CNY',
                  'merchant_currency': 'USD', 'merchant_amount': '21', 'date': config.today(), 'description': 'API'},
            headers={**self.headers, 'Idempotency-Key': key},
        )
        self.assertEqual(conflict.status_code, 409)

    def test_edit_preserves_updates_and_clears_merchant_currency(self):
        e = self.entry(
            kind='expense', expense_type='advance', amount='145.60', currency='CNY',
            merchant_currency='USD', merchant_amount='20', description='初始',
        )
        # Unrelated edit preserves merchant currency & amount
        r1 = self.client.put(
            f'/api/entries/{e["id"]}',
            json={'kind': 'expense', 'amount': '145.60', 'currency': 'CNY', 'date': e['date'], 'description': '改说明'},
            headers=self.headers,
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual((r1.json()['merchant_currency'], r1.json()['merchant_amount']), ('USD', '20.00'))

        # Partial edit of merchant_amount updates stored display amount
        r2 = self.client.put(
            f'/api/entries/{e["id"]}',
            json={'kind': 'expense', 'amount': '146.00', 'currency': 'CNY', 'date': e['date'],
                  'merchant_amount': '22', 'description': '改说明'},
            headers=self.headers,
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual((r2.json()['merchant_currency'], r2.json()['merchant_amount']), ('USD', '22.00'))

        # Changing payment currency to another currency (HKD) preserves USD merchant currency
        r3 = self.client.put(
            f'/api/entries/{e["id"]}',
            json={'kind': 'expense', 'amount': '156.00', 'currency': 'HKD', 'date': e['date'], 'description': '改港币付'},
            headers=self.headers,
        )
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual((r3.json()['currency'], r3.json()['merchant_currency'], r3.json()['merchant_amount']),
                         ('HKD', 'USD', '22.00'))

        # PUT {**entry, 'currency': 'USD'} auto-clears merchant currency when payment currency matches it
        r4 = self.client.put(
            f'/api/entries/{e["id"]}',
            json={**r3.json(), 'currency': 'USD', 'amount': '20.00'},
            headers=self.headers,
        )
        self.assertEqual(r4.status_code, 200, r4.text)
        self.assertEqual((r4.json()['currency'], r4.json()['merchant_mode'], r4.json()['merchant_currency']),
                         ('USD', 'same', ''))

        # PUT {**entry, 'merchant_currency': 'EUR', 'merchant_amount': '18'} flips same -> different
        r5 = self.client.put(
            f'/api/entries/{e["id"]}',
            json={**r4.json(), 'merchant_currency': 'EUR', 'merchant_amount': '18'},
            headers=self.headers,
        )
        self.assertEqual(r5.status_code, 200, r5.text)
        self.assertEqual((r5.json()['merchant_mode'], r5.json()['merchant_currency'], r5.json()['merchant_amount']),
                         ('different', 'EUR', '18.00'))

        # Explicit clear via merchant_mode='same'
        r6 = self.client.put(
            f'/api/entries/{e["id"]}',
            json={**r5.json(), 'merchant_mode': 'same'},
            headers=self.headers,
        )
        self.assertEqual(r6.status_code, 200, r6.text)
        self.assertEqual((r6.json()['merchant_mode'], r6.json()['merchant_currency'], r6.json()['merchant_amount']),
                         ('same', '', ''))

    # ------------------------------------------------------------------
    # 2. Accounting isolation, offline safety, & currency protection
    # ------------------------------------------------------------------

    def test_accounting_reimbursement_offline_and_settings_protection(self):
        # Add custom offline currency SGD
        conf = self.client.get('/api/settings').json()
        custom_currencies = conf['currencies'] + [{'code': 'SGD', 'name': '新币', 'symbol': 'S$', 'decimals': 2}]
        put_ok = self.client.put('/api/settings', json={**conf, 'currencies': custom_currencies}, headers=self.headers)
        self.assertEqual(put_ok.status_code, 200, put_ok.text)

        # Operating income in CNY
        self.entry(kind='income', income_type='operating', amount='1000', currency='CNY', description='回款')
        # Public expense: merchant quoted 50 SGD, actually paid 270 CNY
        self.entry(
            kind='expense', expense_type='public', amount='270', currency='CNY',
            merchant_currency='SGD', merchant_amount='50', description='新加坡服务商',
        )
        # Advance expense: merchant quoted 20 USD, actually paid 145.60 CNY
        adv = self.entry(
            kind='expense', expense_type='advance', amount='145.60', currency='CNY',
            merchant_currency='USD', merchant_amount='20', description='OpenAI API',
        )

        # Even with external FX completely offline, report & public_balance succeed because actual payment currency is CNY!
        with patch('app.fx.fetch_rates', side_effect=RuntimeError('offline')):
            rep = self.report()
            pb = self.public_balance()
        self.assertTrue(rep['complete'])
        self.assertEqual(rep['actual_cash'], '730.00')
        self.assertEqual(rep['public_paid'], '270.00')
        self.assertEqual(rep['due'], '145.60')
        self.assertEqual(rep['actual_net_after_due'], '584.40')
        # Pending member entry includes display-only merchant metadata (no merchant FX conversion)
        adv_item = rep['members'][0]['entries'][0]
        self.assertEqual((adv_item['merchant_currency'], adv_item['merchant_amount']), ('USD', '20.00'))
        self.assertNotIn('merchant_fx_rate', adv_item)
        # Public balance currency accounts only have CNY (never SGD or USD!)
        self.assertEqual([a['code'] for a in pb['currency_accounts']], ['CNY'])
        self.assertEqual(pb['currency_accounts'][0]['balance'], '730.00')

        # Reimbursement settles exact CNY amount (145.60)
        q = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'CNY'}).json()[0]
        self.assertEqual((q['currency'], q['maximum_amount']), ('CNY', '145.60'))
        paid = self.client.post(
            '/api/entries/action',
            json={'action': 'pay', 'ids': [adv['id']], 'note': '银行转账',
                  'payments': [{'id': adv['id'], 'amount': '145.60', 'quote_id': q['id']}]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(paid.status_code, 200, paid.text)
        self.assertEqual(paid.json()[0]['status'], 'reimbursed')

        # Settings protection: cannot remove SGD or change its decimals because it's referenced as merchant_currency
        without_sgd = [c for c in custom_currencies if c['code'] != 'SGD']
        del_sgd = self.client.put('/api/settings', json={**conf, 'currencies': without_sgd}, headers=self.headers)
        self.assertEqual(del_sgd.status_code, 400)
        self.assertIn('SGD', del_sgd.json()['error'])

        bad_dec_sgd = [{**c, 'decimals': 0} if c['code'] == 'SGD' else c for c in custom_currencies]
        chg_sgd = self.client.put('/api/settings', json={**conf, 'currencies': bad_dec_sgd}, headers=self.headers)
        self.assertEqual(chg_sgd.status_code, 400)
        self.assertIn('SGD', chg_sgd.json()['error'])

    # ------------------------------------------------------------------
    # 3. Excel export columns & merchant_currency filter
    # ------------------------------------------------------------------

    def test_excel_export_columns_and_merchant_currency_filter(self):
        e_split = self.entry(
            kind='expense', expense_type='advance', amount='145.60', currency='CNY',
            merchant_currency='USD', merchant_amount='20', description='Cursor Pro',
        )
        e_same = self.entry(
            kind='expense', expense_type='advance', amount='50', currency='CNY',
            description='本地打车',
        )
        resp = self.client.get('/api/export.xlsx')
        self.assertEqual(resp.status_code, 200, resp.text)
        wb = load_workbook(io.BytesIO(resp.content))

        ws_entries = wb['收支明细']
        headers = [c.value for c in ws_entries[1]]
        rows = [dict(zip(headers, [c.value for c in r])) for r in ws_entries.iter_rows(min_row=2)]
        by_id = {r['编号']: r for r in rows}
        self.assertEqual(by_id[e_split['id']]['币种'], 'CNY')
        self.assertEqual(float(by_id[e_split['id']]['金额（原币种）']), 145.6)
        self.assertEqual(by_id[e_split['id']]['商家货币'], 'USD')
        self.assertEqual(float(by_id[e_split['id']]['商家金额']), 20.0)
        self.assertNotIn('付款折算汇率', headers)
        self.assertEqual(by_id[e_same['id']]['商家货币'], '—')
        self.assertIsNone(by_id[e_same['id']]['商家金额'])

        ws_pending = wb['成员待报销']
        p_headers = [c.value for c in ws_pending[1]]
        p_rows = [dict(zip(p_headers, [c.value for c in r])) for r in ws_pending.iter_rows(min_row=2)]
        p_by_id = {r['账目编号']: r for r in p_rows}
        self.assertEqual(p_by_id[e_split['id']]['商家货币'], 'USD')
        self.assertEqual(float(p_by_id[e_split['id']]['商家金额']), 20.0)
        self.assertEqual(p_by_id[e_same['id']]['商家货币'], '—')

        # Filter export by merchant_currency=USD
        resp_f = self.client.get('/api/export.xlsx?merchant_currency=USD')
        self.assertEqual(resp_f.status_code, 200)
        wb_f = load_workbook(io.BytesIO(resp_f.content))
        ws_f = wb_f['收支明细']
        h_f = [c.value for c in ws_f[1]]
        ids_f = [dict(zip(h_f, [c.value for c in r]))['编号'] for r in ws_f.iter_rows(min_row=2)]
        self.assertEqual(ids_f, [e_split['id']])

    # ------------------------------------------------------------------
    # 4. AI assistant: create, edit, query filter, and context
    # ------------------------------------------------------------------

    def test_ai_plan_create_edit_query_and_context(self):
        create_raw = {
            'reply': '已识别区分商家货币与付款货币的支出。',
            'questions': [],
            'actions': [{
                'type': 'create_entry',
                'kind': 'expense',
                'expense_type': 'advance',
                'amount': '145.6',
                'currency': 'CNY',
                'merchant_currency': 'USD',
                'merchant_amount': '20',
                'description': 'Claude 订阅',
                'date': config.today(),
            }],
        }
        with patch('app.ai.plan', return_value=create_raw):
            plan = self.post('/ai/plan', {'text': '我垫了 Claude 订阅，界面显示 20 美元，实际扣款 145.6 元'}).json()
        self.assertTrue(plan['executable'], plan)
        card = plan['actions'][0]
        self.assertEqual(card['resolved']['currency'], 'CNY')
        self.assertEqual(card['resolved']['merchant_currency'], 'USD')
        self.assertEqual(card['resolved']['merchant_amount'], '20.00')
        self.assertNotIn('merchant_fx_rate', card['resolved'])
        self.assertIn('商家标价', card['summary'])
        self.assertNotIn('实付汇率', card['summary'])

        exec_res = self.client.post(
            '/api/ai/execute',
            json={'plan_id': plan['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(exec_res.status_code, 200, exec_res.text)
        entry = self.client.get('/api/entries').json()[0]
        self.assertEqual((entry['currency'], entry['amount'], entry['merchant_currency'], entry['merchant_amount']),
                         ('CNY', '145.60', 'USD', '20.00'))

        # AI context exposes merchant_currency & merchant_amount
        with db() as conn:
            from app.db import settings as get_settings
            user = dict(conn.execute('SELECT * FROM users LIMIT 1').fetchone())
            ctx = ai_context.build(conn, get_settings(conn), user)
        ctx_entry = next(x for x in ctx['entries'] if x['id'] == entry['id'])
        self.assertEqual((ctx_entry['merchant_currency'], ctx_entry['merchant_amount']), ('USD', '20.00'))

        # AI query with filter_merchant_currency
        query_raw = {
            'reply': '查询商家货币为 USD 的支出。',
            'questions': [],
            'actions': [{'type': 'query', 'query': 'list_entries', 'filter_merchant_currency': 'USD'}],
        }
        with patch('app.ai.plan', return_value=query_raw):
            q_plan = self.post('/ai/plan', {'text': '查一下商家货币是美元的支出'}).json()
        self.assertEqual(q_plan['results'][0]['card']['total'], 1)
        self.assertEqual(q_plan['results'][0]['card']['entries'][0]['id'], entry['id'])

        # AI edit: update merchant amount, then clear back to same
        edit_raw1 = {'reply': '修改商家金额。', 'questions': [], 'actions': [
            {'type': 'edit_entry', 'id': entry['id'], 'merchant_currency': 'EUR', 'merchant_amount': '18.2'},
        ]}
        with patch('app.ai.plan', return_value=edit_raw1):
            p1 = self.post('/ai/plan', {'text': '把商家金额改成 18.2 欧元'}).json()
        self.assertFalse(p1['actions'][0]['errors'], p1)
        self.client.post(
            '/api/ai/execute',
            json={'plan_id': p1['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        updated = self.client.get('/api/entries').json()[0]
        self.assertEqual((updated['merchant_currency'], updated['merchant_amount']), ('EUR', '18.20'))

        edit_raw2 = {'reply': '取消区分商家货币。', 'questions': [], 'actions': [
            {'type': 'edit_entry', 'id': entry['id'], 'merchant_mode': 'same'},
        ]}
        with patch('app.ai.plan', return_value=edit_raw2):
            p2 = self.post('/ai/plan', {'text': '取消商家货币区分'}).json()
        self.assertFalse(p2['actions'][0]['errors'], p2)
        self.client.post(
            '/api/ai/execute',
            json={'plan_id': p2['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        cleared = self.client.get('/api/entries').json()[0]
        self.assertEqual((cleared['merchant_mode'], cleared['merchant_currency'], cleared['merchant_amount']),
                         ('same', '', ''))

    # ------------------------------------------------------------------
    # 5. Telegram & Feishu bot flows
    # ------------------------------------------------------------------

    def test_telegram_and_feishu_merchant_currency_flows(self):
        self.link_bot()
        # Telegram /book -> CNY -> 145.6 -> edit:merchant -> mcur:USD -> 20 -> save
        bot.process_update(self.message(10, '/book'))
        token = self.draft()['token']
        self.click(11, f'd:{token}:cur:CNY')
        bot.process_update(self.message(12, '145.6'))
        buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertIn(f'd:{token}:edit:merchant', buttons)
        self.click(13, f'd:{token}:edit:merchant')
        self.click(14, f'd:{token}:mcur:USD')
        bot.process_update(self.message(15, '20'))
        preview_txt = self.last_message()['text']
        self.assertIn('商家标价：$20.00 USD（仅展示）', preview_txt)
        self.assertNotIn('实付汇率', preview_txt)
        self.assertIn('实际付款：¥145.60 CNY', preview_txt)
        self.click(16, f'd:{token}:save')

        entry = self.client.get('/api/entries').json()[0]
        self.assertEqual((entry['currency'], entry['amount'], entry['merchant_currency'], entry['merchant_amount']),
                         ('CNY', '145.60', 'USD', '20.00'))

        # Detail shows merchant currency
        self.click(17, f'e:{entry["id"]}')
        self.assertIn('商家标价：$20.00 USD（仅展示）', self.last_message()['text'])

        # Edit preserves merchant currency, skipping amount with '-' keeps currency only, 'same' clears it
        self.click(18, f'edit:{entry["id"]}')
        edit_tok = self.draft()['token']
        self.assertIn('商家标价：$20.00 USD（仅展示）', self.last_message()['text'])
        self.click(19, f'd:{edit_tok}:edit:merchant')
        self.click(20, f'd:{edit_tok}:mcur:EUR')
        bot.process_update(self.message(21, '-'))
        self.assertIn('商家货币：EUR（仅展示）', self.last_message()['text'])
        self.click(22, f'd:{edit_tok}:edit:merchant')
        self.click(23, f'd:{edit_tok}:mcur:same')
        self.assertNotIn('商家货币：', self.last_message()['text'])
        self.click(24, f'd:{edit_tok}:save')
        self.assertEqual(self.client.get('/api/entries').json()[0]['merchant_mode'], 'same')

        # Income draft does NOT show 商家货币 button
        bot.process_update(self.message(30, '/income'))
        inc_tok = self.draft()['token']
        self.click(31, f'd:{inc_tok}:cur:CNY')
        bot.process_update(self.message(32, '500'))
        inc_buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertNotIn(f'd:{inc_tok}:edit:merchant', inc_buttons)

        # Feishu quick text commands with 商家 + 手续费 + 汇率
        link = self.post('/feishu/link-code').json()['code']
        self.feishu_message('bind', '绑定 ' + link)
        self.assertIn('绑定成功', self.feishu_process())

        self.feishu_message(1, '公账支出 145.6 CNY 买域名 商家 20 USD')
        p1 = self.feishu_process()
        self.assertIn('商家标价 $20.00 USD', p1)
        code1 = re.search(r'确认 ([A-F0-9]{8})', p1)[1]
        self.feishu_message(2, '确认 ' + code1)
        self.assertIn('已完成', self.feishu_process())

        self.feishu_message(3, '成员垫付 108.5 USD 欧洲主机 商家 EUR 100 手续费 1.5 汇率 7.2')
        p2 = self.feishu_process()
        self.assertIn('商家标价 €100.00 EUR', p2)
        self.assertIn('手动汇率 1 USD = 7.2 CNY', p2)
        code2 = re.search(r'确认 ([A-F0-9]{8})', p2)[1]
        self.feishu_message(4, '确认 ' + code2)
        self.assertIn('已完成', self.feishu_process())

        by_desc = {e['description']: e for e in self.client.get('/api/entries').json()}
        self.assertEqual((by_desc['买域名']['currency'], by_desc['买域名']['merchant_currency'], by_desc['买域名']['merchant_amount']),
                         ('CNY', 'USD', '20.00'))
        self.assertEqual((by_desc['欧洲主机']['currency'], by_desc['欧洲主机']['amount'], by_desc['欧洲主机']['merchant_currency'],
                          by_desc['欧洲主机']['merchant_amount'], by_desc['欧洲主机']['fx_rate']),
                         ('USD', '110.00', 'EUR', '100.00', '7.2'))

    # ------------------------------------------------------------------
    # 6. Legacy DB migration & backup/restore compatibility
    # ------------------------------------------------------------------

    def test_legacy_db_migration_and_backup_restore_compatibility(self):
        e = self.entry(
            kind='expense', expense_type='advance', amount='145.60', currency='CNY',
            merchant_currency='USD', merchant_amount='20', description='备份还原验证',
        )
        created = backups.create_backup(['local'])
        backup_name = created['name']
        self.assertIn('merchant_currency', backups.OPTIONAL_COLUMNS['entries'])
        self.assertIn('merchant_decimals', backups.OPTIONAL_COLUMNS['entries'])
        self.assertIn('merchant_amount_minor', backups.OPTIONAL_COLUMNS['entries'])

        # Mutate and restore, verifying merchant fields round-trip intact
        self.client.put(
            f'/api/entries/{e["id"]}',
            json={**e, 'merchant_mode': 'same'},
            headers=self.headers,
        )
        self.assertEqual(self.client.get('/api/entries').json()[0]['merchant_mode'], 'same')
        backups.restore_backup('local', backup_name)
        session = self.login_as(self.client, 'vanora-owner-alice', preferred_username='alice', name='小林')
        self.headers = {'X-CSRF-Token': session['csrf']}
        restored = self.client.get('/api/entries').json()[0]
        self.assertEqual((restored['merchant_mode'], restored['merchant_currency'], restored['merchant_amount']),
                         ('different', 'USD', '20.00'))

        # Verify legacy backup missing merchant_* columns restores cleanly and init_db() migrates them
        with db(write=True) as conn:
            conn.execute('ALTER TABLE entries DROP COLUMN merchant_currency')
            conn.execute('ALTER TABLE entries DROP COLUMN merchant_decimals')
            conn.execute('ALTER TABLE entries DROP COLUMN merchant_amount_minor')
        legacy_backup = backups.create_backup(['local'])
        backups.restore_backup('local', legacy_backup['name'])
        init_db()
        with db() as conn:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(entries)')}
        self.assertTrue({'merchant_currency', 'merchant_decimals', 'merchant_amount_minor'} <= cols)

    # ------------------------------------------------------------------
    # 7. Manual FX rate at reimbursement payment time (fallback to system FX)
    # ------------------------------------------------------------------

    def test_manual_fx_rate_for_reimbursement_payment_and_system_fallback(self):
        # Advance expense paid in USD (no booking-time manual FX), merchant displayed EUR
        adv = self.entry(
            kind='expense', expense_type='advance',
            amount='100', currency='USD',
            merchant_currency='EUR', merchant_amount='92',
            description='海外服务器年付',
        )
        # 1. Without manual fx_rate -> falls back to system reference rate (7.0 from LedgerTest setUp)
        q_sys = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'CNY'}).json()[0]
        self.assertEqual((q_sys['currency'], q_sys['rate'], q_sys['rate_source'], q_sys['maximum_amount']),
                         ('CNY', '7', 'reference', '700.00'))

        # 2. With manual fx_rate -> overrides system rate AND works even when external FX API is offline
        with patch('app.fx.fetch_rates', side_effect=RuntimeError('offline')):
            q_man = self.post('/entries/payment-quotes', {
                'ids': [adv['id']], 'currency': 'CNY', 'fx_rate': '7.1800',
            }).json()[0]
        self.assertEqual((q_man['currency'], q_man['rate'], q_man['rate_source'], q_man['rate_date'], q_man['maximum_amount']),
                         ('CNY', '7.18', 'manual', None, '718.00'))

        # 3. Same-currency quote rejects manual fx_rate
        bad_same = self.post('/entries/payment-quotes', {'ids': [adv['id']], 'currency': 'USD', 'fx_rate': '7.18'})
        self.assertEqual(bad_same.status_code, 400)

        # 4. Partial reimbursement at manual rate 7.18: pay 359 CNY -> credits 50 USD, remaining 50 USD
        pay1 = self.client.post(
            '/api/entries/action',
            json={'action': 'pay', 'ids': [adv['id']], 'note': '手动汇率首笔报销',
                  'payments': [{'id': adv['id'], 'amount': '359.00', 'quote_id': q_man['id']}]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(pay1.status_code, 200, pay1.text)
        p_after = pay1.json()[0]
        self.assertEqual((p_after['reimbursed_amount'], p_after['remaining_amount']), ('50.00', '50.00'))
        self.assertEqual((p_after['payments'][0]['fx_rate'], p_after['payments'][0]['fx_date']), ('7.18', None))

        # 5. Entry with booking-time manual FX can still explicitly override to system rate via fx_mode='auto'
        adv_m = self.entry(
            kind='expense', expense_type='advance',
            amount='100', currency='USD', fx_mode='manual', fx_rate='7.50', description='带记账汇率垫付',
        )
        q_default_m = self.post('/entries/payment-quotes', {'ids': [adv_m['id']], 'currency': 'CNY'}).json()[0]
        self.assertEqual((q_default_m['rate'], q_default_m['rate_source']), ('7.5', 'manual'))
        q_sys_override = self.post('/entries/payment-quotes', {
            'ids': [adv_m['id']], 'currency': 'CNY', 'fx_mode': 'auto',
        }).json()[0]
        self.assertEqual((q_sys_override['rate'], q_sys_override['rate_source']), ('7', 'reference'))
        # And per-entry fx_rates map works
        q_map = self.post('/entries/payment-quotes', {
            'ids': [adv_m['id']], 'currency': 'CNY', 'fx_rates': {str(adv_m['id']): '7.12'},
        }).json()[0]
        self.assertEqual((q_map['rate'], q_map['maximum_amount']), ('7.12', '712.00'))

        # 6. AI pay_entry with manual fx_rate works offline and records manual rate
        ai_pay_raw = {
            'reply': '按 7.20 汇率报销剩余 50 USD。',
            'questions': [],
            'actions': [{
                'type': 'pay_entry',
                'id': adv['id'],
                'settlement_currency': 'CNY',
                'fx_rate': '7.20',
                'note': '银行转账',
            }],
        }
        with patch('app.ai.plan', return_value=ai_pay_raw), patch('app.fx.fetch_rates', side_effect=RuntimeError('offline')):
            ai_p = self.post('/ai/plan', {'text': f'把 #{adv["id"]} 按汇率 7.20 用人民币全额报销'}).json()
        self.assertTrue(ai_p['executable'], ai_p)
        self.assertEqual((ai_p['actions'][0]['resolved']['amount'], ai_p['actions'][0]['resolved']['fx_rate']),
                         ('360.00', '7.2'))
        exec_p = self.client.post(
            '/api/ai/execute',
            json={'plan_id': ai_p['plan_id'], 'actions': [0]},
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(exec_p.status_code, 200, exec_p.text)
        final_adv = next(x for x in self.client.get('/api/entries').json() if x['id'] == adv['id'])
        self.assertEqual(final_adv['status'], 'reimbursed')
        self.assertEqual(final_adv['payments'][1]['amount'], '360.00')
        self.assertEqual(final_adv['payments'][1]['fx_rate'], '7.2')

        # 7. Telegram bot single reimbursement with manual FX rate (payfxcur -> rate -> amount -> note -> commit)
        self.link_bot()
        self.click(100, f'pay:{adv_m["id"]}')
        tok = self.draft()['token']
        buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertIn(f'd:{tok}:payfxcur:CNY', buttons)
        self.click(101, f'd:{tok}:payfxcur:CNY')
        bot.process_update(self.message(102, '7.15'))
        self.assertIn('715.00 CNY', self.last_message()['text'])
        bot.process_update(self.message(103, '全额'))
        bot.process_update(self.message(104, '机器人手动汇率报销'))
        self.assertIn('1 USD = 7.15 CNY · 手动汇率', self.last_message()['text'])
        self.click(105, f'd:{tok}:commit')
        done_m = next(x for x in self.client.get('/api/entries').json() if x['id'] == adv_m['id'])
        self.assertEqual(done_m['status'], 'reimbursed')
        self.assertEqual((done_m['payments'][0]['amount'], done_m['payments'][0]['fx_rate']), ('715.00', '7.15'))


if __name__ == '__main__':
    unittest.main()
