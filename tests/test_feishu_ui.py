import json
import importlib.util
import asyncio
import base64
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from unittest.mock import AsyncMock
from app import feishu, feishu_ui, config
from app.db import db, set_setting
from tests import test_ledger as base
from tests import test_feishu as fixtures


class FeishuUiTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    new_member = base.LedgerTest.new_member
    message = fixtures.FeishuTest.message
    process = fixtures.FeishuTest.process
    bind = fixtures.FeishuTest.bind

    def prepare(self, action):
        with db(write=True) as conn:
            rows = conn.execute("SELECT * FROM fsui_outbox WHERE method IN ('sendMessage','editMessageText') ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(rows)
            feishu_ui._prepare(conn, dict(rows))
            item = conn.execute('SELECT token FROM fsui_actions WHERE action=? ORDER BY rowid DESC LIMIT 1', (action,)).fetchone()
            self.assertIsNotNone(item, action)
            return item['token']

    def click(self, event_id, token, sender='ou_alice', chat='oc_alice', group=False):
        text = feishu_ui.PREFIX + json.dumps({'action_token':token, 'group':group})
        with db(write=True) as conn:
            conn.execute('INSERT INTO feishu_inbox(message_id,open_id,chat_id,text,created_at) VALUES (?,?,?,?,?)',
                         (f'om_{event_id}', sender, chat, text, config.now()))
        self.assertTrue(feishu.process_one())

    def test_button_menu_creates_expense_and_replay_is_safe(self):
        self.bind()
        self.message(1, '/start'); self.assertTrue(feishu.process_one())
        self.click(2, self.prepare('new'))
        with db() as conn:
            draft = dict(conn.execute('SELECT * FROM fsui_drafts').fetchone())
        self.click(3, self.prepare('d:' + draft['token'] + ':cur:CNY'))
        self.message(4, '35'); self.assertTrue(feishu.process_one())
        with db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM entries').fetchone()[0], 0)
        button = self.prepare('d:' + draft['token'] + ':save')
        self.click(5, button)
        self.click(6, button)
        with db() as conn:
            entries = conn.execute('SELECT source,amount_minor FROM entries').fetchall()
            self.assertEqual([tuple(row) for row in entries], [('feishu', 3500)])
            self.assertEqual(conn.execute('SELECT count(*) FROM tg_drafts').fetchone()[0], 0)

    def test_button_cannot_be_used_by_another_member_or_chat(self):
        self.bind(); self.message(1, '/start'); self.assertTrue(feishu.process_one())
        token = self.prepare('new')
        self.click(2, token, sender='ou_other')
        self.click(3, token, chat='oc_other')
        with db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM fsui_drafts').fetchone()[0], 0)

    def test_internal_envelope_prefix_in_user_text_cannot_block_the_queue(self):
        self.bind()
        self.message(1, feishu_ui.PREFIX + '{invalid')
        self.assertTrue(feishu.process_one())
        self.message(2, '/start')
        self.assertTrue(feishu.process_one())
        with db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feishu_inbox WHERE done=0').fetchone()[0], 0)
            self.assertGreater(conn.execute('SELECT count(*) FROM fsui_outbox').fetchone()[0], 0)

    def test_buttons_reimburse_once_after_confirmation(self):
        self.bind()
        entry = self.entry(amount='30')
        self.message(1, '/start'); self.assertTrue(feishu.process_one())
        self.click(2, self.prepare('list:recent:0'))
        self.click(3, self.prepare(f'e:{entry["id"]}'))
        self.click(4, self.prepare(f'pay:{entry["id"]}'))
        with db() as conn:
            token = conn.execute('SELECT token FROM fsui_drafts').fetchone()['token']
        self.click(5, self.prepare(f'd:{token}:paycur:CNY'))
        self.message(6, '全额'); self.assertTrue(feishu.process_one())
        self.message(7, '银行转账'); self.assertTrue(feishu.process_one())
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'], 'pending')
        action = self.prepare(f'd:{token}:commit')
        self.click(8, action); self.click(9, action)
        item = self.client.get('/api/entries').json()[0]
        self.assertEqual(item['status'], 'reimbursed')
        self.assertEqual(len(item['payments']), 1)

    def test_buttons_record_dividend_once(self):
        self.bind(); self.entry(kind='income', amount='100')
        self.message(1, '/start'); self.assertTrue(feishu.process_one())
        self.click(2, self.prepare('dividends'))
        with db() as conn:
            token = conn.execute('SELECT token FROM fsui_drafts').fetchone()['token']
        self.click(3, self.prepare(f'v:{token}:all'))
        self.assertEqual(self.client.get('/api/dividends').json(), [])
        action = self.prepare(f'v:{token}:commit')
        self.click(4, action); self.click(5, action)
        self.assertEqual(len(self.client.get('/api/dividends').json()), 1)

    def test_group_requires_mention_and_allowlist(self):
        self.bind()
        def group(number, text, mentioned=True):
            mention = NS(key='@_user_1', id=NS(open_id='ou_bot'))
            event = NS(event=NS(message=NS(chat_type='group',message_type='text',message_id=f'g{number}',
                chat_id='oc_group',content=json.dumps({'text':text}),mentions=[mention] if mentioned else [],parent_id=None),
                sender=NS(sender_type='user',sender_id=NS(open_id='ou_alice'))))
            with patch.object(feishu,'BOT_OPEN_ID','ou_bot'):
                feishu.accept_event(event)
        group(1,'@_user_1 /start',False)
        self.assertFalse(feishu.process_one())
        group(2,'@_user_1 /start')
        self.assertTrue(feishu.process_one())
        with db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM fsui_outbox').fetchone()[0], 0)
        with db(write=True) as conn:
            set_setting(conn,'feishu_allowed_chats',['oc_group'])
        group(3,'@_user_1 /start')
        self.assertTrue(feishu.process_one())
        with db() as conn:
            self.assertGreater(conn.execute('SELECT count(*) FROM fsui_outbox').fetchone()[0],0)

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'Feishu SDK not installed')
    def test_group_prompt_accepts_only_its_owner_reply(self):
        self.bind()
        other = self.new_member()
        with db(write=True) as conn:
            set_setting(conn, 'feishu_allowed_chats', ['oc_group'])
            conn.execute('INSERT INTO feishu_links VALUES (?,?)', ('ou_other', other['id']))
        def group(number, sender, text, parent=None):
            mention = NS(key='@_user_1', id=NS(open_id='ou_bot'))
            data = NS(event=NS(message=NS(chat_type='group',message_type='text',message_id=f'g{number}',
                chat_id='oc_group',content=json.dumps({'text':'@_user_1 '+text}),mentions=[mention],parent_id=parent),
                sender=NS(sender_type='user',sender_id=NS(open_id=sender))))
            with patch.object(feishu,'BOT_OPEN_ID','ou_bot'):
                feishu.accept_event(data)
            self.assertTrue(feishu.process_one())
        group(1,'ou_alice','/start')
        self.click('g2',self.prepare('new'),chat='oc_group',group=True)
        with db() as conn:
            token = conn.execute('SELECT token FROM fsui_drafts').fetchone()['token']
        self.click('g3',self.prepare(f'd:{token}:cur:CNY'),chat='oc_group',group=True)
        sequence = iter(range(1, 30))
        def create(request):
            return NS(success=lambda:True, data=NS(message_id=f'om_sent_{next(sequence)}'))
        client = NS(im=NS(v1=NS(message=NS(create=create))))
        while feishu_ui.send_one(client):
            pass
        with db() as conn:
            parent = conn.execute('SELECT m.feishu_id FROM fsui_prompts p JOIN fsui_messages m ON m.id=p.message_id ORDER BY m.id DESC LIMIT 1').fetchone()['feishu_id']
        group(4,'ou_other','12',parent)
        group(5,'ou_alice','12')
        with db() as conn:
            self.assertEqual(conn.execute('SELECT step FROM fsui_drafts').fetchone()['step'],'amount')
        group(6,'ou_alice','12',parent)
        with db() as conn:
            self.assertEqual(conn.execute('SELECT step FROM fsui_drafts').fetchone()['step'],'confirm')

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'Feishu SDK not installed')
    def test_sdk_card_event_is_authenticated_and_deduplicated(self):
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        self.bind(); self.message(1,'/start'); self.assertTrue(feishu.process_one())
        token = self.prepare('new')
        def event(actor, event_id):
            return P2CardActionTrigger({'header':{'event_id':event_id},'event':{
                'operator':{'open_id':actor}, 'context':{'open_chat_id':'oc_alice'},
                'action':{'value':{'token':token}}}})
        feishu.accept_card(event('ou_other','other'))
        self.assertFalse(feishu.process_one())
        data = event('ou_alice','correct')
        feishu.accept_card(data); feishu.accept_card(data)
        self.assertTrue(feishu.process_one())
        self.assertFalse(feishu.process_one())
        with db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM fsui_drafts').fetchone()[0], 1)

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'Feishu SDK not installed')
    def test_card_send_retry_preserves_message_and_action_tokens(self):
        self.bind(); self.message(1, '/start'); self.assertTrue(feishu.process_one())
        requests = []
        def create(req):
            requests.append(req)
            return NS(success=lambda:len(requests)>1, code=1, data=NS(message_id='om_card'))
        client = NS(im=NS(v1=NS(message=NS(create=create))))
        with self.assertRaises(RuntimeError):
            feishu_ui.send_one(client)
        self.assertTrue(feishu_ui.send_one(client))
        self.assertEqual(requests[0].request_body.uuid, requests[1].request_body.uuid)
        self.assertEqual(requests[0].request_body.content, requests[1].request_body.content)

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'Feishu SDK not installed')
    def test_websocket_card_frame_is_dispatched_and_acknowledged(self):
        from app.feishu_ws import Client
        from lark_oapi.ws.pb.pbbp2_pb2 import Frame
        from lark_oapi.ws.const import HEADER_TYPE, HEADER_MESSAGE_ID, HEADER_SUM, HEADER_SEQ
        client = Client.__new__(Client)
        client._event_handler = NS(_do_without_validation=lambda data: {'toast': {'type': 'info', 'content': 'ok'}})
        client._write_message = AsyncMock()
        frame = Frame(SeqID=1, LogID=1, service=1, method=1)
        for key, value in ((HEADER_TYPE,'card'), (HEADER_MESSAGE_ID,'id'), (HEADER_SUM,'1'), (HEADER_SEQ,'0')):
            header = frame.headers.add()
            header.key, header.value = key, value
        frame.payload = b'{}'
        asyncio.run(client._handle_data_frame(frame))
        client._write_message.assert_awaited_once()
        response = json.loads(frame.payload)
        self.assertEqual(response['code'], 200)
        self.assertEqual(json.loads(base64.b64decode(response['data']))['toast']['content'], 'ok')


if __name__ == '__main__':
    unittest.main()
