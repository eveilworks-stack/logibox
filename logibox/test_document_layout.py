"""参照PDFの文字配置と、未登録会社情報の行詰めを生成PDFで検証する。"""
import base64
import io
import unittest

import fitz
from PIL import Image
import documents


def example():
    seal = io.BytesIO()
    Image.new('RGB', (80, 80), '#cc3333').save(seal, 'PNG')
    return dict(doc_no='HI-26-0002', issued_date='2026-11-20', due_date='2026-11-30',
                case_due_date='2026-11-30', amount_tax_in=43621278,
                recipient='検証下請け 御中', case_name='検証案件', mgmt_no='26-0002', case_no='SP91005',
                issuer_name='株式会社 TESTS', issuer_postal_code='100-0000',
                issuer_street_address='架空市元町1234-5-6-7', issuer_address='〒100-0000 架空市元町1234-5-6-7',
                issuer_phone='090-0000-0000', issuer_email='sample.test@example.com',
                issuer_contact_name='見本', issuer_invoice_no='T0000000000000',
                issuer_bank_branch='見本信用銀行 本店支店', issuer_bank_account='0000000',
                issuer_bank_holder='カ）テスト', issuer_bank_info='見本信用銀行 本店支店 0000000 カ）テスト',
                issuer_seal_png=base64.b64encode(seal.getvalue()).decode(), note='レイアウト確認用')


def spans(page):
    return [s for b in page.get_text('dict')['blocks'] for line in b.get('lines', []) for s in line['spans']]


class DocumentLayoutTests(unittest.TestCase):
    def test_haak_reference_typography_and_bank_rows(self):
        with fitz.open(stream=documents.haak_invoice(example()), filetype='pdf') as pdf:
            self.assertEqual(len(pdf), 1)
            text = spans(pdf[0])
            expected = [('〒 100-0000', 340, 141.89, 9.3),
                        ('架空市元町1234-5-6-7', 340, 151.89, 9.3),
                        ('TEL：090-0000-0000', 340, 161.89, 9.3),
                        ('Mail：sample.test@example.com', 340, 171.89, 8.5),
                        ('担当：見本', 340, 181.89, 9.3),
                        ('登録番号：T0000000000000', 340, 191.89, 8.5),
                        ('金融機関: 見本信用銀行 本店支店', 352, 230, 9.9),
                        ('口座番号: 0000000', 352, 242, 9.9),
                        ('口座名義: カ）テスト', 352, 254, 9.9)]
            for value, x, y, size in expected:
                found = next(s for s in text if s['text'] == value)
                self.assertAlmostEqual(found['origin'][0], x, places=2)
                self.assertAlmostEqual(found['origin'][1], y, places=2)
                self.assertAlmostEqual(found['size'], size, places=2)
            name = next(s for s in text if s['text'] == '株式会社 TESTS')
            self.assertLessEqual(name['bbox'][2], 492.01)
            self.assertIn('43,621,278', pdf[0].get_text())

    def test_missing_company_rows_close_up_in_all_documents(self):
        for typ, first in [('principal_invoice', 151.9), ('receipt', 168), ('haak_invoice', 141.89)]:
            for missing in (('postal_code',), ('street_address',), ('phone', 'email'),
                            ('postal_code', 'street_address', 'phone', 'email', 'contact_name')):
                with self.subTest(type=typ, missing=missing):
                    d = example()
                    for key in missing:
                        d['issuer_' + key] = ''
                    with fitz.open(stream=documents.render(typ, d), filetype='pdf') as pdf:
                        found = next(s for s in spans(pdf[0]) if s['text'] == '登録番号：T0000000000000')
                        self.assertAlmostEqual(found['origin'][1], first + (5-len(missing))*10, places=2)
                        self.assertEqual(len(pdf), 1)
                    d['issuer_invoice_no'] = ''
                    with fitz.open(stream=documents.render(typ, d), filetype='pdf') as pdf:
                        self.assertNotIn('登録番号：', pdf[0].get_text())

    def test_bank_rows_close_up_and_legacy_bank_text_remains(self):
        d = example()
        d['issuer_bank_account'] = ''
        with fitz.open(stream=documents.haak_invoice(d), filetype='pdf') as pdf:
            found = next(s for s in spans(pdf[0]) if s['text'] == '口座名義: カ）テスト')
            self.assertAlmostEqual(found['origin'][1], 242, places=2)
            self.assertNotIn('口座番号:', pdf[0].get_text())
        for key in ('bank_branch', 'bank_account', 'bank_holder'):
            d.pop('issuer_' + key)
        d['issuer_bank_info'] = '旧形式の銀行情報 普通 1234567 テスト'
        with fitz.open(stream=documents.haak_invoice(d), filetype='pdf') as pdf:
            self.assertIn(d['issuer_bank_info'], pdf[0].get_text())

    def test_full_company_information_does_not_overlap_seal_or_amount_box(self):
        for typ, bottom in [('haak_invoice', 200), ('principal_invoice', 218), ('receipt', 228)]:
            with self.subTest(type=typ), fitz.open(stream=documents.render(typ, example()), filetype='pdf') as pdf:
                page = pdf[0]
                seal = page.get_image_rects(page.get_images()[0][0])[0]
                for span in spans(page):
                    value = span['text']
                    if value.startswith(('株式会社 TESTS', '〒 ', '架空市', 'TEL：', 'Mail：', '担当：', '登録番号：', '発行日：')):
                        rect = fitz.Rect(span['bbox'])
                        self.assertFalse(rect.intersects(seal), value)
                        self.assertLess(rect.y1, bottom)


if __name__ == '__main__':
    unittest.main()
