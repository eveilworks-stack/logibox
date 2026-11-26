"""回帰テスト。精算、CSV、DB移行を一時ディレクトリで確認する。"""
import base64
from contextlib import closing, ExitStack
import csv
import datetime
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        with patch.dict(os.environ, {'APP_DATA': tmp}):
            import server
        self.s = server
        stack.enter_context(patch.multiple(server, DBP=str(Path(tmp) / 'app.db'),
                                         FILES=str(Path(tmp) / 'files'), DOCS=str(Path(tmp) / 'documents')))
        self.s.init()
        self.c = stack.enter_context(closing(self.s.db()))
        self.today = datetime.date.today().isoformat()
        self.old = '2020-11'
        self.new = '2020-12'
        self.call('r_subm_new', {'name':'下請けA'})
        self.call('r_subm_new', {'name':'下請けB'})
        self.sid, self.other = [r['id'] for r in self.c.execute('SELECT id FROM subcontractors ORDER BY id')]

    def call(self, route, body=None, *ids, q=None):
        try:
            value = getattr(self.s, route)(self.c, body or {}, q or {}, *map(str, ids))
        except Exception:
            self.c.rollback()
            raise
        self.c.commit()
        return value

    def case(self, name, month, amount, date=None, other=False):
        cid = self.call('r_case_new', {'name':name, 'due_date':month+'-20', 'planned_rate_pct':7})['id']
        self.call('r_sub_put', {'sub_id':self.sid, 'principal_tax_in':amount, 'principal_received_date':date}, cid, 1)
        if other:
            self.call('r_sub_put', {'sub_id':self.other, 'principal_tax_in':20000, 'principal_received_date':date}, cid, 2)
        return cid

    def draft(self, month, sub=None):
        return self.call('r_settlement_create', {'period':month, 'sub_id':sub or self.sid})

    def invoice(self, amount):
        stream = io.BytesIO()
        font_path = Path(self.s.BASE) / 'fonts' / 'BIZUDPGothic-Regular.ttf'
        pdfmetrics.registerFont(TTFont('SettlementTest', str(font_path)))
        pdf = canvas.Canvas(stream)
        pdf.setFont('SettlementTest', 14)
        pdf.drawString(55, 760, f'ご請求額　￥{amount:,}円')
        pdf.showPage()
        pdf.save()
        return {'filename':'請求書.pdf', 'mime':'application/pdf', 'received_date':self.today,
                'data':base64.b64encode(stream.getvalue()).decode('ascii')}

    def test_numbering_years_and_concurrent_creation(self):
        from concurrent.futures import ThreadPoolExecutor
        class FakeDate(datetime.date):
            year_value = 2026
            @classmethod
            def today(cls): return cls(cls.year_value, 12, 31)
        with patch.object(self.s.datetime, 'date', FakeDate):
            first = self.call('r_case_new', {'name':'年末','planned_rate_pct':7})
            self.assertEqual(first['mgmt_no'], '26-0001')
            self.c.execute('DELETE FROM cases WHERE id=?', (first['id'],)); self.c.commit()
            self.assertEqual(self.call('r_case_new', {'name':'再登録','planned_rate_pct':7})['mgmt_no'], '26-0002')
            FakeDate.year_value = 2027
            self.assertEqual(self.call('r_case_new', {'name':'年始','planned_rate_pct':7})['mgmt_no'], '27-0001')
            def create(i):
                with closing(self.s.db()) as conn:
                    value = self.s.r_case_new(conn, {'name':str(i),'planned_rate_pct':7}, {})
                    conn.commit()
                    return value['mgmt_no']
            with ThreadPoolExecutor(max_workers=4) as pool:
                numbers = list(pool.map(create, range(8)))
            self.assertEqual(len(set(numbers)), 8)

    def test_branch_numbers_persist_and_migrate(self):
        cid = self.case('枝番', self.old, 10000, other=True)
        one = self.s.settlement_number(self.c, cid, self.sid)
        two = self.s.settlement_number(self.c, cid, self.other)
        self.assertTrue(one.endswith('-01')); self.assertTrue(two.endswith('-02'))
        self.call('r_sub_put', {'sub_id':self.sid,'principal_tax_in':12000}, cid, 1)
        self.assertEqual(self.s.settlement_number(self.c,cid,self.sid),one)
        self.call('r_sub_del', {}, cid, 1)
        self.call('r_subm_new', {'name':'三社目'})
        third = self.c.execute('SELECT MAX(id) FROM subcontractors').fetchone()[0]
        self.call('r_sub_put', {'sub_id':third,'principal_tax_in':10000}, cid, 1)
        self.assertTrue(self.s.settlement_number(self.c,cid,third).endswith('-03'))
        with closing(self.s.db()) as reopened:
            self.assertEqual(self.s.settlement_number(reopened,cid,self.other),two)
        self.call('r_company_put', {'name':'採番検証会社'})
        self.assertEqual(self.s.document_context(self.c,cid,'principal_invoice',self.other)['mgmt_no'],two)
        self.s.init_schema(self.c)
        self.assertEqual(self.s.settlement_number(self.c,cid,self.other),two)

    def test_dashboard_principal_and_unsettled_profit(self):
        cid = self.case('利益のみ集計', self.old, 100000, self.today, other=True)
        view = self.s.case_view(self.c, cid)
        dashboard = self.s.r_dashboard(self.c, {}, {})
        self.assertEqual(sum(x['principal'] for x in dashboard['principals']), 120000)
        self.assertEqual(len(dashboard['principals']), 2)
        expected = sum(s['sub_profit'] for s in view['subs'])
        self.assertEqual(dashboard['totals']['sub_unsettled_profit'], expected)
        self.assertEqual(dashboard['totals']['sub_unsettled'], expected + 120000)
        settlement = self.draft(self.old)
        self.c.execute("UPDATE settlements SET status='paid' WHERE id=?", (settlement['id'],))
        self.c.commit()
        dashboard = self.s.r_dashboard(self.c, {}, {})
        self.assertEqual(dashboard['totals']['sub_unsettled_profit'], view['subs'][1]['sub_profit'])

    def test_readable_pdf_copies(self):
        import pdf_archive
        self.assertEqual(pdf_archive.day('2026-10-'), '10／××')
        self.assertEqual(pdf_archive.day(None), '××／××')
        cid = self.case('コピー確認', self.old, 10000)
        self.c.execute('UPDATE cases SET case_no=NULL,start_date=NULL WHERE id=?', (cid,))
        self.c.commit()
        raw = b'%PDF-test-copy'
        from pathlib import Path
        Path(self.s.FILES).mkdir(exist_ok=True)
        Path(self.s.FILES, 'test.pdf').write_bytes(raw)
        self.c.execute("INSERT INTO attachments(case_id,kind,original_name,stored_name,mime,bytes,created_at) VALUES(?,'other','original.pdf','test.pdf','application/pdf',?,?)", (cid,len(raw),self.today))
        self.c.commit()
        root = Path(self.s.DBP).parent / 'LOGIBOX PDF'
        self.s.sync_pdf_archive(self.c)
        copies = list(root.rglob('04_その他/*.pdf'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].name, '添付書類_original.pdf')
        self.assertIn('コピー確認', copies[0].parent.parent.name)
        self.assertTrue((copies[0].parent.parent/'案件証憑一式.pdf').exists())
        self.assertEqual(copies[0].read_bytes(), raw)
        self.s.sync_pdf_archive(self.c)
        self.assertEqual(len(list(root.rglob('*.pdf'))), 3)
        self.assertEqual(Path(self.s.FILES, 'test.pdf').read_bytes(), raw)

    def test_sub_details_preserve_fee_and_legacy_bank(self):
        self.call('r_subm_put', {'name':'既存下請け', 'bank_info':'以前の口座', 'default_fee_rate_pct':3}, self.sid)
        body = dict(name='変更後', address='架空市', phone='09012345678', postal_code='1000000',
                    email='test@example.com', contact_name='担当者', bank_info='以前の口座')
        self.call('r_subm_put', body, self.sid)
        saved = dict(self.c.execute('SELECT * FROM subcontractors WHERE id=?', (self.sid,)).fetchone())
        self.assertEqual(saved['default_fee_rate_pct'], 3)
        self.assertEqual(saved['bank_info'], '以前の口座')
        self.assertEqual(saved['address'], body['address'])
        body.update(bank_name='銀行', bank_branch='本店', account_type='普通', account_number='0012345', account_holder='テスト')
        self.call('r_subm_put', body, self.sid)
        saved = dict(self.c.execute('SELECT * FROM subcontractors WHERE id=?', (self.sid,)).fetchone())
        self.assertEqual(saved['bank_info'], '銀行 本店 普通 0012345 テスト')
        self.assertEqual(saved['default_fee_rate_pct'], 3)
        self.call('r_subm_new', dict(body, name='新規'))
        self.assertEqual(self.c.execute("SELECT default_fee_rate_pct FROM subcontractors WHERE name='新規'").fetchone()[0], 2)

    def test_company_details_and_document_context(self):
        body = dict(name='検証会社', address='架空市見本町', invoice_no='T1234567890123',
                    bank_info='以前の銀行', postal_code='841-0052', phone='09012345678',
                    email='test@example.com', contact_name='担当者', invoice_type='qualified',
                    bank_branch='検証銀行 本店', bank_account='普通 1234567', bank_holder='テスト')
        self.call('r_company_put', body)
        company = dict(self.c.execute('SELECT * FROM company').fetchone())
        for key in self.s.COMPANY_DETAILS:
            self.assertEqual(company[key], body[key])
        self.assertEqual(company['bank_info'], '検証銀行 本店 普通 1234567 テスト')
        cid = self.case('会社情報確認', self.old, 10000)
        ctx = self.s.document_context(self.c, cid, 'principal_invoice', self.sid)
        self.assertIn('841-0052', ctx['issuer_address'])
        self.assertIn('test@example.com', ctx['issuer_contact'])
        with self.assertRaises(self.s.Err):
            self.call('r_company_put', dict(body, invoice_no='T123'))
        self.assertEqual(self.c.execute('SELECT invoice_no FROM company').fetchone()[0], body['invoice_no'])
        self.call('r_company_put', dict(body, invoice_type='standard'))
        self.assertIsNone(self.s.document_context(self.c, cid, 'principal_invoice', self.sid)['issuer_invoice_no'])

    def test_settlement_lifecycle_and_double_counting(self):
        first = self.case('先行案件', self.old, 10000, self.today, other=True)
        second = self.case('別の先行案件', self.old, 10000, self.today)
        for cid in (first,second):
            self.call('r_haak_put',dict(confirmed_amount_tax_in=50000,confirmed_checked=True,paid_date=self.today),cid)
        target = self.case('次案件', self.new, 8000)
        a = self.draft(self.old)
        b = self.draft(self.old, self.other)
        self.assertEqual(a['pay_date'], '2020-12-15')
        self.assertEqual(b['total'], 21000)
        first_line, second_line = [next(x for x in a['lines'] if x['case_id'] == cid) for cid in (first, second)]
        consent = {'carry_over':5500, 'target_id':target, 'consent_date':self.today, 'consent_record':'LINEで承諾'}
        updated = self.call('r_carry', consent, a['id'], first_line['id'])
        self.assertEqual(updated['total'], 15500)
        self.assertEqual(updated['lines'][0]['consent_record'], 'LINEで承諾')
        with self.assertRaisesRegex(self.s.Err, '担当元金を超えています'):
            self.call('r_carry', consent, a['id'], second_line['id'])
        future = self.draft(self.new)
        self.assertEqual(future['pay_date'], '2021-01-15')
        with self.assertRaisesRegex(self.s.Err, '元金入金'):
            self.call('r_settlement_status', {'status':'requested'}, future['id'])
        self.call('r_settlement_status', {'status':'requested'}, a['id'])
        wrong = self.call('r_sub_invoice', self.invoice(98765), a['id'])
        self.assertEqual(wrong['status'], 'mismatch')
        self.assertEqual(wrong['invoices'][0]['detected_amount'], 98765)
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_status', {'status':'paid','paid_at':self.today}, a['id'])
        verified = self.call('r_sub_invoice', self.invoice(15500), a['id'])
        self.assertEqual(verified['status'], 'received')
        self.assertEqual(len(verified['invoices']), 2)
        self.assertEqual(self.call('r_settlement_status', {'status':'paid','paid_at':self.today}, a['id'])['status'], 'paid')
        view = self.call('r_case_get', None, target)
        self.assertEqual(view['subs'][0]['principal_confirmed'], 5500)
        self.call('r_sub_put', {'sub_id':self.sid, 'principal_tax_in':8000, 'principal_received_date':self.today}, target, 1)
        self.assertEqual(self.call('r_case_get', None, target)['subs'][0]['principal_confirmed'], 8000)
        self.call('r_settlement_status', {'status':'requested'}, future['id'])
        with self.assertRaises(self.s.Err):
            self.call('r_trash_move', None, first)
        totals = self.call('r_dashboard')['totals']
        self.assertEqual(totals['sub_unsettled'], 29400)
        self.assertEqual(totals['principal_confirmed'], 48000)
        evidence = self.call('r_evidence', None, first)
        with ZipFile(io.BytesIO(evidence[1])) as archive:
            self.assertEqual(len([n for n in archive.namelist() if n.endswith('.pdf')]), 2)
        backup = self.call('r_backup_full')
        with ZipFile(io.BytesIO(backup[1])) as archive:
            self.assertIn('app.db', archive.namelist())
            self.assertEqual(len([n for n in archive.namelist() if n.startswith('documents/')]), 2)

    def test_csv_and_reporting(self):
        cid = self.case('＝1+1', self.old, 2000000, self.today)
        self.call('r_purchase_new', {'supplier_id':1, 'amount_tax_in':35017990, 'invoice_date':self.today,
                                      'paid_date':self.today, 'paid_amount':1000000}, cid)
        self.call('r_haak_put', {'confirmed_amount_tax_in':37819429, 'billing_date':self.today}, cid)
        profit = self.call('r_export', None, 'profit')
        self.assertTrue(profit[1].startswith(b'\xef\xbb\xbf'))
        rows = list(csv.DictReader(io.StringIO(profit[1].decode('utf-8-sig'))))
        self.assertEqual(rows[0]['案件名'], "'＝1+1")
        self.assertEqual(rows[0]['全体利益'], '2801439')
        self.assertEqual(rows[0]['下請け利益'], '100000')
        self.assertEqual(rows[0]['自社利益'], '2701439')
        for kind in ('transactions','cashflow','balances','settlements'):
            exported = self.call('r_export', None, kind, q={'at':[self.today]})
            self.assertTrue(exported[1].startswith(b'\xef\xbb\xbf'))
        tot = self.call('r_dashboard')['totals']
        self.assertEqual(tot['haak_unpaid'], 37819429)
        self.assertEqual(tot['haak_paid'], 0)

    def test_document_revision_preserves_old_pdf_and_sent_date(self):
        cid = self.case('帳票テスト', self.old, 2000000)
        self.call('r_company_put', {'name':'検証会社','invoice_no':'T1234567890123'})
        issued = self.s.r_doc_new(self.c, {'doc_type':'principal_invoice','sub_id':self.sid}, {}, str(cid),
                                  session='test-session')
        did = str(issued['id'])
        original = self.call('r_doc_download', None, did)[1]
        self.call('r_doc_sent', {'sent_date':self.today}, did)
        preview = self.s.r_doc_edit_preview(self.c, {'note':'更新後の備考'}, {}, did, session='test-session')
        self.c.commit()
        draft = self.s.load_doc_draft(preview['token'], 'test-session')
        with self.assertRaisesRegex(self.s.Err, '送付済み'):
            self.call('r_doc_put', {}, did)
        self.s.r_doc_put(self.c, {'preview_token':preview['token'],'confirm_sent_edit':True}, {}, did,
                         session='test-session')
        self.c.commit()
        history = self.call('r_doc_history', None, did)
        self.assertEqual(history[0]['sent_date'], self.today)
        self.assertEqual(self.call('r_doc_old_download', None, history[0]['id'])[1], original)
        self.assertEqual(self.call('r_doc_download', None, did)[1], draft['pdf'])
        self.assertIsNone(self.call('r_docs', None, cid)[0]['sent_date'])

    def test_migration_preserves_legacy_data_and_makes_backup(self):
        path = Path(self.s.DBP).with_name('legacy.db')
        with closing(sqlite3.connect(path)) as legacy:
            legacy.executescript((Path(self.s.BASE) / 'schema.sql').read_text(encoding='utf-8'))
            legacy.execute("INSERT INTO cases(id,mgmt_no,name,planned_rate_pct) VALUES(1,'24-0001','旧案件',7)")
            legacy.execute("INSERT INTO subcontractors(id,name) VALUES(1,'旧下請け')")
            legacy.execute("INSERT INTO case_subs(case_id,sub_id,slot,principal_tax_in,tax_mode) VALUES(1,1,1,9000,'sale')")
            legacy.commit()
        with patch.object(self.s, 'DBP', str(path)):
            self.s.init()
            self.s.init()
        archive, = path.parent.glob('before-upgrade-*.zip')
        with ZipFile(archive) as backup:
            old = backup.read('app.db')
        self.assertTrue(old.startswith(b'SQLite format 3'))
        with closing(sqlite3.connect(path)) as current:
            self.assertEqual(current.execute('SELECT tax_mode FROM case_subs').fetchone()[0], 'sale')
            self.assertEqual(current.execute('PRAGMA user_version').fetchone()[0], self.s.SCHEMA_VERSION)
            self.assertEqual(current.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main()
