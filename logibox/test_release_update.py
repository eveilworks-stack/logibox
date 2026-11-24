"""Release identity and non-destructive update regression tests."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile
import app_version
import launcher
import update
from auth_security import data_lock


class LaunchIdentityTests(unittest.TestCase):
    def test_same_release_and_data_can_be_reused(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(app_version.runtime_identity()).encode())):
            self.assertTrue(launcher.ready())

    def test_old_version_other_folder_data_or_build_cannot_be_reused(self):
        for key in ('version', 'source_id', 'data_id', 'build'):
            identity = dict(app_version.runtime_identity(), **{key: 'old'})
            with self.subTest(key=key), patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(identity).encode())):
                with self.assertRaises(RuntimeError): launcher.ready()

    def test_legacy_server_without_runtime_is_rejected(self):
        error = urllib.error.HTTPError(launcher.URL, 404, 'missing', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(RuntimeError): launcher.ready()

    def test_connection_refused_allows_start(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError(ConnectionRefusedError())):
            self.assertFalse(launcher.ready())

    def test_timeout_with_free_port_allows_start(self):
        for error in (TimeoutError(), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=type(error)), patch('urllib.request.urlopen', side_effect=error), patch.object(launcher, 'ensure_stopped') as check:
                self.assertFalse(launcher.ready())
                check.assert_called_once_with()

    def test_timeout_with_busy_or_uncheckable_port_blocks_start(self):
        for error in (TimeoutError(), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=type(error)), patch('urllib.request.urlopen', side_effect=error), patch.object(launcher, 'ensure_stopped', side_effect=RuntimeError('busy')):
                with self.assertRaises(RuntimeError):
                    launcher.ready()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.target = self.root / 'new', self.root / 'old'
        self.source.mkdir(); (self.target / 'data').mkdir(parents=True)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop); os.environ.pop('APP_DATA', None)
        self.db = self.target / 'data/app.db'
        with sqlite3.connect(self.db) as c:
            for table in ('cases', 'invoices', 'payments', 'principal', 'allocations'):
                c.execute(f'CREATE TABLE {table}(id INTEGER, amount INTEGER)')
                c.execute(f'INSERT INTO {table} VALUES(1, 2000000)')
        for name in ('files/evidence.pdf', 'documents/invoice.pdf', 'backups/old.zip'):
            path = self.target / 'data' / name
            path.parent.mkdir(exist_ok=True); path.write_bytes(b'preserve-' + name.encode())
        (self.target / 'server.py').write_text('old')
        (self.source / 'server.py').write_text('new')
        self.manifest = dict(version=app_version.VERSION, sha256={'server.py': hashlib.sha256(b'new').hexdigest()})
        self.save_manifest()

    def save_manifest(self):
        (self.source / 'SOURCE_MANIFEST.json').write_text(json.dumps(self.manifest))

    def snapshots(self):
        return {p.relative_to(self.target).as_posix():p.read_bytes() for p in (self.target / 'data').rglob('*')
                if p.is_file() and p.name != '.logibox-auth.lock'}

    def test_backup_precedes_update_and_all_data_is_unchanged(self):
        before = self.snapshots(); backup = update.update(self.target, self.source)
        self.assertEqual(before, self.snapshots())
        self.assertEqual((self.target / 'server.py').read_text(), 'new')
        with zipfile.ZipFile(backup) as z:
            self.assertEqual(z.read('server.py'), b'old')
            for path, content in before.items(): self.assertEqual(z.read(path), content)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            for table in ('cases','invoices','payments','principal','allocations'):
                self.assertEqual(c.execute(f'SELECT * FROM {table}').fetchall(), [(1, 2000000)])

    def test_only_replaced_module_bytecode_is_invalidated(self):
        cache=self.target/'__pycache__'; cache.mkdir()
        stale=cache/'server.cpython-312.pyc'; stale.write_bytes(b'old bytecode')
        other=cache/'unrelated.cpython-312.pyc'; other.write_bytes(b'keep')
        user_file=self.target/'data/cache.pyc'; user_file.write_bytes(b'user data')
        backup=update.update(self.target,self.source)
        self.assertFalse(stale.exists())
        self.assertEqual(other.read_bytes(),b'keep')
        self.assertEqual(user_file.read_bytes(),b'user data')
        with zipfile.ZipFile(backup) as z:
            self.assertEqual(z.read('__pycache__/server.cpython-312.pyc'),b'old bytecode')

    def test_running_server_lock_prevents_changes(self):
        with data_lock(self.target / 'data'):
            with self.assertRaises(RuntimeError): update.update(self.target, self.source)
        self.assertEqual((self.target / 'server.py').read_text(), 'old')

    def test_missing_database_never_creates_one(self):
        self.db.unlink()
        with self.assertRaises(RuntimeError): update.update(self.target, self.source)
        self.assertFalse(self.db.exists())

    def test_bad_hash_or_data_entry_aborts_before_update(self):
        for name in ('server.py','data/app.db','../outside.py'):
            self.manifest['sha256'] = {name:'invalid'}; self.save_manifest()
            with self.assertRaises(RuntimeError): update.update(self.target,self.source)
        self.assertEqual((self.target / 'server.py').read_text(), 'old')

    def test_backup_failure_prevents_update(self):
        with patch('zipfile.ZipFile.testzip', return_value='broken'):
            with self.assertRaises(RuntimeError): update.update(self.target,self.source)
        self.assertEqual((self.target / 'server.py').read_text(), 'old')

    def test_mac_command_is_allowed_and_executable(self):
        command = self.source / 'start.command'
        command.write_text('#!/bin/sh\nexec python3 server.py\n')
        self.manifest['sha256']['start.command'] = hashlib.sha256(command.read_bytes()).hexdigest()
        self.save_manifest()
        update.update(self.target,self.source)
        self.assertEqual((self.target / 'start.command').read_bytes(),command.read_bytes())
        if os.name != 'nt':
            self.assertTrue(os.access(self.target / 'start.command',os.X_OK))

    def test_existing_fixed_template_is_preserved(self):
        name = 'document_templates/haak_invoice.pdf'
        source = self.source / name
        target = self.target / name
        source.parent.mkdir();target.parent.mkdir()
        source.write_bytes(b'public redacted template')
        target.write_bytes(b'existing private fixed template')
        self.manifest['sha256'][name] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.save_manifest()
        update.update(self.target,self.source)
        self.assertEqual(target.read_bytes(),b'existing private fixed template')


if __name__ == '__main__': unittest.main()
