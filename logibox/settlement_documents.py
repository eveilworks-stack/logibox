"""月次精算の依頼文面と記入用PDF。同じ依頼時点のデータを使用する。"""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

import documents


def recipient(d):
    return ' '.join(filter(None, (d['sub_name'], d['contact'], d['honorific'])))


def case_label(line, target=False):
    keys = ('target_no', 'target_case_no', 'target_name') if target else ('settlement_no', 'case_no', 'case_name')
    return ' / '.join(str(line.get(key)) for key in keys if line.get(key))


def mail_case_label(line, target=False):
    number, name = ('target_case_no', 'target_name') if target else ('case_no', 'case_name')
    return f"{line.get(number) or '案件番号未登録'} {line.get(name) or ''}".strip()


def line_text(line, email=False):
    label = mail_case_label if email else case_label
    mode = '利益のみの請求' if line['profit_only'] else '元金残額と利益の請求'
    text = (f"{label(line)}　{mode}\n"
            f"開始日 {line.get('start_date') or '未登録'} / 納期 {line.get('due_date') or '未登録'}\n"
            f"元金残額 {line['principal'] - line['carry_over'] - line.get('ledger_outgoing',0) - line.get('refunded',0):,}円 / 利益 {line['sub_profit']:,}円 / "
            f"税込請求額 {line['payout']:,}円")
    if line['carry_over'] and not line.get('carry_to_case_id'):
        text += (f"\n元金一時預かり {line['carry_over']:,}円（充当先なし）。預かり元金は今回の請求額に含めません。"
                 f"\n一時預かり承諾日 {line.get('consent_date') or '了承待ち（未承諾）'} / 承諾方法 {line.get('consent_method') or '未登録'}"
                 f"\n承諾記録 {line.get('consent_record') or '未登録'}")
    elif line['carry_over']:
        title = '元金充当（精算額の計算参照）' if email else '元金充当'
        text += (f"\n{title} {line['carry_over']:,}円：充当元 {label(line)} → 充当先 {label(line, True)}。"
                 '充当分の元金は今回の請求額に含めません。')
    return text


def request_text(d):
    lines = [recipient(d), f"{d['period']}納期分の請求書の発行をお願いいたします。",
             f"請求先　{d['company']['name'] or '（自社名を設定してください）'}",
             f"ご請求額　税込 {d['total']:,}円",
             f"内訳　税抜 {d['amount_ex_tax']:,}円 / 消費税 {d['tax_amount']:,}円（税率 {d['tax_rate']}%）",
             f"支払期限　{d['pay_date']}", f"請求内容　{d['description']}"]
    lines.extend('・' + line_text(line, email=True) for line in d['lines'])
    current = [line for line in d.get('carry_details', d['lines']) if line['carry_over'] and line.get('carry_to_case_id') and
               (line.get('carry_confirmed_date') or line.get('consent_date') or '').startswith(d['period'])]
    if current:
        lines += ['', '元金充当明細（新規現金入金とは別の資金移動）']
    for line in current:
        lines.append(f"・{mail_case_label(line)} → {mail_case_label(line, True)} / "
                     f"充当金額 {line['carry_over']:,}円 / 充当日 {line.get('carry_confirmed_date') or line.get('consent_date') or '未登録'} / 承諾日 {line.get('consent_date') or '未登録'} / "
                     f"承諾方法 {line.get('consent_method') or '旧記録：未登録'}")
        if line.get('consent_record'):
            lines.append('  承諾記録 ' + line['consent_record'])
    if current:
        lines += ['新規現金入金：この元金充当明細には含みません。元金の現金入金記録と分けて管理します。']
    events = d.get('funding_events', [])
    if events:
        lines += ['', '元金・一時預かり金の発生・充当・返金履歴（請求額への加算なし）']
    labels = {'created':'一時預かり発生','principal':'他案件元金充当','hold':'一時預かり金充当','refund':'一時預かり金返金','cancel':'充当取消'}
    for e in events:
        lines.append(f"・{labels[e['kind']]} / {e['case_no'] or '案件番号未登録'} {e['name']} → {e.get('target_case_no') or '―'} {e.get('target_name') or ''} / {e['amount']:,}円 / {e['event_date']} / 承諾日 {e['consent_date']} / {e['consent_method']} / {e['consent_record']} / {e['note']}")
    lines += ['明細には上記の案件番号・案件名・業務内容・元金残額・利益・税込請求額を記載してください。',
              '元金充当がある明細には充当元・充当先案件番号と充当額を記載し、全額充当時は「利益のみの請求」と記載してください。',
              '請求書全体を上記の同一税率で計算し、消費税は税込合計から1回だけ円未満切り捨てで算出しています。',
              '請求書は貴社名義で発行し、発行日・請求書番号・登録番号・振込先を確認のうえPDFでお送りください。']
    return '\n'.join(lines)


