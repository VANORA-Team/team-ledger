"""Owner-only historical bookkeeping edits across REST API, receipts, AI assistant, Telegram and Feishu."""
import base64
import json
import secrets
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import bot, config, feishu, feishu_ui
from app.db import db
from app.main import app
from tests import test_ledger as base


class HistoricalEditTest(unittest.TestCase):
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

    def _client_for(self, sub, username, name, role):
        member = self.new_member(sub=sub, username=username, name=name, role=role)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        session = self.login_as(client, sub, preferred_username=username, name=name, roles=(role,))
        return member, client, {'X-CSRF-Token': session['csrf']}

    def _edit(self, client, headers, entry_id, payload):
        return client.put(f'/api/entries/{entry_id}', json=payload, headers=headers)

    def _entry_by_id(self, entry_id):
        return next(e for e in self.client.get('/api/entries').json() if e['id'] == entry_id)

    def _ai_plan(self, client, headers, actions, reply='已整理。'):
        raw = {'reply': reply, 'actions': actions, 'questions': []}
        with patch('app.ai.plan', return_value=raw):
            res = client.post('/api/ai/plan', json={'text': '修改历史账目'}, headers=headers)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _ai_execute(self, client, headers, plan):
        selected = [card['index'] for card in plan['actions'] if not card['errors']]
        return client.post(
            '/api/ai/execute',
            json={'plan_id': plan['plan_id'], 'actions': selected},
            headers={**headers, 'Idempotency-Key': secrets.token_hex(16)},
        )

    def _last_tg_message(self):
        with db() as conn:
            row = conn.execute(
                "SELECT payload FROM tg_outbox WHERE method IN ('sendMessage','editMessageText') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row['payload'])

    def _last_tg_alert(self):
        with db() as conn:
            row = conn.execute(
                "SELECT payload FROM tg_outbox WHERE method='answerCallbackQuery' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row['payload'])

    def _tg_click(self, update_id, data, sender=123, chat=123):
        update = self.callback(update_id, data, sender, chat)
        update['callback_query']['message'].update(message_id=77)
        bot.process_update(update)

    def _tg_draft(self):
        with db() as conn:
            return dict(conn.execute('SELECT * FROM tg_drafts ORDER BY rowid DESC LIMIT 1').fetchone())

    def test_rbac_only_owner_can_edit_partially_or_fully_reimbursed_entries(self):
        fin, fin_client, fin_headers = self._client_for('vanora-fin-1', 'fin1', '财务小李', 'finance')
        mem, mem_client, mem_headers = self._client_for('vanora-mem-1', 'mem1', '成员小王', 'member')

        partial = self.entry(amount='100', currency='USD', description='部分报销账目', payer_id=mem['id'])
        self.assertEqual(self.action(partial, 'pay', '首次报销', amount='40').status_code, 200)
        partial = self._entry_by_id(partial['id'])

        reimbursed = self.entry(amount='80', currency='CNY', description='全额报销账目', payer_id=mem['id'])
        self.assertEqual(self.action(reimbursed, 'pay', '全额结清').status_code, 200)
        reimbursed = self._entry_by_id(reimbursed['id'])

        unpaid_adv = self.entry(amount='50', currency='CNY', description='未报销垫付', payer_id=mem['id'])
        public_exp = self.entry(expense_type='public', amount='60', currency='CNY', description='公账支出')
        income = self.entry(kind='income', amount='200', currency='CNY', description='营业收入')
        voided = self.entry(amount='30', currency='CNY', description='作废账目')
        self.assertEqual(self.action(voided, 'void', '重复录入').status_code, 200)

        # Finance and member are strictly blocked (403) on partially and fully reimbursed entries.
        for target in (partial, reimbursed):
            for role_client, role_headers in ((fin_client, fin_headers), (mem_client, mem_headers)):
                res = self._edit(role_client, role_headers, target['id'], {
                    'kind': 'expense', 'expense_type': 'advance', 'amount': target['amount'],
                    'currency': target['currency'], 'description': '越权修改', 'date': target['date'],
                    'payer_id': target['payer_id'], 'status': target['status'],
                })
                self.assertEqual(res.status_code, 403, res.text)
                self.assertIn('仅账本所有者', res.json()['error'])

        # Finance retains existing edit permissions on unpaid advances, posted public expenses, and posted incomes.
        for target in (unpaid_adv, public_exp, income):
            res = self._edit(fin_client, fin_headers, target['id'], {
                'kind': target['kind'], 'expense_type': target['expense_type'], 'income_type': target.get('income_type', ''),
                'amount': target['amount'], 'currency': target['currency'], 'description': target['description'] + '·财务已改',
                'date': target['date'], 'payer_id': target['payer_id'], 'status': target['status'],
            })
            self.assertEqual(res.status_code, 200, res.text)

        # Member retains edit permission on own unpaid advance.
        res = self._edit(mem_client, mem_headers, unpaid_adv['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '55', 'currency': 'CNY',
            'description': '成员自改未报销', 'date': unpaid_adv['date'], 'payer_id': mem['id'], 'status': 'pending',
        })
        self.assertEqual(res.status_code, 200, res.text)

        # Voided entries remain non-editable even for owner.
        res = self._edit(self.client, self.headers, voided['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '30', 'currency': 'CNY',
            'description': '尝试改作废', 'date': voided['date'], 'payer_id': voided['payer_id'], 'status': 'pending',
        })
        self.assertEqual(res.status_code, 409)

        # Owner CAN edit both partially and fully reimbursed entries.
        res_p = self._edit(self.client, self.headers, partial['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '100', 'currency': 'USD',
            'description': '所有者修正部分报销', 'date': partial['date'], 'payer_id': partial['payer_id'],
        })
        self.assertEqual(res_p.status_code, 200, res_p.text)
        res_r = self._edit(self.client, self.headers, reimbursed['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '80', 'currency': 'CNY',
            'description': '所有者修正已报销', 'date': reimbursed['date'], 'payer_id': reimbursed['payer_id'],
        })
        self.assertEqual(res_r.status_code, 200, res_r.text)

    def test_owner_historical_edit_preserves_payments_syncs_status_and_enforces_invariants(self):
        self.entry(kind='income', amount='2000', currency='CNY')
        entry = self.entry(amount='100', currency='USD', description='旧服务器', date='2026-10-01', note='原备注')
        self.assertEqual(self.action(entry, 'pay', '首期转账40', amount='40').status_code, 200)
        self.assertEqual(self.action(entry, 'pay', '尾款转账60', amount='60').status_code, 200)
        paid_entry = self._entry_by_id(entry['id'])
        self.assertEqual(paid_entry['status'], 'reimbursed')
        orig_paid_at = paid_entry['paid_at']
        orig_review_note = paid_entry['review_note']
        orig_payments = paid_entry['payments']

        # Locked invariants return 409.
        for bad_override, label in (
            ({'currency': 'CNY'}, 'change_currency'),
            ({'payment_currency': 'EUR'}, 'change_payment_currency'),
            ({'expense_type': 'public'}, 'change_expense_type'),
            ({'status': 'draft'}, 'revert_to_draft'),
            ({'amount': '99.99'}, 'below_reimbursed'),
        ):
            with self.subTest(case=label):
                payload = {
                    'kind': 'expense', 'expense_type': 'advance', 'amount': '100', 'currency': 'USD',
                    'description': '旧服务器', 'date': '2026-10-01', 'payer_id': paid_entry['payer_id'],
                }
                payload.update(bad_override)
                res = self._edit(self.client, self.headers, paid_entry['id'], payload)
                self.assertEqual(res.status_code, 409, res.text)

        # Raising amount on fully reimbursed entry (100 -> 120 + 5 fee = 125) transitions back to pending (partial)
        # while keeping all payment records, review_note, paid_at, and updating report due without changing paid.
        res = self._edit(self.client, self.headers, paid_entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '120', 'currency': 'USD',
            'fee_mode': 'fixed', 'fee_amount': '5',
            'fx_mode': 'manual', 'fx_rate': '7.2', 'fx_currency': 'CNY',
            'merchant_mode': 'different', 'merchant_currency': 'EUR', 'merchant_amount': '110',
            'description': '新服务器续费', 'date': '2026-10-02', 'note': '修正金额与手续费',
            'payer_id': paid_entry['payer_id'], 'status': 'reimbursed',
        })
        self.assertEqual(res.status_code, 200, res.text)
        updated = res.json()
        self.assertEqual(updated['status'], 'pending')
        self.assertEqual((updated['gross_amount'], updated['fee_amount'], updated['amount']), ('120.00', '5.00', '125.00'))
        self.assertEqual((updated['reimbursed_amount'], updated['remaining_amount']), ('100.00', '25.00'))
        self.assertEqual((updated['fx_rate'], updated['fx_currency']), ('7.2', 'CNY'))
        self.assertEqual((updated['merchant_currency'], updated['merchant_amount']), ('EUR', '110.00'))
        self.assertEqual(updated['paid_at'], orig_paid_at)
        self.assertEqual(updated['review_note'], orig_review_note)
        self.assertEqual(updated['payments'], orig_payments)

        # Verify audit log records detailed diff with owner historical prefix.
        events = self.client.get(f'/api/entries/{paid_entry["id"]}/events').json()
        edit_ev = [ev for ev in events if ev['action'] == 'edited'][-1]
        self.assertIn('所有者修改已报销历史账目：', edit_ev['detail'])
        self.assertIn('100.00 USD → 125.00 USD', edit_ev['detail'])
        self.assertIn('已报销 → 部分报销', edit_ev['detail'])

        # Removing fee when gross_amount (95) < reimbursed (100) must fail with 409.
        res_low_fee = self._edit(self.client, self.headers, paid_entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '95', 'currency': 'USD',
            'fee_mode': 'none', 'description': '新服务器续费', 'date': '2026-10-02', 'payer_id': paid_entry['payer_id'],
        })
        self.assertEqual(res_low_fee.status_code, 409)

        # Payment quote taken before an edit is invalidated when owner edits the entry.
        stale_quote = self.post('/entries/payment-quotes', {'ids': [paid_entry['id']], 'currency': 'original'}).json()[0]
        res_meta = self._edit(self.client, self.headers, paid_entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '120', 'currency': 'USD',
            'fee_mode': 'fixed', 'fee_amount': '5', 'description': '再次更新备注', 'date': '2026-10-02',
            'note': '触发版本递增', 'payer_id': paid_entry['payer_id'],
        })
        self.assertEqual(res_meta.status_code, 200)
        stale_pay = self.client.post('/api/entries/action', json={
            'ids': [paid_entry['id']], 'action': 'pay', 'note': '旧报价付款',
            'payments': [{'id': paid_entry['id'], 'amount': '25', 'quote_id': stale_quote['id']}],
        }, headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)})
        self.assertEqual(stale_pay.status_code, 409)

        # Lowering amount down to exact reimbursed total (100 USD) auto-transitions pending -> reimbursed.
        res_settle = self._edit(self.client, self.headers, paid_entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '100', 'currency': 'USD',
            'fee_mode': 'none', 'description': '调回已结清', 'date': '2026-10-02', 'payer_id': paid_entry['payer_id'],
        })
        self.assertEqual(res_settle.status_code, 200, res_settle.text)
        settled = res_settle.json()
        self.assertEqual((settled['status'], settled['remaining_amount']), ('reimbursed', '0.00'))

        # Raise again to 110 USD and let finance pay remaining 10 USD cleanly.
        self.assertEqual(self._edit(self.client, self.headers, paid_entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '110', 'currency': 'USD',
            'fee_mode': 'none', 'description': '补差价10美元', 'date': '2026-10-02', 'payer_id': paid_entry['payer_id'],
        }).status_code, 200)
        _, fin_client, fin_headers = self._client_for('vanora-fin-2', 'fin2', '财务', 'finance')
        q = fin_client.post('/api/entries/payment-quotes', json={'ids': [paid_entry['id']], 'currency': 'original'}, headers=fin_headers).json()[0]
        self.assertEqual(q['maximum_amount'], '10.00')
        pay_res = fin_client.post('/api/entries/action', json={
            'ids': [paid_entry['id']], 'action': 'pay', 'note': '补报10美元',
            'payments': [{'id': paid_entry['id'], 'amount': '10', 'quote_id': q['id']}],
        }, headers={**fin_headers, 'Idempotency-Key': secrets.token_hex(16)})
        self.assertEqual(pay_res.status_code, 200, pay_res.text)
        final_entry = pay_res.json()[0]
        self.assertEqual((final_entry['status'], final_entry['reimbursed_amount'], len(final_entry['payments'])), ('reimbursed', '110.00', 3))

    def test_owner_can_edit_entry_whose_historical_payer_was_deactivated(self):
        old_mem = self.new_member(sub='vanora-old-mem', username='oldmem', name='离职成员', role='member')
        other_inactive = self.new_member(sub='vanora-other-inactive', username='inactive2', name='另一离职', role='member')
        entry = self.entry(amount='100', currency='CNY', description='历史报销', payer_id=old_mem['id'])
        self.assertEqual(self.action(entry, 'pay', '已报销').status_code, 200)

        # Deactivate both members via Vanora sync.
        self.oidc.set_user('vanora-old-mem', enabled=False)
        self.oidc.set_user('vanora-other-inactive', enabled=False)
        self.assertEqual(self.post('/members/sync').status_code, 200)

        # Keeping the deactivated historical payer succeeds for owner.
        res_keep = self._edit(self.client, self.headers, entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '100', 'currency': 'CNY',
            'description': '历史报销（修正说明）', 'date': entry['date'], 'payer_id': old_mem['id'],
        })
        self.assertEqual(res_keep.status_code, 200, res_keep.text)
        self.assertEqual(res_keep.json()['payer_id'], old_mem['id'])

        # Switching to a different deactivated member must still be rejected.
        res_switch = self._edit(self.client, self.headers, entry['id'], {
            'kind': 'expense', 'expense_type': 'advance', 'amount': '100', 'currency': 'CNY',
            'description': '历史报销', 'date': entry['date'], 'payer_id': other_inactive['id'],
        })
        self.assertEqual(res_switch.status_code, 400)

    def test_receipt_upload_on_reimbursed_entry_is_owner_only(self):
        _, fin_client, fin_headers = self._client_for('vanora-fin-r', 'finr', '财务', 'finance')
        mem, mem_client, mem_headers = self._client_for('vanora-mem-r', 'memr', '成员', 'member')
        entry = self.entry(amount='50', currency='CNY', description='待传凭证', payer_id=mem['id'])
        self.assertEqual(self.action(entry, 'pay', '结清').status_code, 200)

        pdf = {'data': base64.b64encode(b'%PDF-1.4 receipt').decode(), 'name': 'invoice.pdf'}
        self.assertEqual(fin_client.post(f'/api/entries/{entry["id"]}/receipt', json=pdf, headers=fin_headers).status_code, 403)
        self.assertEqual(mem_client.post(f'/api/entries/{entry["id"]}/receipt', json=pdf, headers=mem_headers).status_code, 403)
        res = self.post(f'/entries/{entry["id"]}/receipt', pdf)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(self._entry_by_id(entry['id'])['receipt'])
        self.assertEqual(self.client.get(f'/api/entries/{entry["id"]}/receipt').status_code, 200)

    def test_ai_plan_allows_owner_and_blocks_finance_on_reimbursed_entries(self):
        _, fin_client, fin_headers = self._client_for('vanora-fin-ai', 'finai', '财务', 'finance')
        entry = self.entry(amount='100', currency='USD', description='云服务')
        self.assertEqual(self.action(entry, 'pay', '全额报销').status_code, 200)

        # Finance AI edit plan is rejected with owner-only error card.
        fin_plan = self._ai_plan(fin_client, fin_headers, [{'type': 'edit_entry', 'id': entry['id'], 'description': '财务尝试改'}])
        self.assertIsNone(fin_plan.get('plan_id'))
        self.assertTrue(any('仅账本所有者' in err for err in fin_plan['actions'][0]['errors']))

        # Owner AI edit plan forbidden mutations (currency / draft / below reimbursed) produce error cards.
        for bad_fields in (
            {'currency': 'CNY'},
            {'status': 'draft'},
            {'amount': '50'},
        ):
            with self.subTest(bad=bad_fields):
                bad_plan = self._ai_plan(self.client, self.headers, [{'type': 'edit_entry', 'id': entry['id'], **bad_fields}])
                self.assertIsNone(bad_plan.get('plan_id'))
                self.assertTrue(bad_plan['actions'][0]['errors'])

        # Owner AI edit plan raising amount generates historical warning card and executes atomically.
        owner_plan = self._ai_plan(self.client, self.headers, [
            {'type': 'edit_entry', 'id': entry['id'], 'amount': '130', 'description': '云服务（含增购）'},
        ])
        self.assertIsNotNone(owner_plan.get('plan_id'))
        card = owner_plan['actions'][0]
        self.assertEqual(card['label'], '修改历史账目')
        self.assertEqual(card['errors'], [])
        self.assertTrue(any('保留' in w for w in card['warnings']))
        self.assertTrue(any('部分报销' in w for w in card['warnings']))

        exec_res = self._ai_execute(self.client, self.headers, owner_plan)
        self.assertEqual(exec_res.status_code, 200, exec_res.text)
        updated = self._entry_by_id(entry['id'])
        self.assertEqual((updated['amount'], updated['reimbursed_amount'], updated['status']), ('130.00', '100.00', 'pending'))
        self.assertEqual(len(updated['payments']), 1)

        # If owner is demoted to finance before executing a queued plan, execution is blocked.
        second_plan = self._ai_plan(self.client, self.headers, [
            {'type': 'edit_entry', 'id': entry['id'], 'description': '降级后执行'},
        ])
        self.assertIsNotNone(second_plan.get('plan_id'))
        self.oidc.set_user('vanora-owner-alice', roles=('finance',))
        with db(write=True) as conn:
            conn.execute("UPDATE users SET role='finance' WHERE id=?", (self.user['id'],))
        demoted_res = self._ai_execute(self.client, self.headers, second_plan)
        self.assertEqual(demoted_res.status_code, 403, demoted_res.text)
        self.oidc.set_user('vanora-owner-alice', roles=('owner',))
        with db(write=True) as conn:
            conn.execute("UPDATE users SET role='owner' WHERE id=?", (self.user['id'],))

    def test_telegram_bot_historical_edit_is_owner_only(self):
        fin = self.new_member(sub='vanora-fin-tg', username='fintg', name='财务', role='finance')
        self.link_bot()  # links owner (id=1) to telegram_id '123'
        with db(write=True) as conn:
            conn.execute('INSERT INTO tg_links VALUES (?,?)', ('456', fin['id']))

        entry = self.entry(amount='100', currency='USD', description='域名续费')
        self.assertEqual(self.action(entry, 'pay', '结清').status_code, 200)

        # Finance views detail: no edit button, shows lock hint; direct edit callback returns 403 alert.
        self._tg_click(10, f'e:{entry["id"]}', sender=456, chat=456)
        fin_msg = self._last_tg_message()
        fin_cbs = {b.get('callback_data') for row in fin_msg['reply_markup']['inline_keyboard'] for b in row}
        self.assertNotIn(f'edit:{entry["id"]}', fin_cbs)
        self.assertIn('仅账本所有者', fin_msg['text'])

        self._tg_click(11, f'edit:{entry["id"]}', sender=456, chat=456)
        self.assertIn('仅账本所有者', self._last_tg_alert()['text'])

        # Owner views detail: sees historical edit button, preview locks currency and rejects lowering amount.
        self._tg_click(20, f'e:{entry["id"]}', sender=123, chat=123)
        owner_msg = self._last_tg_message()
        owner_buttons = [b for row in owner_msg['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any(b.get('callback_data') == f'edit:{entry["id"]}' and '修改历史账目' in b['text'] for b in owner_buttons))

        self._tg_click(21, f'edit:{entry["id"]}', sender=123, chat=123)
        draft = self._tg_draft()
        token = draft['token']
        preview_msg = self._last_tg_message()
        preview_cbs = {b.get('callback_data') for row in preview_msg['reply_markup']['inline_keyboard'] for b in row}
        self.assertIn('累计已报', preview_msg['text'])
        self.assertNotIn(f'd:{token}:edit:currency', preview_cbs)

        # Direct currency callback must fail.
        self._tg_click(22, f'd:{token}:edit:currency', sender=123, chat=123)
        self.assertIn('不能修改记账币种', self._last_tg_alert()['text'])

        # Lowering amount below reimbursed (100 USD) fails before preview.
        self._tg_click(23, f'd:{token}:edit:amount', sender=123, chat=123)
        bot.process_update(self.message(24, '80', sender=123, chat=123))
        self.assertIn('不能低于累计已报销金额', self._last_tg_message()['text'])

        # Raising amount to 120 USD succeeds and saves with status synced to partial (pending).
        bot.process_update(self.message(25, '120', sender=123, chat=123))
        self.assertIn('部分报销', self._last_tg_message()['text'])
        self._tg_click(26, f'd:{token}:save', sender=123, chat=123)
        saved = self._entry_by_id(entry['id'])
        self.assertEqual((saved['amount'], saved['reimbursed_amount'], saved['status']), ('120.00', '100.00', 'pending'))
        self.assertEqual(len(saved['payments']), 1)

    def test_feishu_card_historical_edit_is_owner_only(self):
        fin = self.new_member(sub='vanora-fin-fs', username='finfs', name='财务', role='finance')
        with db(write=True) as conn:
            conn.execute('INSERT INTO feishu_links VALUES (?,?)', ('ou_alice', self.user['id']))
            conn.execute('INSERT INTO feishu_links VALUES (?,?)', ('ou_fin', fin['id']))

        entry = self.entry(amount='100', currency='CNY', description='飞书历史账目')
        self.assertEqual(self.action(entry, 'pay', '结清').status_code, 200)

        def fs_msg(num, text, sender='ou_alice', chat='oc_alice'):
            with db(write=True) as conn:
                conn.execute(
                    'INSERT INTO feishu_inbox(message_id,open_id,chat_id,text,created_at) VALUES (?,?,?,?,?)',
                    (f'om_hist_{num}', sender, chat, text, config.now()),
                )
            self.assertTrue(feishu.process_one())

        def fs_last_payload():
            with db() as conn:
                row = conn.execute(
                    "SELECT payload FROM fsui_outbox WHERE method IN ('sendMessage','editMessageText') ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return json.loads(row['payload'])

        def fs_prepare(action):
            with db(write=True) as conn:
                row = conn.execute(
                    "SELECT * FROM fsui_outbox WHERE method IN ('sendMessage','editMessageText') ORDER BY id DESC LIMIT 1"
                ).fetchone()
                feishu_ui._prepare(conn, dict(row))
                item = conn.execute('SELECT token FROM fsui_actions WHERE action=? ORDER BY rowid DESC LIMIT 1', (action,)).fetchone()
                self.assertIsNotNone(item, action)
                return item['token']

        def fs_click(num, action_token, sender='ou_alice', chat='oc_alice'):
            fs_msg(num, feishu_ui.PREFIX + json.dumps({'action_token': action_token, 'group': False}), sender=sender, chat=chat)

        # Finance opens entry detail in Feishu: no edit button.
        fs_msg(1, '/recent', sender='ou_fin', chat='oc_fin')
        fs_click(2, fs_prepare(f'e:{entry["id"]}'), sender='ou_fin', chat='oc_fin')
        fin_detail = fs_last_payload()
        fin_cbs = {b.get('callback_data') for row in fin_detail['reply_markup']['inline_keyboard'] for b in row}
        self.assertNotIn(f'edit:{entry["id"]}', fin_cbs)
        self.assertIn('仅账本所有者', fin_detail['text'])

        # Owner opens entry detail in Feishu, edits amount to 150 CNY and saves.
        fs_msg(3, '/recent', sender='ou_alice', chat='oc_alice')
        fs_click(4, fs_prepare(f'e:{entry["id"]}'), sender='ou_alice', chat='oc_alice')
        fs_click(5, fs_prepare(f'edit:{entry["id"]}'), sender='ou_alice', chat='oc_alice')
        with db() as conn:
            token = conn.execute('SELECT token FROM fsui_drafts ORDER BY rowid DESC LIMIT 1').fetchone()['token']
        fs_click(6, fs_prepare(f'd:{token}:edit:amount'), sender='ou_alice', chat='oc_alice')
        fs_msg(7, '150', sender='ou_alice', chat='oc_alice')
        fs_click(8, fs_prepare(f'd:{token}:save'), sender='ou_alice', chat='oc_alice')
        saved = self._entry_by_id(entry['id'])
        self.assertEqual((saved['amount'], saved['reimbursed_amount'], saved['status']), ('150.00', '100.00', 'pending'))
        self.assertEqual(len(saved['payments']), 1)


if __name__ == '__main__':
    unittest.main()
