"""Seal-only restoration and bootstrap use disposable DBs and synthetic images."""
import base64
from contextlib import closing, ExitStack
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile
from PIL import Image


class SealRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.root / 'data'
        self.data.mkdir()
        self.seed = self.root / 'bundled_company_seal.png'
        image = Image.new('RGBA', (64, 64), (220, 0, 0, 180))
        image.save(self.seed)
        self.encoded = base64.b64encode(self.seed.read_bytes()).decode('ascii')
        self.stack.enter_context(patch.dict(os.environ, {'APP_DATA': str(self.data)}))
        import server
        self.s = server
        self.stack.enter_context(patch.multiple(server, DBP=str(self.data / 'app.db'),
                                               FILES=str(self.data / 'files'), DOCS=str(self.data / 'documents'),
                                               SEAL_SEED=str(self.seed)))
        server.init()
        self.c = self.stack.enter_context(closing(server.db()))
        self.s.setset(self.c, 'pw', self.s.hash_pw('existing-password'))
        self.c.execute("UPDATE company SET name='既存の会社' WHERE id=1")
        self.c.execute("INSERT INTO cases(mgmt_no,name,planned_rate_pct) VALUES('26-0001','保持する案件',7)")
        self.c.execute('INSERT INTO mgmt_counters(year,last_seq) VALUES(2026,1)')
        self.c.commit()

    def dump(self):
        return list(self.c.iterdump())

    def remove_seal(self):
        self.c.execute("DELETE FROM settings WHERE k='company_seal_png'")
        self.c.commit()

    def test_new_database_seeds_seal_without_password(self):
        self.assertEqual(self.encoded, self.s.getset(self.c, 'company_seal_png'))
        fresh = self.root / 'fresh.db'
        with patch.object(self.s, 'DBP', str(fresh)):
            self.s.init()
            with closing(self.s.db()) as connection:
                self.assertEqual(self.encoded, self.s.getset(connection, 'company_seal_png'))
                self.assertIsNone(self.s.getset(connection, 'pw'))
                self.assertEqual(0, connection.execute('SELECT count(*) FROM cases').fetchone()[0])

    def test_existing_database_not_automatically_changed(self):
        self.remove_seal()
        before = self.dump()
        self.s.init()
        self.assertEqual(before, self.dump())
        self.s.setset(self.c, 'company_seal_png', 'existing-custom-seal')
        self.c.commit()
        self.s.init()
        self.assertEqual('existing-custom-seal', self.s.getset(self.c, 'company_seal_png'))

    def test_restore_changes_only_seal_and_backs_up_old_database(self):
        self.remove_seal()
        files = Path(self.s.FILES)
        files.mkdir(exist_ok=True)
        (files / 'existing.pdf').write_bytes(b'existing evidence')
        before = self.dump()
        result = self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})
        self.c.commit()
        self.assertEqual(before, [line for line in self.dump() if not line.startswith('INSERT INTO "settings" VALUES(\'company_seal_png\'')])
        self.assertTrue(self.s.check_pw('existing-password', self.s.getset(self.c, 'pw')))
        self.assertEqual(self.encoded, self.s.getset(self.c, 'company_seal_png'))
        self.assertEqual(b'existing evidence', (files / 'existing.pdf').read_bytes())
        with zipfile.ZipFile(self.data / result['backup']) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(b'existing evidence', archive.read('files/existing.pdf'))
            old = self.root / 'before.db'
            old.write_bytes(archive.read('app.db'))
            with closing(sqlite3.connect(old)) as connection:
                self.assertEqual(before, list(connection.iterdump()))

    def test_confirmation_required(self):
        before = self.dump()
        with self.assertRaises(self.s.Err):
            self.s.r_company_seal_restore(self.c, {}, {})
        self.assertEqual(before, self.dump())

    def test_invalid_or_missing_image_no_change(self):
        before = self.dump()
        self.seed.write_bytes(b'not a PNG')
        with self.assertRaises(self.s.Err):
            self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})
        self.seed.unlink()
        with self.assertRaises(self.s.Err):
            self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})
        self.assertEqual(before, self.dump())

    def test_backup_write_or_verification_failure_no_change(self):
        self.remove_seal()
        before = self.dump()
        with patch.object(self.s.tempfile, 'NamedTemporaryFile', side_effect=PermissionError('no space')):
            with self.assertRaises(PermissionError):
                self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})
        with patch.object(zipfile.ZipFile, 'testzip', return_value='corrupt'):
            with self.assertRaises(self.s.Err):
                self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})
        self.assertEqual(before, self.dump())

    def test_repeated_restore_is_noop(self):
        before = self.dump()
        self.assertTrue(self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})['unchanged'])
        self.assertEqual(before, self.dump())
        self.assertEqual([], list(self.data.glob('before-seal-restore-*')))

    def test_restored_seal_is_embedded_in_invoice_pdf(self):
        self.remove_seal()
        self.s.r_company_seal_restore(self.c, {'confirmed': True}, {})
        self.c.commit()
        self.s.r_haak_put(self.c, {'confirmed_amount_tax_in': 100000, 'confirmed_checked': True}, {}, '1')
        self.c.commit()
        data = self.s.document_payload(self.c, {'issued_date': '2026-11-19'}, 1, 'haak_invoice')
        data['doc_no'] = 'HI-TEST'
        self.assertEqual(self.encoded, data['issuer_seal_png'])
        raw = self.s.documents.render('haak_invoice', data)
        import pymupdf
        with pymupdf.open(stream=raw, filetype='pdf') as document:
            self.assertTrue(any(item[2:4] == (64, 64) for item in document[0].get_images()))

    def test_http_requires_login_and_restores_preview_data(self):
        self.remove_seal()
        before = self.dump()
        from http.server import ThreadingHTTPServer
        http = ThreadingHTTPServer(('127.0.0.1', 0), self.s.H)
        self.addCleanup(http.server_close)
        self.s.H.port = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{http.server_port}'

        def api(path, body=None, cookie=''):
            request = Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                              headers={'X-Requested-With': 'app', 'Cookie': cookie})
            try:
                response = urlopen(request)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), response.headers.get('Set-Cookie', '').split(';')[0]

        try:
            self.assertEqual(401, api('/api/company/seal/restore', {'confirmed': True})[0])
            self.assertEqual(before, self.dump())
            code, _, cookie = api('/api/login', {'password': 'existing-password'})
            self.assertEqual(200, code)
            self.assertEqual(200, api('/api/company/seal/restore', {'confirmed': True}, cookie)[0])
            company = api('/api/master', cookie=cookie)[1]['company']
            self.assertEqual(self.encoded, company['seal_png'])
            self.assertTrue(company['bundled_seal_available'])
            self.assertEqual('既存の会社', company['name'])
        finally:
            http.shutdown()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
