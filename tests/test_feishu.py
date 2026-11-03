import json
import importlib.util
import re
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import config, feishu
from app.db import db
from tests import test_ledger as base


class FeishuTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry

    def message(self, number, text, sender='ou_alice', chat='oc_alice', kind='p2p'):
        data = NS(event=NS(message=NS(chat_type=kind, message_type='text', message_id=f'om_{number}',
                                      chat_id=chat, content=json.dumps({'text': text})),
                           sender=NS(sender_type='user', sender_id=NS(open_id=sender))))
        feishu.accept_event(data)
        return data

    def process(self):
        self.assertTrue(feishu.process_one())
        with db() as conn:
            return conn.execute('SELECT text FROM feishu_outbox ORDER BY id DESC LIMIT 1').fetchone()['text']

    def bind(self):
        response = self.post('/feishu/link-code')
        self.assertEqual(response.status_code, 200)
        self.message('bind', '绑定 ' + response.json()['code'])
        self.assertIn('绑定成功', self.process())

    def test_bind_manual_entry_confirmation_replay_and_unlink(self):
        self.bind()
        self.assertTrue(self.client.get('/api/integrations').json()['feishu_linked'])
        event = self.message(1, '支出 35 CNY 打车')
        feishu.accept_event(event)
        text = self.process()
        code = re.search(r'确认 ([A-F0-9]{8})', text)[1]
        self.assertEqual(self.client.get('/api/entries').json(), [])
        self.assertFalse(feishu.process_one())
        self.message(2, '确认 ' + code, sender='ou_other', chat='oc_other')
        self.assertIn('请先登录', self.process())
        self.message(3, '确认 ' + code)
        self.assertIn('已入账', self.process())
        entries = self.client.get('/api/entries').json()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['source'], 'feishu')
        self.message(4, '确认 ' + code)
        self.assertIn('无效或已过期', self.process())
        self.assertEqual(len(self.client.get('/api/entries').json()), 1)
        self.assertEqual(self.post('/feishu/unlink').status_code, 200)
        self.message(5, '最近账目')
        self.assertIn('请先登录', self.process())
        with db() as conn:
            self.assertEqual(conn.execute("SELECT text FROM feishu_inbox WHERE message_id='om_bind'").fetchone()[0], '')

    def test_group_ignored_and_queries_need_no_ai(self):
        self.bind()
        self.entry(description='测试账目')
        self.message('group', '最近账目', kind='group')
        self.assertFalse(feishu.process_one())
        self.message(1, '最近账目')
        self.assertIn('测试账目', self.process())
        self.message(2, '余额')
        self.assertIn('待报销', self.process())

    def test_ai_multiple_entries_and_unsupported_action(self):
        self.bind()
        raw = {'reply': '两笔', 'questions': [], 'actions': [
            {'type': 'create_entry', 'kind': 'expense', 'amount': '35', 'currency': 'CNY', 'description': '打车'},
            {'type': 'create_entry', 'kind': 'income', 'amount': '100', 'currency': 'CNY', 'description': '收款'},
            {'type': 'delete_entry', 'id': 1}]}
        with patch('app.ai.parse', return_value=raw):
            self.message(1, '记两笔')
            text = self.process()
        self.assertIn('飞书暂不支持', text)
        code = re.search(r'确认 ([A-F0-9]{8})', text)[1]
        self.message(2, '确认 ' + code)
        self.assertIn('已入账', self.process())
        self.assertEqual(len(self.client.get('/api/entries').json()), 2)

    def test_expired_code_and_cancel(self):
        self.bind()
        self.message(1, '收入 100 CNY 项目')
        code = re.search(r'确认 ([A-F0-9]{8})', self.process())[1]
        self.message(2, '取消 ' + code)
        self.assertIn('已取消', self.process())
        self.message(3, '确认 ' + code)
        self.assertIn('无效或已过期', self.process())
        self.assertEqual(self.client.get('/api/entries').json(), [])
        response = self.post('/feishu/link-code')
        with db(write=True) as conn:
            conn.execute('UPDATE feishu_link_codes SET expires=?', (int(time.time()) - 1,))
        self.message(4, '绑定 ' + response.json()['code'])
        self.assertIn('无效或已过期', self.process())

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'Feishu SDK not installed')
    def test_official_sdk_event_and_retry_use_same_message_uuid(self):
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        event = P2ImMessageReceiveV1({'event': {
            'message': {'chat_type': 'p2p', 'message_type': 'text', 'message_id': 'om_sdk',
                        'chat_id': 'oc_sdk', 'content': json.dumps({'text': '帮助'})},
            'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'ou_sdk'}}}})
        feishu.accept_event(event)
        self.process()
        requests = []
        def create(request):
            requests.append(request)
            return NS(success=lambda: len(requests) > 1, code=123)
        client = NS(im=NS(v1=NS(message=NS(create=create))))
        with self.assertRaises(RuntimeError):
            feishu.send_one(client)
        self.assertTrue(feishu.send_one(client))
        self.assertEqual(requests[0].request_body.uuid, requests[1].request_body.uuid)
        self.assertEqual(requests[1].request_body.receive_id, 'oc_sdk')
        with db() as conn:
            self.assertEqual(conn.execute('SELECT sent,text FROM feishu_outbox').fetchone()[:], (1, ''))

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'Feishu SDK not installed')
    def test_unlinked_account_does_not_receive_queued_private_results(self):
        self.bind()
        self.message(1, '最近账目')
        self.process()
        self.assertEqual(self.post('/feishu/unlink').status_code, 200)
        calls = []
        client = NS(im=NS(v1=NS(message=NS(create=lambda request: calls.append(request)))))
        self.assertTrue(feishu.send_one(client))
        self.assertTrue(feishu.send_one(client))
        self.assertEqual(calls, [])
        with db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feishu_outbox WHERE sent=0 OR text<>''").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
