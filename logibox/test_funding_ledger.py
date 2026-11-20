"""分割充当・預かり金・実入金の保存と資金保存則の回帰テスト。"""
import csv
import io
import sqlite3
import unittest
import uuid
import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import funding_ledger as ledger
import test_principal_funding


class FundingLedgerTests(unittest.TestCase):
    setUp=test_principal_funding.PrincipalFundingTests.setUp
    call=test_principal_funding.PrincipalFundingTests.call
    case=test_principal_funding.PrincipalFundingTests.case
    draft=test_principal_funding.PrincipalFundingTests.draft
    setup_cases=test_principal_funding.PrincipalFundingTests.setup_cases
    options=test_principal_funding.PrincipalFundingTests.options
    release=test_principal_funding.PrincipalFundingTests.release
    save=test_principal_funding.PrincipalFundingTests.save

    def action(self,fn,b,*ids):
        try:
            result=fn(self.c,b,{},*map(str,ids))
            self.c.commit()
            return result
        except Exception:
            self.c.rollback()
            raise

    def consent(self,**kw):
        return dict(consent_date=self.today,consent_method='LINE',consent_record='担当者承諾確認',**kw)

    def entry(self,source,amount,kind='principal'):
        return self.consent(source_id=source,carry_over=amount,source_type=kind,request_key=uuid.uuid4().hex)

    def hold(self,cid,amount=100000):
        return self.action(ledger.create_hold,self.consent(amount=amount,reason='予定変更による預かり',occurred_on=self.today),cid,1)['id']

    def cash_entry(self,amount,**kw):
        return dict(amount=amount,received_on=self.today,method='bank',request_key=uuid.uuid4().hex,**kw)

    def test_one_source_split_many_targets_and_many_sources_combine(self):
        a,b,t=self.setup_cases();other=self.case('別充当先','2021-01',200000)
        before=self.call('r_case_get',None,t)['calc']
        self.save(t,[self.entry(a,30000),self.entry(b,20000)])
        self.save(other,[self.entry(a,40000)])
        self.assertEqual(ledger.outgoing(self.c,a,self.sid),70000)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),50000)
        self.assertEqual(self.s.incoming_carry(self.c,other,self.sid),40000)
        self.assertEqual(next(x for x in self.options(other)['sources'] if x['case_id']==a)['available'],30000)
        self.assertEqual(self.call('r_case_get',None,t)['calc'],before)
        view=self.draft(self.old)
        self.assertEqual(view['total'],99000)  # 元金18万＋利益9千−充当7万−2万
        self.assertEqual(self.call('r_dashboard')['principals'][0]['principal'],180000)

    def test_legacy_allocation_preserved_and_residual_can_split(self):
        a,b,t=self.setup_cases()
        old=self.consent(source_id=a,carry_over=30000)
        self.save(t,[old])
        original=[tuple(x) for x in self.c.execute('SELECT * FROM settlement_lines')]
        other=self.case('別先','2021-01',200000)
        self.save(other,[self.entry(a,20000)])
        self.assertEqual([tuple(x) for x in self.c.execute('SELECT * FROM settlement_lines')],original)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),30000)
        self.assertEqual(self.s.incoming_carry(self.c,other,self.sid),20000)
        self.assertEqual(next(x for x in self.options(other)['sources'] if x['case_id']==a)['available'],50000)

    def test_hold_split_refund_and_no_double_count(self):
        a,b,t=self.setup_cases();fund=self.hold(a)
        other=self.case('別先','2021-01',200000)
        self.assertEqual(self.s.principal_balance.values(self.c,self.c.execute('SELECT * FROM case_subs WHERE case_id=?',(a,)).fetchone())['available'],0)
        self.save(t,[self.entry(fund,30000,'hold')])
        self.save(other,[self.entry(fund,40000,'hold')])
        self.assertEqual(ledger.funds(self.c)[0]['balance'],30000)
        body=self.consent(amount=10000,event_date=self.today,note='銀行振込 控え123',request_key='refund-1')
        self.action(ledger.refund,body,fund)
        with self.assertRaises(self.s.Err):self.action(ledger.refund,body,fund)
        self.assertEqual(ledger.funds(self.c)[0]['balance'],20000)
        self.assertEqual(len(ledger.funds(self.c)[0]['history']),4)
        self.assertEqual(self.call('r_dashboard')['principals'][0]['principal'],170000)
        with self.assertRaises(self.s.Err):self.action(ledger.refund,self.consent(amount=20001,event_date=self.today,note='超過',request_key='refund-2'),fund)

    def test_principal_hold_and_new_cash_combined_and_reload(self):
        a,b,t=self.setup_cases();fund=self.hold(a,60000)
        self.save(t,[self.entry(fund,30000,'hold'),self.entry(b,50000)],cash_entry=self.cash_entry(120000))
        data=self.options(t)
        self.assertEqual(data['cash_received'],120000)
        self.assertEqual(sum(x['amount'] for x in data['ledger_incoming']),80000)
        view=self.call('r_case_get',None,t)
        self.assertEqual(view['subs'][0]['principal_confirmed'],200000)
        self.assertIsNone(view['subs'][0]['principal_received_date'])
        self.assertEqual(self.options(t)['cash_history'][0]['amount'],120000)
        with self.assertRaises(self.s.Err):self.save(t,[],cash_entry=self.cash_entry(1))
        self.assertEqual(len(self.options(t)['cash_history']),1)

    def test_overdraw_wrong_sub_duplicate_and_atomic_rollback(self):
        a,b,t=self.setup_cases()
        for entries in ([self.entry(a,100001)], [self.entry(a,10000),self.entry(a,10000)],
                        [self.entry(a,10000),self.entry(b,80001)]):
            with self.assertRaises(self.s.Err):self.save(t,entries)
        self.assertEqual(ledger.history(self.c),[])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM funding_requests').fetchone()[0],0)
        f=self.hold(a)
        self.call('r_sub_put',dict(sub_id=self.other,principal_tax_in=200000),t,2)
        with self.assertRaises(self.s.Err):self.call('r_sub_put',dict(sub_id=self.other,principal_tax_in=200000,funding_method='carry',allocations=[self.entry(f,1,'hold')]),t,2)

    def test_duplicate_request_cancel_edit_restore_and_history_retained(self):
        a,b,t=self.setup_cases();entry=self.entry(a,30000)
        self.save(t,[entry])
        with self.assertRaises(self.s.Err):self.save(t,[entry])
        eid=ledger.active(self.c)[0]['id']
        self.action(ledger.cancel,self.consent(note='金額訂正'),eid)
        with self.assertRaises(self.s.Err):self.action(ledger.cancel,self.consent(note='重複'),eid)
        self.save(t,[self.entry(a,20000)])
        self.assertEqual(ledger.outgoing(self.c,a,self.sid),20000)
        self.assertEqual(len(ledger.history(self.c)),3)
        self.assertTrue(ledger.history(self.c)[0]['cancelled'])
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),20000)
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_cash_request_retry_hand_checks_and_partial_payments(self):
        _,_,t=self.setup_cases()
        hand=self.cash_entry(50000);hand['method']='hand'
        with self.assertRaises(self.s.Err):self.save(t,[],cash_entry=hand)
        hand.update(receipt_checked=True,stamp_checked=True)
        self.save(t,[],cash_entry=hand)
        with self.assertRaises(self.s.Err):self.save(t,[],cash_entry=hand)
        self.save(t,[],cash_entry=self.cash_entry(30000))
        self.assertEqual(ledger.cash(self.c,t,self.sid),80000)
        self.assertEqual(self.call('r_case_get',None,t)['subs'][0]['principal_confirmed'],80000)

    def test_backups_consent_actual_receipt_and_immutable_history(self):
        a,b,t=self.setup_cases()
        entry=self.entry(a,10000);entry['consent_date']='2999-01-01'
        with self.assertRaises(self.s.Err):self.save(t,[entry])
        with patch.object(self.s.carry_management,'backup_database',side_effect=OSError('disk')):
            with self.assertRaisesRegex(self.s.Err,'バックアップ'):self.save(t,[self.entry(a,10000)])
        self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True),a)
        with self.assertRaises(self.s.Err):self.hold(a)
        with self.assertRaises(self.s.Err):self.save(t,[self.entry(a,10000)])
        self.save(t,[self.entry(b,10000)])
        for sql in ('DELETE FROM funding_events','UPDATE funding_events SET amount=1'):
            with self.assertRaises(sqlite3.IntegrityError):self.c.execute(sql)
            self.c.rollback()

    def test_deletion_guards_and_mixed_cancellation_preserves_cash(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)])
        for cid in (a,t):
            with self.assertRaises(self.s.Err):self.call('r_trash_move',None,cid)
            with self.assertRaises(self.s.Err):self.call('r_sub_del',None,cid,1)
        self.save(t,[],cash_entry=self.cash_entry(10000))
        self.action(ledger.cancel,self.consent(note='併用の充当だけ訂正'),ledger.active(self.c)[0]['id'])
        self.assertEqual(ledger.cash(self.c,t,self.sid),10000)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),0)
        self.save(t,[self.entry(a,20000)])
        self.c.execute('UPDATE case_subs SET principal_received_date=? WHERE case_id=?',(self.today,t));self.c.commit()
        with self.assertRaises(self.s.Err):self.action(ledger.cancel,self.consent(note='旧日付記録との矛盾'),ledger.active(self.c)[0]['id'])

    def test_legacy_hold_adoption_preserves_original_and_blocks_old_editor(self):
        a,b,t=self.setup_cases();view=self.draft(self.old)
        line=next(x for x in view['lines'] if x['case_id']==a)
        self.call('r_carry',self.consent(target_id='hold',carry_over=100000),view['id'],line['id'])
        original=tuple(self.c.execute('SELECT * FROM settlement_lines WHERE id=?',(line['id'],)).fetchone())
        fund=self.hold(a)
        self.assertEqual(tuple(self.c.execute('SELECT * FROM settlement_lines WHERE id=?',(line['id'],)).fetchone()),original)
        self.save(t,[self.entry(fund,50000,'hold')])
        with self.assertRaises(self.s.Err):self.call('r_carry',self.consent(target_id=t,carry_over=100000),view['id'],line['id'])
        self.assertEqual(ledger.funds(self.c)[0]['balance'],50000)

    def test_email_csv_sp_numbers_and_profit_calculation_unchanged(self):
        a,b,t=self.setup_cases();fund=self.hold(a)
        before=self.call('r_case_get',None,t)['calc']
        self.save(t,[self.entry(fund,40000,'hold'),self.entry(b,10000)])
        self.action(ledger.refund,self.consent(amount=10000,event_date=self.today,note='返金銀行控え',request_key='refund-export'),fund)
        view=self.s.settlement_view(self.c,self.c.execute('SELECT settlement_id FROM settlement_lines LIMIT 1').fetchone()[0])
        document=view['request_document'];document['period']=self.today[:7]
        document['funding_events']=ledger.period_events(self.c,self.sid,self.today[:7])
        text=self.s.settlement_documents.request_text(document)
        for word in ('SP23648','SP24392','一時預かり発生','一時預かり金充当','一時預かり金返金','返金銀行控え'):
            self.assertIn(word,text)
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'funding')[1].decode('utf-8-sig'))))
        self.assertEqual(len([x for x in rows if not x['区分'].startswith('旧')]),4)
        self.assertEqual(rows[1]['充当先案件番号'],'SP24392')
        self.assertEqual(self.call('r_case_get',None,t)['calc'],before)

    def test_concurrent_allocations_recheck_source_balance(self):
        a,b,t=self.setup_cases();other=self.case('同時充当先','2021-01',200000)
        def save_target(cid):
            with closing(self.s.db()) as c:
                try:
                    self.s.r_sub_put(c,dict(sub_id=self.sid,principal_tax_in=200000,funding_method='carry',allocations=[self.entry(a,80000)]),{},str(cid),'1')
                    c.commit();return True
                except self.s.Err:
                    c.rollback();return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(save_target,(t,other)))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(ledger.outgoing(self.c,a,self.sid),80000)

    def test_historical_balance_before_cancellation_and_refund(self):
        a,b,t=self.setup_cases();f=self.hold(a)
        self.save(t,[self.entry(f,30000,'hold')]);eid=ledger.active(self.c)[0]['id']
        original_date=datetime.date
        class Tomorrow(original_date):
            @classmethod
            def today(cls):return original_date.fromisoformat(self.today)+datetime.timedelta(days=1)
        with patch.object(ledger.datetime,'date',Tomorrow):
            self.action(ledger.cancel,self.consent(note='翌日取消'),eid)
        self.assertEqual(ledger.incoming(self.c,t,self.sid,self.today),30000)
        self.assertEqual(ledger.incoming(self.c,t,self.sid),0)
        self.assertEqual(ledger.funds(self.c,cutoff=self.today)[0]['balance'],70000)
        self.assertEqual(ledger.funds(self.c)[0]['balance'],100000)

    def test_monthly_email_snapshot_and_notification_deduplication(self):
        a,b,_=self.setup_cases();f=self.hold(a)
        current=self.case('今月納期',self.today[:7],200000)
        self.save(current,[self.entry(f,100000,'hold'),self.entry(b,80000)],cash_entry=self.cash_entry(20000))
        view=self.draft(self.today[:7])
        self.assertEqual(len(view['request_document']['funding_events']),3)
        original_date=datetime.date
        end=(original_date.fromisoformat(self.today).replace(day=28)+datetime.timedelta(days=4)).replace(day=1)
        class NextMonth(original_date):
            @classmethod
            def today(cls):return end
        with patch.object(ledger.datetime,'date',NextMonth):
            self.call('r_settlement_status',dict(status='requested'),view['id'])
            frozen=self.s.settlement_view(self.c,view['id'])['request_document']
            self.assertEqual(len(frozen['funding_events']),3)
            self.assertEqual(ledger.period_events(self.c,self.sid,self.today[:7]),[])
            self.call('r_settlement_status',dict(status='not_requested'),view['id'])
            self.assertEqual(self.s.settlement_view(self.c,view['id'])['request_document']['funding_events'],[])

    def test_original_source_balance_and_existing_hold_cannot_be_double_spent(self):
        a,b,t=self.setup_cases();f=self.hold(a,20000)
        self.save(t,[self.entry(a,80000)])
        other=self.case('別先','2021-01',200000)
        with self.assertRaises(self.s.Err):self.save(other,[self.entry(a,1)])
        self.save(other,[self.entry(f,20000,'hold')])
        self.assertEqual(ledger.funds(self.c)[0]['balance'],0)
        with self.assertRaises(self.s.Err):self.action(ledger.refund,self.consent(amount=1,event_date=self.today,note='返金',request_key='overdraw'),f)
        self.assertEqual(self.s.settlement_view(self.c,self.c.execute('SELECT settlement_id FROM settlement_lines WHERE case_id=?',(a,)).fetchone()[0])['total'],5000)

    def test_cashflow_and_settlement_csv_report_actual_cash_only(self):
        a,b,t=self.setup_cases();f=self.hold(a)
        self.save(t,[self.entry(f,50000,'hold'),self.entry(b,30000)],cash_entry=self.cash_entry(120000))
        self.action(ledger.refund,self.consent(amount=10000,event_date=self.today,note='返金控え',request_key='cashflow-refund'),f)
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'cashflow',q={'period':[self.today[:7]]})[1].decode('utf-8-sig'))))
        self.assertEqual(int(rows[0]['下請け元金入金']),120000)
        self.assertEqual(int(rows[0]['下請け精算支払']),10000)
        view=self.draft(self.old)
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'settlements')[1].decode('utf-8-sig'))))
        row=next(x for x in rows if x['案件番号']=='SP23649')
        self.assertEqual(int(row['充当額']),30000)
        self.assertIn('SP24392',row['分割充当先・金額・確定日'])
        self.assertIn(self.today,row['分割充当先・金額・確定日'])


if __name__=='__main__':unittest.main()
