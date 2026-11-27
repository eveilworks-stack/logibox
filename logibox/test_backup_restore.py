"""Restore only temporary copies: legacy, business invariants, malicious input, recovery."""
import base64
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import backup_restore as br
import test_workflow


class RestoreTests(unittest.TestCase):
    setUp=test_workflow.WorkflowTests.setUp
    call=test_workflow.WorkflowTests.call
    case=test_workflow.WorkflowTests.case
    draft=test_workflow.WorkflowTests.draft

    def root(self):return Path(self.s.DBP).parent

    def backup(self):return self.s.r_backup_full(self.c,{}, {})[1]

    def prepare(self,raw):return br.prepare(raw,self.s.BASE,self.root())

    def restore(self,info):
        return self.call('r_backup_restore',{'confirmed':True,'token':info['token']})

    def payload(self,raw):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:return {n:z.read(n) for n in z.namelist() if n!='backup-info.json'}

    def zip(self,payload):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
            for n,raw in payload.items():z.writestr(n,raw)
        return stream.getvalue()

    def change_db(self,raw,sql):
        payload=self.payload(raw)
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'app.db';file.write_bytes(payload['app.db'])
            with sqlite3.connect(file) as c:c.executescript(sql)
            payload['app.db']=file.read_bytes()
        return self.zip(payload)

    def state(self):
        return {r[0]:self.c.execute('SELECT * FROM "'+r[0]+'"').fetchall() for r in self.c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('settings')")}

    def test_full_roundtrip_repeated_preserves_business_tables_and_pdf(self):
        cid=self.case('復元検証',self.old,2000000,self.today)
        self.call('r_haak_put',{'confirmed_amount_tax_in':3000000,'confirmed_checked':True,'actual_paid_amount':3000000,'paid_date':self.today},cid)
        self.draft(self.old)
        self.s.setset(self.c,'company_seal_png','original-seal-preserved')
        self.s.setset(self.c,'pdf_archive_dir','C:\\old-pc\\PDF')
        self.s.setset(self.c,'pw',self.s.hash_pw('source-password'));self.c.commit()
        root=self.root();(root/'documents').mkdir(exist_ok=True)
        (root/'documents/旧版.pdf').write_bytes(b'%PDF-original')
        raw=self.backup();before=self.state()
        for _ in range(2):
            self.case('置換で消える案件',self.new,40000,self.today)
            self.s.setset(self.c,'pw',self.s.hash_pw('destination-password'));self.c.commit()
            (root/'documents/remove.pdf').write_bytes(b'extra')
            result=self.restore(self.prepare(raw))
            self.assertEqual(before,self.state())
            self.assertEqual((root/'documents/旧版.pdf').read_bytes(),b'%PDF-original')
            self.assertFalse((root/'documents/remove.pdf').exists())
            self.assertTrue(self.s.check_pw('destination-password',self.s.getset(self.c,'pw')))
            self.assertEqual(self.s.getset(self.c,'pdf_archive_dir'),'')
            self.assertEqual(self.s.getset(self.c,'company_seal_png'),'original-seal-preserved')
            self.assertTrue(Path(result['backup']).is_file())
        # Counters continue; restored IDs are never appended a second time.
        self.s.setset(self.c,'company_seal_png','');self.c.commit()
        self.call('r_company_put',{'name':'復元検証会社'})
        new=self.case('復元後の新規',self.new,50000,self.today)
        self.assertGreater(new,cid)
        issued=self.s.r_doc_new(self.c,{'doc_type':'principal_invoice','sub_id':self.sid},{},str(new),session='after-restore')
        self.c.commit()
        self.assertTrue(self.call('r_doc_download',None,str(issued['id']))[1].startswith(b'%PDF-'))

    def test_verified_legacy_073_format_and_migration(self):
        cid=self.case('旧形式',self.old,2000000,self.today)
        raw=self.change_db(self.backup(),'PRAGMA user_version=13;')
        info=self.prepare(raw)
        self.assertEqual(info['schema'],13);self.assertIsNone(info['created_at'])
        self.assertTrue(info['warnings']);self.restore(info)
        self.assertEqual(self.c.execute('PRAGMA user_version').fetchone()[0],14)
        self.assertEqual(self.c.execute('SELECT name FROM cases WHERE id=?',(cid,)).fetchone()[0],'旧形式')

    def test_new_install_destination_password_and_company(self):
        self.call('r_company_put',{'name':'移行元会社'})
        raw=self.backup()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.multiple(self.s,DBP=str(Path(tmp)/'app.db'),FILES=str(Path(tmp)/'files'),DOCS=str(Path(tmp)/'documents')):
                self.s.init()
                with closing(self.s.db()) as dest:
                    self.s.setset(dest,'pw',self.s.hash_pw('new-install-password'));dest.commit()
                    info=br.prepare(raw,self.s.BASE,Path(tmp))
                    br.restore(dest,info['token'],tmp,self.s.FILES,self.s.DOCS,self.s.getset(dest,'pw'))
                    self.assertEqual(dest.execute('SELECT name FROM company').fetchone()[0],'移行元会社')
                    self.assertTrue(self.s.check_pw('new-install-password',self.s.getset(dest,'pw')))

    def test_corrupt_zip_and_hash_tampering_rejected(self):
        with self.assertRaises(ValueError):self.prepare(b'broken ZIP')
        raw=self.backup()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:p={n:z.read(n) for n in z.namelist()}
        p['files/extra.pdf']=b'%PDF-extra'
        with self.assertRaisesRegex(ValueError,'一覧'):self.prepare(self.zip(p))
        p.pop('files/extra.pdf');p['app.db']=b'bad'
        with self.assertRaisesRegex(ValueError,'検証値'):self.prepare(self.zip(p))

    def test_crc_corruption(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_STORED) as z:z.writestr('app.db',b'uniqueDBcontent')
        raw=stream.getvalue().replace(b'uniqueDBcontent',b'brokenDBconten')
        with self.assertRaises(ValueError):self.prepare(raw)

    def test_traversal_program_absolute_reserved_duplicate_and_symlink_rejected(self):
        base=self.payload(self.backup())
        for name in ('../server.py','server.py','files/../../outside','/app.db','files/C:bad.pdf','files/CON.pdf','files/a\\b.pdf','documents/nested/x.pdf'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.prepare(self.zip(dict(base,**{name:b'x'})))
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('app.db',base['app.db']);z.writestr('APP.DB',base['app.db'])
        with self.assertRaises(ValueError):self.prepare(stream.getvalue())
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('app.db',base['app.db'])
            i=zipfile.ZipInfo('files/link.pdf');i.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(i,b'/outside')
        with self.assertRaises(ValueError):self.prepare(stream.getvalue())

    def test_future_schema_and_version_rejected(self):
        with self.assertRaisesRegex(ValueError,'新しい'):self.prepare(self.change_db(self.backup(),'PRAGMA user_version=999;'))
        raw=self.backup()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:p={n:z.read(n) for n in z.namelist()}
        info=json.loads(p['backup-info.json']);info['version']='99.0.0';p['backup-info.json']=json.dumps(info).encode()
        with self.assertRaisesRegex(ValueError,'新しい'):self.prepare(self.zip(p))

    def test_fk_and_invalid_database_rejected(self):
        raw=self.change_db(self.backup(),"INSERT INTO purchases(case_id,supplier_id,amount_tax_in) VALUES(999999,99999,20);")
        with self.assertRaisesRegex(ValueError,'参照整合性'):self.prepare(raw)
        with self.assertRaises(sqlite3.DatabaseError):self.prepare(self.zip({'app.db':b'not SQLite'}))

    def test_missing_originals_reported_without_generation(self):
        cid=self.case('添付不足',self.old,2000000,self.today)
        self.c.execute("INSERT INTO attachments(case_id,kind,original_name,stored_name,mime,bytes,created_at) VALUES(?,'other','missing.pdf','missing.pdf','application/pdf',10,'2026-01-01')",(cid,));self.c.commit()
        info=self.prepare(self.backup())
        self.assertEqual(info['missing'],['files/missing.pdf']);result=self.restore(info)
        self.assertEqual(result['missing'],['files/missing.pdf'])
        self.assertFalse((self.root()/'files/missing.pdf').exists())

    def test_failure_mid_restore_and_startup_recovery(self):
        self.case('復元元',self.old,2000000,self.today);raw=self.backup()
        self.case('現在の案件',self.new,30000,self.today);before=self.state()
        root=self.root();(root/'files').mkdir(exist_ok=True);(root/'files/current.pdf').write_bytes(b'current')
        actual=br.load_db
        calls=[]
        def fail_once(source,c):
            calls.append(source)
            if len(calls)==1:raise OSError('simulated disk failure after file replacement')
            return actual(source,c)
        with patch.object(br,'load_db',side_effect=fail_once):
            with self.assertRaisesRegex(self.s.Err,'戻しました'):self.restore(self.prepare(raw))
        self.assertEqual(self.state(),before);self.assertEqual((root/'files/current.pdf').read_bytes(),b'current')
        # Simulate a process termination with the journal left behind.
        previous=root/'.restore-rollback';previous.mkdir()
        br.unpack(self.backup(),previous)
        (root/'.restore-journal').write_text('in progress')
        (root/'files/current.pdf').unlink()
        self.case('途中の変更',self.new,10000,self.today)
        self.assertTrue(br.recover(root,self.c));self.assertEqual(before,self.state())
        self.assertTrue((root/'files/current.pdf').exists());self.assertFalse((root/'.restore-journal').exists())

    def test_backup_failure_and_invalid_token_leave_data_unchanged(self):
        info=self.prepare(self.backup());before=self.state()
        with patch.object(br,'create',side_effect=OSError('full disk')):
            with self.assertRaises(self.s.Err):self.restore(info)
        self.assertEqual(before,self.state())
        with self.assertRaises(self.s.Err):self.restore(info)
        with self.assertRaises(self.s.Err):self.call('r_backup_restore',{'token':'fake'})

    def test_templates_lossless_and_installed_program_untouched(self):
        templates=self.root()/'templates';templates.mkdir()
        original={name:Path(path).read_bytes() for name,path in br.template_files(self.root(),self.s.BASE).items()}
        for name,raw in original.items():(templates/name).write_bytes(raw+b'\n% private fixed template')
        program=Path(self.s.BASE)/'server.py';before=program.read_bytes()
        raw=self.backup()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name,value in original.items():self.assertEqual(z.read('templates/'+name),value+b'\n% private fixed template')
        for name in original:(templates/name).write_bytes(b'changed')
        self.restore(self.prepare(raw))
        for name,value in original.items():self.assertEqual((templates/name).read_bytes(),value+b'\n% private fixed template')
        self.assertEqual(program.read_bytes(),before)

    def test_linked_original_is_not_backed_up(self):
        root=self.root();(root/'files').mkdir(exist_ok=True)
        (root/'files/link.pdf').symlink_to(root/'app.db')
        with self.assertRaisesRegex(ValueError,'リンク'):self.backup()


if __name__=='__main__':unittest.main()
