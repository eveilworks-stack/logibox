"""下請けタブからの元金充当と通知期間の回帰テスト。"""
import json
import unittest
from unittest.mock import patch
import test_workflow


class PrincipalFundingTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    case = test_workflow.WorkflowTests.case
    draft = test_workflow.WorkflowTests.draft

    def setup_cases(self):
        a = self.case('スポット 香水・オードトワレ',self.old,100000,'2020-11-01')
        b = self.case('第2充当元',self.old,80000,'2020-11-01')
        target = self.case('医薬品差し替え案件 プチプラスキンケア・UV',self.new,200000)
        for cid,no in ((a,'SP23648'),(b,'SP23649'),(target,'SP24392')):
            self.c.execute('UPDATE cases SET case_no=? WHERE id=?',(no,cid))
        self.c.commit()
        for cid in (a,b):
            self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True,paid_date=self.today),cid)
        return a,b,target

    def release(self,cid,amount,revision=0):
        return self.call('r_principal_release',dict(amount=amount,record='返金予定の元金から充当へ回せることを確認',expected_revision=revision),cid,1)

    def options(self,target,sub=None):
        return self.call('r_funding_options',None,target,q={'sub_id':[str(sub or self.sid)]})

    def entry(self,source,amount):
        return dict(source_id=source,carry_over=amount,consent_date=self.today,consent_method='LINE',consent_record='担当者の承諾メッセージ')

    def save(self,target,entries,**kwargs):
        body=dict(sub_id=self.sid,principal_tax_in=200000,funding_method='mixed',allocations=entries)
        body.update(kwargs)
        return self.call('r_sub_put',body,target,1)

    def test_cash_is_not_automatically_releasable(self):
        a,b,t=self.setup_cases()
        self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True),a)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True),b)
        self.assertEqual(self.options(t)['sources'],[])
        with self.assertRaises(self.s.Err): self.release(a,60000)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True,paid_date=self.today),a)
        self.release(a,60000)
        self.assertEqual([(x['case_id'],x['available']) for x in self.options(t)['sources']],[(a,60000)])
        with self.assertRaises(self.s.Err): self.release(b,80001)

    def test_multiple_partial_allocations_preserve_profit_and_cash_dates(self):
        a,b,t=self.setup_cases(); self.release(a,60000); self.release(b,80000)
        before=self.call('r_case_get',None,t)['calc']
        self.save(t,[self.entry(a,50000),self.entry(b,70000)])
        actual=self.call('r_case_get',None,t)
        self.assertEqual(actual['subs'][0]['carry_reserved'],120000)
        self.assertIsNone(actual['subs'][0]['principal_received_date'])
        self.assertEqual(actual['calc'],before)
        lines=self.c.execute('SELECT * FROM settlement_lines ORDER BY id').fetchall()
        self.assertEqual(sum(x['principal']+x['sub_profit']-x['carry_over'] for x in lines),69000)
        for line in lines:
            self.assertEqual(line['consent_method'],'LINE')
            self.assertEqual(line['carry_confirmed_date'],self.today)
            self.assertTrue(line['carry_created_at'])
        # 分割充当対応後は、使用済み案件の残額も候補として残す。
        self.assertEqual([x['available'] for x in self.options(t)['sources']],[10000,10000])

    def test_invalid_second_allocation_rolls_back_entire_save(self):
        a,b,t=self.setup_cases();self.release(a,60000);self.release(b,80000)
        with self.assertRaises(self.s.Err):self.save(t,[self.entry(a,50000),self.entry(b,80001)])
        self.assertEqual(self.c.execute('SELECT count(*) FROM settlement_lines').fetchone()[0],0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0],0)
        self.assertEqual(self.options(t)['sources'][0]['available'],60000)

    def test_duplicate_source_and_wrong_subcontractor_rejected(self):
        a,b,t=self.setup_cases();self.release(a,60000)
        with self.assertRaises(self.s.Err):self.save(t,[self.entry(a,10000),self.entry(a,10000)])
        self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True),b)
        with self.assertRaises(self.s.Err):self.save(t,[self.entry(b,10000)])
        self.assertEqual(self.options(t,self.other)['sources'],[])

    def test_edit_cancel_history_restore_and_deletion_guard(self):
        a,b,t=self.setup_cases();self.release(a,60000);self.save(t,[self.entry(a,50000)])
        item=self.options(t)['incoming'][0]
        with self.assertRaises(self.s.Err):self.call('r_trash_move',None,a)
        with self.assertRaises(self.s.Err):self.call('r_trash_move',None,t)
        body=dict(self.entry(a,40000),target_id=t,expected_revision=item['carry_revision'],change_reason='金額訂正')
        self.call('r_carry',body,item['settlement_id'],item['id'])
        with self.assertRaises(self.s.Err):self.call('r_carry',body,item['settlement_id'],item['id'])
        body.update(carry_over=0,expected_revision=item['carry_revision']+1,change_reason='取消')
        self.call('r_carry',body,item['settlement_id'],item['id'])
        self.assertEqual(self.options(t)['sources'][0]['available'],60000)
        history=self.s.carry_management.change_history(self.c,item['id'])
        self.assertEqual(len(history),3)
        self.assertEqual(history[0]['before']['carry_over'],40000)
        self.assertEqual(history[0]['after']['carry_over'],0)
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_edit_destination_uses_ids_preserves_unrelated_cases(self):
        a,b,t=self.setup_cases();self.release(a,60000);self.save(t,[self.entry(a,50000)])
        other=self.case('別の充当先', '2021-01',200000)
        before=self.call('r_case_get',None,b)
        item=self.options(t)['incoming'][0]
        self.call('r_carry',dict(self.entry(a,50000),target_id=other,expected_revision=item['carry_revision']),item['settlement_id'],item['id'])
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),0)
        self.assertEqual(self.s.incoming_carry(self.c,other,self.sid),50000)
        self.assertEqual(self.call('r_case_get',None,b),before)

    def test_release_revision_backup_and_history(self):
        a,b,t=self.setup_cases();self.release(a,60000)
        with self.assertRaises(self.s.Err):self.release(a,50000)
        self.release(a,50000,1)
        self.assertEqual(self.options(t)['sources'][0]['available'],50000)
        self.assertEqual(self.c.execute('SELECT count(*) FROM principal_release_history').fetchone()[0],2)
        with patch.object(self.s.carry_management,'backup_database',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(self.s.Err,'バックアップ'):self.release(a,40000,2)
        self.assertEqual(self.options(t)['sources'][0]['available'],50000)

    def test_email_case_numbers_consent_and_out_of_period(self):
        import settlement_documents as docs
        a,b,t=self.setup_cases();self.release(a,60000);self.save(t,[self.entry(a,50000)])
        line=self.options(t)['incoming'][0]
        view=self.s.settlement_view(self.c,line['settlement_id'])
        document=view['request_document'];document['period']=self.today[:7];document['carry_details']=view['lines']
        text=docs.request_text(document)
        self.assertIn('SP23648 スポット 香水・オードトワレ',text)
        self.assertIn('SP24392 医薬品差し替え案件',text)
        self.assertIn('承諾方法 LINE',text)
        self.assertNotRegex(text,r'\d{2}-\d{4}')
        section=text.split('元金充当明細（')[1]
        self.assertEqual(section.count('充当金額 50,000円'),1)
        document['period']='2020-01'
        self.assertNotIn('元金充当明細（',docs.request_text(document))

    def test_notified_carry_is_not_new_again_after_request_withdrawal(self):
        a,b,t=self.setup_cases();self.release(a,60000);self.save(t,[self.entry(a,50000)])
        item=self.options(t)['incoming'][0]
        self.c.execute('UPDATE settlement_lines SET carry_confirmed_date=? WHERE id=?',(self.old+'-21',item['id']))
        self.c.commit()
        view=self.draft(self.old)
        self.assertEqual(len(view['request_document']['carry_details']),1)
        requested=self.call('r_settlement_status',{'status':'requested'},view['id'])
        self.assertEqual(len(requested['request_document']['carry_details']),1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM carry_notifications').fetchone()[0],1)
        fresh=self.call('r_settlement_status',{'status':'not_requested'},view['id'])
        self.assertEqual(fresh['request_document']['carry_details'],[])

    def test_completed_source_and_unfunded_release_are_not_selectable(self):
        a,b,t=self.setup_cases();self.release(a,60000)
        self.c.execute("UPDATE case_subs SET principal_received_date=NULL WHERE case_id=?",(a,));self.c.commit()
        self.assertNotIn(a,[x['case_id'] for x in self.options(t)['sources']])
        with self.assertRaises(self.s.Err):self.release(a,1,1)

    def test_destination_capacity_and_retry_do_not_duplicate(self):
        a,b,t=self.setup_cases();self.release(a,100000);self.release(b,80000)
        entries=[self.entry(a,100000),self.entry(b,80000)]
        self.save(t,entries)
        with self.assertRaises(self.s.Err):self.save(t,entries)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),180000)
        self.assertEqual(self.c.execute('SELECT count(*) FROM settlement_lines').fetchone()[0],2)


if __name__=='__main__':unittest.main()
