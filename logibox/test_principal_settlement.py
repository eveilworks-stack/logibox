"""ver.0.73 元金を含む都度精算。金額の成分分離と全経路での二重使用防止。"""
import csv
import io
import json
import secrets
import sqlite3
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fitz
import profit_settlement as profit
import principal_balance
import principal_funding
import funding_ledger
import test_profit_settlement


class PrincipalSettlementTests(unittest.TestCase):
    setUp=test_profit_settlement.ProfitSettlementTests.setUp
    call=test_profit_settlement.ProfitSettlementTests.call
    case=test_profit_settlement.ProfitSettlementTests.case
    draft=test_profit_settlement.ProfitSettlementTests.draft
    invoice=test_profit_settlement.ProfitSettlementTests.invoice
    prepare=test_profit_settlement.ProfitSettlementTests.prepare
    save=test_profit_settlement.ProfitSettlementTests.save
    get=test_profit_settlement.ProfitSettlementTests.get

    def values(self,cid):
        return principal_balance.values(self.c,self.c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,self.sid)).fetchone())

    def action(self,fn,b,*ids):
        try:r=fn(self.c,b,{},*ids);self.c.commit();return r
        except Exception:self.c.rollback();raise

    def close_and_pay(self,s):
        s=self.call('r_settlement_status',{'status':'requested'},s['id'])
        if s['total']:s=self.call('r_sub_invoice',self.invoice(s['total']),s['id'])
        return self.call('r_settlement_status',{'status':'paid','paid_at':self.today},s['id'])

    def test_01_combined_components_and_balance(self):
        cid=self.prepare();d=self.save(cid,include_principal=True)
        p=d['payments'][0]
        self.assertEqual((p['amount'],p['profit_amount'],p['principal_amount']),(2100000,100000,2000000))
        self.assertEqual((d['paid'],d['remaining'],d['principal_balance'],d['principal_available']),(100000,0,0,0))
        self.assertEqual(self.values(cid)['account']['refunded'],0)
        self.assertEqual(principal_funding.available_pool(self.c,self.sid)['available'],0)

    def test_02_profit_only_unchanged(self):
        cid=self.prepare();d=self.save(cid)
        self.assertEqual((d['paid'],d['principal_balance'],d['principal_available']),(100000,2000000,2000000))

    def test_03_principal_only_after_profit_payment(self):
        cid=self.prepare();self.save(cid)
        d=self.save(cid,amount=0,include_principal=True,paid_date='2020-10-02')
        self.assertEqual((d['paid'],d['principal_paid'],d['principal_balance']),(100000,2000000,0))
        self.assertEqual((d['payments'][1]['profit_amount'],d['payments'][1]['amount']),(0,2000000))
        with self.assertRaises(self.s.Err):self.save(cid,amount=0,include_principal=True)

    def test_04_plan_reserves_but_does_not_return_principal(self):
        cid=self.prepare();d=self.save(cid,include_principal=True,paid_date=None,planned_date='2020-12-10')
        self.assertEqual((d['paid'],d['principal_paid'],d['principal_balance'],d['principal_planned'],d['principal_available']),(0,0,2000000,2000000,0))
        self.assertEqual(self.call('r_dashboard')['principals'][0]['available'],0)
        s=self.draft(self.old);self.assertEqual(s['total'],0)
        target=self.case('充当先',self.new,2000000)
        with self.assertRaises(self.s.Err):self.call('r_carry',dict(carry_over=2000000,target_id=target,consent_date=self.today,consent_record='確認'),s['id'],s['lines'][0]['id'])
        with self.assertRaises(self.s.Err):self.action(funding_ledger.create_hold,dict(amount=1,reason='予約後の預かり',occurred_on=self.today,consent_date=self.today,consent_method='LINE',consent_record='確認'),cid,1)

    def test_05_monthly_then_confirm_reserved_combined_payment(self):
        cid=self.prepare();d=self.save(cid,include_principal=True,paid_date=None,planned_date=self.today)
        p=d['payments'][0]
        s=self.close_and_pay(self.draft(self.old))
        self.assertEqual((s['total'],s['lines'][0]['principal_payable']),(0,0))
        d=self.save(cid,id=p['id'],revision=p['revision'],reason='支払完了',include_principal=True,planned_date=self.today,paid_date=self.today)
        self.assertEqual((d['paid'],d['principal_paid'],d['principal_balance']),(100000,2000000,0))
        self.assertEqual(self.s.settlement_view(self.c,s['id'])['total'],0)
        self.s.init();self.assertEqual(self.get(cid)['principal_balance'],0)

    def test_06_after_month_end_preserves_snapshot_and_requires_revision(self):
        cid=self.prepare();s=self.draft(self.old)
        s=self.call('r_settlement_status',{'status':'requested'},s['id']);snapshot=s['request_snapshot_json']
        self.save(cid,include_principal=True)
        current=self.s.settlement_view(self.c,s['id'])
        self.assertEqual(current['total'],0);self.assertEqual(current['request_snapshot_json'],snapshot)
        self.assertTrue(current['request_needs_revision'])
        with self.assertRaises(self.s.Err):self.call('r_settlement_status',dict(status='paid',paid_at=self.today),s['id'])
        self.action(profit.r_monthly_revision,dict(reason='元金を含む都度精算を反映'),s['id'])
        self.call('r_settlement_status',dict(status='paid',paid_at=self.today),s['id'])
        self.assertEqual(self.get(cid)['principal_balance'],0)

    def test_07_allocated_principal_excluded_and_other_case_preserved(self):
        cid=self.prepare();target=self.case('別案件',self.new,1000000)
        s=self.draft(self.old)
        self.call('r_carry',dict(carry_over=1000000,target_id=target,consent_date=self.today,consent_record='確認'),s['id'],s['lines'][0]['id'])
        before=[tuple(x) for x in self.c.execute('SELECT * FROM settlement_lines')]
        d=self.save(cid,include_principal=True)
        self.assertEqual(d['payments'][0]['principal_amount'],1000000)
        self.assertEqual(before,[tuple(x) for x in self.c.execute('SELECT * FROM settlement_lines')])
        self.assertEqual(self.get(target)['principal_paid'],0)

    def test_08_existing_manual_refund_separate(self):
        cid=self.prepare()
        self.action(lambda c,b,q,cid,slot:principal_balance.save(c,b,cid,slot),dict(expected_revision=0,recovered=2000000,refunded=400000,record_date=self.today,note='旧返金'),cid,1)
        d=self.save(cid,include_principal=True)
        self.assertEqual(d['payments'][0]['principal_amount'],1600000)
        self.assertEqual(self.values(cid)['account']['refunded'],400000)
        self.assertEqual(self.values(cid)['refunded'],2000000)
        self.assertEqual(d['paid'],100000)

    def test_09_cancellation_restores_both_balances(self):
        cid=self.prepare();d=self.save(cid,include_principal=True);p=d['payments'][0]
        d=self.save(cid,id=p['id'],revision=p['revision'],reason='返還取消',cancel=True)
        self.assertEqual((d['remaining'],d['principal_balance'],d['principal_available']),(100000,2000000,2000000))
        self.assertEqual(len(d['history']),2)
        self.assertTrue(d['payments'][0]['cancelled'])

    def test_10_unreceived_principal_not_payable(self):
        cid=self.prepare(received=False)
        with self.assertRaises(self.s.Err):self.save(cid,include_principal=True)
        self.assertEqual(self.get(cid)['principal_paid'],0)

    def test_11_duplicate_request_is_idempotent(self):
        cid=self.prepare();key=secrets.token_hex(16)
        self.save(cid,include_principal=True,request_key=key)
        d=self.save(cid,include_principal=True,request_key=key)
        self.assertEqual(len(d['payments']),1)
        with self.assertRaises(self.s.Err):self.save(cid,request_key=key,include_principal=False)

    def test_12_edit_payment_date_without_double_principal(self):
        cid=self.prepare();d=self.save(cid,include_principal=True);p=d['payments'][0]
        d=self.save(cid,id=p['id'],revision=p['revision'],reason='支払日訂正',include_principal=True,paid_date='2020-10-02')
        self.assertEqual((d['principal_paid'],d['paid'],len(d['payments'])),(2000000,100000,1))
        with self.assertRaises(self.s.Err):self.save(cid,id=p['id'],revision=p['revision'],reason='古い編集',include_principal=True)

    def test_13_pdf_components_and_immutable_old_edition(self):
        cid=self.prepare();d=self.save(cid,include_principal=True);p=d['payments'][0]
        raw=self.action(profit.r_document,dict(issue=True),p['id'])[1]
        with fitz.open(stream=raw,filetype='pdf') as doc:
            text=''.join(x.get_text() for x in doc)
            for value in ('都度精算書','100,000円','2,000,000円','2,100,000円','今回の元金返還額','返還後の元金残高'):
                self.assertIn(value,text)
            self.assertNotIn('契約利益率',text);self.assertNotIn('前回まで',text)
        self.save(cid,id=p['id'],revision=1,reason='支払日訂正',include_principal=True,paid_date='2020-10-02')
        self.assertEqual(profit.r_document_get(self.c,{}, {},1)[1],raw)

    def test_14_migrate_schema13_preserves_profit_payment(self):
        cid=self.prepare();self.save(cid)
        self.c.execute('ALTER TABLE profit_payments DROP COLUMN principal_balance_after')
        self.c.execute('ALTER TABLE profit_payments DROP COLUMN principal_amount')
        for name in ('principal_amount','principal_deducted','principal_reserved'):
            self.c.execute('ALTER TABLE profit_monthly_payments DROP COLUMN '+name)
        self.c.execute('PRAGMA user_version=13');self.c.commit()
        before=[tuple(x) for x in self.c.execute('SELECT * FROM profit_payments')]
        self.s.init();self.s.init()
        after=[tuple(x)[:-2] for x in self.c.execute('SELECT * FROM profit_payments')]
        self.assertEqual(before,after)
        self.assertEqual((self.get(cid)['paid'],self.get(cid)['principal_paid']),(100000,0))
        self.assertTrue(list(Path(self.s.DBP).parent.glob('before-upgrade-*.zip')))
        self.assertEqual(self.c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_15_concurrent_principal_only_no_double_return(self):
        cid=self.prepare();self.save(cid)
        def pay(index):
            with closing(self.s.db()) as c:
                b=dict(method='full',amount=0,include_principal=True,paid_date=f'2020-10-0{index}',payment_method='bank',note='同時実行',request_key=secrets.token_hex(16))
                try:profit.r_save(c,b,{},cid,self.sid);c.commit();return True
                except self.s.Err:c.rollback();return False
        with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(pay,[2,3]))
        self.assertEqual(sorted(r),[False,True]);self.assertEqual(self.get(cid)['principal_paid'],2000000)

    def test_16_csv_and_cashflow_components_no_double_count(self):
        cid=self.prepare();self.save(cid,include_principal=True)
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'individual-settlements')[1].decode('utf-8-sig'))))
        self.assertEqual((rows[0]['利益精算額'],rows[0]['元金返還額'],rows[0]['支払合計']),('100000','2000000','2100000'))
        self.close_and_pay(self.draft(self.old))
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'cashflow')[1].decode('utf-8-sig'))))
        self.assertEqual(sum(int(x['下請け精算支払']) for x in rows),2100000)

    def test_17_principal_cancellation_after_monthly_retains_frozen_payment(self):
        cid=self.prepare();d=self.save(cid,include_principal=True);p=d['payments'][0]
        s=self.close_and_pay(self.draft(self.old));self.assertEqual(s['total'],0)
        d=self.save(cid,id=p['id'],revision=1,reason='個別送金取消',cancel=True)
        self.assertEqual((d['remaining'],d['principal_available']),(100000,2000000))
        self.assertEqual(self.s.settlement_view(self.c,s['id'])['total'],0)
        self.save(cid,include_principal=True,paid_date='2020-10-02')
        self.assertEqual(self.get(cid)['principal_balance'],0)

    def test_18_legacy_and_managed_custody_not_automatically_refunded(self):
        cid=self.prepare();s=self.draft(self.old)
        self.call('r_carry',dict(carry_over=500000,target_id='hold',consent_date=self.today,consent_record='預かり承諾'),s['id'],s['lines'][0]['id'])
        d=self.save(cid,include_principal=True)
        self.assertEqual(d['payments'][0]['principal_amount'],1500000)
        self.assertEqual(self.c.execute('SELECT carry_over FROM settlement_lines').fetchone()[0],500000)
        self.assertEqual(self.get(cid)['principal_balance'],500000)

    def test_19_subcontractor_and_case_scope(self):
        cid=self.prepare(other=True);other=self.prepare()
        self.save(cid,include_principal=True)
        self.assertEqual(self.get(cid,self.other)['principal_paid'],0)
        self.assertEqual(self.get(other)['principal_paid'],0)

    def test_20_new_receipt_cannot_hide_refunded_principal(self):
        cid=self.prepare();self.save(cid,include_principal=True)
        with self.assertRaises(self.s.Err):self.call('r_haak_put',dict(confirmed_amount_tax_in=3000000,confirmed_checked=True,paid_date=self.today,actual_paid_amount=1000000),cid)
        self.assertEqual(self.get(cid)['principal_paid'],2000000)

    def test_21_balances_csv_retains_reserved_payment_after_monthly_paid(self):
        cid=self.prepare();self.save(cid,include_principal=True,paid_date=None,planned_date=self.today)
        self.close_and_pay(self.draft(self.old))
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'balances',q={'at':[self.today]})[1].decode('utf-8-sig'))))
        self.assertEqual(sum(int(x['金額']) for x in rows if x['区分']=='下請け未精算'),2100000)

    def test_22_dashboard_keeps_pending_total_and_due_after_monthly_paid(self):
        cid=self.prepare();self.save(cid,include_principal=True,paid_date=None,planned_date=self.today)
        self.close_and_pay(self.draft(self.old))
        d=self.call('r_dashboard')
        self.assertEqual(d['totals']['sub_unsettled_profit'],100000)
        pending=[x for x in d['todo'] if '都度精算予定' in x['text']]
        self.assertEqual(len(pending),1);self.assertIn('2,100,000',pending[0]['text'])

    def test_23_pdf_keeps_principal_balance_at_original_payment(self):
        cid=self.prepare()
        self.action(lambda c,b,q,cid,slot:principal_balance.save(c,b,cid,slot),dict(expected_revision=0,recovered=1000000,refunded=0,record_date=self.today,note='初回回収'),cid,1)
        d=self.save(cid,include_principal=True);p=d['payments'][0]
        self.assertEqual(p['principal_balance_after'],1000000)
        self.action(lambda c,b,q,cid,slot:principal_balance.save(c,b,cid,slot),dict(expected_revision=1,recovered=2000000,refunded=0,record_date=self.today,note='残りを回収'),cid,1)
        self.save(cid,include_principal=True,amount=0,paid_date='2020-10-02')
        self.assertEqual(self.get(cid)['principal_balance'],0)
        self.action(profit.r_document,dict(issue=True),p['id'])
        snap=json.loads(self.c.execute('SELECT snapshot_json FROM profit_documents').fetchone()[0])
        self.assertEqual(snap['principal_balance_after'],1000000)

    def test_24_allocation_limit_does_not_prevent_return_of_owned_principal(self):
        cid=self.prepare()
        self.action(lambda c,b,q,cid,slot:principal_funding.release(c,b,cid,slot),dict(amount=0,expected_revision=0,record='他案件への充当を停止'),cid,1)
        self.assertEqual(principal_funding.available_pool(self.c,self.sid)['available'],0)
        self.assertEqual(self.get(cid)['principal_available'],2000000)
        d=self.save(cid,include_principal=True)
        self.assertEqual((d['principal_paid'],d['principal_balance']),(2000000,0))

    def test_25_stale_principal_quote_rejected(self):
        cid=self.prepare()
        self.action(lambda c,b,q,cid,slot:principal_balance.save(c,b,cid,slot),dict(expected_revision=0,recovered=1000000,refunded=0,record_date=self.today,note='回収確認を訂正'),cid,1)
        with self.assertRaises(self.s.Err):self.save(cid,include_principal=True,principal_amount=2000000)
        self.assertEqual(self.get(cid)['principal_paid'],0)

    def test_26_confirm_plan_keeps_original_amount_after_more_recovery(self):
        cid=self.prepare()
        self.action(lambda c,b,q,cid,slot:principal_balance.save(c,b,cid,slot),dict(expected_revision=0,recovered=1000000,refunded=0,record_date=self.today,note='一回目の回収'),cid,1)
        d=self.save(cid,include_principal=True,paid_date=None,planned_date=self.today);p=d['payments'][0]
        self.action(lambda c,b,q,cid,slot:principal_balance.save(c,b,cid,slot),dict(expected_revision=1,recovered=2000000,refunded=0,record_date=self.today,note='追加回収'),cid,1)
        d=self.save(cid,id=p['id'],revision=p['revision'],reason='予定どおり支払',include_principal=True,principal_amount=1000000,paid_date=self.today,planned_date=self.today)
        self.assertEqual((d['principal_paid'],d['principal_available']),(1000000,1000000))
        self.assertEqual(d['payments'][0]['amount'],1100000)


if __name__=='__main__':unittest.main()
