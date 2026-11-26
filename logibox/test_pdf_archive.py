"""Case evidence folders, consolidation, safe migration and original preservation."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import fitz
from PIL import Image
import test_workflow


def pdf_bytes(label='Test document'):
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((40,60),label)
        return doc.tobytes()


class CaseEvidenceTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    case = test_workflow.WorkflowTests.case
    draft = test_workflow.WorkflowTests.draft

    @property
    def root(self): return Path(self.s.DBP).parent/'LOGIBOX PDF'

    def stored(self, raw, issued=False, suffix='.pdf'):
        import attachments
        return attachments.store(self.s.DOCS if issued else self.s.FILES,raw,suffix)

    def attachment(self,cid,raw=None,kind='purchase_invoice',suffix='.pdf'):
        raw=raw if raw is not None else pdf_bytes('Imported invoice')
        stored=self.stored(raw,suffix=suffix)
        cursor=self.c.execute('''INSERT INTO attachments(case_id,kind,original_name,stored_name,mime,bytes,created_at)
             VALUES(?,?,?,?,?,?,?)''',(cid,kind,'original'+suffix,stored,'application/pdf' if suffix=='.pdf' else 'image/png',len(raw),self.today))
        self.c.commit();return cursor.lastrowid,stored

    def issued(self,cid,raw=None,number='HI-26-0001'):
        raw=raw if raw is not None else pdf_bytes('Issued invoice')
        stored=self.stored(raw,True)
        cursor=self.c.execute('''INSERT INTO issued_documents(doc_type,case_id,issued_date,amount_tax_in,snapshot_json,file)
             VALUES('haak_invoice',?,?,1000,?,?)''',(cid,self.today,json.dumps({'doc_no':number}),stored))
        self.c.commit();return cursor.lastrowid,stored

    def sync(self): return self.s.pdf_archive.sync(self.c,self.s.FILES,self.s.DOCS,self.root)

    def exported(self,key):
        state=json.loads((Path(self.s.DBP).parent/'pdf_export_manifest.json').read_text())
        return self.root/state['roots'][str(self.root.resolve())][key]['name']

    def case_dir(self,cid):
        row=self.c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone()
        return self.root/self.s.pdf_archive.case_folder(row)

    def all_files(self): return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_case_folders_index_and_bundle_are_searchable_and_repeatable(self):
        cid=self.case('証憑整理',self.old,10000)
        aid,attachment=self.attachment(cid);did,issued=self.issued(cid)
        self.assertEqual(self.sync(),4)
        folder=self.case_dir(cid)
        self.assertEqual(folder.parent.name,'2020年')
        self.assertIn('証憑整理',folder.name)
        self.assertIn('01_仕入',self.exported(f'attachment:{aid}').parts)
        self.assertIn('03_ハーク',self.exported(f'issued:{did}').parts)
        with fitz.open(folder/'案件証憑一式.pdf') as bundle:
            text=''.join(p.get_text() for p in bundle)
            self.assertIn('案件証憑一覧',text);self.assertIn('証憑整理',text)
            self.assertIn('Imported invoice',text);self.assertIn('Issued invoice',text)
            self.assertEqual(bundle.page_count,3);self.assertEqual(len(bundle.get_toc()),3)
        before=self.all_files();self.assertEqual(self.sync(),0);self.assertEqual(before,self.all_files())
        self.assertEqual(Path(self.s.FILES,attachment).read_bytes(),pdf_bytes_from(self.exported(f'attachment:{aid}')))
        self.assertTrue(Path(self.s.DOCS,issued).is_file())

    def test_flat_v069_migration_backs_up_known_copies_only(self):
        cid=self.case('旧版移行',self.old,10000);did,stored=self.issued(cid)
        self.root.mkdir(exist_ok=True)
        old=self.root/'HI-26-0001.pdf';raw=Path(self.s.DOCS,stored).read_bytes();old.write_bytes(raw)
        manual=self.root/'手動で保存.pdf';manual.write_bytes(b'user')
        empty=self.root/'利用者の空フォルダ';empty.mkdir()
        state={'version':1,'roots':{str(self.root.resolve()):{f'issued:{did}':{'name':old.name,'digest':hashlib.sha256(raw).hexdigest()}}}}
        (Path(self.s.DBP).parent/'pdf_export_manifest.json').write_text(json.dumps(state))
        self.sync();self.assertFalse(old.exists());self.assertEqual(manual.read_bytes(),b'user');self.assertTrue(empty.exists())
        backup,=Path(self.s.DBP).parent.glob('before-pdf-folder-*.zip')
        with zipfile.ZipFile(backup) as archive:self.assertEqual(archive.read(old.name),raw)

    def test_legacy_nested_copies_migrate_but_manual_files_stay(self):
        cid=self.case('旧構成',self.old,10000);_,stored=self.attachment(cid)
        raw=Path(self.s.FILES,stored).read_bytes()
        prefix=self.s.pdf_archive.case_name(self.c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone())
        folder=self.root/prefix/'読込PDF';folder.mkdir(parents=True)
        old=folder/(prefix+'_旧名_'+hashlib.sha256(raw).hexdigest()[:12]+'.pdf');old.write_bytes(raw)
        manual=folder/'手動.pdf';manual.write_bytes(b'user')
        self.sync();self.assertFalse(old.exists());self.assertEqual(manual.read_bytes(),b'user')

    def test_reissue_replaces_export_bundle_and_keeps_old_original(self):
        cid=self.case('再発行',self.old,10000);did,old=self.issued(cid)
        self.sync();target=self.exported(f'issued:{did}');before=self.exported(f'case:{cid}:1').read_bytes()
        new=self.stored(pdf_bytes('New revision'),True)
        self.c.execute('UPDATE issued_documents SET file=?,revision=2 WHERE id=?',(new,did));self.c.commit();self.sync()
        self.assertEqual(self.exported(f'issued:{did}'),target)
        self.assertNotEqual(self.exported(f'case:{cid}:1').read_bytes(),before)
        self.assertTrue(Path(self.s.DOCS,old).is_file())
        self.assertTrue(list(Path(self.s.DBP).parent.glob('before-pdf-folder-*.zip')))

    def test_user_modified_export_and_bundle_are_preserved(self):
        cid=self.case('手動保護',self.old,10000);did,_=self.issued(cid);self.sync()
        individual=self.exported(f'issued:{did}');bundle=self.exported(f'case:{cid}:1')
        individual.write_bytes(b'user invoice');bundle.write_bytes(b'user bundle')
        self.sync();self.assertEqual(individual.read_bytes(),b'user invoice');self.assertEqual(bundle.read_bytes(),b'user bundle')
        self.assertNotEqual(self.exported(f'issued:{did}'),individual);self.assertNotEqual(self.exported(f'case:{cid}:1'),bundle)
        self.assertEqual(self.sync(),0)

    def test_same_name_manual_file_uses_alternate_name(self):
        cid=self.case('同名保護',self.old,10000);did,_=self.issued(cid)
        folder=self.case_dir(cid)/'03_ハーク';folder.mkdir(parents=True)
        target=folder/'HI-26-0001_ハーク請求書_HI-26-0001.pdf';target.write_bytes(b'user')
        self.sync();self.assertEqual(target.read_bytes(),b'user');self.assertNotEqual(target,self.exported(f'issued:{did}'))

    def test_case_rename_moves_managed_copies_and_keeps_manual_file(self):
        cid=self.case('変更前',self.old,10000);self.attachment(cid);self.sync()
        old=self.case_dir(cid);manual=old/'個人メモ.txt';manual.write_text('keep')
        self.c.execute('UPDATE cases SET name=?,due_date=? WHERE id=?',('変更後','2021-01-20',cid));self.c.commit();self.sync()
        self.assertTrue(self.case_dir(cid).joinpath('案件証憑一式.pdf').exists())
        self.assertEqual(manual.read_text(),'keep');self.assertFalse((old/'案件証憑一式.pdf').exists())

    def test_duplicate_filenames_do_not_overwrite_evidence(self):
        cid=self.case('同名資料',self.old,10000)
        first,_=self.attachment(cid,pdf_bytes('First'));second,_=self.attachment(cid,pdf_bytes('Second'))
        self.sync();self.assertNotEqual(self.exported(f'attachment:{first}'),self.exported(f'attachment:{second}'))
        with fitz.open(self.exported(f'case:{cid}:1')) as bundle:
            self.assertEqual(bundle.page_count,3)

    def test_monthly_invoice_saved_once_and_referenced_by_both_cases(self):
        ids=[self.case('案件'+name,self.old,10000) for name in ('A','B')]
        settlement=self.draft(self.old)
        for label in ('Old monthly','Current monthly'):
            stored=self.stored(pdf_bytes(label),True)
            self.c.execute('INSERT INTO sub_invoices(settlement_id,received_date,amount_tax_in,file) VALUES(?,?,1000,?)',(settlement['id'],self.today,stored))
        self.c.commit();self.sync()
        original=self.exported(f"received:{settlement['id']}")
        self.assertEqual(original.parent,self.root/'月次精算'/self.old)
        self.assertEqual(len(list((self.root/'月次精算').rglob('*.pdf'))),1)
        for cid in ids:
            with fitz.open(self.exported(f'case:{cid}:1')) as bundle:
                text=''.join(p.get_text() for p in bundle)
                self.assertIn('参照のみ',text);self.assertIn('月次精算',text)
                self.assertNotIn('Current monthly',text)

    def test_bad_pdf_is_copied_and_reported_without_losing_good_documents(self):
        cid=self.case('破損資料',self.old,10000);aid,_=self.attachment(cid,b'%PDF-bad');self.issued(cid)
        self.sync();self.assertEqual(self.exported(f'attachment:{aid}').read_bytes(),b'%PDF-bad')
        with fitz.open(self.exported(f'case:{cid}:1')) as bundle:
            self.assertIn('結合不可',''.join(p.get_text() for p in bundle));self.assertEqual(bundle.page_count,2)

    def test_encrypted_pdf_keeps_original_and_is_reported(self):
        cid=self.case('暗号化資料',self.old,10000)
        with fitz.open(stream=pdf_bytes(),filetype='pdf') as source:
            raw=source.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,owner_pw='owner',user_pw='reader')
        aid,_=self.attachment(cid,raw);self.sync()
        self.assertEqual(self.exported(f'attachment:{aid}').read_bytes(),raw)
        with fitz.open(self.exported(f'case:{cid}:1')) as bundle:self.assertEqual(bundle.page_count,1)

    def test_legacy_image_is_preserved_and_converted_in_bundle(self):
        cid=self.case('画像資料',self.old,10000);out=io.BytesIO();Image.new('RGB',(80,80),'white').save(out,format='PNG')
        aid,_=self.attachment(cid,out.getvalue(),suffix='.png');self.sync()
        self.assertEqual(self.exported(f'attachment:{aid}').suffix,'.png')
        with fitz.open(self.exported(f'case:{cid}:1')) as bundle:self.assertEqual(bundle.page_count,2)

    def test_empty_case_and_unissued_previews(self):
        cid=self.case('未発行',self.old,10000);self.stored(pdf_bytes('Unissued preview'),True);self.sync()
        with fitz.open(self.exported(f'case:{cid}:1')) as bundle:
            text=''.join(p.get_text() for p in bundle)
            self.assertIn('証憑未登録',text);self.assertNotIn('Unissued preview',text)

    def test_missing_original_does_not_remove_or_update_exports(self):
        cid=self.case('原本不足',self.old,10000);did,stored=self.issued(cid);self.sync();before=self.all_files()
        Path(self.s.DOCS,stored).unlink()
        with self.assertRaisesRegex(ValueError,'原本が見つかりません'):self.sync()
        self.assertEqual(before,self.all_files())

    def test_backup_failure_leaves_exports_unchanged(self):
        cid=self.case('保全',self.old,10000);did,_=self.issued(cid);self.sync();before=self.all_files()
        new=self.stored(pdf_bytes('Changed'),True);self.c.execute('UPDATE issued_documents SET file=? WHERE id=?',(new,did));self.c.commit()
        with patch.object(self.s.pdf_archive.zipfile,'ZipFile',side_effect=OSError('backup failure')):
            with self.assertRaises(OSError):self.sync()
        self.assertEqual(before,self.all_files())

    def test_root_overlapping_originals_is_rejected(self):
        for root in (self.s.FILES,self.s.DOCS,str(Path(self.s.DBP).parent)):
            with self.assertRaisesRegex(ValueError,'重ならない'):
                self.s.pdf_archive.sync(self.c,self.s.FILES,self.s.DOCS,root)

    def test_symlinked_case_folder_is_rejected(self):
        cid=self.case('リンク保護',self.old,10000)
        with tempfile.TemporaryDirectory() as outside:
            folder=self.case_dir(cid);folder.parent.mkdir(parents=True,exist_ok=True);folder.symlink_to(outside,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'リンク'):self.sync()
            self.assertEqual(list(Path(outside).iterdir()),[])

    def test_untrusted_manifest_path_cannot_escape_root(self):
        cid=self.case('管理情報保護',self.old,10000)
        path=Path(self.s.DBP).parent/'pdf_export_manifest.json'
        path.write_text(json.dumps({'version':1,'roots':{str(self.root.resolve()):{'bad':{'name':'../outside.pdf','digest':'x'}}}}))
        with self.assertRaisesRegex(ValueError,'管理ファイル名'):self.sync()

    def test_download_bundle_api_matches_export(self):
        cid=self.case('ダウンロード',self.old,10000);self.attachment(cid)
        response=self.call('r_evidence_bundle',None,cid)
        self.assertEqual(response[3],'application/pdf');self.assertEqual(response[1],self.exported(f'case:{cid}:1').read_bytes())

    def test_many_documents_have_paginated_index_and_bookmarks(self):
        cid=self.case('多数書類',self.old,10000)
        for i in range(45):self.attachment(cid,pdf_bytes('Document '+str(i)))
        self.sync()
        with fitz.open(self.exported(f'case:{cid}:0')) as index:pages=index.page_count
        with fitz.open(self.exported(f'case:{cid}:1')) as bundle:
            self.assertGreater(pages,1);self.assertEqual(bundle.page_count,pages+45)
            self.assertEqual(bundle.get_toc()[1][2],pages+1)


def pdf_bytes_from(path):return path.read_bytes()

if __name__=='__main__':unittest.main()
