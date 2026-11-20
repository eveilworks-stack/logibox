"""添付と仕入登録を、認証済みのHTTP APIと一時DBで検証する。"""
import base64
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from itertools import count
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import fitz
from PIL import Image

from test_purchase_reader import synthetic_invoice


def upload(raw, name='invoice.pdf', mime='application/pdf'):
    return dict(filename=name, mime=mime, data=base64.b64encode(raw).decode('ascii'))


pdf_sequence = count()


def sample_pdf(pages=1):
    serial = next(pdf_sequence)
    with fitz.open() as doc:
        for number in range(pages):
            doc.new_page().insert_text((80, 80), f'Test PDF {serial}, page {number + 1} ({pages})')
        return doc.tobytes(no_new_id=True)


class PurchaseApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        with patch.dict(os.environ, {'APP_DATA': directory.name}):
            import server
        cls.server = server
        server.init()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        cls.addClassCleanup(httpd.server_close)
        server.H.loopback = True
        server.H.port = httpd.server_port
        cls.url = f'http://127.0.0.1:{httpd.server_port}'
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(thread.join)
        cls.addClassCleanup(httpd.shutdown)
        cls.cookie = ''
        status, headers, _ = cls.request('POST', '/api/setup', {'password': 'test-password'})
        if status != 200:
            raise RuntimeError('test server setup failed')
        cls.cookie = headers['Set-Cookie'].split(';')[0]

    @classmethod
    def request(cls, method, path, body=None, *, auth=True):
        req = Request(cls.url + path, json.dumps(body).encode() if body is not None else None, {
            'Content-Type': 'application/json',
            'X-Requested-With': 'app',
            'Cookie': cls.cookie if auth else '',
        }, method=method)
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.code, response.headers, raw

    def api(self, method, path, body=None, expected=200, **kwargs):
        status, _, raw = self.request(method, path, body, **kwargs)
        result = json.loads(raw)
        self.assertEqual(status, expected, result)
        return result

    def create_case(self, case_no=None):
        result = self.api('POST', '/api/cases', {'name': '検証案件', 'case_no': case_no, 'planned_rate_pct': 7})
        return '/api/cases/' + str(result['id'])

    def test_manual_purchase_supplier_create_edit_reload_without_pdf(self):
        case = self.create_case('MANUAL-064')
        other = self.create_case('MANUAL-PROTECTED')
        untouched = self.api('GET', other)
        original_case = self.api('GET', case)['case']
        supplier = self.api('POST', '/api/suppliers', {'name': '手入力仕入先064', 'invoice_no': 'T1234567890123'})
        self.assertIn(supplier['id'], [s['id'] for s in self.api('GET', '/api/master')['suppliers']])
        self.assertIn('同じ名前', self.api('POST', '/api/suppliers', {'name': '手入力仕入先064'}, expected=400)['error'])
        body = dict(supplier_id=supplier['id'], amount_tax_in='123,456', invoice_date='2026-11-01',
                    due_date='2026-11-30', paid_amount='23,456', paid_date='2026-11-20')
        pid = self.api('POST', case + '/purchases', body)['id']
        saved = self.api('GET', case)['purchases'][0]
        self.assertEqual(saved['id'], pid)
        for key, value in body.items():
            self.assertEqual(saved[key], int(value.replace(',', '')) if key in ('amount_tax_in', 'paid_amount') else value)
        updated = dict(supplier_id=2, amount_tax_in=234567, invoice_date='2026-11-02',
                       due_date='2026-12-01', paid_amount=34567, paid_date='2026-11-21')
        self.api('PUT', f'/api/purchases/{pid}', updated)
        saved = self.api('GET', case)['purchases'][0]
        for key, value in updated.items():
            self.assertEqual(saved[key], value)
        for invalid, message in ((dict(supplier_id=''), '仕入先'), (dict(supplier_id=9999999), '仕入先'),
                                 (dict(amount_tax_in=''), '税込請求額'), (dict(paid_amount=999999), '請求額'),
                                 (dict(invoice_date='bad'), '請求日')):
            result = self.api('PUT', f'/api/purchases/{pid}', {**updated, **invalid}, expected=400)
            self.assertIn(message, result['error'])
            self.assertEqual(self.api('GET', case)['purchases'][0], saved)
        self.assertEqual(self.api('GET', case + '/attachments'), [])
        self.assertEqual(self.api('GET', case)['case'], original_case)
        self.assertEqual(self.api('GET', other), untouched)

    def test_partial_read_values_can_be_saved_without_attaching_pdf(self):
        case = self.create_case('MANUAL-PARTIAL-064')
        pdf = upload(synthetic_invoice('エムロジ', missing_gross=True))
        result = self.api('POST', '/api/purchase-read', pdf)
        self.assertIsNone(result['fields']['amount_tax_in'])
        body = dict(supplier_id=2, amount_tax_in=123456, invoice_date=result['fields']['invoice_date'],
                    due_date=result['fields']['due_date'],
                    invoice_details={**result['fields'], 'items': result['items']})
        self.api('POST', case + '/purchases', body)
        saved = self.api('GET', case)['purchases'][0]
        self.assertEqual(saved['amount_tax_in'], 123456)
        self.assertEqual(saved['invoice_date'], result['fields']['invoice_date'])
        self.assertEqual(saved['invoice_details']['items'],
                         [{k: v for k, v in item.items() if k != 'tax_rate'} for item in result['items']])
        self.assertEqual(self.api('GET', case + '/attachments'), [])

    def test_preview_security_pdf_pages_and_image(self):
        case = self.create_case()
        raw = sample_pdf(2)
        result = self.api('POST', case + '/attachments', {**upload(raw), 'kind': 'purchase_invoice'})
        route = f'/api/attachments/{result["id"]}'
        for suffix in ('/preview', '/preview?info=1', '/preview?page=1', '/file'):
            status, headers, _ = self.request('GET', route + suffix, auth=False)
            self.assertEqual(status, 401)
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
        status, headers, received = self.request('GET', route + '/preview')
        self.assertEqual((status, received), (200, raw))
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertTrue(headers['Content-Disposition'].startswith('inline;'))
        self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'self'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.api('GET', route + '/preview?info=1'), {'pages': 2})
        for page in (1, 2):
            status, headers, raster = self.request('GET', route + f'/preview?page={page}')
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'image/png')
            self.assertTrue(raster.startswith(b'\x89PNG\r\n\x1a\n'))
        self.api('GET', route + '/preview?page=3', expected=404)
        status, headers, received = self.request('GET', route + '/file')
        self.assertEqual((status, received), (200, raw))
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        _, headers, _ = self.request('GET', '/')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')

        image = io.BytesIO()
        Image.new('RGB', (32, 64), 'white').save(image, 'PNG')
        png = image.getvalue()
        self.api('POST', case + '/attachments', {
            **upload(png, 'receipt.png', 'image/png'), 'kind': 'purchase_receipt',
        }, expected=400)
        cid = int(case.rsplit('/', 1)[1])
        stored = self.server.attachments.store(self.server.FILES, png, '.png')
        with closing(self.server.db()) as c, c:
            aid = c.execute("""INSERT INTO attachments(case_id,kind,original_name,stored_name,mime,bytes,created_at)
                               VALUES (?,'purchase_receipt','receipt.png',?,'image/png',?,'2026-10-01')""",
                            (cid, stored, len(png))).lastrowid
        status, headers, raw = self.request('GET', f'/api/attachments/{aid}/preview')
        self.assertEqual((status, headers['Content-Type'], raw), (200, 'image/png', png))
        self.assertTrue(headers['Content-Disposition'].startswith('inline;'))

    def test_pdf_content_validation_and_manual_entry_for_scanned_pdf(self):
        case = self.create_case()
        data = io.BytesIO()
        Image.new('RGB', (16, 24), 'white').save(data, 'JPEG')
        jpg = data.getvalue()
        for raw in (jpg, b'%PDF-1.7\nnot a real PDF'):
            pdf = upload(raw)
            self.api('POST', case + '/attachments', {**pdf, 'kind': 'purchase_invoice'}, expected=400)
            self.api('POST', '/api/purchase-read', pdf, expected=400)
        pdf = upload(sample_pdf())
        saved = self.api('POST', case + '/attachments', {**pdf, 'kind': 'other'})
        previous = self.api('GET', case + '/attachments')[0]
        self.api('PUT', f'/api/attachments/{saved["id"]}', {
            **upload(jpg, 'fake.pdf'), 'confirmed': True, 'version': previous['version'],
        }, expected=400)
        self.assertEqual(self.api('GET', case + '/attachments'), [previous])
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_image(fitz.Rect(40, 40, 80, 100), stream=jpg)
            scanned = upload(doc.tobytes())
        result = self.api('POST', '/api/purchase-read', scanned)
        self.assertEqual(result['status'], 'image_only')
        self.assertIn('手入力', result['message'])
        self.assertEqual(result['fields'], {})
        receipt = self.api('POST', case + '/purchase-import', {
            'supplier_id': 1, 'amount_tax_in': 2000000, 'invoice_details': {},
            'attachment': scanned, 'confirm_unmatched_case': True,
        })
        pdf_preview = self.request('GET', f'/api/attachments/{receipt["attachment_id"]}/preview')
        self.assertEqual(pdf_preview[2], base64.b64decode(scanned['data']))

    def test_both_sub_slots_fixed_and_legacy_values_are_reported(self):
        case = self.create_case('SP99100')
        for name in ('移行検証１', '移行検証２'):
            self.api('POST', '/api/subcontractors', {'name': name})
        subs = [s['id'] for s in self.api('GET', '/api/master')['subs'] if s['name'].startswith('移行検証')]
        for slot, sub in enumerate(subs, 1):
            body = dict(sub_id=sub, principal_tax_in=2000000)
            path = case + f'/subs/{slot}'
            for invalid in ('sale', 'deposit', '', None):
                self.api('PUT', path, {**body, 'tax_mode': invalid}, expected=400)
            self.api('PUT', path, body if slot == 1 else {**body, 'tax_mode': 'consignment'})
        self.assertEqual([sub['tax_mode'] for sub in self.api('GET', case)['subs']],
                         ['consignment', 'consignment'])
        cid = int(case.rsplit('/', 1)[1])
        with closing(self.server.db()) as c, c:
            c.execute("""UPDATE case_subs SET tax_mode=CASE slot WHEN 1 THEN 'sale' ELSE 'deposit' END
                         WHERE case_id=?""", (cid,))
        before = self.api('GET', case)['subs']
        for sub in before:
            path = case + f"/subs/{sub['slot']}"
            body = dict(sub_id=sub['sub_id'], principal_tax_in=100)
            self.api('PUT', path, {**body, 'tax_mode': 'sale'}, expected=400)
            self.api('PUT', path, body, expected=409)
        self.assertEqual(self.api('GET', case)['subs'], before)
        report = [row for row in self.api('GET', '/api/sub-mode-migrations') if row['case_id'] == cid]
        self.assertEqual([(row['slot'], row['tax_mode']) for row in report], [(1, 'sale'), (2, 'deposit')])

    def test_invoice_slot_dedup_replace_and_backup(self):
        case = self.create_case()
        pid = self.api('POST', case + '/purchases', {'supplier_id': 1, 'amount_tax_in': 100})['id']
        attach_path = case + '/attachments'
        original = sample_pdf()
        first = {**upload(original), 'purchase_id': pid, 'kind': 'purchase_invoice'}
        aid = self.api('POST', attach_path, first)['id']
        self.api('POST', attach_path, {**first, 'filename': 'renamed.pdf', 'purchase_id': None}, expected=409)
        second = sample_pdf(2)
        self.api('POST', attach_path, {**first, **upload(second)}, expected=409)
        receipt = self.api('POST', attach_path, {
            **first, **upload(second, 'receipt.pdf'), 'kind': 'purchase_receipt',
        })
        rows = self.api('GET', attach_path)
        current = next(row for row in rows if row['id'] == aid)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(receipt['id'], aid)
        replace = f'/api/attachments/{aid}'
        newest = sample_pdf(3)
        body = {**upload(newest, 'new.pdf'), 'version': current['version']}
        self.api('PUT', replace, body, expected=409)
        self.api('PUT', replace, {**body, **upload(second), 'confirmed': True}, expected=409)
        self.assertEqual(self.request('GET', replace + '/preview')[2], original)
        self.api('PUT', replace, {**body, 'confirmed': True})
        self.api('PUT', replace, {**body, 'confirmed': True}, expected=409)
        self.assertEqual(self.request('GET', replace + '/preview')[2], newest)
        after = next(row for row in self.api('GET', attach_path) if row['id'] == aid)
        self.assertEqual((after['purchase_id'], after['kind'], after['original_name']),
                         (pid, 'purchase_invoice', 'new.pdf'))
        status, _, data = self.request('GET', '/api/backup/full')
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as backup:
            self.assertEqual(backup.read('files/' + current['version']), original)
            self.assertEqual(backup.read('files/' + after['version']), newest)

    def test_legacy_duplicate_display_and_existing_links(self):
        case = self.create_case()
        pid = self.api('POST', case + '/purchases', {'supplier_id': 1, 'amount_tax_in': 100})['id']
        raw = sample_pdf()
        aid = self.api('POST', case + '/attachments', {
            **upload(raw), 'kind': 'purchase_invoice', 'purchase_id': pid,
        })['id']
        srv = self.server
        stored = srv.attachments.store(srv.FILES, raw, '.pdf')
        with srv.db() as connection:
            duplicate = connection.execute(
                '''INSERT INTO attachments(case_id, kind, original_name, stored_name, mime, bytes, created_at)
                   SELECT case_id, kind, 'legacy-copy.pdf', ?, mime, bytes, created_at FROM attachments WHERE id=?''',
                (stored, aid)).lastrowid
        self.assertEqual([row['id'] for row in self.api('GET', case + '/attachments')], [aid])
        self.assertEqual(self.request('GET', f'/api/attachments/{duplicate}/file')[2], raw)
        self.assertEqual(self.api('GET', case + '/attachments')[0]['purchase_id'], pid)

    def test_import_is_confirmed_atomic_and_keeps_original_tax(self):
        case = self.create_case('SP89001')
        original_file = upload(synthetic_invoice('エムエスト', case_no='SP89001'))
        result = self.api('POST', case + '/purchase-read', original_file)
        self.assertEqual(result['fields']['tax_as_printed'], 3183454)
        self.assertEqual(self.api('GET', case)['purchases'], [])
        self.assertEqual(self.api('GET', case + '/attachments'), [])
        body = dict(
            supplier_id=1, amount_tax_in=35017990, invoice_date='2026-10-08', due_date='2026-10-09',
            invoice_details={**result['fields'], 'items': result['items'], 'tax_as_printed': 3183455},
            attachment=original_file,
        )
        wrong = self.create_case('SP99999')
        self.api('POST', wrong + '/purchase-import', body, expected=409)
        self.assertEqual(self.api('GET', wrong)['purchases'], [])
        saved = self.api('POST', case + '/purchase-import', body)
        # A repeated import does not persist an extra purchase or attachment.
        self.api('POST', case + '/purchase-import', body, expected=409)
        purchases = self.api('GET', case)['purchases']
        self.assertEqual(len(purchases), 1)
        self.assertEqual(len(self.api('GET', case + '/attachments')), 1)
        self.assertEqual(purchases[0]['invoice_original']['fields']['tax_as_printed'], 3183454)
        self.assertEqual(purchases[0]['invoice_details']['tax_as_printed'], 3183455)
        self.assertEqual(len(purchases[0]['invoice_original']['items']), 7)
        self.assertEqual(purchases[0]['invoice_details']['items'][0]['quantity'], 80)
        body['invoice_details']['tax_as_printed'] = 3183456
        self.api('PUT', f'/api/purchases/{saved["id"]}', body)
        purchase = self.api('GET', case)['purchases'][0]
        self.assertEqual(purchase['invoice_original']['fields']['tax_as_printed'], 3183454)
        self.assertEqual(purchase['invoice_details']['tax_as_printed'], 3183456)

    def test_find_active_case_by_pdf_number_and_block_other_destination(self):
        match = self.create_case('ＳＰ２５３７５')
        other = self.create_case('SP90001')
        raw_pdf = synthetic_invoice()
        pdf = upload(raw_pdf)
        self.api('POST', '/api/purchase-read', pdf, expected=401, auth=False)
        result = self.api('POST', '/api/purchase-read', pdf)
        selection = result['case_selection']
        self.assertEqual(selection['mode'], 'matched')
        self.assertEqual([case['id'] for case in selection['cases']], [int(match.rsplit('/', 1)[1])])
        self.assertEqual(result['fields']['amount_tax_in'], 15026299)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(self.api('GET', match)['purchases'], [])
        body = {'supplier_id': 2, 'amount_tax_in': 15026299,
                'invoice_details': {'items': result['items']}, 'attachment': pdf, 'confirm_unmatched_case': True}
        self.api('POST', other + '/purchase-import', body, expected=409)
        saved = self.api('POST', match + '/purchase-import', body)
        self.assertEqual(self.api('GET', match + '/attachments')[0]['purchase_id'], saved['id'])
        self.api('POST', other + '/attachments', {**pdf, 'kind': 'purchase_invoice'}, expected=409)
        self.assertEqual(self.api('GET', other)['purchases'], [])
        self.assertEqual(self.api('GET', other + '/attachments'), [])

    def test_unmatched_requires_explicit_choice_and_does_not_create_case(self):
        case = self.create_case('SP90002')
        source_case = self.create_case('SP80001')
        self.api('POST', source_case + '/trash')
        pdf = upload(synthetic_invoice('エムエスト', carry_over=True, case_no='SP80001'))
        before = self.api('GET', '/api/cases')
        result = self.api('POST', '/api/purchase-read', pdf)
        self.assertEqual(result['case_selection']['mode'], 'unmatched')
        allowed = [item['id'] for item in result['case_selection']['cases']]
        self.assertIn(int(case.rsplit('/', 1)[1]), allowed)
        self.assertNotIn(int(source_case.rsplit('/', 1)[1]), allowed)
        body = {'supplier_id': 1, 'amount_tax_in': result['fields']['amount_tax_in'],
                'invoice_details': {'items': result['items']}, 'attachment': pdf}
        self.api('POST', case + '/purchase-import', body, expected=409)
        self.assertEqual(self.api('GET', case)['purchases'], [])
        self.api('POST', case + '/purchase-import', {**body, 'confirm_unmatched_case': True})
        saved = self.api('GET', case)['purchases'][0]
        self.assertEqual(saved['invoice_original']['fields']['case_no'], 'SP80001')
        self.assertEqual(saved['invoice_original']['fields']['tax_as_printed'], 3183454)
        self.assertEqual({row['id'] for row in self.api('GET', '/api/cases')}, {row['id'] for row in before})

    def set_document_context(self, case):
        self.api('PUT', '/api/company', {'name': '検証会社'})
        name = '検証下請け ' + case.rsplit('/', 1)[1]
        self.api('POST', '/api/subcontractors', {'name': name})
        sid = next(sub['id'] for sub in self.api('GET', '/api/master')['subs'] if sub['name'] == name)
        self.api('PUT', case + '/subs/1', {'sub_id': sid, 'principal_tax_in': 2000000})
        self.api('PUT', case + '/haak', {'confirmed_amount_tax_in': 3000000, 'confirmed_checked': True})
        return sid

    def document_preview(self, case, data):
        token = self.api('POST', case + '/document-preview', data)['token']
        url = f'/api/document-previews/{token}/preview'
        status, headers, pdf = self.request('GET', url)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertTrue(headers['Content-Disposition'].startswith('inline;'))
        return token, url, pdf

    def test_principal_items_from_sp23648_and_legacy_notes(self):
        case = self.create_case('SP23648')
        sid = self.set_document_context(case)
        fixture = os.environ.get('PHASE2_EMEST_PDF')
        raw = Path(fixture).read_bytes() if fixture else synthetic_invoice('エムエスト')
        pdf = upload(raw)
        read = self.api('POST', '/api/purchase-read', pdf)
        expected = [item['name'] for item in read['items']]
        self.assertEqual(len(expected), 7)
        self.api('POST', case + '/purchase-import', {
            'supplier_id': 1, 'amount_tax_in': 35017990,
            'invoice_details': {'items': read['items']}, 'attachment': pdf,
        })
        special = '香水 A/B-015（限定） × + ＆ ® ™ ① ／ 50mL'
        self.api('POST', case + '/purchases', {
            'supplier_id': 1, 'amount_tax_in': 100,
            'items_text': special + ' / 10Lot / 12,340円（税抜）',
        })
        expected.append(special)
        defaults = self.api('GET', case)['principal_items_text']
        self.assertEqual(defaults.splitlines(), expected)

        tests = {'doc_type': 'principal_invoice', 'sub_id': sid}
        _, _, plain = self.document_preview(case, {**tests, 'items_text': ''})
        token, _, pdf = self.document_preview(case, tests)
        totals = ('1,818,182', '181,818', '2,000,000')
        with fitz.open(stream=plain, filetype='pdf') as base, fitz.open(stream=pdf, filetype='pdf') as preview:
            self.assertEqual(len(preview), 1)
            page = preview[0]
            names = page.get_text(clip=fitz.Rect(47, 266, 397, 610))
            positions = [names.index(name) for name in expected]
            self.assertEqual(positions, sorted(positions))
            self.assertFalse(page.get_text(clip=fitz.Rect(397, 266, 595, 610)).strip())
            self.assertNotIn('12,340', page.get_text())
            self.assertNotIn('割引', page.get_text())
            for amount in totals:
                self.assertIn(amount, page.get_text().splitlines())
                self.assertEqual(page.get_text().count(amount), base[0].get_text().count(amount))
        self.assertEqual(self.api('GET', case + '/documents'), [])
        issued = self.api('POST', case + '/documents', {'preview_token': token})
        after = self.request('GET', f'/api/documents/{issued["id"]}/file')[2]
        self.assertEqual(after, pdf)

    def test_all_document_drafts_and_exact_issuance_keep_history(self):
        case = self.create_case()
        sid = self.set_document_context(case)
        for form in (
            {'doc_type': 'principal_invoice', 'sub_id': sid, 'items_text': '確認中の商品 100mL'},
            {'doc_type': 'receipt', 'sub_id': sid, 'received_date': '2026-11-10', 'amount_tax_in': 2000000},
            {'doc_type': 'haak_invoice'},
        ):
            with self.subTest(type=form['doc_type']):
                before = self.api('GET', case + '/documents')
                draft, url, pdf = self.document_preview(case, {**form, 'note': '入力中の備考'})
                self.assertIn('入力中の備考', self.pdf_text(pdf))
                with fitz.open(stream=pdf, filetype='pdf') as embedded:
                    fonts = [font[0] for font in embedded[0].get_fonts() if 'BIZUDPGothic-Regular' in font[3]]
                    self.assertTrue(fonts)
                    self.assertTrue(all(embedded.extract_font(font)[-1] for font in fonts))
                    if form['doc_type'] == 'principal_invoice':
                        amounts = embedded[0].search_for('1,818,182')
                        self.assertEqual(len(amounts), 3)
                        self.assertTrue(all(rect.x1 < 562.8 for rect in amounts))
                    if form['doc_type'] == 'haak_invoice':
                        self.assertTrue(embedded[0].get_text(clip=fitz.Rect(459, 719, 555, 738)).strip())
                self.assertEqual(self.request('GET', url, auth=False)[0], 401)
                self.assertEqual(self.api('GET', url + '?info=1'), {'pages': 1})
                status, headers, image = self.request('GET', url + '?page=1')
                self.assertEqual((status, headers['Content-Type'], image[:4]), (200, 'image/png', b'\x89PNG'))
                self.assertEqual(self.api('GET', case + '/documents'), before)
                issued = self.api('POST', case + '/documents', {'preview_token': draft})
                self.assertFalse(issued['doc_no'].endswith('-2'))
                self.api('POST', case + '/documents', {'preview_token': draft}, expected=404)
                route = f'/api/documents/{issued["id"]}'
                self.assertEqual(self.request('GET', route + '/file')[2], pdf)
                self.assertEqual(self.request('GET', route + '/preview', auth=False)[0], 401)
                self.assertEqual(self.request('GET', route + '/preview')[2], pdf)
                self.assertEqual(self.api('GET', route + '/preview?info=1'), {'pages': 1})
                self.api('PUT', route, {'note': '履歴として参照する前版'})
                history = self.api('GET', route + '/history')
                self.assertEqual(len(history), 1)
                self.document_preview(case, {**form, 'note': '次の発行の確認中'})
                self.assertEqual(self.api('GET', route + '/history'), history)
                self.assertEqual(self.request('GET', f'/api/document-history/{history[0]["id"]}/file')[2], pdf)
                self.assertEqual(self.request('GET', f'/api/document-history/{history[0]["id"]}/preview')[2], pdf)

    def test_stale_draft_and_overflow_keep_one_set_of_amounts(self):
        case = self.create_case()
        sid = self.set_document_context(case)
        words = '折返し商品名（限定）' * 15 + '末尾 ABC-123®'
        items = [f'商品{i:02d}' for i in range(20)] + [words]
        body = {'doc_type': 'principal_invoice', 'sub_id': sid, 'items_text': '\n'.join(items)}
        token, url, pdf = self.document_preview(case, body)
        self.assertEqual(self.api('GET', url + '?info=1'), {'pages': 2})
        with fitz.open(stream=pdf, filetype='pdf') as doc:
            self.assertIn('末尾 ABC-123®', doc[1].get_text())
            self.assertNotIn('2,000,000', doc[1].get_text())
            self.assertIn('2,000,000', doc[0].get_text())
        competing = self.api('POST', case + '/documents', body)
        self.assertEqual(self.api('POST', case + '/documents', {'preview_token':token}, expected=409)['error'],
                         '帳票に反映する内容が更新されました。仮プレビューを作成し直してください。')
        self.assertEqual([doc['id'] for doc in self.api('GET', case + '/documents')], [competing['id']])

    def pdf_text(self, pdf):
        with fitz.open(stream=pdf, filetype='pdf') as document:
            return '\n'.join(page.get_text() for page in document)

    def test_existing_document_issuance_and_history(self):
        case = self.create_case()
        sid = self.set_document_context(case)
        for data in (
            {'doc_type': 'principal_invoice', 'sub_id': sid},
            {'doc_type': 'receipt', 'sub_id': sid, 'received_date': '2026-11-10', 'amount_tax_in': 2000000},
            {'doc_type': 'haak_invoice'},
        ):
            saved = self.api('POST', case + '/documents', data)
            route = '/api/documents/' + str(saved['id'])
            original = self.request('GET', route + '/file')[2]
            self.assertTrue(original.startswith(b'%PDF-'))
            self.assertEqual(self.api('PUT', route, {'note': '編集履歴の検証'})['revision'], 2)
            history = self.api('GET', route + '/history')
            self.assertEqual([row['revision'] for row in history], [1])
            self.assertEqual(self.request('GET', f'/api/document-history/{history[0]["id"]}/file')[2], original)

    def test_concurrent_uploads_only_create_one_invoice_slot(self):
        case = self.create_case()
        pid = self.api('POST', case + '/purchases', {'supplier_id': 1, 'amount_tax_in': 100})['id']

        def attach(pages):
            body = {**upload(sample_pdf(pages)), 'kind': 'purchase_invoice', 'purchase_id': pid}
            return self.request('POST', case + '/attachments', body)[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(attach, (1, 2))), [200, 409])
        self.assertEqual(len(self.api('GET', case + '/attachments')), 1)


if __name__ == '__main__':
    unittest.main()

