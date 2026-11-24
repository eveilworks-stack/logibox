"""フェーズ2帳票生成。元の請求書PDFを背景に利用し、計算はcalc.pyに委譲。"""
import io
import base64
import os
import unicodedata
import fitz
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import calc
import company_seal

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(BASE, 'document_templates')
FONT = 'BIZUDPGothic'
embedded_font = TTFont(FONT, os.path.join(BASE, 'fonts', 'BIZUDPGothic-Regular.ttf'))
pdfmetrics.registerFont(embedded_font)
BOLD = 'BIZUDPGothic-Bold'
pdfmetrics.registerFont(TTFont(BOLD, os.path.join(BASE, 'fonts', 'BIZUDPGothic-Bold.ttf')))
DIGIT_WIDTH = max(pdfmetrics.stringWidth(digit, FONT, 1) for digit in '0123456789')
W, H = A4
BLUE = '#385B95'
PALE = '#DCE5F5'
INK = '#1E2736'

def _text(value):
    text = unicodedata.normalize('NFC', str(value if value is not None else ''))
    text = text.translate(str.maketrans({'\n': ' ', '\r': ' ', '\t': ' '})).strip()
    for character in text:
        if not embedded_font.face.charToGlyph.get(ord(character)):
            raise ValueError(f'帳票用フォントで表示できない文字があります。「{character}」を確認してください。')
    return text


def _fit(value, size, width, font=FONT):
    text = _text(value)
    if width is None or not text:
        return text, size
    measured = pdfmetrics.stringWidth(text, font, size)
    if measured <= width:
        return text, size
    size = max(7, width * size / measured)
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text, size
    ellipsis = '…'
    prefix = _wrap(text, width - pdfmetrics.stringWidth(ellipsis, FONT, size), size)[0]
    return prefix + ellipsis, size


def _draw(c, x, top, value, size=9, *, width=None, font=FONT):
    text, size = _fit(value, size, width, font)
    c.setFont(font, size)
    c.setFillColor(INK)
    c.drawString(x, H-top, text)


def _right(c, x, top, value, size=9, *, width=None):
    text, size = _fit(value, size, width)
    c.setFont(FONT, size)
    c.setFillColor(INK)
    c.drawRightString(x, H-top, text)


def _amount(c, right, top, value, size=9, *, width=None, unit='', prefix='', font=FONT):
    text = prefix + _money(value) + unit
    digit_width = max(pdfmetrics.stringWidth(ch, font, 1) for ch in '0123456789')
    advances = [digit_width if char in '0123456789' else pdfmetrics.stringWidth(char, font, 1) for char in text]
    total = sum(advances)
    if width is not None:
        size = min(size, width / total)
    line = c.beginText(right - total * size, H-top)
    line.setFont(font, size)
    line.setFillColor(INK)
    for char, advance in zip(text, advances):
        actual = pdfmetrics.stringWidth(char, font, 1)
        line.setCharSpace((advance - actual) * size)
        line.textOut(char)
    c.drawText(line)


def _wrap(text, width, size):
    lines = []
    while text:
        used = 0
        end = 0
        last_space = 0
        for char in text:
            used += pdfmetrics.stringWidth(char, FONT, size)
            if used > width and end:
                break
            end += 1
            if char.isspace():
                last_space = end
        if end < len(text) and last_space > end * .7:
            end = last_space
        lines.append(text[:end].rstrip())
        text = text[end:].lstrip()
    return lines


def _paragraph(c, x, top, value, width, size, leading, limit):
    lines = _wrap(_text(value), width, size)
    if len(lines) > limit:
        joined = ' '.join(lines[limit - 1:])
        ellipsis_width = pdfmetrics.stringWidth('…', FONT, size)
        lines = lines[:limit - 1] + [_wrap(joined, width - ellipsis_width, size)[0] + '…']
    for index, line in enumerate(lines):
        _draw(c, x, top + index * leading, line, size)


def _overlay(background, draw, continuation=()):
    stream = io.BytesIO()
    can = canvas.Canvas(stream, pagesize=A4, initialFontName=FONT)
    for paint in (draw, *continuation):
        paint(can)
        can.showPage()
    can.save()
    with fitz.open(os.path.join(TEMPLATES, background)) as template, \
            fitz.open(stream=stream.getvalue(), filetype='pdf') as added:
        for _ in range(len(template), len(added)):
            template.fullcopy_page(0)
        for index in range(len(added)):
            page = template[index]
            page.show_pdf_page(page.rect, added, index, overlay=True)
        return template.tobytes(garbage=4, deflate=True)


