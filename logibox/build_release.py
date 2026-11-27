"""Explicitly allowlisted, reproducible, data-free update package."""
from pathlib import Path
import argparse,hashlib,json,sys,zipfile
APP=Path(__file__).resolve().parent
sys.path.insert(0,str(APP))
from app_version import VERSION,DISPLAY_VERSION,SCHEMA_VERSION,build_id

APPLICATION='''app_version.py attachments.py auth_security.py calc.py carry_management.py
case_summary.py sub_participation.py company_seal.py documents.py evidence_documents.py funding_ledger.py launcher.py line_case_reader.py
password_recovery.py pdf_archive.py principal_balance.py principal_funding.py
purchase_reader.py server.py settlement_documents.py profit_settlement.py profit_settlement_documents.py settlement_principal.py update.py launch.ps1
password_recovery.ps1 start.bat start.command update.bat schema.sql requirements.txt app.ico logi-tomos.ico
logibox-yellow.ico build_release.py run_tests.py README.txt 起動.vbs パスワード再設定.vbs 更新手順.txt UPDATE_ONLY.txt 修正報告.txt 利益精算操作手順.txt ver0.73実装・テスト報告.txt'''.split()
ASSETS='''static/index.html static/workflow.js static/funding.js static/profit.js static/logibox.svg
fonts/BIZUDPGothic-Regular.ttf fonts/BIZUDPGothic-Bold.ttf fonts/OFL.txt
document_templates/haak_invoice.pdf document_templates/principal_invoice.pdf'''.split()

def manifest_bytes(manifest):
 return (json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def write_package(path,prefix,payload,manifest):
 with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
  content=dict(payload,**{'SOURCE_MANIFEST.json':manifest_bytes(manifest)})
  for name,raw in sorted(content.items()):
   info=zipfile.ZipInfo(prefix+name,(2026,1,1,0,0,0))
   info.compress_type=zipfile.ZIP_DEFLATED
   info.external_attr=(0o100755 if name.endswith('.command') else 0o100644)<<16
   z.writestr(info,raw)


def main():
 parser=argparse.ArgumentParser()
 parser.add_argument('--output-dir',default=str(APP.parent/'dist'))
 args=parser.parse_args()
 output=Path(args.output_dir);output.mkdir(parents=True,exist_ok=True)
 names=sorted(set(APPLICATION+ASSETS+[p.name for p in APP.glob('test_*.py')]))
 payload={name:(APP/name).read_bytes() for name in names if name!='UPDATE_ONLY.txt'}
 marker=APP/'UPDATE_ONLY.txt'
 payload['UPDATE_ONLY.txt']=marker.read_bytes() if marker.exists() else 'これは更新専用パッケージです。利用中のlogiboxフォルダを指定してupdate.batまたはupdate.pyで更新してください。\n'.encode('utf-8')
 manifest=dict(version=VERSION,display_version=DISPLAY_VERSION,schema=SCHEMA_VERSION,build=build_id(),
  sha256={n:hashlib.sha256(raw).hexdigest() for n,raw in payload.items()})
 clean_payload={n:raw for n,raw in payload.items() if n!='UPDATE_ONLY.txt'}
 clean_manifest=dict(manifest,sha256={n:h for n,h in manifest['sha256'].items() if n!='UPDATE_ONLY.txt'})
 # Keep a clean source runnable when building from a data-free distribution.
 (APP/'SOURCE_MANIFEST.json').write_bytes(manifest_bytes(manifest if marker.exists() else clean_manifest))
 update=output/f'LOGIBOX_ver{DISPLAY_VERSION}_全修正統合_更新専用.zip'
 distribution=output/f'LOGIBOX_ver{DISPLAY_VERSION}_配布用_データなし.zip'
 write_package(update,f'LOGIBOX_ver{DISPLAY_VERSION}_update/logibox/',payload,manifest)
 write_package(distribution,f'LOGIBOX_ver{DISPLAY_VERSION}/logibox/',clean_payload,clean_manifest)
 (output/'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (update,distribution)))
 print(update)
 print(distribution)


if __name__=='__main__':main()
