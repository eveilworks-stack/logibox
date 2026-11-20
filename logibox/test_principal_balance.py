"""回収済み元金のみの充当・返金・永続化・同時更新を確認する。"""
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch
import principal_balance as balance
import test_workflow


class PrincipalBalanceTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    case = test_workflow.WorkflowTests.case
    draft = test_workflow.WorkflowTests.draft
    invoice = test_workflow.WorkflowTests.invoice

    def source(self, amount=2000000, other=False):
        return self.case('香水・オードトワレ',self.old,amount,'2020-10-01',other=other)

    def haak(self,cid,paid=None,checked=True,date=True):
        return self.call('r_haak_put',dict(confirmed_amount_tax_in=3000000,confirmed_checked=checked,
            paid_date=self.today if date else None,actual_paid_amount=paid),cid)

    def value(self,cid,sid=None):
        sub=self.c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid or self.sid)).fetchone()
        return balance.values(self.c,sub)

    def record(self,cid,recovered,refunded=0,revision=0,slot=1):
        return self.call('r_principal_account',dict(recovered=recovered,refunded=refunded,
            record_date=self.today,note='銀行明細確認／累計額の訂正',expected_revision=revision),cid,slot)

    def transfer(self,cid,target,amount):
        return self.call('r_sub_put',dict(sub_id=self.sid,principal_tax_in=2000000,
            funding_method='carry',allocations=[dict(source_id=cid,carry_over=amount,
            consent_date=self.today,consent_method='LINE',consent_record='了承確認')]),target,1)

    def test_required_sequence_and_restart(self):
        source=self.source(); target=self.case('充当先',self.new,2000000)
        self.assertEqual(self.value(source)['balance'],2000000)
        self.assertEqual(self.value(source)['available'],0)
        self.haak(source)
        self.assertEqual(self.value(source)['available'],2000000)
        self.transfer(source,target,500000)
        self.assertEqual(self.value(source)['available'],1500000)
        self.assertEqual(self.value(source)['balance'],1500000)
        self.record(source,2000000,300000)
        self.assertEqual(self.value(source)['available'],1200000)
        line=self.c.execute('SELECT * FROM settlement_lines WHERE case_id=?',(source,)).fetchone()
        view=self.s.settlement_view(self.c,line['settlement_id'])
        self.assertEqual(view['total'],1200000+view['lines'][0]['sub_profit'])
        self.call('r_carry',dict(carry_over=0,expected_revision=line['carry_revision'],change_reason='正規取消'),line['settlement_id'],line['id'])
        self.assertEqual(self.value(source)['available'],1700000)
        self.assertEqual(self.value(source)['balance'],1700000)
        with closing(self.s.db()) as reopened:
            self.s.init_schema(reopened)
            sub=reopened.execute('SELECT * FROM case_subs WHERE case_id=?',(source,)).fetchone()
            self.assertEqual(balance.values(reopened,sub)['available'],1700000)
            self.assertEqual(reopened.execute('SELECT count(*) FROM principal_account_history').fetchone()[0],1)

    def test_partial_receipt_profit_excluded_and_separate_subs(self):
        source=self.source(other=True)
        self.haak(source,1000000)
        self.assertEqual(self.value(source)['available'],0)
        self.record(source,700000)
        with self.assertRaises(self.s.Err): self.record(source,400000,slot=2)
        self.record(source,20000,slot=2)
        self.assertEqual(self.value(source)['available'],700000)
        self.assertEqual(self.value(source,self.other)['available'],20000)
        with self.assertRaises(self.s.Err):self.record(source,2000001,revision=1)
        with self.assertRaises(self.s.Err):self.haak(source,500000)
        with self.assertRaises(self.s.Err):self.haak(source,1000000,checked=False)
        with self.assertRaises(self.s.Err):self.haak(source,1000000,date=False)

    def test_unpaid_and_unconfirmed_cannot_release_or_bypass_popup(self):
        source=self.source(); target=self.case('先',self.new,2000000)
        with self.assertRaises(self.s.Err):self.record(source,1)
        with self.assertRaises(self.s.Err):self.transfer(source,target,1)
        with self.assertRaises(self.s.Err):self.call('r_principal_release',dict(amount=1,record='確認',expected_revision=0),source,1)
        view=self.draft(self.old); line=view['lines'][0]
        with self.assertRaises(self.s.Err):self.call('r_carry',dict(carry_over=1,target_id=target,consent_date=self.today,consent_record='確認'),view['id'],line['id'])

    def test_multiple_sources_retry_stale_refund_and_audit(self):
        a=self.source();b=self.source();t=self.case('先',self.new,2000000)
        self.haak(a);self.haak(b)
        self.transfer(a,t,500000);self.transfer(b,t,300000)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),800000)
        with self.assertRaises(self.s.Err):self.transfer(a,t,500000)
        with self.assertRaises(self.s.Err):self.record(a,2000000,1600000)
        self.record(a,2000000,200000)
        with self.assertRaises(self.s.Err):self.record(a,2000000,300000)
        self.record(a,2000000,0,revision=1)
        self.assertEqual(self.value(a)['available'],1500000)
        with self.assertRaises(sqlite3.IntegrityError):self.c.execute('DELETE FROM principal_account_history')
        self.c.rollback()
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_dashboard_conserves_transfers_and_email_only_when_applicable(self):
        a=self.source();t=self.case('先',self.new,2000000)
        self.c.execute('UPDATE cases SET case_no=? WHERE id=?',('SP23648',a))
        self.c.execute('UPDATE cases SET case_no=? WHERE id=?',('SP24000',t));self.c.commit()
        self.haak(a);self.transfer(a,t,500000)
        dash=self.call('r_dashboard')['principals'][0]
        self.assertEqual((dash['principal'],dash['available']),(2000000,1500000))
        settlement=self.c.execute('SELECT settlement_id FROM settlement_lines WHERE case_id=?',(a,)).fetchone()[0]
        view=self.s.settlement_view(self.c,settlement)
        d=view['request_document'];d['period']=self.today[:7];d['carry_details']=view['lines']
        text=self.s.settlement_documents.request_text(d)
        self.assertIn('SP23648 香水・オードトワレ',text)
        self.assertIn('SP24000 先',text)
        self.assertIn('充当日 '+self.today,text)
        d['carry_details']=[]
        self.assertNotIn('元金充当明細（',self.s.settlement_documents.request_text(d))

    def test_final_payment_does_not_refund_twice(self):
        a=self.source();self.haak(a);self.record(a,2000000,300000)
        view=self.draft(self.old)
        self.assertEqual(view['total'],1800000)  # 残元金170万＋利益10万
        self.call('r_settlement_status',dict(status='requested'),view['id'])
        self.call('r_sub_invoice',self.invoice(view['total']),view['id'])
        self.call('r_settlement_status',dict(status='paid',paid_at=self.today),view['id'])
        self.assertEqual((self.value(a)['balance'],self.value(a)['available']),(0,0))
        rows=list(self.s.transaction_rows(self.c,self.s.report_cases(self.c),self.s.report_settlements(self.c)))
        self.assertEqual(sum(r[8] for r in rows if r[2]=='下請け精算'),2100000)

    def test_hold_is_selectable_and_cannot_also_be_refunded(self):
        a=self.source();t=self.case('先',self.new,2000000);self.haak(a)
        view=self.draft(self.old);line=view['lines'][0]
        self.call('r_carry',dict(target_id='hold',carry_over=2000000,consent_date=self.today),view['id'],line['id'])
        with self.assertRaises(self.s.Err):self.record(a,2000000,1)
        options=self.call('r_funding_options',None,t,q={'sub_id':[str(self.sid)]})
        self.assertEqual(options['sources'][0]['available'],2000000)
        self.transfer(a,t,2000000)
        self.assertEqual(self.value(a)['available'],0)

    def test_backup_failure_and_overlapping_sub_recovery_are_rejected(self):
        a=self.source(other=True);self.haak(a,1000000)
        self.record(a,990000)
        with self.assertRaises(self.s.Err):self.record(a,20000,slot=2)
        self.assertEqual(self.value(a,self.other)['available'],0)
        with patch.object(self.s.carry_management,'backup_database',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(self.s.Err,'バックアップ'):self.record(a,900000,revision=1)
        self.assertEqual(self.value(a)['available'],990000)


if __name__=='__main__':unittest.main()
