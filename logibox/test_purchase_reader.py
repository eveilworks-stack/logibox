"""両社の書式と、請求額・期日・商品明細を検証する。"""
import glob
import os
from pathlib import Path
import unittest

import fitz

from purchase_reader import read_pdf


def synthetic_invoice(supplier='エムロジ', carry_over=False, missing_gross=False, case_no=None):
    """取引先の実データを配布せず、両社の列配置と文字の格納順を再現する。"""
    is_logi = supplier == 'エムロジ'
    case_no = case_no or ('SP91006' if is_logi else 'SP91001')
    net, tax = (13660272, 1366027) if is_logi else (31834536, 3183454)
    gross = net + tax + (999 if carry_over else 0)
    with fitz.open() as doc:
        page = doc.new_page()
        def put(x, y, text):
            page.insert_text((x, y), text, fontname='japan', fontsize=8)
        put(329, 128, '株式会社' + supplier)
        put(293, 64, '請求日：2026年11月9日' if is_logi else '請求日：2026年10月8日')
        put(32, 185, f'件名：{case_no}【検証用案件】')
        totals = [
            (48, 95, '前回請求残高', '999'), (140, 185, '今回入金額', '0'),
            (231, 280, '繰越残高', '999'),
            (320, 334, '今回売上額', f'{net:,}'), (411, 417, '消費税額', f'{tax:,}'),
            (489, 503, '今回御請求額', '' if missing_gross else f'{gross:,}'),
        ]
        for header_x, _, label, _ in totals:
            put(header_x, 209, label)
        for _, value_x, _, value in reversed(totals):
            if value:
                put(value_x, 231, value)
        for x, label in zip((171, 345, 382, 425, 493, 540), ('品名', '数量', '単位', '単価', '金額', '税区分')):
            put(x, 256, label)
        detail = (
            [(342, 37176, 12714192), (20, 47304, 946080)] if is_logi else
            [(80, 76560, 6124800), (74, 61248, 4532352), (49, 90336, 4426464),
             (64, 59856, 3830784), (82, 61248, 5022336), (61, 60840, 3711240), (56, 74760, 4186560)]
        )
        for i, (qty, price, amount) in enumerate(detail):
            y = 273 + 17 * i
            for x, val in ((364, str(qty)), (385, 'Lot'), (433, f'{price:,}'), (495, f'{amount:,}'), (543, '10%')):
                put(x, y, val)
        for i, _ in enumerate(detail):
            sample = '検証商品' if is_logi else 'サンプル　サンプル商品'
            put(32, 273 + 17 * i, f'{sample}{i + 1}')
        put(32, 714, '支払期限')
        put(94, 714, '2026年11月10日' if is_logi else '2026年10月9日')
        put(360, 714, '税抜金額')
        put(32, 727, '振込先')
        put(94, 727, '検証銀行　検証支店')
        put(94, 740, '普通　0000009')
        put(94, 753, 'カ）テスト')
        if is_logi:
            put(40, 770, '【納品日】2026年11月17日')
            put(470, 23, '請求番号：ML70116409')
        return doc.tobytes()


class PurchaseReaderTests(unittest.TestCase):
    def test_reject_non_pdf(self):
        with self.assertRaises(ValueError):
            read_pdf(b'not-a-pdf')

    def test_image_only_pdf(self):
        with fitz.open() as doc:
            doc.new_page()
            result = read_pdf(doc.tobytes())
        self.assertEqual(result['status'], 'image_only')
        self.assertFalse(result['fields'])

    def verify_invoice(self, raw, supplier, real=False):
        is_logi = supplier == 'エムロジ'
        expected = {
            'supplier_name': supplier,
            'case_no': 'SP91006' if is_logi else 'SP91001',
            'invoice_number': 'ML70116409' if is_logi else None,
            'invoice_date': '2026-11-09' if is_logi else '2026-10-08',
            'due_date': '2026-11-10' if is_logi else '2026-10-09',
            'delivery_date': '2026-11-17' if is_logi else None,
            'amount_tax_in': 15026299 if is_logi else 35017990,
            'amount_ex_tax_as_printed': 13660272 if is_logi else 31834536,
            'tax_as_printed': 1366027 if is_logi else 3183454,
        }
        result = read_pdf(raw)
        fields = result['fields']
        self.assertEqual({key: fields[key] for key in expected}, expected)
        self.assertFalse(result['warnings'])
        self.assertTrue(fields['case_name'])
        self.assertIn('銀行', fields['bank_info'])
        self.assertEqual(len(fields['bank_info'].splitlines()), 3)
        self.assertEqual(len(result['items']), 2 if is_logi else 7)
        first = result['items'][0]
        numbers = (342, 37176, 12714192) if is_logi else (80, 76560, 6124800)
        self.assertEqual(tuple(first[key] for key in ('quantity', 'unit_price', 'amount_ex_tax')), numbers)
        self.assertEqual(first['unit'], 'Lot')
        self.assertEqual(first['tax_rate'], 10)
        if real:
            self.assertIn('ハウス オブ ローゼ' if is_logi else 'ブルガリ', first['name'])
            self.assertEqual(first['maker'], None if is_logi else 'ブルガリ')
        else:
            self.assertEqual(first['maker'], None if is_logi else 'サンプル')
            self.assertIn('0000009', fields['bank_info'])

    def test_emest_layout(self):
        self.verify_invoice(synthetic_invoice('エムエスト'), 'エムエスト')

    def test_emlogi_layout(self):
        self.verify_invoice(synthetic_invoice(), 'エムロジ')

    def test_totals_with_carry_over_and_missing_gross(self):
        with_carry = read_pdf(synthetic_invoice(carry_over=True))
        self.assertEqual(with_carry['fields']['amount_tax_in'], 15027298)
        self.assertEqual(with_carry['fields']['tax_as_printed'], 1366027)
        self.assertTrue(with_carry['warnings'])
        missing = read_pdf(synthetic_invoice(missing_gross=True))
        self.assertIsNone(missing['fields']['amount_tax_in'])
        self.assertEqual(missing['fields']['amount_ex_tax_as_printed'], 13660272)

    def verify_real_pdf(self, supplier, variable, pattern):
        supplied = os.environ.get(variable)
        if not supplied:
            matches = glob.glob(str(Path(__file__).resolve().parent.parent / pattern))
            if not matches:
                self.skipTest(variable + ' に検証用PDFを指定してください')
            supplied = matches[0]
        self.verify_invoice(Path(supplied).read_bytes(), supplier, real=True)

    def test_emest_real_pdf(self):
        self.verify_real_pdf('エムエスト', 'PHASE2_EMEST_PDF', '*20261009*SP91001*.pdf')

    def test_emlogi_real_pdf(self):
        self.verify_real_pdf('エムロジ', 'PHASE2_EMLOGI_PDF', '2026年11月10日*SP91006*.pdf')


if __name__ == '__main__':
    unittest.main()