def _principal_item_lines(value):
    for name in str(value or '').splitlines():
        yield from _wrap(_text(name), 290, 8)


def _principal_item_rows(c, lines):
    for index, line in enumerate(lines):
        # 既存の罫線に合わせて、案件名の下の明細行を使う。
        _draw(c, 55, 280.5 + index * 20.24, line, 8)


def _money(v):
    return f'{int(v):,}'

def _date(s):
    if not s: return ''
    y,m,d = s.split('-')
    return f'{y}年{int(m):02d}月{int(d):02d}日'

SEAL_SIZE = 21 * 72 / 25.4  # 21 mm, PDF coordinates are points.


def _seal(c, data, x, top, size=SEAL_SIZE, baseline=None):
    encoded = data.get('issuer_seal_png')
    if encoded:
        reader = ImageReader(io.BytesIO(base64.b64decode(company_seal.encoded_png(encoded), validate=True)))
        width, height = reader.getSize()
        scale = size / max(width, height)
        width, height = width * scale, height * scale
        if baseline is not None and top + height < baseline + 2:
            top = baseline + 2 - height
        c.drawImage(reader, x, H-top-height, width, height, mask='auto')


def _issuer_rows(d):
    """未入力の会社情報は行自体を作らず、後続の項目を詰める。"""
    rows = [
        (d.get('issuer_postal_code'), '〒 ', 9.3),
        (d.get('issuer_street_address', d.get('issuer_address')), '', 9.3),
        (d.get('issuer_phone'), 'TEL：', 9.3),
        (d.get('issuer_email'), 'Mail：', 8.5),
        (d.get('issuer_contact_name'), '担当：', 9.3),
        (d.get('issuer_invoice_no'), '登録番号：', 8.5),
    ]
    # 旧形式の帳票コンテキストにも対応する。
    if not any(d.get('issuer_' + key) for key in ('phone', 'email', 'contact_name')):
        rows.insert(-1, (d.get('issuer_contact'), '', 9.3))
    return [(prefix + str(value).strip(), size) for value, prefix, size in rows
            if value is not None and str(value).strip()]


def _company_rows(c, d, x, top, width=211):
    for index, (value, size) in enumerate(_issuer_rows(d)):
        baseline = top + index * 10
        safe_width = width
        if d.get('issuer_seal_png') and top == 141.89 and baseline - size < 90 + SEAL_SIZE:
            safe_width = min(width, 496-x-4)
        _draw(c, x, baseline, value, size, width=safe_width)


def _company(c, data, x=340, top=124.65):
    # 見本PDFと同じく、社名右端の約4ptに社印を重ねる。
    right = 500 if data.get('issuer_seal_png') else 557
    name, size = _fit(data.get('issuer_name'), 12, right-x, BOLD)
    _draw(c, right-pdfmetrics.stringWidth(name, BOLD, size), top, name, size, font=BOLD)
    _seal(c, data, 496, 90, baseline=top)
    _company_rows(c, data, x, 141.89, width=217)


def _haak_bank(c, d):
    rows = [(d.get('issuer_bank_branch'), '金融機関: '),
            (d.get('issuer_bank_account'), '口座番号: '),
            (d.get('issuer_bank_holder'), '口座名義: ')]
    values = [prefix + str(value).strip() for value, prefix in rows
              if value is not None and str(value).strip()]
    if values:
        for index, value in enumerate(values):
            _draw(c, 352, 230 + index * 12, value, 9.9, width=195)
    else:
        _paragraph(c, 352, 230, d.get('issuer_bank_info'), 195, 9.9, 12, 3)

def _issuer_name(c, d, x, top, right):
    name, size = _fit(d.get('issuer_name'), 12, right-x, BOLD)
    _draw(c, right-pdfmetrics.stringWidth(name, BOLD, size), top, name, size, font=BOLD)


def _principal_company(c, d):
    # PI-26-0001-1 reference positions, in PDF points.
    _issuer_name(c, d, 402.6, 126.7, 504.5 if d.get('issuer_seal_png') else 559.6)
    _seal(c, d, 500.5, 82.5, baseline=126.7)
    _company_rows(c, d, 347.8, 151.9)


