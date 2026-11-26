"""請求依頼ひな形、元金全額充当、依頼時情報の保持、PDF保存先の回帰テスト。"""
from contextlib import closing
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import fitz
import test_workflow


class SettlementRequestTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    case = test_workflow.WorkflowTests.case
    draft = test_workflow.WorkflowTests.draft
    invoice = test_workflow.WorkflowTests.invoice

    def prepare(self, carry=0):
        self.call('r_company_put', {'name': 'テスト自社'})
        self.call('r_subm_put', {'name': '下請けA', 'contact_name': '山田太郎',
                               'invoice_no': 'T1234567890123'}, self.sid)
        source = self.case('充当元案件', self.old, 2000000, self.today)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=3000000,confirmed_checked=True,paid_date=self.today),source)
        target = self.case('充当先案件', self.new, 3000000)
        self.c.execute("UPDATE cases SET case_no='SP-SOURCE',start_date='2020-11-01' WHERE id=?", (source,))
        self.c.execute("UPDATE cases SET case_no='SP-TARGET' WHERE id=?", (target,))
        self.c.commit()
        s = self.draft(self.old)
        if carry:
            s = self.call('r_carry', dict(carry_over=carry, target_id=target, consent_date=self.today,
                                         consent_record='山田様 LINE承諾'), s['id'], s['lines'][0]['id'])
        return s, source, target

    def configure(self, s, **kwargs):
        return self.call('r_settlement_request', dict(invoice_description='商品販売委託業務',
                         request_contact='山田太郎', request_honorific='様', invoice_tax_rate=10, **kwargs), s['id'])

    def test_067_edited_request_text_survives_reopen_and_requested_snapshot(self):
        s, _, _ = self.prepare(2000000)
        s = self.configure(s)
        text = '編集済みの請求依頼文\n金額と充当を確認しました。'
        amounts = (s['total'], s['amount_ex_tax'], s['tax_amount'])
        s = self.call('r_settlement_request_text', dict(text=text, basis=s['request_text_basis'],
                     expected_revision=s['request_text_revision']), s['id'])
        with closing(self.s.db()) as reopened:
            saved = self.s.settlement_view(reopened, s['id'])
            self.assertEqual(saved['request_text'], text)
            self.assertEqual((saved['total'], saved['amount_ex_tax'], saved['tax_amount']), amounts)
        s = self.call('r_settlement_status', {'status': 'requested'}, s['id'])
        self.assertEqual(s['request_text'], text)
        self.assertEqual(s['request_document']['edited_request_text']['text'], text)
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_request_text', dict(text='変更', basis=s['request_text_basis'],
                      expected_revision=s['request_text_revision']), s['id'])

    def test_full_principal_carry_is_profit_only_in_text_pdf_and_csv(self):
        s, source, target = self.prepare(2000000)
        s = self.configure(s)
        self.assertEqual(s['total'], 100000)
        self.assertEqual((s['amount_ex_tax'], s['tax_amount']), (90910, 9090))
        self.assertTrue(s['lines'][0]['profit_only'])
        for value in ('下請けA 山田太郎 様', 'テスト自社', '利益のみの請求', 'SP-SOURCE', 'SP-TARGET',
                      '充当先案件', '100,000', '90,910', '9,090', '10%', '2020-12-15', '商品販売委託業務'):
            self.assertIn(value, s['request_text'])
        result = self.call('r_settlement_template', None, s['id'])
        self.assertEqual(result[0], 'file')
        with fitz.open(stream=result[1], filetype='pdf') as pdf:
            text = '\n'.join(page.get_text() for page in pdf)
            for value in ('記入用ひな形', 'テスト自社', '山田太郎', 'SP-SOURCE', 'SP-TARGET', '利益のみの請求', '90,910', '9,090'):
                self.assertIn(value, text)
        report = self.call('r_export', None, 'settlements')
        row, = csv.DictReader(io.StringIO(report[1].decode('utf-8-sig')))
        self.assertEqual(row['請求方式'], '利益のみ')
        self.assertEqual(row['充当先案件番号'], 'SP-TARGET')
        self.assertEqual(row['振込額'], '100000')

    def test_partial_and_no_carry_preserve_principal_remainder(self):
        s, _, target = self.prepare()
        self.assertEqual(s['total'], 2100000)
        self.assertFalse(s['lines'][0]['profit_only'])
        self.assertIn('元金残額と利益', s['request_text'])
        s = self.call('r_carry', dict(carry_over=500000, target_id=target, consent_date=self.today,
                                     consent_record='一部充当承諾'), s['id'], s['lines'][0]['id'])
        self.assertEqual(s['total'], 1600000)
        self.assertFalse(s['lines'][0]['profit_only'])
        self.assertIn('元金残額 1,500,000円', s['request_text'])
        s = self.call('r_carry', {'carry_over': 0}, s['id'], s['lines'][0]['id'])
        self.assertEqual(s['total'], 2100000)

    def test_request_configuration_validation_and_snapshot(self):
        s, _, _ = self.prepare()
        body = dict(invoice_description='販売委託 <確認> & 精算', request_contact='担当者',
                    request_honorific='様', invoice_tax_rate=8)
        s = self.call('r_settlement_request', body, s['id'])
        self.assertEqual(s['tax_amount'], 155555)
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_request', dict(body, request_honorific='御中'), s['id'])
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_request', dict(body, invoice_tax_rate=7), s['id'])
        s = self.call('r_settlement_status', {'status': 'requested'}, s['id'])
        before = s['request_text']
        self.call('r_company_put', {'name': '変更後自社'})
        self.call('r_subm_put', {'name': '変更後下請け', 'contact_name': '別担当'}, self.sid)
        after = self.s.settlement_view(self.c, s['id'])
        self.assertEqual(after['request_text'], before)
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_request', body, s['id'])
        csv_result = self.call('r_export', None, 'transactions')
        rows = list(csv.DictReader(io.StringIO(csv_result[1].decode('utf-8-sig'))))
        self.assertEqual(next(row['税率'] for row in rows if row['区分'] == '下請け精算'), '8%')
        with fitz.open(stream=self.call('r_settlement_template', None, s['id'])[1], filetype='pdf') as pdf:
            text = ''.join(page.get_text() for page in pdf)
            self.assertIn('テスト自社', text)
            self.assertNotIn('変更後自社', text)
            self.assertIn('<確認> & 精算', text)
        self.call('r_settlement_status', {'status': 'not_requested'}, s['id'])
        self.assertIn('変更後自社', self.s.settlement_view(self.c, s['id'])['request_text'])

    def test_invoice_status_and_configurable_archive_preserve_all_revisions(self):
        s, source, target = self.prepare(2000000)
        self.call('r_settlement_status', {'status': 'requested'}, s['id'])
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_status', {'status': 'received'}, s['id'])
        s = self.call('r_sub_invoice', self.invoice(99999), s['id'])
        self.assertEqual(s['status'], 'mismatch')
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_status', {'status': 'paid', 'paid_at': self.today}, s['id'])
        s = self.call('r_sub_invoice', self.invoice(100000), s['id'])
        self.assertEqual(s['status'], 'received')
        self.assertEqual(self.s.SETTLEMENT_STATES[s['status']], '請求書受領済')
        self.assertTrue(all(Path(invoice['stored_path']).is_file() for invoice in s['invoices']))
        directory = Path(self.s.DBP).parent / 'logibox連携'
        result = self.call('r_pdf_archive_settings_put', {'directory': str(directory)})
        self.assertIsNone(result['error'])
        copies = list((directory/'月次精算').rglob('*.pdf'))
        self.assertEqual(len(list(directory.rglob('案件証憑一式.pdf'))), 2)
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].name, '下請けA_受領請求書.pdf')
        self.assertEqual(copies[0].parent.name, '2020-11')
        self.assertEqual(copies[0].read_bytes(), Path(s['invoices'][0]['stored_path']).read_bytes())
        self.call('r_settlement_status', {'status': 'paid', 'paid_at': self.today}, s['id'])
        self.assertEqual(self.call('r_case_get', None, target)['subs'][0]['principal_confirmed'], 2000000)
        with self.assertRaises(self.s.Err):
            self.call('r_sub_invoice', self.invoice(100000), s['id'])
        override = directory.parent / 'env-archive'
        with patch.dict(os.environ, {'LOGIBOX_PDF_DIR': str(override)}):
            result = self.call('r_pdf_archive_settings')
            self.assertTrue(result['environment_override'])
            self.assertEqual(result['effective_directory'], str(override))
            self.s.sync_pdf_archive(self.c)
            self.assertEqual(len(list((override/'月次精算').rglob('*.pdf'))), 1)
            self.assertEqual(len(list(override.rglob('案件証憑一式.pdf'))), 2)

    def test_archive_failure_visible_and_retry_keeps_original(self):
        s, _, _ = self.prepare()
        self.call('r_settlement_status', {'status': 'requested'}, s['id'])
        s = self.call('r_sub_invoice', self.invoice(s['total']), s['id'])
        blocked = Path(self.s.DBP).parent / 'blocked'
        blocked.write_text('this is a file', encoding='utf-8')
        result = self.call('r_pdf_archive_settings_put', {'directory': str(blocked)})
        self.assertTrue(result['error'])
        self.assertTrue(Path(s['invoices'][0]['stored_path']).is_file())
        with self.assertRaises(self.s.Err):
            self.call('r_pdf_archive_settings_put', {'directory': '../relative'})
        result = self.call('r_pdf_archive_settings_put', {'directory': ''})
        self.assertIsNone(result['error'])
        self.assertEqual(len(list((Path(result['effective_directory'])/'月次精算').rglob('*.pdf'))), 1)
        self.assertEqual(len(list(Path(result['effective_directory']).rglob('案件証憑一式.pdf'))), 2)

    def test_authenticated_http_template_preview_and_settings(self):
        s, _, _ = self.prepare()
        with closing(self.s.db()) as conn:
            self.s.setset(conn, 'pw', self.s.hash_pw('test-passphrase'))
            conn.commit()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.s.H)
        with patch.multiple(self.s.H, port=httpd.server_port, loopback=True):
            worker = threading.Thread(target=httpd.serve_forever, daemon=True)
            worker.start()
            try:
                root = f'http://127.0.0.1:{httpd.server_port}'
                def req(path, body=None, cookie=''):
                    return urlopen(Request(root + path, data=json.dumps(body).encode() if body is not None else None,
                                          headers={'X-Requested-With': 'app', 'Cookie': cookie}), timeout=10)
                with self.assertRaises(HTTPError) as exc:
                    req(f"/api/settlements/{s['id']}/invoice-template")
                self.assertEqual(exc.exception.code, 401)
                exc.exception.close()
                cid = s['lines'][0]['case_id']
                with self.assertRaises(HTTPError) as unauthenticated:
                    req(f'/api/cases/{cid}/evidence-bundle')
                self.assertEqual(unauthenticated.exception.code, 401)
                unauthenticated.exception.close()
                with req('/api/login', {'password': 'test-passphrase'}) as login:
                    cookie = login.headers['Set-Cookie'].split(';')[0]
                with req(f"/api/settlements/{s['id']}/invoice-template?preview=1", cookie=cookie) as pdf:
                    self.assertEqual(pdf.headers['Content-Type'], 'application/pdf')
                    self.assertTrue(pdf.headers['Content-Disposition'].startswith('inline;'))
                    self.assertTrue(pdf.read().startswith(b'%PDF-'))
                with req(f'/api/cases/{cid}/evidence-bundle', cookie=cookie) as bundle:
                    self.assertEqual(bundle.headers['Content-Type'], 'application/pdf')
                    self.assertTrue(bundle.headers['Content-Disposition'].startswith('attachment;'))
                    with fitz.open(stream=bundle.read(), filetype='pdf') as document:
                        self.assertIn('案件証憑一覧', ''.join(page.get_text() for page in document))
                with req('/api/settings/pdf-archive', cookie=cookie) as config:
                    self.assertIn('effective_directory', json.load(config))
                with req(f"/api/settlements/{s['id']}/invoice-template/preview?info=1", cookie=cookie) as info:
                    self.assertEqual(json.load(info)['pages'], 1)
                with req(f"/api/settlements/{s['id']}/invoice-template/preview?page=1", cookie=cookie) as page:
                    self.assertEqual(page.headers['Content-Type'], 'image/png')
                    self.assertTrue(page.read().startswith(b'\x89PNG'))
            finally:
                httpd.shutdown()
                worker.join()
                httpd.server_close()

    def test_multi_page_template_wraps_long_names_and_descriptions(self):
        import settlement_documents
        s, _, _ = self.prepare(2000000)
        d = s['request_document']
        d['description'] = '販売委託に係る業務内容の長文テスト。' * 25
        line = dict(d['lines'][0], case_name='長い案件名の折り返し確認' * 8)
        d['lines'] = [dict(line) for _ in range(14)]
        with fitz.open(stream=settlement_documents.render(d), filetype='pdf') as pdf:
            self.assertGreater(len(pdf), 2)
            text = ''.join(page.get_text() for page in pdf)
            for index in range(1, 15):
                self.assertIn(f'明細 {index}', text)
            for page in pdf:
                for block in page.get_text('blocks'):
                    self.assertGreaterEqual(block[0], 30)
                    self.assertLessEqual(block[2], page.rect.width - 25)
                    self.assertLessEqual(block[3], page.rect.height - 10)

    def test_version6_upgrade_keeps_existing_data_and_creates_backup(self):
        s, _, _ = self.prepare(2000000)
        legacy_path = Path(self.s.DBP).with_name('version6.db')
        with closing(sqlite3.connect(legacy_path)) as legacy:
            self.c.backup(legacy)
            for column in ('invoice_description', 'request_contact', 'request_honorific',
                           'invoice_tax_rate', 'request_snapshot_json'):
                legacy.execute(f'ALTER TABLE settlements DROP COLUMN {column}')
            legacy.execute('PRAGMA user_version=6')
            legacy.commit()
        with patch.object(self.s, 'DBP', str(legacy_path)):
            self.s.init()
            with closing(self.s.db()) as updated:
                migrated = self.s.settlement_view(updated, s['id'])
                self.assertEqual(migrated['total'], 100000)
                self.assertIn('利益のみの請求', migrated['request_text'])
                self.assertEqual(migrated['invoice_tax_rate'], 10)
                self.assertEqual(updated.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertTrue(list(legacy_path.parent.glob('before-upgrade-*.zip')))

    def test_carry_and_month_end_guards(self):
        s, source, target = self.prepare()
        wrong = self.case('別下請け', self.new, 3000000)
        self.call('r_sub_del', {}, wrong, 1)
        self.call('r_sub_put', {'sub_id': self.other, 'principal_tax_in': 3000000}, wrong, 1)
        consent = dict(carry_over=2000000, target_id=wrong, consent_date=self.today, consent_record='承諾記録')
        with self.assertRaises(self.s.Err):
            self.call('r_carry', consent, s['id'], s['lines'][0]['id'])
        with self.assertRaises(self.s.Err):
            self.call('r_carry', dict(consent, target_id=target, consent_record=''), s['id'], s['lines'][0]['id'])
        current = self.today[:7]
        self.case('当月案件', current, 10000, self.today)
        current_s = self.draft(current)
        with self.assertRaisesRegex(self.s.Err, '月末締め後'):
            self.call('r_settlement_status', {'status': 'requested'}, current_s['id'])


if __name__ == '__main__':
    unittest.main()
