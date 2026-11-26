"""Invoice bank selection uses temporary databases and fictional accounts."""
import json
import unittest
import fitz
import test_principal_invoice


class BankAccountTests(unittest.TestCase):
    setUp = test_principal_invoice.PrincipalInvoiceTests.setUp
    call = test_principal_invoice.PrincipalInvoiceTests.call
    case = test_principal_invoice.PrincipalInvoiceTests.case
    prepare = test_principal_invoice.PrincipalInvoiceTests.prepare
    issue = test_principal_invoice.PrincipalInvoiceTests.issue

    def accounts(self, suffix=''):
        self.call('r_company_put',dict(name='株式会社 サンプル',bank_branch='標準銀行 本店',
            bank_account='普通 1111111',bank_holder='テスト',extra_bank_accounts=[
                dict(label='入金用',bank_branch='第二銀行 支店'+suffix,bank_account='普通 2222222',bank_holder='テスト'),
                dict(label='事業用',bank_branch='第三銀行 支店',bank_account='普通 3333333',bank_holder='テスト')]))

    def test_selection_pdf_and_immutable_reissue(self):
        _,cid=self.prepare();self.accounts()
        did=self.issue(cid,bank_account_id='2')
        row=self.s.issued_document(self.c,did);saved=json.loads(row['snapshot_json'])
        self.assertEqual(saved['issuer_bank_account'],'普通 2222222')
        old=self.call('r_doc_download',None,did)[1]
        with fitz.open(stream=old,filetype='pdf') as pdf:
            text=''.join(p.get_text() for p in pdf)
            self.assertIn('2222222',text);self.assertNotIn('3333333',text)
        self.accounts('変更後')
        self.call('r_doc_put',{'note':'備考のみ変更'},did)
        saved=json.loads(self.s.issued_document(self.c,did)['snapshot_json'])
        self.assertEqual(saved['issuer_bank_branch'],'第二銀行 支店')
        self.call('r_doc_put',{'bank_account_id':'3'},did)
        saved=json.loads(self.s.issued_document(self.c,did)['snapshot_json'])
        self.assertEqual(saved['issuer_bank_account'],'普通 3333333')
        history=self.call('r_doc_history',None,did)
        self.assertEqual(self.call('r_doc_old_download',None,history[-1]['id'])[1],old)

    def test_legacy_default_and_independent_invoices(self):
        _,cid=self.prepare();self.accounts()
        did=self.issue(cid)
        other=self.case('別案件',self.new,100000)
        second=self.issue(other,bank_account_id='3')
        self.assertEqual(json.loads(self.s.issued_document(self.c,did)['snapshot_json'])['bank_account_id'],'1')
        self.assertEqual(json.loads(self.s.issued_document(self.c,second)['snapshot_json'])['bank_account_id'],'3')
        master=self.call('r_master')['company']['bank_accounts']
        self.assertEqual([a['id'] for a in master],['1','2','3'])
        self.call('r_company_put',dict(name='株式会社 サンプル'))
        self.assertEqual(len(self.call('r_master')['company']['bank_accounts']),3)

    def test_third_slot_survives_empty_second_slot_and_saved_invoice_survives_removal(self):
        _,cid=self.prepare();self.accounts();did=self.issue(cid,bank_account_id='3')
        self.call('r_company_put',dict(name='株式会社 サンプル',extra_bank_accounts=[{},
            dict(bank_branch='第三銀行 支店',bank_account='普通 3333333',bank_holder='テスト')]))
        self.assertEqual([a['id'] for a in self.call('r_master')['company']['bank_accounts']],['1','3'])
        self.call('r_company_put',dict(name='株式会社 サンプル',extra_bank_accounts=[]))
        self.call('r_doc_put',{'bank_account_id':'keep','note':'設定削除後'},did)
        self.assertEqual(json.loads(self.s.issued_document(self.c,did)['snapshot_json'])['issuer_bank_account'],'普通 3333333')

    def test_haak_pdf_uses_selected_account(self):
        _,cid=self.prepare();self.accounts()
        self.call('r_haak_put',dict(confirmed_amount_tax_in=100000,confirmed_checked=True),cid)
        payload=self.s.document_payload(self.c,{'bank_account_id':'2'},cid,'haak_invoice')
        with fitz.open(stream=self.s.documents.render('haak_invoice',payload|{'doc_no':'HI-TEST'}),filetype='pdf') as pdf:
            text=''.join(p.get_text() for p in pdf)
            self.assertIn('2222222',text);self.assertNotIn('3333333',text)

    def test_validation_and_atomic_company_save(self):
        self.accounts()
        with self.assertRaises(self.s.Err):
            self.call('r_company_put',dict(name='変更',extra_bank_accounts=[dict(bank_branch='不完全')]))
        self.assertEqual(self.call('r_master')['company']['name'],'株式会社 サンプル')
        _,cid=self.prepare()
        for account_id in ('99',{},[]):
            with self.assertRaises(self.s.Err): self.issue(cid,bank_account_id=account_id or '99')

    def test_preview_rejects_bank_setting_changes(self):
        _,cid=self.prepare();self.accounts()
        preview=self.s.r_doc_preview(self.c,dict(doc_type='principal_invoice',sub_id=self.sid,bank_account_id='2'),{},str(cid),session='invoice-test')
        self.c.commit();self.accounts('変更後')
        with self.assertRaisesRegex(self.s.Err,'更新'):
            self.issue(cid,preview_token=preview['token'])

    def test_legacy_edit_keeps_original_bank(self):
        _,cid=self.prepare();self.accounts();did=self.issue(cid)
        saved=json.loads(self.s.issued_document(self.c,did)['snapshot_json'])
        del saved['bank_account_id']
        self.c.execute('UPDATE issued_documents SET snapshot_json=? WHERE id=?',(json.dumps(saved),did));self.c.commit()
        self.call('r_company_put',dict(name='株式会社 サンプル',bank_branch='変更後',bank_account='普通 9999999',bank_holder='別名義'))
        self.call('r_doc_put',{'note':'旧版の編集'},did)
        self.assertEqual(json.loads(self.s.issued_document(self.c,did)['snapshot_json'])['issuer_bank_account'],'普通 1111111')

if __name__=='__main__':unittest.main()
