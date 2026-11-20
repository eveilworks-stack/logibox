"""見送り時の充当解除、未実行精算の取消と下請け設定解除。"""
import unittest
from unittest.mock import patch
import test_carry_editing


class CaseCancellationTests(unittest.TestCase):
    setUp = test_carry_editing.CarryEditingTests.setUp
    call = test_carry_editing.CarryEditingTests.call
    case = test_carry_editing.CarryEditingTests.case
    draft = test_carry_editing.CarryEditingTests.draft
    prepare = test_carry_editing.CarryEditingTests.prepare
    invoice = test_carry_editing.CarryEditingTests.invoice
    paid = test_carry_editing.CarryEditingTests.paid

    def cancel(self, cid, token=None):
        if token is None:
            token = self.call('r_case_deletion_check', None, cid)['cancellation']['token']
        return self.call('r_trash_move', dict(cancel_to_hold=True, expected_token=token), cid)

    def test_target_cancellation_detaches_sub_and_removes_draft_keeps_source_profit(self):
        s, line, source, wrong, _ = self.prepare(same_month=False)
        target_s = self.draft(self.new)
        check = self.call('r_case_deletion_check', None, wrong)
        self.assertFalse(check['can_delete'])
        self.assertTrue(check['cancellation']['can_cancel'])
        self.assertEqual(check['cancellation']['hold_total'], 2000000)
        self.assertEqual(check['cancellation']['draft_line_count'], 1)
        result = self.cancel(wrong)
        self.assertTrue(result['consent_pending'])
        self.assertFalse(self.c.execute('SELECT 1 FROM case_subs WHERE case_id=?',(wrong,)).fetchone())
        self.assertFalse(self.c.execute('SELECT 1 FROM settlement_lines WHERE case_id=?',(wrong,)).fetchone())
        view = self.s.settlement_view(self.c,s['id'])
        self.assertEqual(view['total'],100000)
        actual = view['lines'][0]
        self.assertEqual(actual['case_id'], source)
        self.assertTrue(actual['temporary_hold'])
        self.assertTrue(actual['hold_consent_pending'])
        self.assertIsNone(actual['consent_date'])
        self.assertIsNone(actual['consent_record'])
        self.assertEqual(actual['consent_method'],'LINE')
        self.assertEqual(actual['carry_history'][0]['before']['carry_to_case_id'],wrong)
        self.assertTrue(actual['carry_history'][0]['before']['cancelled_case_settings']['subs'])
        with self.assertRaisesRegex(self.s.Err,'了承待ち'):
            self.call('r_settlement_status',{'status':'requested'},s['id'])
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_consent_date_only_completes_pending_hold(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        self.cancel(wrong)
        actual = self.s.settlement_view(self.c,s['id'])['lines'][0]
        s = self.call('r_carry',dict(target_id='hold',carry_over=2000000,consent_date=self.today,
                    expected_revision=actual['carry_revision']),s['id'],line['id'])
        self.assertFalse(s['lines'][0]['hold_consent_pending'])
        self.assertEqual(s['total'],100000)
        self.call('r_settlement_status',{'status':'requested'},s['id'])

    def test_already_paid_source_can_be_held_without_changing_payment(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        s = self.paid(s)
        self.cancel(wrong)
        view = self.s.settlement_view(self.c,s['id'])
        self.assertEqual(view['status'],'paid')
        self.assertEqual(view['total'],100000)
        self.assertEqual(view['paid_at'],s['paid_at'])
        self.assertEqual(view['request_document'],s['request_document'])
        self.assertTrue(view['request_needs_revision'])

    def test_cash_or_purchases_or_requested_target_prevent_cancellation(self):
        for kind in ('cash','purchase','requested'):
            with self.subTest(kind=kind):
                target = self.case(kind,self.new,2000000)
                if kind=='cash':
                    self.call('r_sub_put',dict(sub_id=self.sid,principal_tax_in=2000000,principal_received_date=self.today),target,1)
                elif kind=='purchase':
                    self.call('r_purchase_new',dict(supplier_id=1,amount_tax_in=1000),target)
                else:
                    s=self.draft(self.new)
                    self.c.execute("UPDATE settlements SET status='requested' WHERE id=?",(s['id'],));self.c.commit()
                self.assertFalse(self.call('r_case_deletion_check',None,target)['cancellation']['can_cancel'])
                with self.assertRaisesRegex(self.s.Err,'見送り処理ができません'):
                    self.cancel(target)
                self.assertTrue(self.c.execute('SELECT 1 FROM case_subs WHERE case_id=?',(target,)).fetchone())

    def test_stale_preview_or_backup_failure_rolls_back_all_changes(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        token=self.call('r_case_deletion_check',None,wrong)['cancellation']['token']
        self.c.execute("UPDATE cases SET name='変更あり' WHERE id=?",(wrong,));self.c.commit()
        with self.assertRaisesRegex(self.s.Err,'更新されました'):
            self.cancel(wrong,token)
        with patch.object(self.s.carry_management,'backup_database',side_effect=OSError('disk')):
            with self.assertRaisesRegex(self.s.Err,'バックアップに失敗'):
                self.cancel(wrong)
        self.assertEqual(self.c.execute('SELECT carry_to_case_id FROM settlement_lines WHERE id=?',(line['id'],)).fetchone()[0],wrong)
        self.assertTrue(self.c.execute('SELECT 1 FROM case_subs WHERE case_id=?',(wrong,)).fetchone())

    def test_already_held_source_is_not_changed_when_target_is_cancelled(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        self.call('r_carry',dict(target_id='hold',carry_over=2000000,consent_date=self.today,
                 expected_revision=line['carry_revision']),s['id'],line['id'])
        self.draft(self.new)
        before=dict(self.c.execute('SELECT * FROM settlement_lines WHERE id=?',(line['id'],)).fetchone())
        self.cancel(wrong)
        self.assertEqual(dict(self.c.execute('SELECT * FROM settlement_lines WHERE id=?',(line['id'],)).fetchone()),before)

    def test_empty_draft_removed_and_restore_does_not_reassign_funds(self):
        source=self.case('元案件',self.old,2000000,'2020-11-09')
        target=self.case('見送り',self.new,2000000)
        s=self.draft(self.old)
        line=s['lines'][0]
        self.call('r_carry',dict(target_id=target,carry_over=2000000,consent_date=self.today,consent_record='旧了承'),s['id'],line['id'])
        target_s=self.draft(self.new)
        self.cancel(target)
        self.assertFalse(self.c.execute('SELECT 1 FROM settlements WHERE id=?',(target_s['id'],)).fetchone())
        self.call('r_restore',None,target)
        self.assertEqual(self.s.incoming_carry(self.c,target,self.sid),0)
        self.assertFalse(self.c.execute('SELECT 1 FROM case_subs WHERE case_id=?',(target,)).fetchone())


if __name__=='__main__':
    unittest.main()
