import json
import importlib.util
import re
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from app import config, feishu
from app.db import db
from tests import test_ledger as base


class FeishuTest(unittest.TestCase):
    setUp = base.LedgerTest.setUp
    tearDown = base.LedgerTest.tearDown
    post = base.LedgerTest.post
    entry = base.LedgerTest.entry
    new_member = base.LedgerTest.new_member

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
        self.assertIn('已完成', self.process())
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

    def test_members_can_book_their_own_advance_but_not_public_expenses(self):
        member = self.new_member()
        with db(write=True) as conn:
            conn.execute('INSERT INTO feishu_links VALUES (?,?)', ('ou_alice', member['id']))
        self.message('public', '公账支出 35 CNY 打车')
        self.assertIn('财务管理员', self.process())
        self.message('advance', '成员垫付 35 CNY 打车')
        code = re.search(r'确认 ([A-F0-9]{8})', self.process())[1]
        self.message('confirm', '确认 ' + code)
        self.assertIn('已完成', self.process())
        entries = self.client.get('/api/entries').json()
        self.assertEqual([(entry['expense_type'], entry['payer_id']) for entry in entries],
                         [('advance', member['id'])])

    def test_ai_multiple_entries(self):
        self.bind()
        raw = {'reply': '两笔', 'questions': [], 'actions': [
            {'type': 'create_entry', 'kind': 'expense', 'amount': '35', 'currency': 'CNY', 'description': '打车'},
            {'type': 'create_entry', 'kind': 'income', 'amount': '100', 'currency': 'CNY', 'description': '收款'}]}
        with patch('app.ai.parse', return_value=raw):
            self.message(1, '记两笔')
            text = self.process()
        code = re.search(r'确认 ([A-F0-9]{8})', text)[1]
        self.message(2, '确认 ' + code)
        self.assertIn('已完成', self.process())
        self.assertEqual(len(self.client.get('/api/entries').json()), 2)

    def test_ai_void_requires_confirmation(self):
        self.bind()
        entry = self.entry(description='误记的支出')
        raw = {'reply': '', 'questions': [], 'actions': [
            {'type': 'void_entry', 'id': entry['id'], 'note': '重复录入'}]}
        with patch('app.ai.parse', return_value=raw):
            self.message(1, '作废误记的支出')
            preview = self.process()
        self.assertIn('作废', preview)
        code = re.search(r'确认 ([A-F0-9]{8})', preview)[1]
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'], 'pending')
        self.message(2, '确认 ' + code)
        self.assertIn('作废', self.process())
        self.assertEqual(self.client.get('/api/entries').json()[0]['status'], 'voided')

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

    def test_web_credentials_are_private_and_can_be_rotated(self):
        response = self.post('/feishu/config', {'app_id': 'cli_test', 'app_secret': 'private-secret'})
        self.assertEqual(response.status_code, 200, response.text)
        info = self.client.get('/api/integrations')
        self.assertTrue(info.json()['feishu_configured'])
        self.assertEqual(info.json()['feishu_app_id'], 'cli_test')
        self.assertNotIn('private-secret', info.text)
        self.assertNotIn('private-secret', self.client.get('/api/settings').text)
        self.assertEqual(feishu.credentials(), ('cli_test', 'private-secret', '1'))
        self.assertEqual(self.post('/feishu/config', {'app_id': 'cli_test', 'app_secret': ''}).status_code, 200)
        self.assertEqual(feishu.credentials(), ('cli_test', 'private-secret', '2'))
        self.bind()
        self.assertEqual(self.post('/feishu/config', {'app_id': 'cli_other', 'app_secret': 'new'}).status_code, 409)
        self.assertEqual(feishu.credentials(), ('cli_test', 'private-secret', '2'))

    def test_feishu_config_requires_session_and_csrf(self):
        self.assertEqual(self.client.post('/api/feishu/config', json={'app_id': 'cli_test', 'app_secret': 'secret'}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/feishu/config', json={'app_id': 'cli_test', 'app_secret': 'secret'}).status_code, 401)

    def test_worker_restarts_after_web_credentials_change(self):
        first, second = MagicMock(), MagicMock()
        first.poll.return_value = None
        second.poll.return_value = None
        with patch('app.feishu.init_db'), \
                patch('app.feishu.credentials', side_effect=[
                    ('cli_test', 'first', '1'), ('cli_test', 'second', '2')]), \
                patch('app.feishu.subprocess.Popen', side_effect=[first, second]) as spawn, \
                patch('app.feishu.time.sleep', side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                feishu.main()
        self.assertEqual(spawn.call_count, 2)
        first.terminate.assert_called_once()
        second.terminate.assert_called_once()
        self.assertNotIn('first', str(spawn.call_args_list))
        self.assertNotIn('second', str(spawn.call_args_list))

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

    def test_domain_resolution_config_and_integrations_status(self):
        self.assertEqual(feishu.resolve_domain(''), 'https://open.feishu.cn')
        self.assertEqual(feishu.resolve_domain('feishu'), 'https://open.feishu.cn')
        self.assertEqual(feishu.resolve_domain('lark'), 'https://open.larksuite.com')
        self.assertEqual(feishu.resolve_domain('https://open.corp.example/'), 'https://open.corp.example')
        self.assertEqual(feishu.resolve_domain('http://insecure.example'), 'https://open.feishu.cn')

        bad = self.post('/feishu/config', {'app_id': 'cli_test', 'app_secret': 'secret', 'domain': 'http://bad.example'})
        self.assertEqual(bad.status_code, 400)
        ok = self.post('/feishu/config', {'app_id': 'cli_test', 'app_secret': 'secret', 'domain': 'lark'})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(feishu.feishu_domain(), 'https://open.larksuite.com')

        with db(write=True) as conn:
            from app.db import set_setting
            set_setting(conn, 'feishu_heartbeat', int(time.time()))
            set_setting(conn, 'feishu_bot_name', '团队记账机器人')
            set_setting(conn, 'feishu_last_error', '')
        info = self.client.get('/api/integrations').json()
        self.assertEqual(info['feishu_domain'], 'lark')
        self.assertTrue(info['feishu_online'])
        self.assertEqual(info['feishu_bot_name'], '团队记账机器人')
        self.assertEqual(info['feishu_last_error'], '')
        self.assertFalse(info['feishu_embed_enabled'])

    def test_post_rich_text_media_hint_shortcuts_and_groupid(self):
        self.bind()
        # Rich-text post message in p2p
        post_content = json.dumps({
            'zh_cn': {
                'title': '',
                'content': [[{'tag': 'text', 'text': '公账支出 42 CNY 买云盘'}]],
            }
        })
        post_event = NS(event=NS(
            message=NS(chat_type='p2p', message_type='post', message_id='om_post_1',
                       chat_id='oc_alice', content=post_content),
            sender=NS(sender_type='user', sender_id=NS(open_id='ou_alice')),
        ))
        feishu.accept_event(post_event)
        preview = self.process()
        self.assertIn('公账支出', preview)
        code = re.search(r'确认 ([A-F0-9]{8})', preview)[1]
        self.message('confirm_post', '确认 ' + code)
        self.assertIn('已完成', self.process())
        entries = self.client.get('/api/entries').json()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['expense_type'], entries[0]['amount']), ('public', '42.00'))

        # p2p image message gives friendly Web receipt upload hint
        img_event = NS(event=NS(
            message=NS(chat_type='p2p', message_type='image', message_id='om_img_1',
                       chat_id='oc_alice', content=json.dumps({'image_key': 'img_123'})),
            sender=NS(sender_type='user', sender_id=NS(open_id='ou_alice')),
        ))
        with patch.dict('os.environ', {'PUBLIC_URL': 'https://ledger.example'}):
            feishu.accept_event(img_event)
            hint = self.process()
        self.assertIn('暂不支持直接上传凭证文件', hint)
        self.assertIn('https://ledger.example', hint)

        # '菜单' shortcut opens interactive menu
        self.message('menu_cn', '菜单')
        self.assertTrue(feishu.process_one())
        with db() as conn:
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM fsui_outbox').fetchone()[0], 0)

        # Case-insensitive /GroupID in group chat (via post @bot)
        group_post = json.dumps({
            'content': [[{'tag': 'at', 'user_id': 'ou_bot'}, {'tag': 'text', 'text': ' /GroupID'}]],
        })
        group_event = NS(event=NS(
            message=NS(chat_type='group', message_type='post', message_id='om_grp_id',
                       chat_id='oc_team_99', content=group_post, mentions=[], parent_id=None),
            sender=NS(sender_type='user', sender_id=NS(open_id='ou_alice')),
        ))
        with patch.object(feishu, 'BOT_OPEN_ID', 'ou_bot'):
            feishu.accept_event(group_event)
        reply = self.process()
        self.assertIn('oc_team_99', reply)

    def test_member_list_exposes_feishu_linked_and_disable_purges_feishu_state(self):
        member = self.new_member()
        with db(write=True) as conn:
            conn.execute('INSERT INTO feishu_links VALUES (?,?)', ('ou_bob', member['id']))
            conn.execute('INSERT INTO fsui_identities(open_id) VALUES (?)', ('ou_bob',))
            vid = str(conn.execute('SELECT id FROM fsui_identities WHERE open_id=?', ('ou_bob',)).fetchone()['id'])
            conn.execute('INSERT INTO fsui_links VALUES (?,?)', (vid, member['id']))
            conn.execute('INSERT INTO fsui_actions VALUES (?,?,?,?,?,?)',
                         ('tok_bob', vid, 'oc_bob', 'home', int(time.time()) + 300, 0))
            conn.execute('INSERT INTO feishu_pending VALUES (?,?,?,?,?)',
                         ('ou_bob', member['id'], 'plan_1', 'ABCD1234', int(time.time()) + 300))
        members = {m['id']: m for m in self.client.get('/api/members').json()}
        self.assertTrue(members[member['id']]['feishu_linked'])
        self.assertFalse(members[self.user['id']]['feishu_linked'])

        res = self.client.patch(f'/api/members/{member["id"]}', json={'active': False}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        with db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fsui_actions WHERE telegram_id=?', (vid,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM feishu_pending WHERE user_id=?', (member['id'],)).fetchone()[0], 0)
        # Deactivated member cannot interact via Feishu
        self.message('disabled_cmd', '最近账目', sender='ou_bob', chat='oc_bob')
        self.assertIn('请先登录', self.process())

    def test_inbox_error_boundary_prevents_head_of_line_blocking(self):
        self.bind()
        self.message('boom', '/start')
        self.message('after', '帮助')
        with patch('app.feishu_ui.process', side_effect=RuntimeError('unexpected crash')):
            self.assertTrue(feishu.process_one())
        with db() as conn:
            outbox_text = conn.execute('SELECT text FROM feishu_outbox ORDER BY id DESC LIMIT 1').fetchone()['text']
            self.assertIn('出现异常', outbox_text)
        # Next message still processes normally
        self.assertIn('发送「绑定', self.process())

    def test_outbox_drops_permanent_error_and_caps_retries(self):
        self.bind()
        with db(write=True) as conn:
            conn.execute('DELETE FROM feishu_outbox')
            feishu.enqueue(conn, 'oc_alice', 'ou_alice', 'permanent fail')
            feishu.enqueue(conn, 'oc_alice', 'ou_alice', 'transient fail')
        class _Builder:
            def __init__(self): self.data = {}
            def __getattr__(self, k):
                def setter(v):
                    self.data[k] = v
                    return self
                return setter
            def build(self): return NS(**self.data)
        fake_im = NS(
            CreateMessageRequest=NS(builder=_Builder),
            CreateMessageRequestBody=NS(builder=_Builder),
        )
        fake_modules = {'lark_oapi': NS(), 'lark_oapi.api': NS(), 'lark_oapi.api.im': NS(), 'lark_oapi.api.im.v1': fake_im}
        perm_client = NS(im=NS(v1=NS(message=NS(create=lambda req: NS(success=lambda: False, code=230002)))))
        with patch.dict('sys.modules', fake_modules):
            # Permanent error drops row immediately without raising
            self.assertTrue(feishu.send_one(perm_client))
            with db() as conn:
                first = conn.execute('SELECT attempts,text FROM feishu_outbox ORDER BY id LIMIT 1').fetchone()
                self.assertEqual((first['attempts'], first['text']), (feishu.MAX_ATTEMPTS, ''))
            # Transient error retries up to MAX_ATTEMPTS then stops blocking
            trans_client = NS(im=NS(v1=NS(message=NS(create=lambda req: NS(success=lambda: False, code=99999)))))
            for _ in range(feishu.MAX_ATTEMPTS):
                with self.assertRaises(RuntimeError):
                    feishu.send_one(trans_client)
            self.assertFalse(feishu.send_one(trans_client))
            with db() as conn:
                second = conn.execute('SELECT attempts,text FROM feishu_outbox ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual((second['attempts'], second['text']), (feishu.MAX_ATTEMPTS, ''))


if __name__ == '__main__':
    unittest.main()
