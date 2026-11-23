"""Explicitly allowlisted, reproducible, data-free private-source update package."""
from pathlib import Path
import argparse,hashlib,json,sys,zipfile
APP=Path(__file__).resolve().parent
sys.path.insert(0,str(APP))
from app_version import VERSION,DISPLAY_VERSION,SCHEMA_VERSION,build_id

APPLICATION='''app_version.py attachments.py auth_security.py calc.py carry_management.py
case_summary.py documents.py funding_ledger.py launcher.py line_case_reader.py
password_recovery.py pdf_archive.py principal_balance.py principal_funding.py
purchase_reader.py server.py settlement_documents.py update.py launch.ps1
password_recovery.ps1 start.bat update.bat schema.sql requirements.txt app.ico logi-tomos.ico
logibox-yellow.ico build_release.py README.txt 起動.vbs パスワード再設定.vbs 更新手順.txt UPDATE_ONLY.txt 修正報告.txt'''.split()
ASSETS='''static/index.html static/workflow.js static/funding.js static/logibox.svg
fonts/BIZUDPGothic-Regular.ttf fonts/BIZUDPGothic-Bold.ttf fonts/OFL.txt
document_templates/haak_invoice.pdf document_templates/principal_invoice.pdf'''.split()

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument('--output-dir', default=str(APP.parent/'dist'))
 args=parser.parse_args()
 OUTPUT=Path(args.output_dir); OUTPUT.mkdir(parents=True,exist_ok=True)
 files=sorted([APP/n for n in APPLICATION+ASSETS]+list(APP.glob('test_*.py')))
 manifest=dict(version=VERSION,display_version=DISPLAY_VERSION,schema=SCHEMA_VERSION,build=build_id(),
  sha256={p.relative_to(APP).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
 (APP/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 path=OUTPUT/f'LOGIBOX_ver{DISPLAY_VERSION}_データ保持_元金請求書修正版.zip'
 with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
  for p in sorted(files+[APP/'SOURCE_MANIFEST.json']):
   info=zipfile.ZipInfo(f'LOGIBOX_ver{DISPLAY_VERSION}_update/logibox/'+p.relative_to(APP).as_posix(),(2026,1,1,0,0,0))
   info.compress_type=zipfile.ZIP_DEFLATED
   z.writestr(info,p.read_bytes())
 (OUTPUT/'SHA256SUMS.txt').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name+'\n')
 print(path)

if __name__=='__main__':main()
