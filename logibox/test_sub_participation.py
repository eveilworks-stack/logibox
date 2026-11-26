"""Reversal conserves principal and preserves invoices, with atomic audited changes."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import sub_participation as participation
import funding_ledger as ledger
import test_funding_ledger


class ParticipationTests(unittest.TestCase):
    def setUp(self):
        test_funding_ledger.FundingLedgerTests.setUp(self)
        self.call('r_company_put',dict(name='検証会社'))
    call=test_funding_ledger.FundingLedgerTests.call
    case=test_funding_ledger.FundingLedgerTests.case
    setup_cases=test_funding_ledger.FundingLedgerTests.setup_cases
    save=test_funding_ledger.FundingLedgerTests.save
    entry=test_funding_ledger.FundingLedgerTests.entry
    consent=test_funding_ledger.FundingLedgerTests.consent
    action=test_funding_ledger.FundingLedgerTests.action
    cash_entry=test_funding_ledger.FundingLedgerTests.cash_entry
    options=test_funding_ledger.FundingLedgerTests.options
    hold=test_funding_ledger.FundingLedgerTests.hold

    def preview(self,t,mode='reset'):
        return self.action(participation.preview,{},t,1) if mode=='reset' else self.action_preview(t,mode)

    def action_preview(self,t,mode):
        return participation.preview(self.c,{}, {'mode':[mode]},str(t),'1')

    def reset(self,t,mode='reset',reason='案件の選択間違い。送付前に発見',token=None):
        plan=self.action_preview(t,mode)
        return self.action(participation.apply,dict(mode=mode,reason=reason,token=token or plan['token']),t,1)

    def issue(self,t):
        try:
            result=self.s.r_doc_new(self.c,dict(doc_type='principal_invoice',sub_id=self.sid,issued_date=self.today),{},str(t),session='reset-test')
            self.c.commit()
            return result['id']
        except Exception:
            self.c.rollback();raise

    def test_reset_after_issue_restores_source_and_keeps_pdf_reason_and_other_slot(self):
        a,b,t=self.setup_cases()
        self.call('r_sub_put',dict(sub_id=self.other,principal_tax_in=20000),t,2)
        self.save(t,[self.entry(a,30000),self.entry(b,20000)])
        did=self.issue(t);pdf=self.call('r_doc_download',None,did)[1]
        self.reset(t)
        self.assertEqual(ledger.outgoing(self.c,a,self.sid),0)
        self.assertEqual(ledger.outgoing(self.c,b,self.sid),0)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),0)
        self.assertEqual([r['slot'] for r in self.call('r_case_get',None,t)['subs']],[2])
        doc=self.call('r_docs',None,t)[0]
        self.assertIn('選択間違い',doc['cancellation']['reason'])
        self.assertEqual(self.call('r_doc_download',None,did)[1],pdf)
        self.assertEqual(len(self.call('r_case_get',None,t)['participation_resets']),1)
        with self.assertRaises(self.s.Err):self.call('r_doc_sent',dict(sent_date=self.today),did)
        with self.assertRaises(self.s.Err):self.call('r_doc_put',dict(issued_date=self.today),did)
        self.call('r_sub_put',dict(sub_id=self.sid,principal_tax_in=200000),t,1)
        self.assertNotEqual(self.issue(t),did)
        self.assertEqual(self.c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_allocation_only_keeps_participation_cancels_invoice_can_reissue(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)])
        did=self.issue(t);self.reset(t,'allocations')
        self.assertEqual(self.call('r_case_get',None,t)['subs'][0]['principal_tax_in'],200000)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),0)
        new=self.issue(t)
        self.assertNotEqual(new,did)
        self.assertEqual(self.c.execute('SELECT amount_tax_in FROM issued_documents WHERE id=?',(new,)).fetchone()[0],200000)

    def test_legacy_reversal_restores_source_and_records_carry_history(self):
        a,b,t=self.setup_cases()
        self.save(t,[dict(source_id=a,carry_over=30000,consent_date=self.today,consent_method='LINE',consent_record='承諾')])
        line=self.c.execute('SELECT * FROM settlement_lines WHERE carry_to_case_id=?',(t,)).fetchone()
        self.issue(t);self.reset(t)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),0)
        self.assertEqual(self.c.execute('SELECT carry_over FROM settlement_lines WHERE id=?',(line['id'],)).fetchone()[0],0)
        self.assertIn('選択間違い',self.c.execute('SELECT reason FROM carry_change_history ORDER BY id DESC').fetchone()[0])

    def test_hold_allocation_returns_to_fund(self):
        a,b,t=self.setup_cases();fund=self.hold(a,60000)
        self.save(t,[self.entry(fund,30000,'hold')]);self.reset(t)
        self.assertEqual(ledger.funds(self.c,self.sid)[0]['balance'],60000)

    def test_missing_reason_stale_token_and_backup_failure_leave_everything_unchanged(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)]);self.issue(t)
        before='\n'.join(self.c.iterdump())
        for reason,token in [('',None),('訂正','stale')]:
            with self.assertRaises(self.s.Err):self.reset(t,reason=reason,token=token)
            self.assertEqual('\n'.join(self.c.iterdump()),before)
        with patch('funding_ledger.backup',side_effect=self.s.Err('backup failed')):
            with self.assertRaises(self.s.Err):self.reset(t)
        self.assertEqual('\n'.join(self.c.iterdump()),before)
        with patch('funding_ledger.record',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):self.reset(t)
        self.assertEqual('\n'.join(self.c.iterdump()),before)

    def test_changed_invoice_invalidates_preview_and_duplicate_reset_rejected(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)])
        token=self.preview(t)['token'];self.issue(t)
        with self.assertRaises(self.s.Err):self.reset(t,token=token)
        self.reset(t)
        with self.assertRaises(self.s.Err):self.reset(t)

    def test_actual_cash_and_onward_allocation_block_reset(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)],cash_entry=self.cash_entry(10000))
        with self.assertRaises(self.s.Err):self.reset(t)
        with self.assertRaises(self.s.Err):self.reset(a)
        self.assertEqual(self.s.incoming_carry(self.c,t,self.sid),30000)

    def test_requested_source_blocks_reversal(self):
        a,b,t=self.setup_cases();self.save(t,[self.entry(a,30000)])
        self.call('r_settlement_create',dict(period=self.old,sub_id=self.sid))
        self.c.execute("UPDATE settlements SET status='requested'");self.c.commit()
        with self.assertRaises(self.s.Err):self.reset(t)
        self.assertEqual(ledger.outgoing(self.c,a,self.sid),30000)

    def test_pending_cash_plans_cancel_and_audit_immutable(self):
        a,b,t=self.setup_cases()
        self.save(t,[self.entry(a,30000)],cash_entry=self.cash_entry(10000,confirmed=False))
        self.reset(t)
        self.assertEqual(ledger.cash_plans(self.c,t,self.sid)[0]['status'],'cancelled')
        audit=self.c.execute('SELECT * FROM sub_participation_resets').fetchone()
        self.assertTrue(Path(self.s.DBP).with_name(audit['backup_file']).exists())
        self.assertEqual(json.loads(audit['before_json'])['sub']['case_id'],t)
        with self.assertRaises(Exception):self.c.execute('DELETE FROM sub_participation_resets')
        self.c.rollback()

if __name__=='__main__':unittest.main()
