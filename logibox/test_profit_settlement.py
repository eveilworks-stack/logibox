"""ver.0.72 最新仕様（案件単位の全額都度精算）と再送・改訂・保存を検証。"""
import datetime
import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import unittest
import fitz
import profit_settlement as profit
import test_workflow


class ProfitSettlementTests(unittest.TestCase):
    setUp=test_workflow.WorkflowTests.setUp
    call=test_workflow.WorkflowTests.call
    case=test_workflow.WorkflowTests.case
    draft=test_workflow.WorkflowTests.draft
    invoice=test_workflow.WorkflowTests.invoice

    def prepare(self,other=False,received=True):
        cid=self.case('精算テスト',self.old,2000000,self.today,other=other)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=3000000,confirmed_checked=True,
            actual_paid_amount=3000000 if received else 0,paid_date=self.today if received else None),cid)
        return cid

    def get(self,cid,sid=None): return profit.summary(self.c,cid,sid or self.sid)

    def save(self,cid,**kw):
        b=dict(method='full',amount=100000,paid_date='2020-10-01',planned_date=None,
            payment_method='bank',note='希望により精算',request_key=secrets.token_hex(16))
        b.update(kw)
        try:
            d=profit.r_save(self.c,b,{},str(cid),str(self.sid));self.c.commit();return d
        except Exception:self.c.rollback();raise

    def monthly_paid(self,s):
        s=self.call('r_settlement_status',{'status':'requested'},s['id'])
        s=self.call('r_sub_invoice',self.invoice(s['total']),s['id'])
        return self.call('r_settlement_status',{'status':'paid','paid_at':self.today},s['id'])

    def test_01_normal_unchanged(self):
        cid=self.prepare();s=self.draft(self.old)
        self.assertEqual(s['total'],2100000)
        self.monthly_paid(s)
        self.assertEqual(self.get(cid)['paid'],100000)
        self.assertEqual(self.get(cid)['status'],'精算完了')

    def test_02_full_profit_payment(self):
        cid=self.prepare();d=self.save(cid,method='full',amount=100000)
        self.assertEqual((d['remaining'],d['full_paid'],d['status']),(0,100000,'精算完了'))

    def test_03_arbitrary_and_partial_amount_rejected(self):
        cid=self.prepare()
        with self.assertRaises(self.s.Err):self.save(cid,method='partial',amount=30000)
        with self.assertRaises(self.s.Err):self.save(cid,amount=30000)
        self.assertEqual(self.get(cid)['remaining'],100000)

    def test_04_full_payment_cannot_repeat(self):
        cid=self.prepare();self.save(cid)
        with self.assertRaises(self.s.Err):self.save(cid,paid_date='2020-10-02')
        self.assertEqual(self.get(cid)['paid'],100000)

    def test_05_overpay_blocked(self):
        cid=self.prepare()
        with self.assertRaises(self.s.Err): self.save(cid,amount=100001)
        with self.assertRaises(self.s.Err): self.save(cid,amount=True)

    def test_06_unreceived_and_partial_haak_blocked(self):
        cid=self.prepare(received=False)
        with self.assertRaises(self.s.Err):self.save(cid,method='full',amount=100000)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=3000000,confirmed_checked=True,
            actual_paid_amount=1000000,paid_date=self.today),cid)
        with self.assertRaises(self.s.Err):self.save(cid)

    def test_07_monthly_deducts_actual(self):
        cid=self.prepare();self.save(cid,amount=100000,method='full');s=self.draft(self.old)
        self.assertEqual((s['total'],s['lines'][0]['sub_profit']),(2000000,100000))
        self.assertEqual(s['lines'][0]['profit_payable'],0)

    def test_08_payment_after_closing_requires_revision(self):
        cid=self.prepare();s=self.draft(self.old)
        s=self.call('r_settlement_status',{'status':'requested'},s['id'])
        snapshot=s['request_snapshot_json'];self.save(cid)
        changed=self.s.settlement_view(self.c,s['id'])
        self.assertEqual(changed['total'],2000000)
        self.assertEqual(changed['request_snapshot_json'],snapshot)
        self.assertTrue(changed['request_needs_revision'])
        self.call('r_sub_invoice',self.invoice(changed['total']),s['id'])
        with self.assertRaises(self.s.Err):self.call('r_settlement_status',{'status':'paid','paid_at':self.today},s['id'])
        profit.r_monthly_revision(self.c,{'reason':'支払後の差額を反映'},{},s['id']);self.c.commit()
        self.call('r_settlement_status',{'status':'paid','paid_at':self.today},s['id'])
        self.assertEqual((self.get(cid)['paid'],self.get(cid)['remaining']),(100000,0))
        with self.assertRaises(self.s.Err):self.save(cid,amount=1)
        self.assertGreater(self.c.execute('SELECT COUNT(*) FROM profit_history').fetchone()[0],1)

    def test_09_plan_reserved_not_paid(self):
        cid=self.prepare();d=self.save(cid,paid_date=None,planned_date='2020-12-10')
        self.assertEqual((d['paid'],d['remaining'],d['planned'],d['status']),(0,100000,100000,'精算予定'))
        s=self.monthly_paid(self.draft(self.old))
        self.assertEqual(s['total'],2000000)
        self.assertEqual(self.get(cid)['paid'],0)
        p=d['payments'][0]
        d=self.save(cid,id=p['id'],revision=p['revision'],reason='支払完了',paid_date=self.today,planned_date=p['planned_date'])
        self.assertEqual(d['remaining'],0)

    def test_10_pdf_preview_issue_freeze(self):
        cid=self.prepare();self.call('r_company_put',{'name':'TOMOS','address':'東京都テスト'})
        d=self.save(cid);pid=d['payments'][0]['id']
        preview=profit.r_document(self.c,{}, {},pid)[1]
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM profit_documents').fetchone()[0],0)
        raw=profit.r_document(self.c,{'issue':True},{},pid)[1];self.c.commit()
        self.assertEqual(preview,raw)
        with fitz.open(stream=raw,filetype='pdf') as pdf:
            text=''.join(x.get_text() for x in pdf)
            for value in ('利益精算書','御中','TOMOS','今回の利益精算額','確定利益額','0円','100,000円','2020-10-01'):
                self.assertIn(value,text)
            self.assertNotIn('契約利益率',text);self.assertNotIn('前回まで',text)
        snap=json.loads(self.c.execute('SELECT snapshot_json FROM profit_documents').fetchone()[0])
        self.assertIn('contract_rate',snap);self.assertIn('previous_paid',snap)
        profit.r_document(self.c,{'issue':True},{},pid);self.c.commit()
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM profit_documents').fetchone()[0],1)
        self.save(cid,id=pid,revision=1,reason='支払日訂正',paid_date='2020-10-02')
        self.assertEqual(profit.r_document_get(self.c,{}, {},1)[1],raw)

    def test_11_mail_full_paid_case_retained(self):
        cid=self.prepare();self.c.execute("UPDATE cases SET case_no='SP-072' WHERE id=?",(cid,));self.c.commit()
        self.save(cid,method='full',amount=100000)
        text=self.draft(self.old)['request_text']
        for value in ('SP-072','精算テスト','確定利益 100,000','都度精算済み 100,000','未精算残額 0','2020-10-01','2020-12-15支払予定利益 0'):
            self.assertIn(value,text)

    def test_12_backdated_and_future_payment(self):
        cid=self.prepare();self.assertEqual(self.save(cid)['payments'][0]['paid_date'],'2020-10-01')
        with self.assertRaises(self.s.Err):self.save(cid,amount=10000,paid_date='2099-01-01')

    def test_13_principal_and_hold_untouched(self):
        cid=self.prepare()
        tables=('principal_accounts','principal_release','settlement_lines','funding_events')
        tables=[x for x in tables if self.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(x,)).fetchone()]
        before={t:[tuple(x) for x in self.c.execute('SELECT * FROM '+t)] for t in tables}
        self.save(cid)
        self.assertEqual(before,{t:[tuple(x) for x in self.c.execute('SELECT * FROM '+t)] for t in tables})
        self.assertEqual(self.get(cid)['principal_tax_in'],2000000)

    def test_14_edit_cancel_recalculates(self):
        cid=self.prepare();d=self.save(cid);p=d['payments'][0]
        d=self.save(cid,id=p['id'],revision=1,reason='支払日訂正',paid_date='2020-10-02')
        self.assertEqual(d['remaining'],0)
        d=self.save(cid,id=p['id'],revision=2,reason='振込取消',cancel=True)
        self.assertEqual((d['remaining'],d['monthly_scheduled']),(100000,100000))
        self.assertEqual(len(d['history']),3)
        self.assertTrue(d['payments'][0]['cancelled'])

    def test_15_reopen_retains_history(self):
        cid=self.prepare();self.save(cid)
        with closing(self.s.db()) as reopened:self.assertEqual(profit.summary(reopened,cid,self.sid)['paid'],100000)
        self.s.init()
        self.assertEqual(self.get(cid)['paid'],100000)

    def test_16_legacy_migration_idempotent(self):
        cid=self.prepare();s=self.monthly_paid(self.draft(self.old))
        # Simulate the absence of 0.72-only tables in a 0.71 database.
        for t in ('profit_documents','profit_history','profit_monthly_payments','profit_payments','profit_preferences'):
            self.c.execute('DROP TABLE '+t)
        self.c.execute('PRAGMA user_version=12');self.c.commit()
        self.s.init();self.s.init()
        self.assertEqual(self.get(cid)['paid'],100000)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM profit_monthly_payments').fetchone()[0],1)
        self.assertEqual(self.c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertTrue(list(Path(self.s.DBP).parent.glob('before-upgrade-*.zip')))

    def test_17_existing_calc_csv_and_protections(self):
        cid=self.prepare();d=self.save(cid)
        self.assertEqual(self.call('r_case_get',None,cid)['subs'][0]['sub_profit'],100000)
        self.draft(self.old)
        csv=self.call('r_export',None,'settlements')[1].decode('utf-8-sig')
        self.assertIn('利益精算状態',csv);self.assertIn('精算完了',csv)
        with self.assertRaises(self.s.Err):self.call('r_sub_del',{},cid,1)
        with self.assertRaises(self.s.Err):self.call('r_case_put',dict(name='変更',planned_rate_pct=9,due_date='2020-11-20'),cid)

    def test_18_subcontractor_and_case_isolation(self):
        cid=self.prepare(other=True);other=self.prepare()
        self.save(cid)
        self.assertEqual((self.get(cid,self.other)['paid'],self.get(other)['paid']),(0,0))
        self.assertEqual((self.get(cid,self.other)['method'],self.get(other)['method']),('normal','normal'))

    def test_19_duplicate_and_stale_edit(self):
        cid=self.prepare();key=secrets.token_hex(16)
        self.save(cid,request_key=key);d=self.save(cid,request_key=key)
        self.assertEqual(len(d['payments']),1)
        with self.assertRaises(self.s.Err):self.save(cid,request_key=key,amount=20000)
        with self.assertRaises(self.s.Err):self.save(cid,id=d['payments'][0]['id'],revision=0,reason='古い画面')
        with self.assertRaises(self.s.Err):self.save(cid)

    def test_20_full_case_payments_deduct_only_selected_cases(self):
        cids=[]
        for name,principal in [('A',5000000),('B',3000000),('C',4000000)]:
            cid=self.case(name,self.old,principal,self.today)
            self.call('r_haak_put',dict(confirmed_amount_tax_in=10000000,confirmed_checked=True,paid_date=self.today),cid)
            cids.append(cid)
        self.save(cids[0],method='full',amount=250000)
        self.save(cids[2],amount=200000)
        d=self.draft(self.old)
        self.assertEqual(sum(x['sub_profit'] for x in d['lines']),600000)
        self.assertEqual(sum(x['profit_payable'] for x in d['lines']),150000)
        self.assertEqual(d['total']-sum(x['principal'] for x in d['lines']),150000)

    def test_21_concurrent_overpayment_protected(self):
        from concurrent.futures import ThreadPoolExecutor
        cid=self.prepare()
        def pay(index):
            with closing(self.s.db()) as c:
                b=dict(method='full',amount=100000,paid_date=f'2020-10-0{index}',
                    payment_method='bank',note='同時処理検証',request_key=secrets.token_hex(16))
                try:
                    profit.r_save(c,b,{},cid,self.sid);c.commit();return True
                except self.s.Err:c.rollback();return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(pay,[1,2]))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.get(cid)['remaining'],0)

    def test_22_profit_export_and_cashflow_excludes_plan(self):
        import csv,io
        cid=self.prepare();self.save(cid)
        planned=self.prepare();self.save(planned,paid_date=None,planned_date='2020-10-02')
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'profit')[1].decode('utf-8-sig'))))
        self.assertEqual(sum(int(x['都度精算済み']) for x in rows),100000)
        self.assertEqual(sum(int(x['別途支払予定']) for x in rows),100000)
        rows=list(csv.DictReader(io.StringIO(self.call('r_export',None,'cashflow',q={'period':['2020-10']})[1].decode('utf-8-sig'))))
        self.assertEqual(rows[0]['下請け精算支払'],'100000')
        self.assertEqual(self.get(planned)['monthly_scheduled'],0)


if __name__=='__main__':unittest.main()
