"""Recovery tests operate exclusively on disposable DBs and mocked admin tokens."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import password_recovery as recovery
from auth_security import check_pw, data_lock, hash_pw


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / 'LOGIBOX 日本語'
        self.data = self.app / 'data'
        self.data.mkdir(parents=True)
        self.path = self.data / 'app.db'
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((recovery.ROOT / 'schema.sql').read_text(encoding='utf-8'))
            connection.execute('CREATE TABLE settings(k TEXT PRIMARY KEY,v TEXT)')
            connection.execute('INSERT INTO settings VALUES(?,?)', ('pw', hash_pw('old-password')))
            connection.execute('INSERT INTO settings VALUES(?,?)', ('other-setting', '保持する設定'))
            connection.execute("INSERT INTO company(id,name) VALUES(1,'テスト会社')")
            connection.execute("INSERT INTO cases(mgmt_no,name,planned_rate_pct) VALUES('26-0001','保持する案件',7)")
            connection.execute('PRAGMA user_version=6')
            connection.commit()
        (self.app / 'schema.sql').write_text('unchanged schema', encoding='utf-8')
        for name in ('data/files/仕入.pdf', 'data/documents/請求書.pdf',
                     'document_templates/元金.pdf', 'fonts/font.ttf', 'LOGIBOX PDF/発行.pdf'):
            file = self.app / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'unchanged-content')
        self.before = self.dump()
        self.files_before = self.file_hashes()

    def dump(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return list(connection.iterdump())

    def file_hashes(self):
        return {str(p.relative_to(self.app)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.app.rglob('*') if p.is_file() and p.name != 'app.db' and not p.name.startswith(('.logibox', 'app.db-'))}

    def reset(self, password='new-password', confirmation='new-password', backup=None):
        with patch.object(recovery, 'require_administrator'), patch.object(recovery, 'ensure_stopped'):
            return recovery.reset_password(self.path, password, confirmation, backup or self.root, self.app)

    def test_password_only_and_verified_backup(self):
        backup = self.reset()
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute("SELECT v FROM settings WHERE k='pw'").fetchone()[0]
            self.assertTrue(check_pw('new-password', stored))
            self.assertFalse(check_pw('old-password', stored))
        self.assertEqual([x for x in self.before if not x.startswith('INSERT INTO "settings" VALUES(\'pw\'')],
                         [x for x in self.dump() if not x.startswith('INSERT INTO "settings" VALUES(\'pw\'')])
        self.assertEqual(self.files_before, self.file_hashes())
        with zipfile.ZipFile(backup) as archive:
            self.assertIsNone(archive.testzip())
            old_db = self.root / 'snapshot.db'
            old_db.write_bytes(archive.read('data/app.db'))
            with closing(sqlite3.connect(old_db)) as connection:
                self.assertEqual(self.before, list(connection.iterdump()))
            for name in self.files_before:
                if name == 'schema.sql':
                    continue
                self.assertEqual(archive.read(name.replace('\\', '/')), (self.app / name).read_bytes())
            metadata = archive.read('recovery-backup-info.json')
            self.assertNotIn(b'new-password', metadata)
            self.assertNotIn(b'old-password', metadata)

    @unittest.skipIf(os.name != 'nt', 'Windows管理者トークンのテストはWindows専用')
    def test_non_administrator_denied_before_any_write(self):
        with patch.object(recovery.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=0):
            with self.assertRaises(PermissionError):
                recovery.reset_password(self.path, 'new-password', 'new-password', self.root, self.app)
        self.assertEqual(self.before, self.dump())
        self.assertFalse((self.data / '.logibox-auth.lock').exists())
        self.assertEqual([], list(self.root.glob('LOGIBOX-before-*.zip')))

    def test_short_blank_and_mismatch_no_change(self):
        for password, confirm in [('short', 'short'), ('abcdefgh', 'different'), (' ' * 8, ' ' * 8), ('x' * 1025, 'x' * 1025)]:
            with self.assertRaises(ValueError):
                self.reset(password, confirm)
        self.assertEqual(self.before, self.dump())

    def test_backup_failure_no_change(self):
        with patch.object(recovery, 'create_backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.reset()
        self.assertEqual(self.before, self.dump())

    def test_backup_inside_data_rejected(self):
        with self.assertRaises(ValueError):
            self.reset(backup=self.data)
        self.assertEqual(self.before, self.dump())

    def test_zip_verification_failure_no_change(self):
        with patch.object(zipfile.ZipFile, 'testzip', return_value='corrupt-entry'):
            with self.assertRaises(RuntimeError):
                self.reset()
        self.assertEqual(self.before, self.dump())
        self.assertEqual([], list(self.root.glob('*.partial')))

    def test_update_failure_rolls_back(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TRIGGER refuse_reset BEFORE UPDATE ON settings BEGIN SELECT RAISE(ABORT,'test failure'); END")
            connection.commit()
        before = self.dump()
        with self.assertRaises(sqlite3.IntegrityError):
            self.reset()
        self.assertEqual(before, self.dump())
        self.assertEqual(1, len(list(self.root.glob('LOGIBOX-before-*.zip'))))

    def test_missing_db_not_created(self):
        with self.assertRaises(FileNotFoundError):
            recovery.connect_existing(self.root / 'missing' / 'app.db')
        self.assertFalse((self.root / 'missing').exists())

    def test_wrong_schema_and_unconfigured_db_rejected(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):
            self.reset()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('PRAGMA user_version=6')
            connection.execute("DELETE FROM settings WHERE k='pw'")
            connection.commit()
        before = self.dump()
        with self.assertRaises(ValueError):
            self.reset()
        self.assertEqual(before, self.dump())

    def test_data_lock_blocks_other_process(self):
        with data_lock(self.data):
            code = 'import sys; from auth_security import data_lock\nwith data_lock(sys.argv[1]): pass'
            result = subprocess.run([sys.executable, '-c', code, str(self.data)], cwd=recovery.ROOT, capture_output=True)
            self.assertNotEqual(0, result.returncode)
            with self.assertRaises(RuntimeError):
                self.reset()
        self.assertEqual(self.before, self.dump())

    def test_wal_snapshot_includes_committed_data(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("UPDATE company SET name='WAL内の会社' WHERE id=1")
            connection.commit()
            before = self.dump()
            backup = self.reset()
        with zipfile.ZipFile(backup) as archive:
            old_db = self.root / 'wal-snapshot.db'
            old_db.write_bytes(archive.read('data/app.db'))
            with closing(sqlite3.connect(old_db)) as connection:
                self.assertEqual(before, list(connection.iterdump()))


class SessionTests(unittest.TestCase):
    def test_reset_invalidates_sessions_and_old_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / 'data'
            data.mkdir()
            with patch.dict(os.environ, {'APP_DATA': str(data)}):
                import server
            with patch.dict(os.environ, {'APP_DATA': str(data)}), patch.multiple(server, DBP=str(data / 'app.db'), FILES=str(data / 'files'), DOCS=str(data / 'documents')):
                server.init()
                from http.server import ThreadingHTTPServer
                http = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
                self.addCleanup(http.server_close)
                server.H.port = http.server_port
                thread = threading.Thread(target=http.serve_forever, daemon=True)
                thread.start()
                try:
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

                    code, _, cookie = api('/api/setup', {'password': 'old-password'})
                    self.assertEqual(200, code)
                    self.assertEqual(200, api('/api/cases', cookie=cookie)[0])
                    with patch.object(recovery, 'require_administrator'), patch.object(recovery, 'ensure_stopped'):
                        recovery.reset_password(data / 'app.db', 'new-password', 'new-password', Path(temporary), recovery.ROOT)
                    self.assertEqual(401, api('/api/cases', cookie=cookie)[0])
                    self.assertEqual(401, api('/api/login', {'password': 'old-password'})[0])
                    self.assertEqual(200, api('/api/login', {'password': 'new-password'})[0])
                    self.assertEqual(403, api('/api/setup', {'password': 'unauthorized'})[0])
                    self.assertEqual(401, api('/api/password-reset', {'password': 'unauthorized'})[0])
                finally:
                    http.shutdown()
                    thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
