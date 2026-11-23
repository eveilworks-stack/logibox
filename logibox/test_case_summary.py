"""Saved case summaries: evidence, legacy compatibility and duplicate prevention."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import datetime
import unittest

import case_summary
import funding_ledger
import test_funding_registration


class CaseSummaryTests(unittest.TestCase):
    def view(self, cid):
        return self.call('r_case_get', None, cid)

    def test_payment_requires_both_actual_amount_and_past_date(self):
        today = datetime.date.today()
        for amount, paid, date, expected in [
            (100,100,today.isoformat(),True), (100,99,today.isoformat(),False),
            (100,100,None,False), (100,0,today.isoformat(),False),
            (None,100,today.isoformat(),False), (0,0,today.isoformat(),False),
            (100,100,'2999-01-01',False), (100,100,'2026-02-30',False), (100,101,today.isoformat(),False)]:
            with self.subTest(amount=amount,paid=paid,date=date):
                self.assertEqual(case_summary.payment(amount,paid,date,today)['complete'],expected)

    def test_purchase_amount_without_date_is_not_paid(self):
        cid=self.case('仕入確認',self.old,100000)
        self.call('r_purchase_new',dict(supplier_id=1,amount_tax_in=100000,paid_amount=100000),cid)
        v=self.view(cid)
        self.assertFalse(v['progress']['purchase']['complete'])
        self.assertEqual(v['purchases'][0]['progress']['confirmed'],0)
        self.assertIn('要確認',v['purchases'][0]['progress']['label'])

    def test_mixed_hand_cash_and_allocation_survive_reopen_without_changes(self):
        source,target=self.setup_sp()
        cash=self.cash_entry(1000000,confirmed=True,method='hand',receipt_checked=True,stamp_checked=True)
        self.save_sp(target,[self.entry(source,2000000)],cash)
        before=self.c.execute('SELECT count(*) FROM funding_events').fetchone()[0]
        v=self.view(target);sub=v['subs'][0];p=sub['progress']
        self.assertTrue(p['complete'])
        self.assertEqual((p['allocated'],p['cash'],p['remaining']),(2000000,1000000,0))
        self.assertEqual(sub['funding']['cash_history'][0]['method'],'hand')
        self.assertEqual(sub['funding']['ledger_incoming'][0]['case_no'],'SP91001')
        with closing(self.s.db()) as conn:
            self.assertEqual(self.s.r_case_get(conn,{}, {},str(target))['subs'][0]['progress'],p)
        self.assertEqual(self.c.execute('SELECT count(*) FROM funding_events').fetchone()[0],before)
        self.assertEqual(self.c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_cash_plan_is_not_completed_until_actual_confirmation(self):
        source,target=self.setup_sp()
        self.save_sp(target,[self.entry(source,2000000)],self.cash_entry(1000000,confirmed=False))
        p=self.view(target)['subs'][0]['progress']
        self.assertFalse(p['complete']);self.assertEqual(p['remaining'],1000000)
        plan=self.options(target)['cash_plans'][0]
        self.action(funding_ledger.cash_plan_action,dict(action='confirmed',confirmed=True,
            received_on=self.today,request_key='summary-confirm'),plan['id'])
        self.assertTrue(self.view(target)['subs'][0]['progress']['complete'])

    def test_cancelled_allocation_stops_counting_and_preserves_history(self):
        source,target=self.setup_sp()
        self.save_sp(target,[self.entry(source,2000000)])
        event=self.options(target)['ledger_incoming'][0]
        self.action(funding_ledger.cancel,self.consent(note='充当取消'),event['id'])
        sub=self.view(target)['subs'][0]
        self.assertEqual(sub['progress']['allocated'],0)
        self.assertTrue(any(x['cancelled'] for x in sub['funding']['ledger_history']))

    def test_legacy_cash_is_compatible_without_inventing_payment_method(self):
        cid=self.case('旧現金記録',self.old,100000,'2020-11-01')
        sub=self.view(cid)['subs'][0]
        self.assertTrue(sub['progress']['complete'])
        self.assertEqual(sub['progress']['legacy_cash'],100000)
        self.assertEqual(sub['funding']['cash_history'],[])

    def test_legacy_carry_waits_for_real_settlement_payment_date(self):
        a,b,t=self.setup_cases()
        self.save(t,[self.consent(source_id=a,carry_over=100000)])
        sub=self.view(t)['subs'][0]
        self.assertEqual(sub['progress']['allocated'],0)
        self.c.execute("UPDATE settlements SET status='paid',paid_at=? WHERE id=?",(self.today,sub['funding']['incoming'][0]['settlement_id']))
        self.c.commit()
        self.assertEqual(self.view(t)['subs'][0]['progress']['allocated'],100000)

    def test_haak_missing_partial_future_and_legacy_full(self):
        cid=self.case('ハーク確認',self.old,100000)
        for body,complete in [
            (dict(paid_date=self.today,confirmed_checked=True),False),
            (dict(confirmed_amount_tax_in=100,paid_date=self.today),False),
            (dict(confirmed_amount_tax_in=100,confirmed_checked=True,actual_paid_amount=99,paid_date=self.today),False),
            (dict(confirmed_amount_tax_in=100,confirmed_checked=True,actual_paid_amount=100,paid_date='2999-01-01'),False),
            (dict(confirmed_amount_tax_in=100,confirmed_checked=True,actual_paid_amount=None,paid_date=self.today),True)]:
            with self.subTest(body=body):
                self.call('r_haak_put',body,cid)
                view=self.view(cid)
                self.assertEqual(view['haak_progress']['complete'],complete)
                self.assertEqual(self.s.case_progress(view)=='入金済',complete)

    def test_settlement_paid_flag_requires_real_payment_date(self):
        cid=self.case('精算支払確認',self.old,100000,'2020-11-01')
        self.draft(self.old)
        for date,complete in [(None,False),('2999-01-01',False),(self.today,True)]:
            self.c.execute("UPDATE settlements SET status='paid',paid_at=?",(date,));self.c.commit()
            self.assertEqual(self.view(cid)['settlement_history'][0]['progress']['complete'],complete)

    def test_new_editor_requires_actual_amount_without_breaking_legacy_api(self):
        cid=self.case('実入金額必須',self.old,100000)
        body=dict(confirmed_amount_tax_in=100000,confirmed_checked=True,paid_date=self.today,
                  require_actual_paid_amount=True)
        for amount in ('',None,0):
            with self.assertRaises(self.s.Err):
                self.call('r_haak_put',dict(body,actual_paid_amount=amount),cid)
        self.assertFalse(self.view(cid)['haak_progress']['complete'])
        self.call('r_haak_put',dict(body,actual_paid_amount=100000),cid)
        self.assertTrue(self.view(cid)['haak_progress']['complete'])

    def issue_context(self):
        cid=self.case('発行確認',self.old,100000)
        self.c.execute("UPDATE company SET name='検証会社' WHERE id=1");self.c.commit()
        self.call('r_haak_put',dict(confirmed_amount_tax_in=107000,confirmed_checked=True,paid_date=self.today),cid)
        return cid

    def test_invoice_sync_reissue_and_duplicate_guard(self):
        cid=self.issue_context()
        issued=self.s.r_doc_new(self.c,dict(doc_type='haak_invoice'),{},str(cid),session='test');self.c.commit()
        v=self.view(cid);docs=self.call('r_docs',None,cid)
        self.assertEqual(v['haak_progress']['invoice_ids'],[issued['id']])
        self.assertTrue(v['documents'][0]['progress']['complete'])
        self.assertEqual(v['documents'][0]['snapshot'],docs[0]['snapshot'])
        with self.assertRaises(self.s.Err) as error:
            self.s.r_doc_new(self.c,dict(doc_type='haak_invoice'),{},str(cid),session='test')
        self.c.rollback();self.assertEqual(error.exception.code,409)
        self.call('r_doc_put',dict(note='訂正版'),issued['id'])
        v=self.view(cid);self.assertEqual(len(v['documents']),1)
        self.assertEqual(v['documents'][0]['revision'],2)
        self.assertEqual(len(self.call('r_doc_history',None,issued['id'])),1)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=108000,confirmed_checked=True),cid)
        self.assertTrue(self.view(cid)['haak_progress']['invoice_mismatch'])

    def test_concurrent_invoice_issue_creates_one_record(self):
        cid=self.issue_context()
        def issue(_):
            with closing(self.s.db()) as conn:
                try:
                    self.s.r_doc_new(conn,dict(doc_type='haak_invoice'),{},str(cid),session='race')
                    return 200
                except self.s.Err as e:
                    conn.rollback();return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(issue,range(2))),[200,409])
        self.assertEqual(len(self.view(cid)['documents']),1)

    def test_missing_pdf_or_issue_date_is_not_completed(self):
        cid=self.issue_context()
        issued=self.s.r_doc_new(self.c,dict(doc_type='haak_invoice'),{},str(cid),session='test');self.c.commit()
        self.c.execute('UPDATE issued_documents SET file=NULL WHERE id=?',(issued['id'],));self.c.commit()
        doc=self.view(cid)['documents'][0]
        self.assertFalse(doc['progress']['complete']);self.assertFalse(doc['pdf_available'])
        self.assertEqual(self.view(cid)['haak_progress']['invoice_ids'],[issued['id']])


for helper in ('setUp','action','consent','entry','setup_cases','call','case','draft','save','release','options',
               'setup_sp','save_sp','cash_entry'):
    setattr(CaseSummaryTests,helper,getattr(test_funding_registration.FundingRegistrationTests,helper))

if __name__=='__main__':unittest.main()
