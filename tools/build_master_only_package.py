"""Build the explicitly requested, opt-in master-only Windows switch ZIP."""
import hashlib
import json
from pathlib import Path
import zipfile

REPO=Path(__file__).resolve().parents[1]
OUTPUT=Path('/workspace/logibox-release')
NAME='LOGIBOX_ver0.68_会社・下請け・仕入先・社印のみ保持.zip'
PREFIX='LOGIBOX_0.68_切替用/'
INSTRUCTIONS='''LOGIBOX 0.68への切り替え（今回ご指定の専用版）

残すもの：会社情報、下請けマスター、仕入先マスター、社印。
案件・仕入取引・請求書・入金・充当・精算・添付は、新しい0.68では空になります。
以前のパスワードは引き継ぎません。最初に新しいパスワードを設定してください。
元のフォルダ全体は、隣のLOGIBOX-before-0.68-masters-*へ退避します。
元データはprevious-installationフォルダとfull-installation.zipの両方に残ります。

操作はこれだけです。
1. PCを再起動し、今までのLOGIBOXは開かないでください。
2. このZIPを右クリック→「すべて展開」します。
   今までのLOGIBOXフォルダとは別の場所へ展開してください。
3. 「最初にここをダブルクリック.bat」を開きます。
4. 今まで使用していたlogiboxフォルダを選びます。
   dataフォルダ（その中にapp.db）があるフォルダです。
5. 引継ぎ内容を確認して「はい」を押します。
   自動バックアップ→4種類の引継ぎ→0.68へ切替→起動まで自動で行います。
6. 最初にパスワードを設定し、会社情報・下請け・仕入先・社印とver.0.68を確認します。
   次回からはデスクトップのLOGIBOXアイコンで起動できます。

重要：今までのフォルダを手で削除したり、dataを手動コピーする必要はありません。
      完了後にバックアップを削除すると元の案件・履歴を復元できなくなるため保管してください。

Python 3.10以降が必要です。必要なライブラリは初回にインターネットから準備します。
APP_DATAまたはLOGIBOX_PDF_DIRを指定しているPC、リンクされた保存先には自動対応しません。
その場合は何も切り替えず案内します。環境変数を安易に消さないでください。
外部に保存したPDFコピーは削除・移動しません。

中止・失敗時：
切替前の失敗では、元フォルダを変更しません。
最後のフォルダ切替に失敗した場合は元フォルダへ戻します。
元フォルダが見当たらない場合は、隣のLOGIBOX-before-0.68-masters-*内の
previous-installationとfull-installation.zipを保管し、その場所をお知らせください。

このZIPにはお客様の実データを含めていません。PC上で選んだフォルダから必要情報を引き継ぎます。
Windows実機での操作は未実施です。切替・保持内容・バックアップ・失敗時復旧を一時DBで自動検証しています。
'''
BAT='''@echo off
setlocal
cd /d "%~dp0"
set "LOGIBOX_RUNTIME=py"
py -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul
if not errorlevel 1 goto run
set "LOGIBOX_RUNTIME=python"
python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul
if errorlevel 1 goto error
:run
%LOGIBOX_RUNTIME% "%~dp0master_only_setup.py"
if errorlevel 1 pause
goto :eof
:error
echo Python 3.10+ is required. Please install Python and try again.
pause
'''


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    path=OUTPUT/NAME
    files={}
    with zipfile.ZipFile(REPO/'LOGIBOX_latest_no_data.zip') as original:
        for name in original.namelist():
            relative=name.split('/logibox/',1)[1]
            if '/data/' in '/'+relative or relative.startswith('data/'):
                raise RuntimeError('Application ZIP unexpectedly contains data')
            files['logibox/'+relative]=original.read(name)
    files['master_only_setup.py']=(REPO/'tools/master_only_setup.py').read_bytes()
    files['最初にここをダブルクリック.bat']=BAT.replace('\n','\r\n').encode('ascii')
    files['使い方.txt']=INSTRUCTIONS.encode('utf-8-sig')
    files['検証結果.txt'] = ("会社・下請け・仕入先・社印のみを保持する専用切替ZIP\n"
        "追加9テスト成功：保持内容・業務テーブル空・全体バックアップ照合・バックアップ失敗時停止・切替失敗時復旧・稼働ロック・改変配布物拒否・環境変数指定時停止・リンク拒否・再切替拒否・Windowsロック名の検証を含みます。\n"
        "0.68の既存206テストも再実行：成功203、既存スキップ3、失敗0。HTTPスモーク成功。\n"
        "実際の専用ZIPを一時フォルダへ展開し、切替後の0.68サーバー起動・HTTP応答・初回パスワード設定状態・下請け保持・案件0件を確認。\n"
        "Windowsの起動ロック名を修正し、通常起動と専用切替で共通の正しい名前を使用。\n"
        "お客様のPC上の実データにはアクセスしていません。すべて架空データによる検証です。\n"
        "Windows実機のBAT・VBS・PowerShell・フォルダ切替・ショートカット操作は未検証です。\n").encode('utf-8-sig')

    manifest=json.loads(files['logibox/SOURCE_MANIFEST.json'])
    assert manifest['version']=='0.6.8'
    for name,digest in manifest['sha256'].items():
        assert hashlib.sha256(files['logibox/'+name]).hexdigest()==digest,name
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,content in sorted(files.items()):
            info=zipfile.ZipInfo(PREFIX+name,(2026,11,23,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,content)
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    (OUTPUT/'MASTER_ONLY_SHA256SUMS.txt').write_text(checksum+'  '+NAME+'\n',encoding='utf-8')
    print(path)
    print('SHA256:',checksum)

if __name__=='__main__':main()
