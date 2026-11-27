"""Data-only backup, isolated validation, and journalled replacement (Windows/macOS)."""
import base64
from contextlib import closing
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from app_version import VERSION, SCHEMA_VERSION

MAX_ZIP = 180 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
PENDING = {}
REFERENCES = [('attachments','stored_name','files'), ('attachment_revisions','stored_name','files'),
              ('issued_documents','file','documents'), ('issued_document_history','file','documents'),
              ('sub_invoices','file','documents'), ('purchases','invoice_file','files'), ('purchases','receipt_file','files')]


def safe_name(name):
    return (isinstance(name, str) and 0 < len(name) <= 200 and name not in ('.', '..')
            and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name) and not name.endswith((' ', '.'))
            and not re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name))


def check_db(c):
    if c.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('データベースの整合性チェックに失敗しました')
    if c.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('データベースの参照整合性チェックに失敗しました')
    if c.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
        raise ValueError('新しいバージョンのバックアップです。アプリを更新してください')
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'cases','company','subcontractors','suppliers','settings','issued_documents','settlements'} <= tables:
        raise ValueError('対応するLOGIBOXの業務DBではありません')


def references(c):
    for table, field, folder in REFERENCES:
        columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
        if field in columns:
            for (name,) in c.execute(f'SELECT {field} FROM {table}'):
                if name:
                    if not safe_name(name):
                        raise ValueError('DB内に移行できない原本パスがあります：' + str(name)[:120])
                    yield folder + '/' + name


def history_names(c):
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        quoted='"'+table.replace('"','""')+'"'
        if 'backup_file' not in {r[1] for r in c.execute('PRAGMA table_info('+quoted+')')}:continue
        for (name,) in c.execute('SELECT DISTINCT backup_file FROM '+quoted):
            if not name:continue
            if not re.fullmatch(r'before-carry-edit-[0-9TZ]+-[0-9a-f]+\.db',name):
                raise ValueError('未対応の変更前DB保存名があります')
            yield name


TEMPLATE_NAMES = ('haak_invoice.pdf', 'principal_invoice.pdf')


def template_files(data, app):
    return {name: (Path(data)/'templates'/name if (Path(data)/'templates'/name).is_file()
                   else Path(app)/'document_templates'/name) for name in TEMPLATE_NAMES}


def create(c, files, docs, templates=None):
    stream = io.BytesIO()
    with tempfile.TemporaryDirectory() as temporary:
        db = Path(temporary) / 'app.db'
        with closing(sqlite3.connect(db)) as dst:
            c.backup(dst)
            # Migration backups may precede creation of some tables.
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('バックアップDBが破損しています')
        payload = {'app.db': db.read_bytes()}
        for folder, prefix in ((files,'files'), (docs,'documents')):
            root = Path(folder)
            if root.is_symlink():raise ValueError('原本フォルダにリンクがあります')
            if root.exists():
                for path in root.iterdir():
                    if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                        raise ValueError('原本にリンクがあります')
                    if not path.is_file() or not safe_name(path.name):
                        raise ValueError('バックアップできない原本があります：' + path.name)
                    payload[prefix + '/' + path.name] = path.read_bytes()
        for name in sorted(set(history_names(c))):
            path=Path(files).parent/name
            if path.is_symlink():raise ValueError('変更前DBにリンクがあります')
            if path.is_file():payload['history/'+name]=path.read_bytes()
        for name,path in (templates or {}).items():
            if name not in TEMPLATE_NAMES or path.is_symlink():raise ValueError('帳票原本が不正です')
            payload['templates/'+name]=path.read_bytes()
        info = dict(format=1, app='LOGIBOX', version=VERSION, schema=c.execute('PRAGMA user_version').fetchone()[0],
                    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    sha256={n:hashlib.sha256(raw).hexdigest() for n,raw in payload.items()})
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
            for name, raw in payload.items():z.writestr(name,raw)
            z.writestr('backup-info.json',json.dumps(info,ensure_ascii=False))
    return stream.getvalue()