def _principal_grid(c):
    # Repaint only the table; retain the supplied PDF template unchanged.
    left, right, top, bottom = 20, 563, 218, 610
    edges = [20, 46, 350, 405, 484, 563]
    c.setFillColorRGB(1, 1, 1)
    c.rect(left, H-bottom, right-left, bottom-top, fill=1, stroke=0)
    c.setFillColor(BLUE)
    c.rect(left, H-246, right-left, 28, fill=1, stroke=0)
    for i in range(18):
        y = 246 + i * 20.24
        c.setFillColor(PALE if i % 2 else '#FFFFFF')
        c.rect(left, H-y-20.24, right-left, 20.24, fill=1, stroke=0)
    c.setStrokeColor('#8CA3C7'); c.setLineWidth(.5)
    for x in edges:
        c.line(x,H-246,x,H-bottom)
    for i in range(19):
        y=min(bottom,246+i*20.24)
        c.line(left,H-y,right,H-y)
    c.setFillColorRGB(1,1,1);c.setFont(FONT,11.33)
    for x,label in [(25,'No.'),(49.3,'商品名 ／ 品名'),(364,'数量'),(430,'単価'),(511,'金額')]:
        c.drawString(x,H-239.7,label)


def principal_invoice(d):
    total = int(d['amount_tax_in'])
    gross = int(d.get('principal',total)) if 'invoice_amount' in d else total
    ex,tax=calc.tax_split(gross)
    carried=int(d.get('carried_amount') or 0)
    remaining=total if 'invoice_amount' in d else max(0,total-carried)
    allocation_lines = []
    for allocation in d.get('allocations',[]):
        label = '一時預かり金' if allocation['kind']=='hold' else '前案件'
        allocation_lines.extend(_principal_item_lines(f"{label} {allocation['case_no'] or ''} {allocation['name']} からの充当 −{allocation['amount']:,}円"))
        allocation_lines.extend(_principal_item_lines(f"承諾日 {allocation.get('consent_date') or allocation['confirmed_on']} ／ 確定操作日 {allocation['confirmed_on']} ／ 履歴 {allocation['id']} ／ {allocation.get('record') or ''}"))
    items = allocation_lines + list(_principal_item_lines(d.get('items_text')))
    rows_per_page = 17

    def draw(c):
        _principal_grid(c)
        _right(c, 557, 39, d['doc_no'], 9, width=96)
        _right(c, 557, 54, _date(d['issued_date']), 9, width=96)
        _draw(c, 59, 89.8, d['recipient'], 14.56, width=214)
        _draw(c, 88, 106, _date(d.get('case_due_date')), 9, width=190)
        _draw(c, 88, 126, d.get('payment_terms','指定期日までにお振込み'), 9, width=190)
        if d.get('valid_until'):
            _draw(c, 88, 146, _date(d['valid_until']), 9, width=190)
        _principal_company(c,d)
        _amount(c, 244, 195, remaining, 16.99, width=132, font=BOLD)
        _right(c, 43, 263.1, '1', 10.52)
        _draw(c, 49.3, 263.1, d.get('item_name') or d.get('case_name'), 9.71, width=290)
        _right(c, 399, 263.1, '1式', 9.71, width=24)
        _amount(c, 478, 263.1, ex, 9.71, width=70)
        _amount(c, 557, 263.1, ex, 10.11, width=70)
        if items:
            _draw(c, 24, 280.5, '参考', 7)
        _principal_item_rows(c, items[:rows_per_page])
        c.setFillColorRGB(1,1,1)
        c.rect(21,H-675,321,63,fill=1,stroke=0)
        _draw(c,25,619,'担当元金と確定充当の内訳',7)
        _draw(c,25,632.9,'本案件の担当元金',8)
        _draw(c,25,649,'確定済み元金充当額',8)
        _draw(c,25,665.2,'今回のお支払額',8,font=BOLD)
        _amount(c, 335, 632.9, gross, 8.09, width=125, unit=' 円')
        _draw(c,210,649,'−' if carried else '',8)
        _amount(c, 335, 649, carried, 8.09, width=115, unit=' 円')
        _amount(c, 335, 665.2, remaining, 8.09, width=125, unit=' 円', font=BOLD)
        _draw(c,365,620,'担当元金の税内訳（充当への税加算なし）',7,width=195)
        _amount(c, 560, 633.7, ex, 10.52, width=91, font=BOLD)
        _amount(c, 560, 653.9, tax, 10.52, width=91, font=BOLD)
        _amount(c, 560, 674.1, gross, 11.33, width=91, font=BOLD)
        note=d.get('note') or ''
        if d.get('mgmt_no'): note += f"  管理番号：{d['mgmt_no']} ／ 案件番号：{d.get('case_no') or '―'}"
        _paragraph(c, 87.2, 697.5, note, 472, 10.52, 20.2, 2)
        _draw(c, 66, 744.5, d.get('issuer_bank_info'), 10.52, width=494, font=BOLD)
        _draw(c, 113, 764, _date(d.get('due_date')), 9, width=121)
    def continuation(lines):
        def draw_continued(c):
            _principal_grid(c)
            _right(c, 557, 39, d['doc_no'], 9, width=96)
            _right(c, 557, 54, _date(d['issued_date']), 9, width=96)
            _draw(c, 59, 89.8, d['recipient'], 14.56, width=214)
            _principal_company(c, d)
            _draw(c, 55, 260, '商品名（参考明細・続き）', 8)
            _principal_item_rows(c, lines)
        return draw_continued

    extra_pages = [continuation(items[index:index + rows_per_page])
                   for index in range(rows_per_page, len(items), rows_per_page)]
    return _overlay('principal_invoice.pdf', draw, extra_pages)

