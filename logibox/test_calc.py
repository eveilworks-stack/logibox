import unittest
from calc import *


class T(unittest.TestCase):
    def test_haak_planned(self):
        self.assertEqual(haak_planned_amount(35017990, 7), 37469249)
        self.assertEqual(haak_planned_amount(35017990, 8), 37819429)

    def test_sub_amounts(self):
        self.assertEqual(sub_fee(2000000), 40000)
        self.assertEqual(sub_profit(2000000, 7), 100000)

    def test_rounding_directions(self):
        self.assertEqual(sub_fee(1234567), 24691)
        self.assertEqual(sub_profit(1234567, 7), 61729)

    def test_tax_split(self):
        self.assertEqual(tax_split(2000000), (1818182, 181818))
        self.assertEqual(tax_split(2100000), (1909091, 190909))

    def test_case_profit(self):
        r = case_profit(37819429, 35017990, [{'principal': 2000000}], 7)
        self.assertEqual(r['total_profit'], 2801439)
        self.assertEqual(r['sub_profit_total'], 100000)
        self.assertEqual(r['own_profit'], 2701439)

    def test_payout_and_carry_over(self):
        self.assertEqual(sub_payout(2000000, 100000), 2100000)
        self.assertEqual(sub_payout(2000000, 100000, 2000000), 100000)

    def test_confirmed_amount_does_not_move_sub_profit(self):
        a = case_profit(37469249, 35017990, [{'principal': 2000000}], 7)
        b = case_profit(37819429, 35017990, [{'principal': 2000000}], 7)
        self.assertEqual(a['sub_profit_total'], b['sub_profit_total'])
        self.assertEqual(b['own_profit'] - a['own_profit'], 350180)


unittest.main()
