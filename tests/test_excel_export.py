"""Tests for multi-sheet Excel export, range filtering, embedded receipts, and bot file delivery."""
import base64
import io
import json
import os
import secrets
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from openpyxl import load_workbook
from PIL import Image as PILImage

from app import bot, config, excel_export, feishu, feishu_ui
from app.db import db
from tests import test_bot_ui as bot_fixtures
from tests import test_feishu as feishu_fixtures
from tests import test_feishu_ui as feishu_ui_fixtures
from tests import test_ledger as base


def _png_bytes(width=320, height=200, color=(34, 139, 34)):
    buf = io.BytesIO()
    PILImage.new('RGB', (width, height), color).save(buf, format='PNG')
    return buf.getvalue()


def _jpg_bytes(width=400, height=600, color=(30, 64, 175)):
    buf = io.BytesIO()
    PILImage.new('RGB', (width, height), color).save(buf, format='JPEG', quality=85)
    return buf.getvalue()


class _FakeBuilder:
    def __init__(self):
        self.data = {}

    def __getattr__(self, name):
        def setter(value):
            self.data[name] = value
            return self
        return setter

    def build(self):
        return NS(**self.data)


class ExcelExportTest(unittest.TestCase):
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
    click = bot_fixtures.TelegramUiTest.click
    last_message = bot_fixtures.TelegramUiTest.last_message
    feishu_message = feishu_fixtures.FeishuTest.message
    process = feishu_fixtures.FeishuTest.process

    def bind(self):
        response = self.post('/feishu/link-code')
        self.assertEqual(response.status_code, 200)
        self.feishu_message('bind', '绑定 ' + response.json()['code'])
        self.assertIn('绑定成功', self.process())

    def _upload_receipt(self, entry_id, raw_bytes, filename):
        res = self.post(
            f'/entries/{entry_id}/receipt',
            {'data': base64.b64encode(raw_bytes).decode(), 'name': filename},
        )
        self.assertEqual(res.status_code, 200, res.text)

    def _sheet_rows(self, ws):
        headers = [cell.value for cell in ws[1]]
        return headers, [
            dict(zip(headers, [cell.value for cell in row]))
            for row in ws.iter_rows(min_row=2)
        ]

    def test_multi_sheet_export_with_receipt_images_and_pdf(self):
        self.entry(kind='income', amount='2000', currency='CNY', date='2026-11-01', description='客户首款')
        pub = self.entry(expense_type='public', amount='300', currency='CNY', date='2026-11-05', description='云服务器')
        adv_img = self.entry(amount='100', currency='USD', date='2026-11-10', description='=HYPERLINK("http://bad","机票")')
        adv_pdf = self.entry(amount='500', currency='JPY', date='2026-11-12', description='东京交通')

        self._upload_receipt(pub['id'], _png_bytes(360, 220), '服务器发票.png')
        self._upload_receipt(adv_img['id'], _jpg_bytes(480, 720), '机票行程单.jpg')
        self._upload_receipt(adv_pdf['id'], b'%PDF-1.4 sample receipt', '交通单据.pdf')

        # Partial cross-currency payment on adv_img
        quote = self.post('/entries/payment-quotes', {'ids': [adv_img['id']], 'currency': 'CNY'}).json()[0]
        pay = self.client.post(
            '/api/entries/action',
            json={
                'action': 'pay',
                'ids': [adv_img['id']],
                'note': '银行转账 #001',
                'payments': [{'id': adv_img['id'], 'amount': '280', 'quote_id': quote['id']}],
            },
            headers={**self.headers, 'Idempotency-Key': secrets.token_hex(16)},
        )
        self.assertEqual(pay.status_code, 200, pay.text)

        with patch.dict(os.environ, {'PUBLIC_URL': 'https://ledger.example.com'}):
            resp = self.client.get('/api/export.xlsx')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['content-type'], excel_export.EXCEL_MEDIA_TYPE)
        self.assertIn('filename="team-ledger-', resp.headers['content-disposition'])
        self.assertIn("filename*=UTF-8''", resp.headers['content-disposition'])

        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ['资金汇总', '收支明细', '逐次报销付款', '成员待报销', '收据凭证'])

        # Sheet 2: 收支明细
        ws_entries = wb['收支明细']
        headers, rows = self._sheet_rows(ws_entries)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(ws_entries._images), 2)
        by_id = {r['编号']: r for r in rows}

        # Formula injection sanitized
        self.assertTrue(by_id[adv_img['id']]['用途 / 说明'].startswith("'=HYPERLINK"))
        self.assertEqual(by_id[adv_img['id']]['状态'], '部分报销')
        self.assertEqual(float(by_id[adv_img['id']]['累计已报销（原币种）']), 40.0)
        self.assertEqual(float(by_id[adv_img['id']]['剩余待报销（原币种）']), 60.0)
        self.assertEqual(by_id[pub['id']]['凭证文件'], '服务器发票.png')
        self.assertEqual(by_id[adv_pdf['id']]['收据预览'], 'PDF 凭证')
        self.assertEqual(by_id[adv_pdf['id']]['凭证文件'], '交通单据.pdf')

        # Image rows expand height; internal link jumps to 收据凭证
        pub_row_idx = next(idx for idx, r in enumerate(rows, start=2) if r['编号'] == pub['id'])
        self.assertGreater(ws_entries.row_dimensions[pub_row_idx].height, 50)
        self.assertEqual(ws_entries.cell(row=pub_row_idx, column=14).hyperlink.target, "#'收据凭证'!A2")

        # PDF row links to PUBLIC_URL receipt endpoint
        pdf_row_idx = next(idx for idx, r in enumerate(rows, start=2) if r['编号'] == adv_pdf['id'])
        self.assertEqual(
            ws_entries.cell(row=pdf_row_idx, column=14).hyperlink.target,
            f'https://ledger.example.com/api/entries/{adv_pdf["id"]}/receipt',
        )

        # Sheet 3: 逐次报销付款
        _, pay_rows = self._sheet_rows(wb['逐次报销付款'])
        self.assertEqual(len(pay_rows), 1)
        self.assertEqual(pay_rows[0]['账目编号'], adv_img['id'])
        self.assertEqual(float(pay_rows[0]['实付金额']), 280.0)
        self.assertEqual(pay_rows[0]['实付币种'], 'CNY')
        self.assertEqual(float(pay_rows[0]['抵扣原币金额']), 40.0)
        self.assertEqual(pay_rows[0]['付款方式 / 参考号'], '银行转账 #001')

        # Sheet 4: 成员待报销
        _, pending_rows = self._sheet_rows(wb['成员待报销'])
        self.assertEqual({r['账目编号'] for r in pending_rows}, {adv_img['id'], adv_pdf['id']})

        # Sheet 5: 收据凭证 (2 large embedded images + 1 PDF info row)
        ws_receipts = wb['收据凭证']
        _, receipt_rows = self._sheet_rows(ws_receipts)
        self.assertEqual(len(receipt_rows), 3)
        self.assertEqual(len(ws_receipts._images), 2)
        self.assertEqual(receipt_rows[2]['凭证类型'], 'PDF 文件')

    def test_include_receipts_false_and_corrupt_image_fallback(self):
        e_ok = self.entry(amount='50', description='正常图片')
        e_bad = self.entry(amount='60', description='损坏图片')
        self._upload_receipt(e_ok['id'], _png_bytes(), 'ok.png')
        self._upload_receipt(e_bad['id'], b'\x89PNG\r\n\x1a\ncorrupted-payload', 'broken.png')

        # Default export embeds the valid PNG and degrades gracefully on the corrupted PNG.
        resp = self.client.get('/api/export')
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        ws_entries = wb['收支明细']
        self.assertEqual(len(ws_entries._images), 1)
        _, rows = self._sheet_rows(ws_entries)
        by_id = {r['编号']: r for r in rows}
        self.assertEqual(by_id[e_bad['id']]['收据预览'], '图片不可预览')

        # include_receipts=0 skips embedding images and omits the 收据凭证 sheet.
        resp_no = self.client.get('/api/export?include_receipts=0')
        self.assertEqual(resp_no.status_code, 200)
        wb_no = load_workbook(io.BytesIO(resp_no.content))
        self.assertNotIn('收据凭证', wb_no.sheetnames)
        self.assertEqual(len(wb_no['收支明细']._images), 0)
        _, rows_no = self._sheet_rows(wb_no['收支明细'])
        self.assertEqual(rows_no[0]['收据预览'], '已上传（未内嵌）')

    def test_range_filters_kind_status_payer_currency_search_and_ids(self):
        member = self.new_member(sub='vanora-filter-bob', username='bob', name='小周', role='member')
        member_id = member['id']
        e_inc = self.entry(kind='income', amount='500', currency='CNY', date='2026-10-15', description='十月回款')
        e_pub = self.entry(expense_type='public', amount='120', currency='USD', date='2026-11-02', description='域名续费')
        e_adv1 = self.entry(amount='80', currency='CNY', date='2026-11-10', description='团队聚餐', payer_id=member_id)
        e_adv2 = self.entry(amount='40', currency='CNY', date='2026-11-20', description='快递费', note='顺丰寄送')
        e_void = self.entry(amount='99', currency='CNY', date='2026-11-21', description='误记账目')
        self.assertEqual(self.action(e_void, 'void', '误填').status_code, 200)

        def exported_ids(query):
            resp = self.client.get('/api/export' + query)
            self.assertEqual(resp.status_code, 200, resp.text)
            wb = load_workbook(io.BytesIO(resp.content))
            _, rows = self._sheet_rows(wb['收支明细'])
            return [r['编号'] for r in rows if r['编号'] is not None]

        # Default excludes voided
        self.assertEqual(exported_ids(''), [e_inc['id'], e_pub['id'], e_adv1['id'], e_adv2['id']])
        # status=all_with_voided / include_voided=1 includes voided
        self.assertEqual(exported_ids('?status=all_with_voided'), [e_inc['id'], e_pub['id'], e_adv1['id'], e_adv2['id'], e_void['id']])
        self.assertEqual(exported_ids('?include_voided=1'), [e_inc['id'], e_pub['id'], e_adv1['id'], e_adv2['id'], e_void['id']])
        # Date range
        self.assertEqual(exported_ids('?from=2026-11-01&to=2026-11-15'), [e_pub['id'], e_adv1['id']])
        # Kind & currency & payer & keyword
        self.assertEqual(exported_ids('?kind=public'), [e_pub['id']])
        self.assertEqual(exported_ids('?kind=income'), [e_inc['id']])
        self.assertEqual(exported_ids('?currency=USD'), [e_pub['id']])
        self.assertEqual(exported_ids(f'?payer={member_id}'), [e_adv1['id']])
        self.assertEqual(exported_ids('?q=顺丰'), [e_adv2['id']])
        self.assertEqual(exported_ids('?status=voided'), [e_void['id']])
        # Explicit IDs (including a voided one explicitly chosen)
        self.assertEqual(exported_ids(f'?ids={e_adv1["id"]},{e_void["id"]}'), [e_adv1['id'], e_void['id']])

        # Validation errors
        self.assertEqual(self.client.get('/api/export?from=2026-11-20&to=2026-11-01').status_code, 400)
        self.assertEqual(self.client.get('/api/export?from=not-a-date').status_code, 400)
        self.assertEqual(self.client.get('/api/export?kind=bad').status_code, 400)
        self.assertEqual(self.client.get('/api/export?ids=abc').status_code, 400)

    def test_telegram_export_command_menu_custom_range_delivery_and_rbac(self):
        self.entry(amount='88', date='2026-11-10', description='十一月差旅')
        self.entry(amount='55', date='2026-10-05', description='十月办公')
        self.link_bot()

        # Finance/owner sees 导出 Excel on main menu
        bot.process_update(self.message(10, '/start'))
        menu_buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
        self.assertIn('export', menu_buttons)

        # Direct shortcut /export 2026-11 queues sendDocument
        bot.process_update(self.message(11, '/export 2026-11'))
        with db() as conn:
            docs = conn.execute("SELECT * FROM tg_outbox WHERE method='sendDocument'").fetchall()
        self.assertEqual(len(docs), 1)
        doc_payload = json.loads(docs[0]['payload'])
        self.assertEqual(doc_payload['filters']['date_from'], '2026-11-01')
        self.assertEqual(doc_payload['filters']['date_to'], '2026-11-30')

        # Drain outbox delivers real xlsx bytes via send_document
        sent_docs = []
        def fake_send_doc(chat_id, filename, content, caption='', thread_id=None):
            sent_docs.append((chat_id, filename, content, caption, thread_id))
            return {'message_id': 999}

        with patch('app.bot.api', return_value={'message_id': 100}), patch('app.bot.send_document', side_effect=fake_send_doc):
            bot.drain_outbox()
        self.assertEqual(len(sent_docs), 1)
        chat_id, filename, content, caption, _ = sent_docs[0]
        self.assertEqual(chat_id, '123')
        self.assertTrue(filename.endswith('.xlsx'))
        self.assertIn('2026-11-01 至 2026-11-30', caption)
        wb = load_workbook(io.BytesIO(content))
        _, rows = self._sheet_rows(wb['收支明细'])
        self.assertEqual([r['用途 / 说明'] for r in rows], ['十一月差旅'])

        # Custom range interactive flow via x:range
        self.click(12, 'export')
        self.click(13, 'x:range')
        bot.process_update(self.message(14, '2026-10-01 2026-10-31'))
        with db() as conn:
            pending_doc = conn.execute("SELECT * FROM tg_outbox WHERE method='sendDocument' AND sent=0").fetchone()
        self.assertIsNotNone(pending_doc)

        # Member demotion before drain prevents Excel file delivery
        self.oidc.set_user('vanora-owner-alice', roles=('member',))
        with patch.dict(os.environ, {'VANORA_OIDC_PERMISSIONS_TTL': '0'}), \
             patch('app.bot.api', return_value={'message_id': 101}), \
             patch('app.bot.send_document', side_effect=AssertionError('demoted user must not receive export')):
            bot.drain_outbox()
        with db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tg_outbox WHERE method='sendDocument' AND sent=0").fetchone()[0], 0)

        # Member running /export directly is rejected and does not see menu button
        with patch.dict(os.environ, {'VANORA_OIDC_PERMISSIONS_TTL': '0'}):
            bot.process_update(self.message(15, '/export'))
            self.assertIn('财务管理员权限', self.last_message()['text'])
            bot.process_update(self.message(16, '/start'))
            member_buttons = [b['callback_data'] for row in self.last_message()['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
            self.assertNotIn('export', member_buttons)

    def test_telegram_multipart_encoding_and_proxy_token_safety(self):
        captured = {}
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return json.dumps({'ok': True, 'result': {'message_id': 77}}).encode()
        class FakeOpener:
            def open(self, req, timeout=0):
                captured['url'] = req.full_url
                captured['ct'] = req.get_header('Content-type')
                captured['data'] = req.data
                return FakeResp()

        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret-tok', 'TELEGRAM_PROXY_URL': 'http://127.0.0.1:7890'}), \
             patch('urllib.request.build_opener', return_value=FakeOpener()):
            res = bot.send_document('123', '报表.xlsx', b'PK\x03\x04xlsx', caption='测试说明', thread_id=9)
        self.assertEqual(res['message_id'], 77)
        self.assertIn('/sendDocument', captured['url'])
        self.assertIn('multipart/form-data; boundary=', captured['ct'])
        self.assertIn(b'name="document"', captured['data'])
        self.assertIn(b'PK\x03\x04xlsx', captured['data'])
        self.assertIn('测试说明'.encode('utf-8'), captured['data'])

    def test_feishu_export_shortcut_upload_send_and_retry_idempotency(self):
        entry = self.entry(amount='120', date='2026-11-15', description='飞书导出测试')
        self._upload_receipt(entry['id'], _png_bytes(200, 120), '飞书收据.png')
        self.bind()

        # Plain-text Feishu p2p shortcut "导出Excel 2026-11"
        self.feishu_message(1, '导出Excel 2026-11')
        self.assertTrue(feishu.process_one())

        uploaded = []
        sent_messages = []
        send_attempts = {'count': 0}

        def fake_file_create(req):
            body = req.request_body
            raw = body.file.read()
            uploaded.append((body.file_type, body.file_name, raw))
            return NS(success=lambda: True, data=NS(file_key='file_v2_test_key'))

        def fake_msg_create(req):
            body = req.request_body
            if body.msg_type == 'file':
                send_attempts['count'] += 1
                if send_attempts['count'] == 1:
                    # Simulate transient failure on first message send after file upload succeeded
                    return NS(success=lambda: False, code=50000)
            sent_messages.append((body.receive_id, body.msg_type, body.content, body.uuid))
            return NS(success=lambda: True, data=NS(message_id=f'om_sent_{len(sent_messages)}'))

        fake_client = NS(im=NS(v1=NS(file=NS(create=fake_file_create), message=NS(create=fake_msg_create))))
        fake_im_v1 = NS(
            CreateFileRequest=NS(builder=_FakeBuilder),
            CreateFileRequestBody=NS(builder=_FakeBuilder),
            CreateMessageRequest=NS(builder=_FakeBuilder),
            CreateMessageRequestBody=NS(builder=_FakeBuilder),
            PatchMessageRequest=NS(builder=_FakeBuilder),
            PatchMessageRequestBody=NS(builder=_FakeBuilder),
        )
        fake_modules = {
            'lark_oapi': NS(),
            'lark_oapi.api': NS(),
            'lark_oapi.api.im': NS(),
            'lark_oapi.api.im.v1': fake_im_v1,
        }

        with patch.dict(sys.modules, fake_modules):
            # 1st outbox row: confirmation card
            self.assertTrue(feishu_ui.send_one(fake_client))
            # 2nd outbox row: upload succeeds, file message transiently fails
            with self.assertRaises(RuntimeError):
                feishu_ui.send_one(fake_client)
            # Retry reuses cached _fs_file_key without re-uploading!
            self.assertTrue(feishu_ui.send_one(fake_client))
            self.assertFalse(feishu_ui.send_one(fake_client))

        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0][0], 'xls')
        self.assertTrue(uploaded[0][1].endswith('.xlsx'))
        wb = load_workbook(io.BytesIO(uploaded[0][2]))
        self.assertEqual(len(wb['收支明细']._images), 1)
        self.assertEqual(len(wb['收据凭证']._images), 1)

        file_msgs = [m for m in sent_messages if m[1] == 'file']
        self.assertEqual(len(file_msgs), 1)
        self.assertEqual(json.loads(file_msgs[0][2]), {'file_key': 'file_v2_test_key'})

    def test_feishu_export_drops_when_user_demoted_before_drain(self):
        self.entry(amount='50', date='2026-11-18', description='待导出账目')
        self.bind()
        self.feishu_message(1, '/export all')
        self.assertTrue(feishu.process_one())

        # Demote owner to member before draining fsui_outbox
        self.oidc.set_user('vanora-owner-alice', roles=('member',))
        fake_client = NS(im=NS(v1=NS(
            file=NS(create=lambda req: (_ for _ in ()).throw(AssertionError('demoted user must not upload export'))),
            message=NS(create=lambda req: NS(success=lambda: True, data=NS(message_id='om_card'))),
        )))
        fake_im_v1 = NS(
            CreateFileRequest=NS(builder=_FakeBuilder),
            CreateFileRequestBody=NS(builder=_FakeBuilder),
            CreateMessageRequest=NS(builder=_FakeBuilder),
            CreateMessageRequestBody=NS(builder=_FakeBuilder),
            PatchMessageRequest=NS(builder=_FakeBuilder),
            PatchMessageRequestBody=NS(builder=_FakeBuilder),
        )
        fake_modules = {
            'lark_oapi': NS(),
            'lark_oapi.api': NS(),
            'lark_oapi.api.im': NS(),
            'lark_oapi.api.im.v1': fake_im_v1,
        }
        with patch.dict(os.environ, {'VANORA_OIDC_PERMISSIONS_TTL': '0'}), patch.dict(sys.modules, fake_modules):
            while feishu_ui.send_one(fake_client):
                pass
        with db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fsui_outbox WHERE sent=0").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