def haak_invoice(d):
    total=int(d['amount_tax_in']); ex,tax=calc.tax_split(total)
    with fitz.open(os.path.join(TEMPLATES,'haak_invoice.pdf')) as background:
        fixed_recipient = bool(background[0].get_text(clip=fitz.Rect(40,120,330,160)).strip())
    products = str(d.get('product_details') if d.get('product_details') is not None
                   else d.get('items_text') or '').splitlines()
    # One product per numbered row; long names wrap onto unnumbered rows.
    rows = []
    for number, product in enumerate((p for p in products if p.strip()), 3):
        for index, line in enumerate(_wrap(_text(product), 237, 8.5)):
            rows.append((str(number) if index == 0 else '', line))
    chunks = [rows[:18]] + [rows[i:i+20] for i in range(18, len(rows), 20)]

    def paint(page_index, chunk):
        def draw(c):
            _company(c, d)
            if not fixed_recipient:
                _draw(c,49,143,d.get('recipient') or '請求先',12,width=260)
            _right(c, 550, 73, 'No. ' + (d.get('doc_no') or ''), 9, width=200)
            _right(c, 550, 87, '発行日：' + _date(d.get('issued_date')), 9, width=200)
            _amount(c, 316, 237, total, 17, width=260, prefix='¥')
            _draw(c, 115, 254, _date(d.get('due_date')), 9, width=199)
            _haak_bank(c, d)
            # Remove only the obsolete 品名/内容 divider and row labels.
            # All other rules, colours, total positions and calculations stay fixed.
            c.setFillColor('#1B2238')
            c.rect(71, H-313, 243, 21, fill=1, stroke=0)
            c.setFillColorRGB(1,1,1); c.setFont(FONT,9.9)
            c.drawString(168,H-306,'品名・内容')
            for row in range(20):
                top=313.3+row*18
                c.setFillColor('#FAFBFD' if row%2 else '#FFFFFF')
                # Cover obsolete inner vertical rule and preprinted numbers.
                c.rect(41,H-top-17.4,27,17.4,fill=1,stroke=0)
                c.rect(70.5,H-top-17.4,243.5,17.4,fill=1,stroke=0)
                if page_index == 0 and (row < 2 or row >= len(chunk)+2):
                    _right(c,58,326+row*18,str(row+1),9.9)
                if page_index:
                    for left, width in ((318,49),(371,71),(446,32),(482,72)):
                        c.rect(left,H-top-17.4,width,17.4,fill=1,stroke=0)
            if page_index == 0:
                _draw(c,76,326,d.get('item_name') or d.get('case_name'),9,width=237)
                _amount(c,439,326,ex,9,width=66)
                _amount(c,548,326,ex,9,width=61)
                _draw(c,76,344,'詳細・内容（上記一式に含む）',9,width=237)
            for index,(number,line) in enumerate(chunk):
                top=326+(index+(2 if page_index==0 else 0))*18
                _right(c,58,top,number,9.9)
                _draw(c,76,top,line,8.5)
            if page_index == 0:
                _amount(c,548,694,ex,10,width=91)
                _amount(c,548,713,tax,10,width=91)
                _amount(c,548,732,total,11,width=91)
            else:
                # Continuations never repeat monetary amounts.
                _draw(c,49,808,'商品詳細の続き（上記一式に含む・金額加算なし）',8)
            note=d.get('note') or ''
            if d.get('mgmt_no'):
                note+=f"  管理番号：{d['mgmt_no']} ／ 案件番号：{d.get('case_no') or '―'}"
            _paragraph(c,49,776,note,495,8,12,2)
            if len(chunks)>1:
                _right(c,550,818,f'{page_index+1} / {len(chunks)}',8)
        return draw
    return _overlay('haak_invoice.pdf',paint(0,chunks[0]),
                    [paint(index,chunk) for index,chunk in enumerate(chunks[1:],1)])

