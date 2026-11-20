"""PDF副本の単純保存、旧形式の移行、原本・手動ファイルの保護。"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import test_workflow


class SimplePdfExportTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    case = test_workflow.WorkflowTests.case
    draft = test_workflow.WorkflowTests.draft

    @property
    def root(self):
        return Path(self.s.DBP).parent / 'LOGIBOX PDF'

    def stored(self, raw, issued=False):
        import attachments
        return attachments.store(self.s.DOCS if issued else self.s.FILES, raw, '.pdf')

    def attachment(self, cid, raw=b'%PDF-imported', kind='purchase_invoice'):
        stored = self.stored(raw)
        cur = self.c.execute('''INSERT INTO attachments(case_id,kind,original_name,stored_name,mime,bytes,created_at)
                                VALUES(?,?,?,?,?,?,?)''', (cid,kind,'original.pdf',stored,'application/pdf',len(raw),self.today))
        self.c.commit()
        return cur.lastrowid, stored

    def issued(self, cid, raw=b'%PDF-issued', number='HI-26-0001'):
        stored = self.stored(raw, True)
        cur = self.c.execute('''INSERT INTO issued_documents(doc_type,case_id,issued_date,amount_tax_in,snapshot_json,file)
                                VALUES('haak_invoice',?,?,1000,?,?)''', (cid,self.today,json.dumps({'doc_no':number}),stored))
        self.c.commit()
        return cur.lastrowid, stored

    def sync(self):
        return self.s.pdf_archive.sync(self.c, self.s.FILES, self.s.DOCS, self.root)

    def test_current_pdfs_are_flat_with_readable_and_official_names(self):
        cid = self.case('検証用', self.old, 10000)
        aid, stored = self.attachment(cid)
        did, issued = self.issued(cid)
        self.assertEqual(self.sync(), 2)
        paths = list(self.root.iterdir())
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.is_file() and path.suffix=='.pdf' for path in paths))
        self.assertTrue(any(path.name.endswith('_仕入請求書.pdf') for path in paths))
        self.assertEqual((self.root/'HI-26-0001.pdf').read_bytes(), b'%PDF-issued')
        self.assertEqual(self.sync(), 0)
        self.assertEqual(len(list(self.root.glob('*.pdf'))), 2)
        self.assertEqual(Path(self.s.FILES,stored).read_bytes(), b'%PDF-imported')
        self.assertEqual(Path(self.s.DOCS,issued).read_bytes(), b'%PDF-issued')

    def test_migration_backs_up_only_known_legacy_copies_and_preserves_manual_files(self):
        cid = self.case('旧形式移行', self.old, 10000)
        self.attachment(cid)
        self.issued(cid)
        case = dict(self.c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone())
        prefix = self.s.pdf_archive.case_name(case)
        for kind, raw in [('読込PDF',b'%PDF-imported'),('発行PDF',b'%PDF-issued')]:
            folder = self.root/prefix/kind;folder.mkdir(parents=True,exist_ok=True)
            for label in ('旧ラベル','別ラベル'):
                name = prefix+'_'+label+'_'+hashlib.sha256(raw).hexdigest()[:12]+'.pdf'
                (folder/name).write_bytes(raw)
        manual = self.root/'手動で保存.pdf';manual.write_bytes(b'%PDF-user')
        empty = self.root/'利用者の空フォルダ';empty.mkdir()
        self.sync()
        self.assertFalse((self.root/prefix).exists())
        self.assertEqual(manual.read_bytes(), b'%PDF-user')
        self.assertTrue(empty.is_dir())
        backup, = Path(self.s.DBP).parent.glob('before-pdf-folder-*.zip')
        with zipfile.ZipFile(backup) as archive:
            self.assertEqual(len(archive.namelist()),4)
            self.assertEqual({archive.read(name) for name in archive.namelist()}, {b'%PDF-imported',b'%PDF-issued'})
        self.sync()
        self.assertEqual(len(list(Path(self.s.DBP).parent.glob('before-pdf-folder-*.zip'))),1)

    def test_formal_reissue_updates_one_export_and_retains_old_original(self):
        cid = self.case('再発行', self.old, 10000)
        did, original = self.issued(cid)
        self.sync()
        new = self.stored(b'%PDF-new-revision',True)
        self.c.execute('''INSERT INTO issued_document_history(doc_id,revision,saved_at,amount_tax_in,snapshot_json,file)
                          VALUES(?,1,?,1000,?,?)''',(did,self.today,json.dumps({'doc_no':'HI-26-0001'}),original))
        self.c.execute('UPDATE issued_documents SET file=?,revision=2 WHERE id=?',(new,did));self.c.commit()
        self.sync()
        self.assertEqual([p.name for p in self.root.glob('*.pdf')],['HI-26-0001.pdf'])
        self.assertEqual((self.root/'HI-26-0001.pdf').read_bytes(),b'%PDF-new-revision')
        self.assertEqual(Path(self.s.DOCS,original).read_bytes(),b'%PDF-issued')
        backup, = Path(self.s.DBP).parent.glob('before-pdf-folder-*.zip')
        with zipfile.ZipFile(backup) as archive:self.assertEqual(archive.read('HI-26-0001.pdf'),b'%PDF-issued')

    def test_same_name_manual_file_is_never_overwritten(self):
        cid = self.case('同名保護', self.old, 10000)
        self.issued(cid)
        self.root.mkdir(exist_ok=True)
        (self.root/'HI-26-0001.pdf').write_bytes(b'%PDF-manual')
        self.sync()
        self.assertEqual((self.root/'HI-26-0001.pdf').read_bytes(),b'%PDF-manual')
        self.assertEqual((self.root/'HI-26-0001_2.pdf').read_bytes(),b'%PDF-issued')
        self.sync()
        self.assertEqual(len(list(self.root.glob('*.pdf'))),2)

    def test_user_modified_export_is_preserved_on_next_sync(self):
        cid = self.case('手動変更', self.old, 10000)
        self.issued(cid)
        self.sync()
        (self.root/'HI-26-0001.pdf').write_bytes(b'%PDF-edited-by-user')
        self.sync()
        self.assertEqual((self.root/'HI-26-0001.pdf').read_bytes(),b'%PDF-edited-by-user')
        self.assertEqual((self.root/'HI-26-0001_2.pdf').read_bytes(),b'%PDF-issued')

    def test_multiple_imports_use_short_serial_numbers(self):
        cid = self.case('複数仕入',self.old,10000)
        self.attachment(cid,b'%PDF-first')
        self.attachment(cid,b'%PDF-second')
        self.sync()
        names=sorted(p.name for p in self.root.glob('*.pdf'))
        self.assertEqual(len(names),2)
        self.assertTrue(any(name.endswith('_仕入請求書_2.pdf') for name in names))
        self.assertEqual(self.sync(),0)

    def test_latest_sub_invoice_is_exported_once_for_multiple_cases(self):
        self.case('案件A',self.old,10000)
        self.case('案件B',self.old,10000)
        settlement=self.draft(self.old)
        stored=[]
        for raw in (b'%PDF-received-old',b'%PDF-received-current'):
            name=self.stored(raw,True);stored.append(name)
            self.c.execute('INSERT INTO sub_invoices(settlement_id,received_date,amount_tax_in,file) VALUES(?,?,1000,?)',
                           (settlement['id'],self.today,name))
        self.c.commit();self.sync()
        pdfs=list(self.root.glob('*.pdf'))
        self.assertEqual(len(pdfs),1)
        self.assertEqual(pdfs[0].name,'2020-11_下請けA_受領請求書.pdf')
        self.assertEqual(pdfs[0].read_bytes(),b'%PDF-received-current')
        self.assertTrue(all(Path(self.s.DOCS,name).is_file() for name in stored))

    def test_missing_current_original_does_not_remove_existing_exports(self):
        cid=self.case('原本不足',self.old,10000)
        self.attachment(cid);did,stored=self.issued(cid)
        self.sync();before={p.name:p.read_bytes() for p in self.root.glob('*.pdf')}
        Path(self.s.DOCS,stored).unlink()
        with self.assertRaisesRegex(ValueError,'原本が見つかりません'):self.sync()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.glob('*.pdf')})

    def test_original_folders_cannot_be_used_as_copy_root(self):
        for root in (self.s.FILES,self.s.DOCS,str(Path(self.s.DBP).parent)):
            with self.assertRaisesRegex(ValueError,'重ならない'):
                self.s.pdf_archive.sync(self.c,self.s.FILES,self.s.DOCS,root)

    def test_unissued_preview_file_is_not_exported(self):
        self.stored(b'%PDF-preview-only',True)
        self.assertEqual(self.sync(),0)
        self.assertFalse(list(self.root.glob('*.pdf')))

    def test_backup_failure_leaves_legacy_files_intact(self):
        cid=self.case('バックアップ失敗',self.old,10000)
        self.attachment(cid)
        prefix=self.s.pdf_archive.case_name(self.c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone())
        folder=self.root/prefix/'読込PDF';folder.mkdir(parents=True,exist_ok=True)
        legacy=folder/(prefix+'_添付1_'+hashlib.sha256(b'%PDF-imported').hexdigest()[:12]+'.pdf')
        legacy.write_bytes(b'%PDF-imported')
        with patch.object(self.s.pdf_archive.zipfile,'ZipFile',side_effect=OSError('backup failure')):
            with self.assertRaises(OSError):self.sync()
        self.assertEqual(legacy.read_bytes(),b'%PDF-imported')
        self.assertFalse(list(self.root.glob('*.pdf')))


if __name__=='__main__':unittest.main()