def unpack(raw, target):
    if len(raw) > MAX_ZIP:raise ValueError('バックアップZIPは180MB以内にしてください')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist()
            if len(entries)>20000 or sum(i.file_size for i in entries)>MAX_EXPANDED:
                raise ValueError('ZIPの展開サイズまたはファイル数が上限を超えています')
            seen=set()
            for i in entries:
                parts=i.filename.split('/')
                allowed=(i.filename in ('app.db','backup-info.json') or
                         len(parts)==2 and parts[0] in ('files','documents') and safe_name(parts[1]) or
                         len(parts)==2 and parts[0]=='templates' and parts[1] in TEMPLATE_NAMES or
                         len(parts)==2 and parts[0]=='history' and re.fullmatch(r'before-carry-edit-[0-9TZ]+-[0-9a-f]+\.db',parts[1]))
                mode=i.external_attr>>16
                if not allowed or unicodedata.normalize('NFC',i.filename).casefold() in seen or i.flag_bits&1 or stat.S_ISLNK(mode) or i.is_dir():
                    raise ValueError('ZIPに未対応・不正・重複ファイルがあります：'+i.filename[:120])
                seen.add(unicodedata.normalize('NFC',i.filename).casefold())
            if 'app.db' not in {i.filename for i in entries}:raise ValueError('app.dbがありません')
            # Reading every member checks CRC before any live data is touched.
            for i in entries:
                path=target/i.filename
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(z.read(i))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('破損または未対応のZIPです') from exc
    info={}
    if (target/'backup-info.json').exists():
        info=json.loads((target/'backup-info.json').read_text(encoding='utf-8'))
        if not isinstance(info,dict) or info.get('app')!='LOGIBOX' or info.get('format')!=1:raise ValueError('未対応のバックアップ形式です')
        hashes=info.get('sha256',{})
        if not isinstance(hashes,dict) or any(not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v) for v in hashes.values()):raise ValueError('バックアップの検証値が不正です')
        names={i.filename for i in entries}-{'backup-info.json'}
        if set(hashes)!=names:raise ValueError('バックアップのファイル一覧が一致しません')
        for name in names:
            if hashlib.sha256((target/name).read_bytes()).hexdigest()!=hashes[name]:
                raise ValueError('バックアップの検証値が一致しません')
        try:newer=tuple(map(int,info['version'].split('.')))>tuple(map(int,VERSION.split('.')))
        except (KeyError,ValueError,AttributeError):raise ValueError('バージョン情報が不正です')
        if newer:raise ValueError('アプリより新しいバックアップです')
    return info


def prepare(raw, app_root, data_root):
    now=time.time()
    for token,(path,expires,_) in list(PENDING.items()):
        if expires<now:
            shutil.rmtree(path,ignore_errors=True);PENDING.pop(token,None)
    if len(PENDING)>=3:raise ValueError('確認中のZIPが多すぎます。15分後に再試行してください')
    stage=Path(tempfile.mkdtemp(prefix='.restore-preview-',dir=data_root))
    try:
        info=unpack(raw,stage)
        with closing(sqlite3.connect(stage/'app.db')) as c:
            check_db(c)
            old_schema=c.execute('PRAGMA user_version').fetchone()[0]
        env=dict(os.environ,APP_DATA=str(stage))
        # Separate process prevents migrations from reading or writing the active data paths.
        result=subprocess.run([sys.executable,'-c',
            'import server; from contextlib import closing\nwith closing(server.db()) as c: server.init_schema(c, backup=False)'],
            cwd=app_root,env=env,capture_output=True,timeout=120)
        if result.returncode:raise ValueError('旧DBの構造を更新できません。対応するバックアップか確認してください')
        with closing(sqlite3.connect(stage/'app.db')) as c:
            check_db(c)
            missing=sorted({name for name in references(c) if not (stage/name).is_file()})
            missing_history=sorted({name for name in history_names(c) if not (stage/'history'/name).is_file()})
            counts={table:c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('cases','issued_documents','issued_document_history','subcontractors','settlements','profit_documents')}
        warnings=[]
        if missing_history:warnings.append('変更前DBの補助バックアップはZIPに含まれず復元できません（現在の台帳と変更履歴はDBから復元します）：'+ '、'.join(missing_history))
        if not info:warnings.append('旧形式にはバックアップ日時・作成バージョンの記録がありません')
        absent=[name for name in TEMPLATE_NAMES if not (stage/'templates'/name).is_file()]
        if absent:
            warnings.append('このZIPには固定帳票テンプレートがありません。移行元のテンプレートは復元できません。このPCの既存テンプレートを使用します。')
            (stage/'templates').mkdir(exist_ok=True)
            for name,path in template_files(data_root,app_root).items():
                if name not in absent:continue
                if path.is_symlink():raise ValueError('帳票原本にリンクがあります')
                shutil.copy2(path,stage/'templates'/name)
        import fitz
        for name in TEMPLATE_NAMES:
            try:
                with fitz.open(stage/'templates'/name) as pdf:
                    if not pdf.is_pdf or pdf.needs_pass or len(pdf)<1:raise ValueError('invalid template')
            except Exception as exc:raise ValueError('固定帳票テンプレートPDFを読み込めません：'+name) from exc
        token=secrets.token_urlsafe(32)
        report=dict(token=token,created_at=info.get('created_at'),version=info.get('version','旧形式（バージョン記録なし）'),
                    schema=old_schema,counts=counts,missing=missing,
                    warnings=warnings)
        PENDING[token]=(stage,now+900,report)
        return report
    except Exception:
        shutil.rmtree(stage,ignore_errors=True)
        raise