def render(d):
    """下請けが内容を確認して発行するための静的ひな形。正式発行記録には入れない。"""
    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, leftMargin=40, rightMargin=40,
                           topMargin=38, bottomMargin=40, title='下請け請求書ひな形', author='LOGIBOX')
    body = ParagraphStyle('SettlementBody', fontName=documents.FONT, fontSize=9, leading=15,
                          wordWrap='CJK', spaceAfter=6)
    heading = ParagraphStyle('SettlementHeading', parent=body, fontName=documents.BOLD,
                             fontSize=18, leading=25, spaceAfter=12)
    small = ParagraphStyle('SettlementSmall', parent=body, fontSize=8, leading=13)

    def p(value, style=body):
        text = '\n'.join(documents._text(part) for part in str(value or '').split('\n'))
        return Paragraph(escape(text).replace('\n', '<br/>'), style)

    flow = [p('請求書（記入用ひな形）', heading),
            p('下請けご本人が内容を確認し、発行日・請求書番号などを整えて発行してください。', small),
            p('ご案内先　' + recipient(d)), p(d['company']['name'] + ' 御中'),
            p(f"発行者　{d['sub_name']}\n担当者　{d['contact'] or '＿＿＿＿＿＿'}"),
            p('発行日　＿＿年＿＿月＿＿日　　請求書番号　＿＿＿＿＿＿＿＿'),
            p('登録番号　' + (d['subcontractor'].get('invoice_no') or '＿＿＿＿＿＿＿＿＿＿＿＿')),
            p('発行者住所　' + (d['subcontractor'].get('address') or '＿＿＿＿＿＿＿＿＿＿＿＿')),
            p('振込先　' + (d['subcontractor'].get('bank_info') or '＿＿＿＿＿＿＿＿＿＿＿＿')),
            Spacer(1, 6)]
    summary = Table([[p('税込合計'), p(f"{d['total']:,} 円")],
                     [p(f"税抜金額（税率 {d['tax_rate']}% 対象）"), p(f"{d['amount_ex_tax']:,} 円")],
                     [p(f"消費税（{d['tax_rate']}%）"), p(f"{d['tax_amount']:,} 円")],
                     [p('支払期限'), p(d['pay_date'])]], colWidths=[250, 265])
    summary.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E8EEF6')),
                                 ('GRID', (0, 0), (-1, -1), .4, colors.HexColor('#BAC5D3')),
                                 ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                 ('LEFTPADDING', (0, 0), (-1, -1), 9),
                                 ('TOPPADDING', (0, 0), (-1, -1), 6)]))
    flow += [summary, Spacer(1, 14), p(f"対象　{d['period']} 納期分"), p('請求内容　' + d['description'])]
    for index, line in enumerate(d['lines'], 1):
        flow += [KeepTogether([p(f'明細 {index}　' + case_label(line),
                                  ParagraphStyle('LineHeading', parent=body, fontName=documents.BOLD)),
                               p(line_text(line))]), Spacer(1, 6)]
    flow += [p('消費税は請求書全体・税率ごとに1回、税込合計から円未満切り捨てで算出。明細金額は税込です。', small),
             p('明細には案件番号・案件名・業務内容を記載してください。元金全額を別案件に充当した明細は利益のみの請求です。', small)]

    def footer(canvas, pdf):
        canvas.setFont(documents.FONT, 8)
        canvas.drawString(40, 22, '記入用ひな形 / 下請け本人による確認・発行用')
        canvas.drawRightString(A4[0] - 40, 22, str(pdf.page))

    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
