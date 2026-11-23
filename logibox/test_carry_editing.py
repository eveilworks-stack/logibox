"""元金充当訂正・監査履歴・同月の連続充当・削除制御の回帰テスト。"""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import csv
import io
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_workflow


class CarryEditingTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    call = test_workflow.WorkflowTests.call
    def case(self,*args,**kwargs):
        cid=test_workflow.WorkflowTests.case(self,*args,**kwargs)
        sub=self.c.execute('SELECT * FROM case_subs WHERE case_id=?',(cid,)).fetchone()
        if sub['principal_received_date']:
            self.call('r_haak_put',dict(confirmed_amount_tax_in=sub['principal_tax_in']*2,
                confirmed_checked=True,paid_date=self.today),cid)
        return cid
    draft = test_workflow.WorkflowTests.draft
    invoice = test_workflow.WorkflowTests.invoice

    def prepare(self, same_month=True):
        source = self.case('検証用商品A・商品B', self.old, 2000000, '2020-11-09')
        wrong = self.case('ディワリ案件', self.new, 2000000)
        correct = self.case('検証用差し替え案件', self.old if same_month else self.new, 2000000)
        self.c.execute('UPDATE cases SET due_date=? WHERE id=?',
                       (self.old+'-29' if same_month else self.new+'-29', correct))
        self.c.commit()
        s = self.draft(self.old)
        # 同月の充当先明細も同じ月次精算へ登録される。
        line = next(item for item in s['lines'] if item['case_id'] == source)
        body = dict(carry_over=2000000, target_id=wrong, consent_date='2020-11-21', consent_record='10/21')
        s = self.call('r_carry', body, s['id'], line['id'])
        line = next(item for item in s['lines'] if item['case_id'] == source)
        return s, line, source, wrong, correct

    def change(self, s, line, target, **overrides):
        body = dict(carry_over=line['carry_over'], target_id=target, consent_date=line['consent_date'],
                    consent_record=line['consent_record'], expected_revision=line['carry_revision'],
                    change_reason='充当先の選択誤りを訂正')
        body.update(overrides)
        return self.call('r_carry', body, s['id'], line['id'])

    def paid(self, s):
        self.call('r_settlement_status', {'status': 'requested'}, s['id'])
        self.call('r_sub_invoice', self.invoice(s['total']), s['id'])
        return self.call('r_settlement_status', {'status': 'paid', 'paid_at': self.today}, s['id'])

    def test_existing_line_retarget_preserves_all_financial_values(self):
        s, line, source, wrong, correct = self.prepare()
        count = self.c.execute('SELECT count(*) FROM settlement_lines').fetchone()[0]
        updated = self.change(s, line, correct)
        actual = next(item for item in updated['lines'] if item['id'] == line['id'])
        self.assertEqual(actual['carry_to_case_id'], correct)
        for key in ('principal', 'sub_profit', 'carry_over', 'consent_date', 'consent_record', 'principal_received_date'):
            self.assertEqual(actual[key], line[key], key)
        self.assertEqual(self.c.execute('SELECT count(*) FROM settlement_lines').fetchone()[0], count)
        self.assertEqual(self.s.incoming_carry(self.c, wrong, self.sid), 0)
        self.assertEqual(self.s.incoming_carry(self.c, correct, self.sid), 2000000)
        history = actual['carry_history'][0]
        self.assertEqual(history['before']['carry_to_case_id'], wrong)
        self.assertEqual(history['after']['carry_to_case_id'], correct)
        self.assertEqual(history['reason'], '充当先の選択誤りを訂正')
        with closing(sqlite3.connect(Path(self.s.DBP).parent / history['backup_file'])) as before:
            self.assertEqual(before.execute('SELECT carry_to_case_id FROM settlement_lines WHERE id=?', (line['id'],)).fetchone()[0], wrong)
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_same_month_chain_can_be_requested_paid_without_duplicate_cash(self):
        s, line, source, wrong, correct = self.prepare()
        s = self.change(s, line, correct)
        target = next(item for item in s['lines'] if item['case_id'] == correct)
        self.assertEqual(target['principal_confirmed'], 0)
        self.assertEqual(target['principal_available'], 2000000)
        self.assertEqual(s['total'], 2200000)
        s = self.paid(s)
        self.assertEqual(s['status'], 'paid')
        self.assertEqual(self.call('r_case_get', None, correct)['subs'][0]['principal_confirmed'], 2000000)
        report = self.call('r_export', None, 'transactions')
        rows = list(csv.DictReader(io.StringIO(report[1].decode('utf-8-sig'))))
        cash = [row for row in rows if row['区分'] == '下請け元金（現金）' and row['入出金日']]
        self.assertEqual(sum(int(row['入出金額']) for row in cash), 2000000)
        self.assertEqual(sum(int(row['入出金額']) for row in rows if row['区分'] == '下請け精算'), 2200000)

    def test_second_source_cannot_double_allocate_target_capacity(self):
        s, line, _, _, correct = self.prepare()
        self.change(s, line, correct)
        source2 = self.case('別の充当元', self.old, 2000000, '2020-11-09')
        s = self.draft(self.old)
        line2 = next(item for item in s['lines'] if item['case_id'] == source2)
        with self.assertRaisesRegex(self.s.Err, '担当元金を超えています'):
            self.change(s, line2, correct, carry_over=2000000, consent_date='2020-11-21', consent_record='承諾')
        self.assertEqual(self.s.incoming_carry(self.c, correct, self.sid), 2000000)
        self.assertEqual(self.c.execute('SELECT carry_over FROM settlement_lines WHERE id=?', (line2['id'],)).fetchone()[0], 0)

    def test_self_and_backwards_carry_are_rejected(self):
        s, line, source, _, correct = self.prepare()
        for target in (source,):
            with self.assertRaisesRegex(self.s.Err, '納期が後'):
                self.change(s, line, target)
        s = self.change(s, line, correct)
        target_line = next(item for item in s['lines'] if item['case_id'] == correct)
        with self.assertRaisesRegex(self.s.Err, '入金確認済み'):
            self.change(s, target_line, source, carry_over=2000000, consent_date='2020-11-21', consent_record='逆向き')

    def test_paid_retarget_keeps_original_pdfs_and_request_snapshot(self):
        s, line, source, wrong, correct = self.prepare(same_month=False)
        s = self.paid(s)
        frozen = self.c.execute('SELECT request_snapshot_json FROM settlements WHERE id=?', (s['id'],)).fetchone()[0]
        files = {str(path): path.read_bytes() for path in Path(self.s.DOCS).glob('*.pdf')}
        updated = self.change(s, line, correct)
        self.assertEqual(updated['status'], 'paid')
        self.assertEqual(updated['paid_at'], s['paid_at'])
        self.assertTrue(updated['request_needs_revision'])
        self.assertEqual(self.c.execute('SELECT request_snapshot_json FROM settlements WHERE id=?', (s['id'],)).fetchone()[0], frozen)
        self.assertEqual(files, {str(path): path.read_bytes() for path in Path(self.s.DOCS).glob('*.pdf')})
        self.assertEqual(self.call('r_case_get', None, wrong)['subs'][0]['principal_confirmed'], 0)
        self.assertEqual(self.call('r_case_get', None, correct)['subs'][0]['principal_confirmed'], 2000000)
        current_line = next(item for item in updated['lines'] if item['id'] == line['id'])
        with self.assertRaisesRegex(self.s.Err, '支払済みの充当額'):
            self.change(updated, current_line, correct, carry_over=1900000)

    def test_unpaid_amount_change_rechecks_received_invoice(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        self.call('r_settlement_status', {'status': 'requested'}, s['id'])
        self.call('r_sub_invoice', self.invoice(100000), s['id'])
        updated = self.change(s, line, wrong, carry_over=1500000)
        self.assertEqual(updated['total'], 600000)
        self.assertEqual(updated['status'], 'mismatch')
        with self.assertRaises(self.s.Err):
            self.call('r_settlement_status', {'status': 'paid', 'paid_at': self.today}, s['id'])

    def test_metadata_edit_allowed_but_retarget_blocked_if_old_target_has_cash(self):
        s, line, _, wrong, correct = self.prepare()
        self.call('r_sub_put', {'sub_id': self.sid, 'principal_tax_in': 2000000,
                               'principal_received_date': self.today}, wrong, 1)
        s = self.change(s, line, wrong, consent_record='承諾記録の訂正')
        changed = next(item for item in s['lines'] if item['id'] == line['id'])
        with self.assertRaisesRegex(self.s.Err, '現金入金記録'):
            self.change(s, changed, correct)

    def test_downstream_allocation_prevents_withdrawing_its_principal(self):
        s, line, _, wrong, correct = self.prepare(same_month=False)
        self.paid(s)
        self.call('r_haak_put',dict(confirmed_amount_tax_in=3000000,confirmed_checked=True,paid_date=self.today),wrong)
        future = self.case('次の次の充当先', '2021-01', 2000000)
        next_s = self.draft(self.new)
        next_line = next(item for item in next_s['lines'] if item['case_id'] == wrong)
        self.change(next_s, next_line, future, carry_over=2000000, consent_date='2020-11-21', consent_record='次への承諾')
        with self.assertRaisesRegex(self.s.Err, '次の充当元'):
            self.change(s, line, correct)

    def test_stale_revision_and_noop_do_not_add_history(self):
        s, line, _, wrong, correct = self.prepare()
        history_count = self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0]
        self.change(s, line, wrong)
        self.assertEqual(self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0], history_count)
        self.change(s, line, correct)
        with self.assertRaisesRegex(self.s.Err, '別の操作'):
            self.change(s, line, wrong)
        self.assertEqual(self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0], history_count+1)

    def test_backup_failure_leaves_record_and_history_untouched(self):
        s, line, _, wrong, correct = self.prepare()
        history_count = self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0]
        with patch.object(self.s.carry_management, 'backup_database', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(self.s.Err, 'バックアップに失敗'):
                self.change(s, line, correct)
        self.assertEqual(self.c.execute('SELECT carry_to_case_id FROM settlement_lines WHERE id=?', (line['id'],)).fetchone()[0], wrong)
        self.assertEqual(self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0], history_count)

    def test_old_target_can_be_deleted_after_references_move_and_audit_is_retained(self):
        s, line, source, wrong, correct = self.prepare()
        check = self.call('r_case_deletion_check', None, wrong)
        self.assertFalse(check['can_delete'])
        self.assertIn('元金充当', check['reasons'][0])
        with self.assertRaisesRegex(self.s.Err, '元金充当'):
            self.call('r_trash_move', None, wrong)
        self.change(s, line, correct)
        self.assertTrue(self.call('r_case_deletion_check', None, wrong)['can_delete'])
        number = self.c.execute('SELECT mgmt_no FROM cases WHERE id=?', (wrong,)).fetchone()[0]
        self.call('r_trash_move', None, wrong)
        self.call('r_purge', {'confirm': number}, wrong)
        self.assertFalse(self.c.execute('SELECT 1 FROM cases WHERE id=?', (wrong,)).fetchone())
        self.assertEqual(self.s.carry_management.change_history(self.c, line['id'])[0]['before']['carry_to_case_id'], wrong)
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_other_related_financial_records_are_named_in_japanese(self):
        s, line, _, wrong, correct = self.prepare()
        self.change(s, line, correct)
        self.call('r_purchase_new', {'supplier_id': 1, 'amount_tax_in': 1000}, wrong)
        check = self.call('r_case_deletion_check', None, wrong)
        self.assertFalse(check['can_delete'])
        self.assertTrue(any('仕入#' in reason and '1,000円' in reason for reason in check['reasons']))
        with self.assertRaisesRegex(self.s.Err, '仕入#'):
            self.call('r_trash_move', None, wrong)

    def test_audit_is_append_only_and_not_reused_after_draft_deletion(self):
        s, line, source, wrong, correct = self.prepare()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE carry_change_history SET reason='changed'")
        self.c.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute('DELETE FROM carry_change_history')
        self.c.rollback()
        with self.assertRaisesRegex(self.s.Err, '元金充当'):
            self.call('r_settlement_delete', None, s['id'])
        self.change(s, line, wrong, carry_over=0)
        self.call('r_settlement_delete', None, s['id'])
        new = self.draft(self.old)
        self.assertEqual(len(new['lines'][0]['carry_history']), 0)
        self.assertNotEqual(new['lines'][0]['carry_uid'], line['carry_uid'])
        self.assertGreater(self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0], 0)

    def test_concurrent_edit_only_accepts_one_matching_revision(self):
        s, line, _, wrong, correct = self.prepare()
        other_target = self.case('もう一つの充当先', self.new, 2000000)
        def edit(target):
            with closing(self.s.db()) as conn:
                try:
                    self.s.r_carry(conn, dict(carry_over=2000000, target_id=target,
                        consent_date=line['consent_date'], consent_record='承諾', expected_revision=line['carry_revision']),
                        {}, str(s['id']), str(line['id']))
                    conn.commit()
                    return 'updated'
                except self.s.Err as exc:
                    conn.rollback()
                    return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, (correct, other_target)))
        self.assertCountEqual(results, ['updated', 409])
        self.assertEqual(self.c.execute('SELECT carry_revision FROM settlement_lines WHERE id=?', (line['id'],)).fetchone()[0], line['carry_revision']+1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM carry_change_history').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
