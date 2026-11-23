"""0.6.5: dashboard/selection parity, SP91005 coverage, planned cash and legacy custody."""
from contextlib import closing
import sqlite3
import unittest
import uuid

import funding_ledger as ledger
import test_funding_ledger


class FundingRegistrationTests(unittest.TestCase):
    def cash_entry(self,amount,**kw):
        entry=dict(amount=amount,received_on=self.today,method='bank',request_key=uuid.uuid4().hex)
        entry.update(kw)
        return entry
    def setup_sp(self):
        source=self.case('回収確認済み元案件','2020-11',2000000,'2020-10-01')
        target=self.case('ブラックフライデー案件','2020-10',3000000)
        self.c.execute("UPDATE subcontractors SET name='検証下請けA' WHERE id=?",(self.sid,))
        self.c.execute("UPDATE cases SET case_no='SP91001',due_date='2999-12-31' WHERE id=?",(source,))
        self.c.execute("UPDATE cases SET case_no='SP91005' WHERE id=?",(target,));self.c.commit()
        self.call('r_haak_put',dict(confirmed_amount_tax_in=2500000,confirmed_checked=True,paid_date=self.today),source)
        return source,target

    def save_sp(self,target,entries=(),cash=None,method='mixed'):
        return self.call('r_sub_put',dict(sub_id=self.sid,principal_tax_in=3000000,
            funding_method=method,allocations=list(entries),cash_entry=cash),target,1)

    def test_sp25236_2m_then_planned_1m_then_bank_confirm_and_restart(self):
        source,target=self.setup_sp()
        opts=self.options(target)
        dash=next(x for x in self.call('r_dashboard')['principals'] if x['name']=='検証下請けA')
        self.assertEqual((dash['available'],opts['pool_available'],opts['available']),(2000000,2000000,2000000))
        self.assertEqual(opts['principal_sources'][0]['case_no'],'SP91001')
        before=self.call('r_case_get',None,target)['calc']
        self.save_sp(target,[self.entry(source,2000000)],self.cash_entry(1000000,confirmed=False))
        data=self.options(target)
        self.assertEqual((data['confirmed_total'],data['cash_received'],data['cash_planned']),(2000000,0,1000000))
        self.assertEqual(3000000-data['confirmed_total'],1000000)
        plan=data['cash_plans'][0]
        self.action(ledger.cash_plan_action,dict(action='confirmed',confirmed=True,received_on=self.today,
            request_key=uuid.uuid4().hex),plan['id'])
        self.assertEqual(self.options(target)['confirmed_total'],3000000)
        self.assertEqual(self.options(target)['cash_planned'],0)
        self.assertEqual(self.call('r_case_get',None,target)['calc'],before)
        with closing(self.s.db()) as reopened:
            self.s.init_schema(reopened)
            data=self.s.principal_funding.options(reopened,target,self.sid)
            self.assertEqual((data['cash_received'],data['confirmed_total']),(1000000,3000000))
            self.assertEqual(data['ledger_incoming'][0]['case_no'],'SP91001')
            self.assertEqual(data['ledger_incoming'][0]['target_case_no'],'SP91005')
            self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertFalse(reopened.execute('PRAGMA foreign_key_check').fetchall())

    def test_hand_plan_requires_actual_confirmation_receipt_and_stamp(self):
        source,target=self.setup_sp()
        self.save_sp(target,[self.entry(source,2000000)],self.cash_entry(1000000,confirmed=False,method='hand'))
        plan=self.options(target)['cash_plans'][0]['id']
        body=dict(action='confirmed',confirmed=False,received_on=self.today,request_key=uuid.uuid4().hex)
        with self.assertRaises(self.s.Err):self.action(ledger.cash_plan_action,body,plan)
        body.update(confirmed=True)
        with self.assertRaises(self.s.Err):self.action(ledger.cash_plan_action,body,plan)
        body.update(receipt_checked=True,stamp_checked=True)
        self.action(ledger.cash_plan_action,body,plan)
        with self.assertRaises(self.s.Err):self.action(ledger.cash_plan_action,body,plan)
        self.assertEqual(self.options(target)['confirmed_total'],3000000)
        self.assertEqual(self.c.execute('SELECT count(*) FROM principal_cash_entries').fetchone()[0],1)

    def test_plans_do_not_become_spendable_or_cashflow_and_cancel_is_audited(self):
        source,target=self.setup_sp()
        entry=self.cash_entry(1000000,confirmed=False);entry['received_on']='2999-01-01'
        self.save_sp(target,cash=entry,method='cash')
        data=self.options(target);plan=data['cash_plans'][0]
        self.assertEqual(data['confirmed_total'],0)
        self.assertEqual(ledger.cash(self.c,target,self.sid),0)
        with self.assertRaises(self.s.Err):self.save_sp(target,cash=entry)
        with self.assertRaises(self.s.Err):self.action(ledger.cash_plan_action,
            dict(action='confirmed',confirmed=True,received_on='2999-01-01',request_key=uuid.uuid4().hex),plan['id'])
        with self.assertRaises(self.s.Err):self.save_sp(target,cash=self.cash_entry(2000001,confirmed=False))
        self.action(ledger.cash_plan_action,dict(action='cancelled',note='予定変更',request_key=uuid.uuid4().hex),plan['id'])
        self.assertEqual(self.options(target)['cash_planned'],0)
        with self.assertRaises(sqlite3.IntegrityError):self.c.execute('DELETE FROM principal_cash_plans')
        self.c.rollback()

    def test_shared_optional_limit_and_unrecovered_sources(self):
        source,target=self.setup_sp()
        self.release(source,1200000)
        data=self.options(target)
        self.assertEqual(data['pool_available'],1200000)
        self.assertEqual(self.call('r_dashboard')['principals'][0]['available'],1200000)
        with self.assertRaises(self.s.Err):self.save_sp(target,[self.entry(source,1200001)])
        self.call('r_haak_put',dict(confirmed_amount_tax_in=2500000,confirmed_checked=True),source)
        self.assertEqual(self.options(target)['principal_sources'],[])
        self.assertEqual(self.call('r_dashboard')['principals'][0]['available'],0)

    def test_legacy_hold_can_split_without_changing_original_settlement(self):
        a,b,t=self.setup_cases()
        view=self.draft(self.old);line=next(x for x in view['lines'] if x['case_id']==a)
        self.call('r_carry',dict(target_id='hold',carry_over=100000,consent_date=self.today),view['id'],line['id'])
        before=tuple(self.c.execute('SELECT * FROM settlement_lines WHERE id=?',(line['id'],)).fetchone())
        candidate=next(x for x in self.options(t)['custody_sources'] if x['source_type']=='legacy_hold')
        self.save(t,[self.entry(candidate['id'],30000,'legacy_hold')])
        self.assertEqual(tuple(self.c.execute('SELECT * FROM settlement_lines WHERE id=?',(line['id'],)).fetchone()),before)
        fund=next(x for x in self.options(t)['custody_sources'] if x['source_type']=='hold')
        other=self.case('別案件','2021-01',200000)
        self.save(other,[self.entry(fund['id'],40000,'hold')])
        self.assertEqual(ledger.funds(self.c,self.sid)[0]['balance'],30000)
        self.assertEqual(self.call('r_dashboard')['principals'][0]['principal'],180000)
        pool=self.s.principal_funding.available_pool(self.c,self.sid)
        self.assertEqual(pool['available'],110000)

    def test_event_case_labels_are_immutable_and_monthly_mail_tracks_origin(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)])
        self.c.execute("UPDATE cases SET case_no='RENAMED',name='変更後' WHERE id=?",(a,));self.c.commit()
        history=ledger.history(self.c)
        self.assertEqual(history[0]['case_no'],'SP91001')
        view=self.draft(self.old);doc=view['request_document'];doc['funding_events']=history
        text=self.s.settlement_documents.request_text(doc)
        self.assertIn('SP91001 検証用商品A・商品B',text)
        self.assertIn('30,000円',text)

    def test_existing_allocation_does_not_exclude_source_remainder(self):
        a,b,t=self.setup_cases();self.release(a,100000)
        self.save(t,[self.entry(a,30000)])
        other=self.case('納期未入力の充当先','2021-01',200000)
        self.c.execute('UPDATE cases SET due_date=NULL WHERE id=?',(other,));self.c.commit()
        self.save(other,[self.entry(a,40000)])
        self.assertEqual(next(x for x in self.options(other)['principal_sources'] if x['case_id']==a)['available'],30000)

    def test_new_cash_cannot_rewrite_a_source_with_downstream_ledger_events(self):
        a,b,t=self.setup_cases()
        source=self.case('部分入金済み元案件','2020-11',200000)
        self.save(source,[],cash_entry=self.cash_entry(100000))
        self.call('r_haak_put',dict(confirmed_amount_tax_in=200000,confirmed_checked=True,paid_date=self.today),source)
        self.save(t,[self.entry(source,30000)])
        self.assertIsNone(self.s.principal_funding.source_line(self.c,source,self.sid))
        with self.assertRaises(self.s.Err):self.save(source,[],cash_entry=self.cash_entry(50000))
        with self.assertRaises(self.s.Err):self.save(source,[self.entry(a,50000)])
        self.assertEqual(ledger.cash(self.c,source,self.sid),100000)


for helper in ('setUp','action','consent','entry','setup_cases','call','case','draft','save','release','options'):
    setattr(FundingRegistrationTests,helper,getattr(test_funding_ledger.FundingLedgerTests,helper))

if __name__=='__main__':unittest.main()
