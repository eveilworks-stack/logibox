"""Readable case evidence index and a consolidated copy; originals stay untouched."""
import io
from pathlib import Path
from xml.sax.saxutils import escape

import fitz
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
import documents  # Registers the bundled Japanese font.


def create(case, entries, references):
    """Return a standalone index and a bundle with bookmarks for readable files."""
    combined = fitz.open()
    included, rows = [], []
    for entry in entries:
        start = combined.page_count
        try:
            raw = entry['source'].read_bytes()
            with fitz.open(stream=raw, filetype=entry['source'].suffix.lstrip('.')) as source:
                if source.needs_pass:
                    raise ValueError('password required')
                if not source.is_pdf:
                    converted = source.convert_to_pdf()
                    with fitz.open(stream=converted, filetype='pdf') as pdf:
                        combined.insert_pdf(pdf)
                else:
                    combined.insert_pdf(source)
            if combined.page_count == start:
                raise ValueError('no pages')
            status = f'{combined.page_count-start}ページ'
            included.append((entry['label'], start))
        except Exception:
            # Malformed/encrypted files remain available as individual originals.
            if combined.page_count > start:
                combined.delete_pages(start, combined.page_count-1)
            status = '一式への結合不可（個別ファイルを確認）'
        rows.append((entry['label'], entry['relative'], status))
    for reference in references:
        rows.append(('月次精算・受領請求書', reference, '複数案件共通／参照のみ'))
    output = io.BytesIO()
    style = ParagraphStyle('Evidence', fontName=documents.FONT, fontSize=9, leading=14,
                           wordWrap='CJK', spaceAfter=6)
    title = ParagraphStyle('EvidenceTitle', parent=style, fontSize=18, leading=25)
    def para(value, selected=style):
        return Paragraph(escape(str(value or '')).replace('\n', '<br/>'), selected)
    story = [para('案件証憑一覧', title), para(case['name']),
             para(f"管理番号：{case['mgmt_no']}　案件番号：{case['case_no'] or '未登録'}"),
             para(f"開始日：{case['start_date'] or '未登録'}　納期：{case['due_date'] or '未登録'}"),
             para('正式発行・取込済み書類の現在版です。旧版はアプリ内の履歴で保管します。'),
             para('月次精算の受領請求書は複数案件共通のため、保存場所の参照のみを掲載します。'), Spacer(1, 10)]
    data = [[para('書類'), para('保存場所（案件フォルダからの相対パス）'), para('一式PDFへの収録')]]
    data.extend([para(value) for value in row] for row in rows)
    if not rows:
        data.append([para('証憑未登録'), para('書類の追加・発行後に自動更新します。'), para('')])
    table = LongTable(data, colWidths=[112, 265, 134], repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),
                              ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eaf0f5')),
                              ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#c7d0d9')),
                              ('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7)]))
    story.append(table)
    SimpleDocTemplate(output,pagesize=A4,rightMargin=42,leftMargin=42,topMargin=40,bottomMargin=40,
                      title='案件証憑一覧',author='LOGIBOX',invariant=1).build(story)
    index = output.getvalue()
    with fitz.open(stream=index, filetype='pdf') as result:
        cover_pages = result.page_count
        if combined.page_count:
            result.insert_pdf(combined)
        result.set_toc([[1,'案件証憑一覧',1]] + [[1,label,cover_pages+start+1] for label,start in included])
        bundle = result.tobytes(garbage=3, deflate=True, no_new_id=True)
    combined.close()
    return index, bundle
