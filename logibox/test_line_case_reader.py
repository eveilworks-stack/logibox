"""LINE文章の解析と、登録時の重複チェックを確認する。"""
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from line_case_reader import parse_line_notice


NOTICE = """案件のご案内です。
案件番号：SP25375
案件名：化粧品スポット
開始日：2026年11月10日
納期：2026/12/01
予定利益率：7.5％
メーカー：サンプル株式会社
税込仕入限度額：15026299円
"""
EXPECTED = {
    'case_no': 'SP25375', 'name': '化粧品スポット',
    'start_date': '2026-11-10', 'due_date': '2026-12-01',
    'planned_rate_pct': 7.5, 'maker': 'サンプル株式会社',
    'purchase_limit_tax_in': 15026299,
}


LINE_REPORT = """●案件番号：SP23648
案件名：スポット 香水・オードトワレ
メーカー：ブルガリ／クロエ／ジルスチュアート／フェルナンダ／ランバン／フィアンセ／SHIRO
開始日：2026年10月8日
納期：2026年10月21日（14日間）
利益：7%
税込仕入限度額：222,772,704円"""
LINE_REPORT_EXPECTED = {
    'case_no': 'SP23648', 'name': 'スポット 香水・オードトワレ',
    'start_date': '2026-10-08', 'due_date': '2026-10-21',
    'planned_rate_pct': 7,
    'maker': 'ブルガリ／クロエ／ジルスチュアート／フェルナンダ／ランバン／フィアンセ／SHIRO',
    'purchase_limit_tax_in': 222772704,
}


class LineCaseReaderTests(unittest.TestCase):
    def test_standard_notice(self):
        self.assertEqual(parse_line_notice(NOTICE), (EXPECTED, []))

    def test_reported_line_notice(self):
        self.assertEqual(parse_line_notice(LINE_REPORT), (LINE_REPORT_EXPECTED, []))
        with_line_break_markers = LINE_REPORT.replace('\n', '\\\n')
        self.assertEqual(parse_line_notice(with_line_break_markers), (LINE_REPORT_EXPECTED, []))

    def test_profit_rate_aliases(self):
        for label in ('利益率', '利益率（％）', '予定利益', '利益', '予定利益率'):
            with self.subTest(label=label):
                fields, warnings = parse_line_notice(f'{label}：７．５％')
                self.assertEqual(fields['planned_rate_pct'], 7.5)
                self.assertEqual(warnings, [])
        fields, warnings = parse_line_notice('利益：7円')
        self.assertIsNone(fields['planned_rate_pct'])
        self.assertTrue(warnings)

    def test_date_notes_do_not_replace_invalid_or_missing_dates(self):
        fields, warnings = parse_line_notice('開始日：2026/10/8\n納期：2026/10/25（日）（14日間）')
        self.assertEqual(fields['due_date'], '2026-10-25')
        self.assertEqual(warnings, [])
        for value in ('14日間', '10月21日（14日間）', '2026年2月30日（14日間）'):
            with self.subTest(value=value):
                fields, warnings = parse_line_notice(f'開始日：2026/10/8\n納期：{value}')
                self.assertIsNone(fields['due_date'])
                self.assertTrue(warnings)

    def test_mixed_separators_and_long_manufacturer_field(self):
        makers = '／'.join([f'ブランド{i:02d}/別ブランド{i:02d}、ブランド名{i:02d}' for i in range(10)])
        self.assertGreater(len(makers), 100)
        fields, warnings = parse_line_notice(f'メーカー：{makers}、利益：7%')
        self.assertEqual(fields['maker'], makers)
        self.assertEqual(fields['planned_rate_pct'], 7)
        self.assertEqual(warnings, [])

    def test_reordered_labels_and_format_variations(self):
        notice = """■ 納期予定
【納品期限】２０２６－１２－１（月）
・メーカー名 サンプル株式会社
• 補足のメッセージ
案件名称 化粧品スポット
予定利益率（％）＝７．５　 案件Ｎｏ．：SP25375
開始予定日 2026.11.10
仕入限度額（税込）：15026299 円
"""
        self.assertEqual(parse_line_notice(notice), (EXPECTED, []))

    def test_amount_with_commas_and_zero_values(self):
        fields, warnings = parse_line_notice("税込仕入上限額：￥１５，０２６，２９９円\n予定利益率：０％")
        self.assertEqual(fields['purchase_limit_tax_in'], 15026299)
        self.assertEqual(fields['planned_rate_pct'], 0)
        self.assertEqual(warnings, [])

    def test_missing_values_remain_blank(self):
        fields, warnings = parse_line_notice("案件名：ＡＢＣ　限定品\nメーカー：未定\n11/20に連絡します。\n連絡先：担当者")
        self.assertEqual(fields, {key: 'ＡＢＣ　限定品' if key == 'name' else None for key in EXPECTED})
        self.assertEqual(warnings, [])

    def test_invalid_dates_and_amounts(self):
        for amount in ('12,34円', '-100円', '1.5円', '約100万円', '1000000000001円'):
            with self.subTest(amount=amount):
                fields, warnings = parse_line_notice(
                    f"開始日：2026/2/29\n納期：12/01\n税込仕入限度額：{amount}\n予定利益率：101%"
                )
                self.assertTrue(all(value is None for value in fields.values()))
                self.assertEqual(len(warnings), 4)

    def test_conflicting_and_empty_inline_fields(self):
        fields, warnings = parse_line_notice(
            "案件番号：SP111、案件番号：SP222\n開始日：　納期：2028/2/29"
        )
        self.assertIsNone(fields['case_no'])
        self.assertIsNone(fields['start_date'])
        self.assertEqual(fields['due_date'], '2028-02-29')
        self.assertEqual(len(warnings), 1)


class LineCaseApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = tempfile.TemporaryDirectory()
        cls.addClassCleanup(data.cleanup)
        with patch.dict(os.environ, {'APP_DATA': data.name}):
            import server
        server.init()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        cls.addClassCleanup(httpd.server_close)
        server.H.loopback = True
        server.H.port = httpd.server_port
        cls.url = f'http://127.0.0.1:{httpd.server_port}'
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        cls.addClassCleanup(worker.join)
        cls.addClassCleanup(httpd.shutdown)
        cls.cookie = ''
        status, _, cookies = cls.api('POST', '/api/setup', {'password': 'test-password'})
        if status != 200:
            raise RuntimeError('Test server setup failed')
        cls.cookie = cookies.split(';')[0]

    @classmethod
    def api(cls, method, path, body=None, *, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(cls.url + path, data, {
            'Content-Type': 'application/json',
            'X-Requested-With': 'app',
            'Cookie': cls.cookie if auth else '',
        }, method=method)
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response), response.headers.get('Set-Cookie')

    def test_preview_is_read_only_and_duplicate_is_blocked(self):
        path = '/api/cases/line-preview'
        self.assertEqual(self.api('POST', path, {'text': NOTICE}, auth=False)[0], 401)
        self.assertEqual(self.api('POST', path, {'text': '　'})[0], 400)
        before = self.api('GET', '/api/cases')[1]
        status, preview, _ = self.api('POST', path, {'text': NOTICE})
        self.assertEqual(status, 200)
        self.assertEqual(preview['fields'], EXPECTED)
        self.assertIsNone(preview['duplicate'])
        self.assertEqual(self.api('GET', '/api/cases')[1], before)

        fields = {**preview['fields'], 'name': '確認後に修正した案件名'}
        status, saved, _ = self.api('POST', '/api/cases', fields)
        self.assertEqual(status, 200)
        case_path = f"/api/cases/{saved['id']}"
        stored = self.api('GET', case_path)[1]['case']
        self.assertEqual({key: stored[key] for key in fields}, fields)

        alternate = NOTICE.replace('SP25375', 'ＳＰ２５３７５')
        preview = self.api('POST', path, {'text': alternate})[1]
        self.assertIn(saved['mgmt_no'], preview['duplicate'])
        status, error, _ = self.api('POST', '/api/cases', preview['fields'])
        self.assertEqual(status, 409)
        self.assertEqual(error['error'], preview['duplicate'])
        self.assertEqual(self.api('PUT', case_path, fields)[0], 200)

        other = {**fields, 'case_no': 'SP99999'}
        other_id = self.api('POST', '/api/cases', other)[1]['id']
        self.assertEqual(self.api('PUT', f'/api/cases/{other_id}', fields)[0], 409)
        self.assertEqual(self.api('POST', case_path + '/trash')[0], 200)
        self.assertIn('削除ボックス内', self.api('POST', path, {'text': NOTICE})[1]['duplicate'])

    def test_reported_notice_and_long_manufacturer_save(self):
        status, preview, _ = self.api('POST', '/api/cases/line-preview', {'text': LINE_REPORT})
        self.assertEqual(status, 200)
        self.assertEqual(preview['fields'], LINE_REPORT_EXPECTED)
        status, saved, _ = self.api('POST', '/api/cases', preview['fields'])
        self.assertEqual(status, 200)
        case_path = f"/api/cases/{saved['id']}"
        stored = self.api('GET', case_path)[1]['case']
        self.assertEqual({key: stored[key] for key in LINE_REPORT_EXPECTED}, LINE_REPORT_EXPECTED)
        maker = stored['maker'] + '／' + '、'.join(f'追加ブランド{i:02d}' for i in range(20))
        self.assertGreater(len(maker), 100)
        status, _, _ = self.api('PUT', case_path, {**preview['fields'], 'maker': maker})
        self.assertEqual(status, 200)
        self.assertEqual(self.api('GET', case_path)[1]['case']['maker'], maker)

    def test_concurrent_duplicate_requests(self):
        body = {'case_no': 'RACE-1', 'name': '同時登録の検証', 'planned_rate_pct': 7}

        def register():
            return self.api('POST', '/api/cases', body)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(register), pool.submit(register)
            self.assertEqual(sorted([first.result(), second.result()]), [200, 409])


if __name__ == '__main__':
    unittest.main()