def copy_originals(source, data):
    for name in ('files','documents','templates'):
        dest=data/name
        if dest.is_symlink() or (hasattr(dest,'is_junction') and dest.is_junction()):raise ValueError('復元先にリンクがあります')
        if dest.exists():shutil.rmtree(dest)
        if (source/name).exists():shutil.copytree(source/name,dest)
        else:dest.mkdir()
    if (source/'history').exists():
        for path in (source/'history').iterdir():
            dest=data/path.name
            if dest.is_symlink():raise ValueError('変更前DBの保存先にリンクがあります')
            shutil.copy2(path,dest)
    manifest=data/'pdf_export_manifest.json'
    if manifest.exists():manifest.unlink()
    if (source/'pdf_export_manifest.json').exists():shutil.copy2(source/'pdf_export_manifest.json',manifest)


def load_db(source,c):
    c.rollback()
    with closing(sqlite3.connect(source/'app.db')) as src:src.backup(c)


def durable_data(data):
    for name in ('app.db','pdf_export_manifest.json'):
        path=data/name
        if path.is_file():
            with path.open('rb') as f:os.fsync(f.fileno())
    for name in ('files','documents','templates'):
        if (data/name).exists():
            for path in (data/name).iterdir():
                with path.open('rb') as f:os.fsync(f.fileno())


def recover(data,c):
    data=Path(data)
    journal=data/'.restore-journal'
    if not journal.exists():return False
    previous=data/'.restore-rollback'
    if not (previous/'app.db').is_file():raise RuntimeError('自動復旧用DBがありません。起動を中止します')
    copy_originals(previous,data)
    load_db(previous,c)
    durable_data(data)
    journal.unlink()
    shutil.rmtree(previous)
    return True


def restore(c,token,data_root,files,docs,keep_password,app_root=None):
    item=PENDING.pop(token,None)
    if not item or item[1]<time.time():raise ValueError('確認の有効期限が切れました。ZIPを選び直してください')
    stage,_,report=item
    data=Path(data_root)
    previous=data/'.restore-rollback'
    journal=data/'.restore-journal'
    try:
        if Path(files)!=data/'files' or Path(docs)!=data/'documents':raise ValueError('未対応の保存構造です')
        if journal.exists():raise ValueError('自動復旧が必要です。アプリを再起動してください')
        if previous.exists():shutil.rmtree(previous)
        previous.mkdir(mode=0o700)
        raw=create(c,files,docs,template_files(data,app_root) if app_root else None)
        backups=data/'backups';backups.mkdir(mode=0o700,exist_ok=True)
        if backups.is_symlink():raise ValueError('バックアップ先にリンクがあります')
        backup=backups/('before-restore-'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'-'+secrets.token_hex(4)+'.zip')
        with os.fdopen(os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        unpack(backup.read_bytes(),previous)
        manifest=data/'pdf_export_manifest.json'
        if manifest.exists():shutil.copy2(manifest,previous/manifest.name)
        with closing(sqlite3.connect(stage/'app.db')) as dst:
            # Authentication belongs to the destination installation. No plaintext credentials.
            dst.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('pw',?)",(keep_password,))
            dst.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('pdf_archive_dir','')")
            dst.commit();check_db(dst)
        with journal.open('xb') as f:
            f.write(b'restore in progress\n');f.flush();os.fsync(f.fileno())
        try:
            copy_originals(stage,data)
            load_db(stage,c)
            durable_data(data)
            journal.unlink()
        except Exception:
            recover(data,c)
            raise ValueError('復元に失敗したため、復元前の状態に戻しました')
        shutil.rmtree(previous,ignore_errors=True)
        return dict(ok=True,backup=str(backup),missing=report['missing'])
    finally:
        shutil.rmtree(stage,ignore_errors=True)
