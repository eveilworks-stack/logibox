"""Synthetic DB tests for the explicitly requested master-only switch."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logibox'))
import master_only_setup as setup
import test_principal_invoice

SOURCE=Path(__file__).resolve().parents[1]/'logibox'


class MasterOnlyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_principal_invoice.PrincipalInvoiceTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        a,t=self.fixture.prepare();self.fixture.allocate(a,t);self.fixture.issue(t)
        self.fixture.c.execute("INSERT OR REPLACE INTO settings VALUES('company_seal_png','mock-seal-base64')")
        self.fixture.c.execute("INSERT OR REPLACE INTO settings VALUES('pw','old-password-hash')")
        self.fixture.c.execute("INSERT OR REPLACE INTO settings VALUES('pdf_archive_dir','old-custom-location')")
        self.fixture.c.commit()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.target=Path(self.tmp.name)/'使用中 logibox'
        shutil.copytree(Path(self.fixture.s.DBP).parent,self.target/'data')
        (self.target/'old-program.txt').write_text('old program',encoding='utf-8')
        self.old=setup.read_masters(self.target)
        self.before=self.hashes(self.target)

    def hashes(self,root):
        return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p.name!='.logibox-auth.lock'}

    def test_only_requested_masters_and_seal_survive_and_original_archived(self):
        result=setup.install(self.target,SOURCE)
        archived=Path(result['backup'])/'previous-installation'
        self.assertEqual(self.hashes(archived),self.before)
        with zipfile.ZipFile(Path(result['backup'])/'full-installation.zip') as z:
            for name,digest in self.before.items():
                self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(),digest)
        new=setup.read_masters(self.target)
        for name in (*setup.MASTERS,'seal'):self.assertEqual(new[name],self.old[name])
        self.assertEqual(new['old_cases'],0)
        with closing(sqlite3.connect(self.target/'data/app.db')) as c:
            self.assertEqual(c.execute('SELECT k,v FROM settings').fetchall(),[('company_seal_png','mock-seal-base64')])
            for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                if table not in (*setup.MASTERS,'settings'):
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0],0,table)
        self.assertFalse((self.target/'old-program.txt').exists())
        self.assertEqual(json.loads((self.target/'SOURCE_MANIFEST.json').read_text())['version'],'0.6.8')
        self.assertEqual(list((self.target/'data/documents').iterdir()),[])

    def test_backup_failure_leaves_old_folder_unchanged(self):
        with patch.object(setup,'backup_installation',side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):setup.install(self.target,SOURCE)
        self.assertEqual(self.hashes(self.target),self.before)

    def test_second_rename_failure_restores_original(self):
        actual=os.replace
        def fail_new(source,destination):
            if Path(source).name.startswith('.LOGIBOX-068-preparing-'):
                raise OSError('cannot install staged directory')
            return actual(source,destination)
        with patch.object(setup.os,'replace',side_effect=fail_new):
            with self.assertRaises(OSError):setup.install(self.target,SOURCE)
        self.assertEqual(self.hashes(self.target),self.before)

    def test_running_lock_prevents_any_replacement(self):
        runtime=setup.load_runtime(SOURCE)
        with runtime.data_lock(self.target/'data'):
            with self.assertRaises(RuntimeError):setup.install(self.target,SOURCE)
        self.assertEqual(self.hashes(self.target),self.before)

    def test_bad_package_hash_does_not_touch_old_folder(self):
        source=Path(self.tmp.name)/'bad-source';source.mkdir()
        (source/'server.py').write_text('tampered')
        (source/'SOURCE_MANIFEST.json').write_text(json.dumps(dict(version='0.6.8',sha256={'server.py':'invalid'})))
        with self.assertRaises(RuntimeError):setup.install(self.target,source)
        self.assertEqual(self.hashes(self.target),self.before)

    def test_custom_data_location_aborts(self):
        with patch.dict(os.environ,{'APP_DATA':'other-data'}):
            with self.assertRaises(RuntimeError):setup.install(self.target,SOURCE)
        self.assertEqual(self.hashes(self.target),self.before)

    def test_repeated_switch_cannot_reset_data_again(self):
        setup.install(self.target,SOURCE)
        before=self.hashes(self.target)
        with self.assertRaisesRegex(RuntimeError,'切替済み'):
            setup.install(self.target,SOURCE)
        self.assertEqual(self.hashes(self.target),before)

    def test_windows_launcher_mutex_uses_valid_namespace(self):
        kernel=MagicMock()
        kernel.CreateMutexW.return_value=123
        kernel.WaitForSingleObject.return_value=0
        with patch.object(setup.os,'name','nt'), patch.object(setup.ctypes,'windll',create=True) as windll:
            windll.kernel32=kernel
            with setup.launcher_guard():
                kernel.CreateMutexW.assert_called_once_with(None,False,'Local'+chr(92)+'SeikyuAppLauncher8766')
            kernel.ReleaseMutex.assert_called_once_with(123)
            kernel.CloseHandle.assert_called_once_with(123)

    def test_symlink_aborts(self):
        external=Path(self.tmp.name)/'external';external.mkdir()
        (self.target/'linked').symlink_to(external,target_is_directory=True)
        with self.assertRaises(RuntimeError):setup.install(self.target,SOURCE)
        self.assertEqual(self.hashes(self.target),self.before)

if __name__=='__main__':unittest.main()
