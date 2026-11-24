"""元金請求と実入金の分離。全て一時DBの架空データで実施する。"""
import datetime
import json
import unittest
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fitz
import funding_ledger as ledger
import test_workflow


class PrincipalInvoiceTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    case = test_workflow.WorkflowTests.case

    def prepare(self):
        self.call('r_company_put',dict(name='株式会社 サンプル'))
        self.c.execute('UPDATE subcontractors SET name=? WHERE id=?',('検証下請けA',self.sid))
        self.c.commit()
        source=self.case('検証用充当元案件',self.old,6000000,'2020-11-01')
        target=self.case('検証用商品仕入案件',self.new,4000000)
        self.c.execute('UPDATE cases SET case_no=? WHERE id=?',('SP91001',source))
        self.c.execute('UPDATE cases SET case_no=?,mgmt_no=? WHERE id=?',('SP91005','26-0002',target))
        self.c.commit()
        self.call('r_haak_put',dict(confirmed_amount_tax_in=6500000,confirmed_checked=True,paid_date=self.today),source)
        return source,target

    def consent(self,**kw):
        return dict(consent_date=self.today,consent_method='LINE',consent_record='承諾確認済み',**kw)

    def save(self,target,entries=(),**kw):
        return self.call('r_sub_put',dict(sub_id=self.sid,principal_tax_in=4000000,funding_method='mixed',allocations=list(entries),**kw),target,1)

    def allocate(self,source,target,amount=2000000,legacy=False,kind='principal'):
        body=self.consent(source_id=source,carry_over=amount)
        if not legacy:body.update(source_type=kind,request_key=uuid.uuid4().hex)
        self.save(target,[body])

    def payload(self,target,**kw):
        return self.s.document_payload(self.c,{'issued_date':self.today,**kw},target,'principal_invoice',self.sid)

    def issue(self,target,**kw):
        try:
            result=self.s.r_doc_new(self.c,dict(doc_type='principal_invoice',sub_id=self.sid,**kw),{},str(target),session='invoice-test')
            self.c.commit()
            return result['id']
        except Exception:
            self.c.rollback();raise

    def test_SP91005_legacy_confirmed_allocation_and_pdf(self):
        a,t=self.prepare();self.allocate(a,t,legacy=True)
        before='\n'.join(self.c.iterdump())
        d=self.payload(t)
        self.assertEqual(d['amount_tax_in'],2000000)
        self.assertEqual(d['principal'],4000000)
        self.assertEqual(self.call('r_funding_options',None,t,q={'sub_id':[str(self.sid)]})['invoice']['invoice_amount'],2000000)
        preview=self.s.r_doc_preview(self.c,dict(doc_type='principal_invoice',sub_id=self.sid),{},str(t),session='invoice-test');self.c.commit()
        pdf=self.s.load_doc_draft(preview['token'],'invoice-test')['pdf']
        self.assertEqual('\n'.join(self.c.iterdump()),before)
        with fitz.open(stream=pdf,filetype='pdf') as document:
            text=''.join(page.get_text() for page in document)
            self.assertIn('SP91001',text);self.assertIn('検証用充当元案件',text)
            self.assertIn('4,000,000',text);self.assertIn('2,000,000',text)
            self.assertIn('今回のお支払額',text);self.assertIn('御中',text)
        did=self.issue(t,preview_token=preview['token'])
        self.assertEqual(self.call('r_doc_download',None,did)[1],pdf)
        self.assertEqual(ledger.cash(self.c,t,self.sid),0)
        self.assertEqual(self.call('r_docs',None,t)[0]['unpaid_amount'],2000000)

    def test_no_allocation_full_principal(self):
        _,t=self.prepare();self.assertEqual(self.payload(t)['amount_tax_in'],4000000)

    def test_fully_allocated_zero_cannot_issue(self):
        a,t=self.prepare();self.allocate(a,t,4000000)
        self.assertEqual(self.payload(t)['amount_tax_in'],0)
        with self.assertRaisesRegex(self.s.Err,'0円'):self.issue(t)
        self.assertEqual(self.call('r_docs',None,t),[])

    def test_principal_and_temporary_hold_details(self):
        a,t=self.prepare()
        fund=ledger.create_hold(self.c,self.consent(amount=1000000,reason='一時預かり',occurred_on=self.today),{},str(a),'1')['id'];self.c.commit()
        self.allocate(a,t,1000000)
        self.allocate(fund,t,1000000,kind='hold')
        d=self.payload(t)
        self.assertEqual((d['allocated_principal'],d['allocated_hold'],d['amount_tax_in']),(1000000,1000000,2000000))
        with fitz.open(stream=self.s.documents.render('principal_invoice',d|{'doc_no':'PI-test'}),filetype='pdf') as pdf:
            self.assertIn('一時預かり金',''.join(p.get_text() for p in pdf))

    def test_multiple_sources(self):
        a,t=self.prepare();b=self.case('第2充当元',self.old,1000000,'2020-11-01')
        self.call('r_haak_put',dict(confirmed_amount_tax_in=1100000,confirmed_checked=True,paid_date=self.today),b)
        self.allocate(a,t,1300000);self.allocate(b,t,700000)
        d=self.payload(t);self.assertEqual(d['amount_tax_in'],2000000);self.assertEqual(len(d['allocations']),2)

    def test_cancel_recalculates_and_old_invoice_is_immutable(self):
        a,t=self.prepare();self.allocate(a,t)
        did=self.issue(t);old=self.call('r_doc_download',None,did)[1]
        event=ledger.active(self.c,'e.target_id=?',(t,))[0]
        ledger.cancel(self.c,self.consent(note='充当取消'),{},str(event['id']));self.c.commit()
        self.assertEqual(self.payload(t)['amount_tax_in'],4000000)
        self.assertEqual(self.call('r_doc_download',None,did)[1],old)
        self.assertEqual(self.call('r_docs',None,t)[0]['amount_tax_in'],2000000)
        self.call('r_doc_put',{'note':'備考のみ'},did)
        self.assertEqual(self.call('r_docs',None,t)[0]['amount_tax_in'],2000000)
        self.call('r_doc_put',{'recalculate_principal':True},did)
        self.assertEqual(self.call('r_docs',None,t)[0]['amount_tax_in'],4000000)
        history=self.call('r_doc_history',None,did)
        self.assertEqual(self.call('r_doc_old_download',None,history[-1]['id'])[1],old)

    def test_existing_unpaid_invoice_prevents_duplicate(self):
        a,t=self.prepare();self.allocate(a,t);self.issue(t)
        with self.assertRaisesRegex(self.s.Err,'二重発行'):self.issue(t)

    def test_partial_cash_changes_balance_only(self):
        a,t=self.prepare();self.allocate(a,t);did=self.issue(t)
        original=self.call('r_doc_download',None,did)[1]
        self.save(t,cash_entry=dict(amount=800000,received_on=self.today,method='bank',request_key=uuid.uuid4().hex))
        self.assertEqual(self.payload(t)['amount_tax_in'],2000000)
        doc=self.call('r_docs',None,t)[0]
        self.assertEqual((doc['amount_tax_in'],doc['cash_received'],doc['unpaid_amount']),(2000000,800000,1200000))
        self.assertEqual(self.call('r_doc_download',None,did)[1],original)
        with self.assertRaises(self.s.Err):self.issue(t)

    def test_future_confirmation_not_reflected_in_past_date(self):
        a,t=self.prepare();self.allocate(a,t)
        past=(datetime.date.today()-datetime.timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(self.s.Err,'承諾日'):
            self.payload(t,issued_date=past)
        self.assertEqual(self.payload(t)['amount_tax_in'],2000000)

    def test_backdated_legacy_cancellation_requires_review(self):
        a,t=self.prepare();self.allocate(a,t,legacy=True)
        past=(datetime.date.today()-datetime.timedelta(days=1)).isoformat()
        self.c.execute('UPDATE settlement_lines SET carry_confirmed_date=?,consent_date=?',(past,past));self.c.commit()
        line=self.c.execute('SELECT * FROM settlement_lines WHERE carry_to_case_id=?',(t,)).fetchone()
        self.call('r_carry',dict(carry_over=0,change_reason='後日の取消'),line['settlement_id'],line['id'])
        self.assertEqual(self.payload(t)['amount_tax_in'],4000000)
        with self.assertRaisesRegex(self.s.Err,'変更・取消'):
            self.payload(t,issued_date=past)

    def test_unapproved_legacy_not_deducted(self):
        a,t=self.prepare();self.allocate(a,t,legacy=True)
        self.c.execute('UPDATE settlement_lines SET consent_record=NULL');self.c.commit()
        self.assertEqual(self.payload(t)['amount_tax_in'],4000000)

    def test_over_allocation_visible_and_clamped(self):
        a,t=self.prepare();self.allocate(a,t)
        self.c.execute('UPDATE case_subs SET principal_tax_in=1000000 WHERE case_id=?',(t,));self.c.commit()
        d=self.payload(t);self.assertEqual((d['amount_tax_in'],d['excess']),(0,1000000))

    def test_preview_invalidated_by_allocation_change(self):
        a,t=self.prepare()
        preview=self.s.r_doc_preview(self.c,dict(doc_type='principal_invoice',sub_id=self.sid),{},str(t),session='invoice-test');self.c.commit()
        self.allocate(a,t)
        with self.assertRaisesRegex(self.s.Err,'更新'):self.issue(t,preview_token=preview['token'])

    def test_concurrent_issuance_only_one_succeeds(self):
        _,t=self.prepare()
        def issue(_):
            with closing(self.s.db()) as c:
                try:
                    self.s.r_doc_new(c,dict(doc_type='principal_invoice',sub_id=self.sid),{},str(t),session='concurrent')
                    c.commit();return True
                except self.s.Err:
                    c.rollback();return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(issue,range(2))),[False,True])

if __name__=='__main__':unittest.main()
