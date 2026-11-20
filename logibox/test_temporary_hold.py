"""充当先解除・LINE承諾日だけの一時預かり・精算額と残高の回帰確認。"""
import csv
import io
import unittest

import test_carry_editing


class TemporaryHoldTests(unittest.TestCase):
    setUp = test_carry_editing.CarryEditingTests.setUp
    call = test_carry_editing.CarryEditingTests.call
    case = test_carry_editing.CarryEditingTests.case
    draft = test_carry_editing.CarryEditingTests.draft
    invoice = test_carry_editing.CarryEditingTests.invoice
    prepare = test_carry_editing.CarryEditingTests.prepare
    paid = test_carry_editing.CarryEditingTests.paid

    def hold(self, s, line, **overrides):
        body = dict(target_id='hold', carry_over=2000000, consent_date=self.today,
                    expected_revision=line['carry_revision'],
                    change_reason='充当先見送り。LINEで一時預かり了承')
        body.update(overrides)
        return self.call('r_carry', body, s['id'], line['id'])

    def test_remove_target_and_hold_with_only_line_consent_date(self):
        s, line, source, wrong, _ = self.prepare(same_month=False)
        s = self.hold(s, line)
        actual = s['lines'][0]
        self.assertTrue(actual['temporary_hold'])
        self.assertIsNone(actual['carry_to_case_id'])
        self.assertEqual(actual['consent_method'], 'LINE')
        self.assertFalse(actual['consent_record'])
        self.assertEqual(actual['payout'], 100000)
        self.assertEqual(actual['carry_over'], 2000000)
        self.assertEqual(self.s.incoming_carry(self.c, wrong, self.sid), 0)
        self.assertTrue(self.call('r_case_deletion_check', None, wrong)['can_delete'])
        self.assertIn('元金一時預かり 2,000,000円（充当先なし）', s['request_text'])
        self.assertNotIn('→ 充当先 。', s['request_text'])
        history = actual['carry_history'][0]
        self.assertEqual(history['before']['carry_to_case_id'], wrong)
        self.assertIsNone(history['after']['carry_to_case_id'])
        self.assertFalse(self.c.execute('PRAGMA foreign_key_check').fetchall())

    def test_hold_requires_actual_consent_date(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        for value in ('', '2999-01-01'):
            with self.assertRaisesRegex(self.s.Err, '承諾日'):
                self.hold(s, line, consent_date=value)
        self.assertEqual(self.s.incoming_carry(self.c, wrong, self.sid), 2000000)

    def test_paid_hold_keeps_profit_payment_and_reports_custody(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        s = self.paid(s)
        frozen = s['request_document']
        s = self.hold(s, line)
        self.assertEqual(s['status'], 'paid')
        self.assertEqual(s['total'], 100000)
        self.assertTrue(s['request_needs_revision'])
        self.assertEqual(s['request_document'], frozen)
        self.assertEqual(self.call('r_case_get', None, wrong)['subs'][0]['principal_confirmed'], 0)
        report = self.call('r_export', None, 'settlements')
        row = next(csv.DictReader(io.StringIO(report[1].decode('utf-8-sig'))))
        self.assertEqual(row['元金の取扱い'], '一時預かり')
        self.assertEqual(row['一時預かり額'], '2000000')
        report = self.call('r_export', None, 'balances', q={'at': [self.today]})
        rows = list(csv.DictReader(io.StringIO(report[1].decode('utf-8-sig'))))
        self.assertEqual(sum(int(row['金額']) for row in rows if row['区分']=='元金一時預かり'), 2000000)

    def test_paid_hold_cannot_change_retained_amount(self):
        s, line, _, _, _ = self.prepare(same_month=False)
        s = self.paid(s)
        with self.assertRaisesRegex(self.s.Err, '支払済みの充当額'):
            self.hold(s, line, carry_over=0)

    def test_partial_hold_then_release_returns_principal(self):
        s, line, _, _, _ = self.prepare(same_month=False)
        s = self.hold(s, line, carry_over=1500000)
        self.assertEqual(s['total'], 600000)
        s = self.hold(s, s['lines'][0], carry_over=0)
        self.assertFalse(s['lines'][0]['temporary_hold'])
        self.assertEqual(s['total'], 2100000)

    def test_old_target_cash_still_prevents_hold(self):
        s, line, _, wrong, _ = self.prepare(same_month=False)
        self.call('r_sub_put', dict(sub_id=self.sid, principal_tax_in=2000000,
                                  principal_received_date=self.today), wrong, 1)
        with self.assertRaisesRegex(self.s.Err, '現金入金記録'):
            self.hold(s, line)


if __name__ == '__main__':
    unittest.main()
