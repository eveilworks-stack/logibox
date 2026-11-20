"""エムエスト・エムロジの文字付き仕入請求書から、原本に明記された値を読む。"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

import fitz as pymupdf

import calc


YEN = r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)'
DATE = r'([0-9]{4})\s*(?:年|[-/])\s*([0-9]{1,2})\s*(?:月|[-/])\s*([0-9]{1,2})(?:日|(?=\s|$))'
TOTAL_LABELS = ('今回売上額', '消費税額', '今回御請求額')
DETAIL_LABELS = ('品名', '数量', '単位', '単価', '金額', '税区分')
SUBJECT = r'件名\s*[:：]\s*(SP[0-9]+)(?:\s*【([^】]+)】)?'


@dataclass(frozen=True)
class Cell:
    left: float
    right: float
    top: float
    bottom: float
    text: str

    @property
    def x(self):
        return (self.left + self.right) / 2

    @property
    def y(self):
        return (self.top + self.bottom) / 2


def norm(text):
    return unicodedata.normalize('NFKC', text).strip()


def page_rows(page):
    cells = []
    for block in page.get_text('dict', flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)['blocks']:
        for line in block.get('lines', ()):
            text = ''.join(span['text'] for span in line['spans']).strip()
            if text:
                x0, y0, x1, y1 = line['bbox']
                cells.append(Cell(x0, x1, y0, y1, text))
    rows = []
    for cell in sorted(cells, key=lambda cell: cell.y):
        if not rows or cell.y - rows[-1][0].y > 4:
            rows.append([])
        rows[-1].append(cell)
    return [sorted(row, key=lambda cell: cell.left) for row in rows]


def numbered(text, pattern):
    match = re.fullmatch(pattern, norm(text))
    return int(match[0].replace(',', '')) if match else None


def printed_date(text, label):
    match = re.search(r'(?:' + label + r')\s*[:=]?\s*' + DATE, norm(text))
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return None


def header(rows, labels):
    for index, row in enumerate(rows):
        by_label = {norm(cell.text): cell for cell in row}
        if all(label in by_label for label in labels):
            return index, [by_label[label] for label in labels]
    return None, []


def columns(row, heads):
    # 数値が右寄せされる表。隣の見出しに近い位置を列境界にする。
    bounds = [head.x + (next_head.x - head.x) * .68 for head, next_head in zip(heads, heads[1:])]
    values = [[] for _ in heads]
    for cell in row:
        index = sum(cell.x >= bound for bound in bounds)
        values[index].append(cell.text)
    return [' '.join(value) for value in values]


def invoice_totals(rows):
    # 左側の残高や入金額があっても、見出しと同じ列の数値だけを使う。
    idx, heads = header(rows, TOTAL_LABELS)
    if idx is None:
        return None, None, None
    for row in rows[idx + 1:]:
        if row[0].y - heads[0].y > 35 or any(norm(cell.text) == '品名' for cell in row):
            break
        # 残高の列を除外してから、今回売上額・消費税額・今回御請求額に分ける。
        values = columns([cell for cell in row if cell.x >= heads[0].left - 12], heads)
        result = tuple(numbered(value, YEN) for value in values)
        if any(value is not None for value in result):
            return result
    return None, None, None


def product_items(rows):
    idx, heads = header(rows, DETAIL_LABELS)
    if idx is None:
        return []
    items = []
    for row in rows[idx + 1:]:
        if any(re.match(r'(?:お?支払期限|税抜金額|振込先|合計|計$|[※*])', norm(cell.text)) for cell in row):
            break
        name, quantity, unit, price, amount, tax = columns(row, heads)
        # 隣接した数量と単位が、作成ソフトによって連結されることがある。
        if not quantity:
            combined_quantity = re.fullmatch(rf'({YEN})\s*([^\W\d_].*)', norm(unit))
            if combined_quantity:
                quantity, unit = combined_quantity.groups()
        # 一部のPDFでは金額と税率が同じテキスト行に入っている。
        combined = re.fullmatch(rf'({YEN})\s+([0-9]+%)', norm(amount))
        if combined:
            amount, tax = combined.groups()
        if not name or not any((quantity, unit, price, amount)):
            continue
        maker = None
        parts = name.split('\u3000', 1)
        if len(parts) == 2 and parts[1].strip().startswith(parts[0].strip()):
            maker, name = (part.strip() for part in parts)
        items.append(dict(
            name=name, maker=maker, quantity=numbered(quantity, YEN), unit=unit or None,
            unit_price=numbered(price, YEN), amount_ex_tax=numbered(amount, YEN),
            tax_rate=numbered(norm(tax).removesuffix('%'), r'[0-9]+') if norm(tax).endswith('%') else None,
        ))
    return items


def bank_details(rows):
    for index, row in enumerate(rows):
        label = next((cell for cell in row if norm(cell.text) == '振込先'), None)
        if label is None:
            continue
        result = []
        for bank_row in rows[index:]:
            if bank_row[0].y - label.y > 43:
                break
            for cell in bank_row:
                if not label.right < cell.left < label.left + 95:
                    continue
                text = norm(cell.text)
                if text and not text.startswith(('※', '【')):
                    result.append(cell.text)
        return '\n'.join(result) or None
    return None


def label_value(text, pattern, group=1):
    match = re.search(pattern, text)
    return match[group].strip() if match and match[group] else None


def open_pdf(raw):
    if not raw.startswith(b'%PDF-'):
        raise ValueError('PDF形式ではありません')
    try:
        pdf = pymupdf.open(stream=raw, filetype='pdf')
        if pdf.needs_pass or not len(pdf):
            pdf.close()
            raise ValueError('パスワード付き、またはページのないPDFは読み取れません')
        return pdf
    except RuntimeError as exc:
        raise ValueError('PDFを読み取れませんでした') from exc


def read_pdf(raw):
    with open_pdf(raw) as pdf:
        if len(pdf) > 20:
            raise ValueError('PDFは20ページ以内にしてください')
        pages = [page_rows(page) for page in pdf]
    rows = [row for page in pages for row in page]
    if not rows:
        return {'status': 'image_only', 'message': '画像のみ、または抽出できる文字情報がないPDFです。仕入情報を手入力し、原本PDFを添付してください。',
                'fields': {}, 'items': [], 'warnings': ['読み取り可能な文字情報がありません']}
    text = '\n'.join(' '.join(cell.text for cell in row) for row in rows)
    search = norm(text)
    suppliers = re.findall(r'株式会社\s*(エムエスト|エムロジ)', search)
    supplier = suppliers[0] if len(set(suppliers)) == 1 else None
    totals = next((result for result in map(invoice_totals, pages) if any(value is not None for value in result)),
                  (None, None, None))
    pretax, tax, gross = totals
    items = [item for page in pages for item in product_items(page)]
    case_no = label_value(search, SUBJECT)
    warnings = []
    if supplier is None:
        warnings.append('仕入先を特定できません。原本で確認してください。')
    if gross is None:
        warnings.append('今回御請求額を特定できません。原本で確認してください。')
    if case_no is None:
        warnings.append('案件番号を特定できません。原本で確認してください。')
    if gross is not None and pretax is not None and tax is not None and gross != pretax + tax:
        warnings.append('今回売上額と原本の消費税額の合計が今回御請求額と異なります。繰越・入金の記載を確認してください。')
    if pretax is not None and items and all(item['amount_ex_tax'] is not None for item in items):
        if sum(item['amount_ex_tax'] for item in items) != pretax:
            warnings.append('商品明細の合計と今回売上額が一致しません。')
    return {
        'status': 'extracted', 'message': '読取り候補です。登録前に原本と照合してください。',
        'fields': {
            'supplier_name': supplier,
            'supplier_invoice_no': label_value(search, r'登録番号\s*:\s*(T[0-9]{13})'),
            'invoice_number': label_value(search, r'請求(?:書)?番号\s*:\s*([A-Z0-9][A-Z0-9./-]*)'),
            'case_no': case_no,
            'case_name': label_value(text, SUBJECT, 2),
            'invoice_date': printed_date(text, r'請求日'),
            'due_date': printed_date(text, r'お?支払期限'),
            'delivery_date': printed_date(text, r'【納品日】|(?<!【)納品日'),
            'amount_tax_in': gross, 'amount_ex_tax_as_printed': pretax, 'tax_as_printed': tax,
            'bank_info': next((bank for page in pages if (bank := bank_details(page))), None),
            'tax_difference_from_internal_rule': tax - calc.tax_split(gross)[1] if tax is not None and gross is not None else None,
            'bank_info': next((bank for page in pages if (bank := bank_details(page))), None),
        },
        'items': items, 'warnings': warnings,
    }

