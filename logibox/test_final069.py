"""0.69: product completeness, consent-based dates, transparent seals and snapshots."""
import base64
import datetime
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from PIL import Image, ImageChops
import fitz
import company_seal
import documents
import test_principal_invoice as support
import test_document_layout as layout


class Final069Tests(unittest.TestCase):
    setUp = support.PrincipalInvoiceTests.setUp
    call = support.PrincipalInvoiceTests.call
    case = support.PrincipalInvoiceTests.case
    prepare = support.PrincipalInvoiceTests.prepare
    save = support.PrincipalInvoiceTests.save
    allocate = support.PrincipalInvoiceTests.allocate
    issue = support.PrincipalInvoiceTests.issue
    payload = support.PrincipalInvoiceTests.payload
    consent = support.PrincipalInvoiceTests.consent

    def past_consent(self, days=14):
        date=(datetime.date.today()-datetime.timedelta(days=days)).isoformat()
        self.consent=lambda **kw: dict(consent_date=date,consent_method='LINE',consent_record='承諾確認',**kw)
        return date

    def test_consent_day_default_and_past_issuance_preview_identical(self):
        source,target=self.prepare(); day=self.past_consent(); self.allocate(source,target)
        doc=self.s.document_payload(self.c,{},target,'principal_invoice',self.sid)
        self.assertEqual(doc['issued_date'],day)
        for date in [day,(datetime.date.today()-datetime.timedelta(days=1)).isoformat()]:
            self.assertEqual(self.payload(target,issued_date=date)['amount_tax_in'],2000000)
        with self.assertRaisesRegex(self.s.Err,'承諾日'):
            self.payload(target,issued_date=(datetime.date.fromisoformat(day)-datetime.timedelta(days=1)).isoformat())
        with self.assertRaises(self.s.Err):
            self.payload(target,issued_date=(datetime.date.today()+datetime.timedelta(days=1)).isoformat())
        draft=self.s.r_doc_preview(self.c,dict(doc_type='principal_invoice',sub_id=self.sid,issued_date=day),{},str(target),session='invoice-test');self.c.commit()
        raw=self.s.load_doc_draft(draft['token'],'invoice-test')['pdf']
        did=self.issue(target,preview_token=draft['token'])
        self.assertEqual(self.call('r_doc_download',None,did)[1],raw)
        row=self.c.execute('SELECT * FROM issued_documents WHERE id=?',(did,)).fetchone()
        self.assertEqual(row['issued_date'],day)
        self.assertTrue(row['updated_at'].startswith(self.today))

    def test_multiple_legacy_and_hold_use_latest_consent(self):
        source,target=self.prepare();early=self.past_consent(14);self.allocate(source,target,500000,legacy=True)
        late=self.past_consent(7)
        hold_source=self.case('預かり元案件',self.old,1000000,'2020-11-01')
        self.call('r_haak_put',dict(confirmed_amount_tax_in=1100000,confirmed_checked=True,paid_date=self.today),hold_source)
        fund=self.s.funding_ledger.create_hold(self.c,self.consent(amount=500000,reason='一時預かり',occurred_on=early),{},str(hold_source),'1')['id'];self.c.commit()
        self.allocate(fund,target,500000,kind='hold')
        doc=self.s.document_payload(self.c,{},target,'principal_invoice',self.sid)
        self.assertEqual((doc['issued_date'],doc['amount_tax_in']),(late,3000000))
        with self.assertRaises(self.s.Err):self.payload(target,issued_date=early)

    def test_no_allocation_accepts_old_date_and_missing_consent_requires_review(self):
        source,target=self.prepare()
        self.assertEqual(self.payload(target,issued_date='2020-12-01')['amount_tax_in'],4000000)
        self.allocate(source,target,legacy=True)
        self.c.execute('UPDATE settlement_lines SET consent_date=NULL');self.c.commit()
        with self.assertRaisesRegex(self.s.Err,'承諾日'):self.payload(target)

    def test_explicit_date_edit_of_068_snapshot_preserves_amount_and_old_pdf(self):
        import json
        source,target=self.prepare();day=self.past_consent();self.allocate(source,target)
        did=self.issue(target,issued_date=self.today)
        original=self.call('r_doc_download',None,did)[1]
        row=self.s.issued_document(self.c,str(did));snapshot=json.loads(row['snapshot_json'])
        for allocation in snapshot['allocations']:allocation.pop('consent_date',None)
        self.c.execute('UPDATE issued_documents SET snapshot_json=? WHERE id=?',(json.dumps(snapshot),did));self.c.commit()
        self.call('r_doc_put',dict(issued_date=day),did)
        saved=self.call('r_docs',None,target)[0]
        self.assertEqual((saved['issued_date'],saved['amount_tax_in']),(day,2000000))
        history=self.call('r_doc_history',None,did)
        self.assertEqual(self.call('r_doc_old_download',None,history[-1]['id'])[1],original)

    def test_registered_products_and_fallback_both_reach_haak(self):
        _,target=self.prepare()
        supplier=self.call('r_supplier_new',dict(name='検証仕入先'))['id']
        purchase=self.call('r_purchase_new',dict(supplier_id=supplier,amount_tax_in=1000,items_text='旧商品A\n旧商品B'),target)['id']
        self.call('r_haak_put',dict(confirmed_amount_tax_in=43621278,confirmed_checked=True,items_text='任意欄'),target)
        payload=self.s.document_payload(self.c,{},target,'haak_invoice')
        self.assertEqual(payload['product_details'],'旧商品A\n旧商品B')
        self.c.execute('INSERT INTO purchase_invoice_details(purchase_id,original,reviewed) VALUES(?,?,?)',(purchase,'{}','{"items":[{"maker":"検証メーカー","name":"確認済商品"}]}'));self.c.commit()
        payload=self.s.document_payload(self.c,{},target,'haak_invoice')
        self.assertEqual(payload['product_details'],'検証メーカー　確認済商品')
        draft=self.s.r_doc_preview(self.c,dict(doc_type='haak_invoice'),{},str(target),session='haak-test');self.c.commit()
        raw=self.s.load_doc_draft(draft['token'],'haak-test')['pdf']
        issued=self.s.r_doc_new(self.c,dict(preview_token=draft['token']),{},str(target),session='haak-test');self.c.commit()
        self.assertEqual(self.call('r_doc_download',None,issued['id'])[1],raw)

    def test_many_products_no_truncation_or_detail_amounts(self):
        data=layout.example();products=[f'商品{i:03d} '+('長い商品説明'*20 if i==19 else 'テスト') for i in range(65)]
        data['product_details']='\n'.join(products)
        with fitz.open(stream=documents.haak_invoice(data),filetype='pdf') as pdf:
            self.assertGreater(len(pdf),3)
            text=''.join(p.get_text() for p in pdf)
            for i in range(65):self.assertEqual(text.count(f'商品{i:03d}'),1)
            self.assertNotIn('…',text)
            for page in pdf:
                for block in page.get_text('dict')['blocks']:
                    for line in block.get('lines',[]):
                        for span in line['spans']:
                            if span['text'].startswith('商品'):
                                self.assertLess(span['bbox'][2],315)
                # No values in any monetary column on the detail rows.
                for word in page.get_text('words'):
                    if 350<word[1]<673 and word[0]>318:self.fail(str(word))

    def test_white_seal_unmatting_and_original_backup(self):
        im=Image.new('RGB',(10,10),'white');im.putpixel((4,4),(255,50,50));buf=io.BytesIO();im.save(buf,'PNG');raw=buf.getvalue()
        converted=company_seal.transparent_png(raw)
        out=Image.open(io.BytesIO(converted));self.assertEqual(out.getpixel((0,0))[3],0)
        composite=Image.alpha_composite(Image.new('RGBA',im.size,'white'),out).convert('RGB')
        self.assertIsNone(ImageChops.difference(im,composite).getbbox())
        self.assertEqual(company_seal.transparent_png(converted),converted)
        value=base64.b64encode(raw).decode();company_seal.save(self.c,value);self.c.commit()
        self.assertTrue(self.c.execute("SELECT 1 FROM settings WHERE k LIKE 'company_seal_original_%' AND v=?",(value,)).fetchone())
        self.assertNotEqual(self.s.getset(self.c,'company_seal_png'),value)
        self.call('r_company_seal_upload',dict(png_base64=value))

    def test_all_seals_overlap_issuer_name_and_avoid_address(self):
        for typ in ['haak_invoice','principal_invoice','receipt']:
            data=layout.example()
            with fitz.open(stream=documents.render(typ,data),filetype='pdf') as pdf:
                page=pdf[0];rect=page.get_image_rects(page.get_images()[0][0])[0]
                name=next(s for s in layout.spans(page) if s['text']==data['issuer_name'])
                self.assertTrue(rect.intersects(fitz.Rect(name['bbox'])),typ)
                self.assertAlmostEqual(name['bbox'][2]-rect.x0,4,delta=.1)
                for span in layout.spans(page):
                    if span['text'].startswith(('〒 ','架空市','TEL：','Mail：','担当：','登録番号：')):
                        self.assertFalse(rect.intersects(fitz.Rect(span['bbox'])),(typ,span['text']))

if __name__=='__main__':unittest.main(verbosity=2)