def receipt(d):
    """ユーザーが承認した青色系プレビューに合わせた領収書。"""
    buf=io.BytesIO();c=canvas.Canvas(buf,pagesize=A4,initialFontName=FONT)
    c.setTitle('領収書')
    def rect(x,top,w,h,color,stroke=None):
        c.setFillColor(color);c.setStrokeColor(stroke or color)
        c.rect(x,H-top-h,w,h,fill=1,stroke=1 if stroke else 0)
    def rule(x,y,x2,y2):
        c.setStrokeColor(BLUE);c.setLineWidth(.7);c.line(x,H-y,x2,H-y2)
    rect(26,41,165,44,BLUE)
    c.setFillColorRGB(1,1,1);c.setFont(FONT,21);c.drawString(58,H-72,'領 収 書')
    rule(191,85,W-26,85)
    _right(c,W-34,50,'No.： '+d['doc_no'],9)
    _right(c,W-34,70,'発行日： '+_date(d['issued_date']),9)
    _draw(c,42,125,d['recipient'],12,width=260);_draw(c,310,125,'御中',11)
    rule(30,133,350,133)
    _draw(c,42,158,'対象案件： '+d.get('mgmt_no','')+' ／ '+(d.get('case_no') or '―'),9,width=295)
    _draw(c,42,176,'案件名： '+str(d.get('case_name') or ''),9,width=295)
    _draw(c,42,212,'下記の金額を正に領収いたしました。',11)
    _issuer_name(c, d, 365, 152, W-34-SEAL_SIZE if d.get('issuer_seal_png') else 557)
    _seal(c,d,W-38-SEAL_SIZE,99,baseline=152)
    _company_rows(c,d,365,168,width=192)
    rect(26,228,162,39,BLUE);rect(188,228,247,39,PALE)
    c.setFillColorRGB(1,1,1);c.setFont(FONT,12);c.drawString(36,H-252,'今回の領収金額')
    _amount(c,421,254,d['amount_tax_in'],18,width=210,prefix='¥')
    rect(26,310,W-52,30,BLUE)
    c.setFillColorRGB(1,1,1);c.setFont(FONT,10)
    for x,label in [(39,'No.'),(73,'領収内容／品名'),(369,'受領方法'),(493,'金 額')]:c.drawString(x,H-330,label)
    rows=[('1',str(d.get('case_name') or '')+' 元金',d.get('payment_method') or '銀行振込',_money(d['amount_tax_in'])+'円'),('','管理番号：'+str(d.get('mgmt_no'))+' ／ 案件番号：'+str(d.get('case_no') or '―'),'','')]
    for i in range(11):
        top=340+i*29;rect(26,top,W-52,29,PALE if i%2 else '#FFFFFF')
        if i<len(rows):
            no,item,method,amt=rows[i]
            _draw(c,39,top+19,no,9)
            _draw(c,73,top+19,item,9,width=275)
            _draw(c,369,top+19,method,9,width=72)
            if amt:
                _amount(c,W-37,top+19,d['amount_tax_in'],10,width=106,unit='円')
    for x in [26,67,357,448,W-26]:rule(x,340,x,659)
    rule(26,659,W-26,659)
    rect(26,677,W-52,45,'#EFF3FA',BLUE)
    _draw(c,38,696,'お受け取り内訳',10)
    _draw(c,38,713,(d.get('payment_method') or '銀行振込')+'：'+_money(d['amount_tax_in'])+'円',9,width=352)
    _right(c,W-40,696,'領収金額',10);_amount(c,W-40,714,d['amount_tax_in'],14,width=154,prefix='¥')
    rule(26,732,W-26,732)
    _draw(c,29,751,'但し書き：'+(d.get('note') or '上記案件の担当元金として'),9,width=535)
    _draw(c,29,770,'受領日：'+_date(d.get('received_date')),9)
    _draw(c,29,793,'発行者：'+(d.get('issuer_name') or ''),9,width=535)
    c.showPage();c.save()
    return buf.getvalue()

def render(doc_type, d):
    return {'principal_invoice':principal_invoice,'receipt':receipt,'haak_invoice':haak_invoice}[doc_type](d)
