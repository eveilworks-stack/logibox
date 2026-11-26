"""個別利益精算書。契約率・前回累計は内部スナップショットだけに保持。"""
import io
from functools import partial
from reportlab.pdfgen.canvas import Canvas
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
import documents


def render(d):
    out=io.BytesIO()
    navy=colors.HexColor('#18375a'); blue=colors.HexColor('#eaf3fc')
    body=ParagraphStyle('Profit',fontName=documents.FONT,fontSize=10,leading=17,wordWrap='CJK')
    heading=ParagraphStyle('ProfitTitle',parent=body,fontName=documents.BOLD,fontSize=19,leading=28,textColor=navy)
    def p(x,style=body): return Paragraph(escape(str(x or '―')).replace('\n','<br/>'),style)
    company=d['company']; payment=d['payment']
    flow=[p('利益精算書',heading),p(f"精算書番号 {d['number']}　発行日 {d['issued_date']}"),Spacer(1,12),
          p(d['sub_name']+' 御中'),Spacer(1,100)]
    rows=[('案件番号・案件名',f"{d['case_no'] or '案件番号未登録'}　{d['case_name']}"),
          ('担当元金',f"{d['principal_tax_in']:,}円"),('確定利益額',f"{d['profit']:,}円"),
          ('今回の利益精算額',f"{payment['amount']:,}円"),('精算後の未精算残額',f"{d['remaining_after']:,}円"),
          ('実際の支払日',payment['paid_date']),('支払方法','銀行振込' if payment['payment_method']=='bank' else '現金手渡し'),
          ('備考',payment['note'])]
    table=Table([[p(k),p(v)] for k,v in rows],colWidths=[165,345],hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(0,-1),blue),('BOX',(0,0),(-1,-1),.7,navy),
        ('INNERGRID',(0,0),(-1,-1),.3,colors.HexColor('#ccd9e6')),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),
        ('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9)]))
    flow.append(table)
    def first(canvas,doc):
        documents._company(canvas,dict(issuer_name=company['name'],issuer_seal_png=d['seal'],
            issuer_address=company.get('address'),issuer_invoice_no=company.get('invoice_no'),
            issuer_phone=company.get('phone'),issuer_email=company.get('email')),x=340)
    pdf=SimpleDocTemplate(out,pagesize=A4,leftMargin=42,rightMargin=42,topMargin=40,bottomMargin=40,title='利益精算書',author=company['name'])
    def canvasmaker(*args,**kwargs):
        kwargs['invariant']=1
        return Canvas(*args,**kwargs)
    pdf.build(flow,onFirstPage=first,canvasmaker=canvasmaker)
    return out.getvalue()
