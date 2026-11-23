"""Opt-in clean installation: keep company, masters and seal; archive the old installation."""
from contextlib import contextmanager, closing
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

MASTERS = ('company', 'subcontractors', 'suppliers')
COMPLETED = '0.68切替済み.json'


def load_runtime(source):
    sys.path.insert(0, str(source))
    import update
    return update


def existing_database(target):
    return sqlite3.connect((target / 'data/app.db').as_uri() + '?mode=ro', uri=True)


def read_masters(target):
    with closing(existing_database(target)) as c:
        c.execute('BEGIN')
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('元のDBにエラーがあります。変更せず中止しました。')
        version = c.execute('PRAGMA user_version').fetchone()[0]
        if version not in range(6, 13):
            raise RuntimeError('対応するLOGIBOXのDB（スキーマ6〜12）を選んでください。')
        result = {}
        for table in MASTERS:
            rows = c.execute('SELECT * FROM ' + table)
            columns = [x[0] for x in rows.description]
            result[table] = [dict(zip(columns, row)) for row in rows]
        if len(result['company']) != 1:
            raise RuntimeError('会社情報を確認できません。元のデータは変更していません。')
        seal = c.execute("SELECT v FROM settings WHERE k='company_seal_png'").fetchone()
        result['seal'] = seal[0] if seal else None
        seed = target / 'bundled_company_seal.png'
        if seal is None and seed.is_file():
            result['seal'] = base64.b64encode(seed.read_bytes()).decode('ascii')
        result['old_cases'] = c.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
        return result


@contextmanager
def launcher_guard():
    """Block the normal Windows launcher during the directory switch."""
    if os.name != 'nt':
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, r'Local\SeikyuAppLauncher8766')
    if not handle:
        raise RuntimeError('切替用ロックを作成できません。')
    acquired = False
    try:
        if kernel.WaitForSingleObject(handle, 0) not in (0, 128):
            raise RuntimeError('LOGIBOXの起動処理中です。PCを再起動してから実行してください。')
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def reject_links(root):
    for item in (root, *root.rglob('*')):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise RuntimeError('フォルダ内にリンクがあります。保存先を確認できないため中止します。')


def backup_installation(target, destination):
    hashes = {}
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(target.rglob('*')):
            if path.is_file() and path.name != '.logibox-auth.lock':
                name = path.relative_to(target).as_posix()
                with path.open('rb') as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024*1024), b''):
                        digest.update(chunk)
                    hashes[name] = digest.hexdigest()
                archive.write(path, name)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():
            raise RuntimeError('バックアップの照合に失敗しました。元のフォルダは変更していません。')
        for name, digest in hashes.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise RuntimeError('バックアップの内容が一致しません。元のフォルダは変更していません。')
            if hashlib.sha256((target / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('作業中に元ファイルが変更されました。切り替えずに中止します。')
    return hashes


def make_clean_installation(staged, source, files, masters):
    for rel in files:
        destination = staged / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, destination)
    load_runtime(source).verified_files(staged)
    (staged / 'data').mkdir()
    for name in ('files', 'documents', 'backups'):
        (staged / 'data' / name).mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ('APP_DATA', 'LOGIBOX_PDF_DIR')}
    result = subprocess.run([sys.executable, '-c', 'import server; server.init()'], cwd=staged,
                            env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('0.68の準備に失敗しました。元データは変更していません。\n' + result.stderr[-2000:])
    with closing(sqlite3.connect(staged / 'data/app.db')) as c:
        with c:
            for table in MASTERS:
                columns = {x[1] for x in c.execute('PRAGMA table_info(' + table + ')')}
                c.execute('DELETE FROM ' + table)
                for row in masters[table]:
                    if set(row) - columns:
                        raise RuntimeError('会社・マスター情報に未対応の項目があります。変更せず中止します。')
                    names = list(row)
                    c.execute('INSERT INTO ' + table + '(' + ','.join('"'+n+'"' for n in names) + ') VALUES(' + ','.join('?' for _ in names) + ')', tuple(row[n] for n in names))
            c.execute('DELETE FROM settings')
            if masters['seal'] is not None:
                c.execute("INSERT INTO settings(k,v) VALUES('company_seal_png',?)", (masters['seal'],))
        for table in MASTERS:
            cursor = c.execute('SELECT * FROM ' + table)
            columns = [x[0] for x in cursor.description]
            actual = [dict(zip(columns, row)) for row in cursor]
            for row in masters[table]:
                saved = next((x for x in actual if x['id'] == row['id']), None)
                if saved is None or any(saved[k] != v for k, v in row.items()):
                    raise RuntimeError('引継ぎ内容の照合に失敗しました。元データは変更していません。')
        for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            if table not in (*MASTERS, 'settings') and c.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]:
                raise RuntimeError('業務データの初期化を確認できません。切替を中止します。')
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('新しいDBの整合性を確認できません。切替を中止します。')


def install(target, source, progress=lambda message: None):
    original_target = Path(target)
    if original_target.is_symlink() or (hasattr(original_target, 'is_junction') and original_target.is_junction()):
        raise RuntimeError('リンクされたフォルダには対応していません。')
    target, source = original_target.resolve(), Path(source).resolve()
    if target == source or target in source.parents or source in target.parents:
        raise RuntimeError('新しいZIPは、今までのLOGIBOXとは別の場所へ展開してください。')
    if os.environ.get('APP_DATA') or os.environ.get('LOGIBOX_PDF_DIR'):
        raise RuntimeError('データ・PDF保存先の環境変数が指定されています。管理者が実際の保存先を確認してください。')
    if not (target / 'data/app.db').is_file():
        raise RuntimeError('選択したフォルダにdata/app.dbがありません。今まで使っていたlogiboxフォルダを選んでください。')
    if (target / COMPLETED).is_file():
        raise RuntimeError('このフォルダは0.68へ切替済みです。データを再初期化せず、起動.vbsから開いてください。')
    runtime = load_runtime(source)
    if runtime.VERSION != '0.6.8':
        raise RuntimeError('0.68専用の切替パッケージを使用してください。')
    files = runtime.verified_files(source)
    reject_links(target)
    with launcher_guard():
        runtime.ensure_stopped()
        backup_dir = None
        staged = Path(tempfile.mkdtemp(prefix='.LOGIBOX-068-preparing-', dir=target.parent))
        try:
            with runtime.data_lock(target / 'data'):
                masters = read_masters(target)
                progress('会社・下請け・仕入先・社印を0.68へ引き継いでいます…')
                make_clean_installation(staged, source, files, masters)
                progress('今までのフォルダ全体をバックアップして照合しています…')
                backup_dir = Path(tempfile.mkdtemp(prefix='LOGIBOX-before-0.68-masters-', dir=target.parent))
                backup_installation(target, backup_dir / 'full-installation.zip')
                report = dict(version='0.6.8', company=len(masters['company']),
                              subcontractors=len(masters['subcontractors']), suppliers=len(masters['suppliers']),
                              seal=masters['seal'] is not None, previous_cases=masters['old_cases'],
                              password='初回起動時に新しく設定', backup=str(backup_dir))
                (backup_dir / '切替結果.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                (staged / COMPLETED).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                runtime.ensure_stopped()
            # Close DB/lock handles before renaming on Windows. The normal launcher remains excluded.
            archived = backup_dir / 'previous-installation'
            progress('0.68へ切り替えています…')
            os.replace(target, archived)
            try:
                os.replace(staged, target)
            except BaseException:
                os.replace(archived, target)
                raise
            return report
        finally:
            if staged.exists():
                shutil.rmtree(staged)


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    source = Path(__file__).resolve().parent / 'logibox'
    window = tk.Tk(); window.withdraw()
    selected = filedialog.askdirectory(title='今まで使用していた「logibox」フォルダを選んでください')
    if not selected:
        return
    try:
        if (Path(selected) / COMPLETED).is_file():
            messagebox.showinfo('切替済みです', 'このフォルダは0.68へ切替済みです。初期化せずに起動します。')
            subprocess.Popen(['wscript.exe', str(Path(selected) / '起動.vbs')], cwd=selected)
            return
        masters = read_masters(Path(selected).resolve())
        if not messagebox.askyesno('LOGIBOX 0.68へ切替',
                '残すもの：会社情報・下請け'+str(len(masters['subcontractors']))+'件・仕入先'+str(len(masters['suppliers']))+'件・社印\n'
                '案件・請求書・入金・充当・添付は、新しい0.68では空になります。\n'
                '今までのフォルダ全体は復旧用バックアップへ退避します。\n'
                '初回起動時に新しいパスワードを設定します。\n\n'
                '切替先：'+selected+'\n\nこの内容で切り替えて起動しますか？'):
            return
        status = tk.Toplevel(window); status.title('LOGIBOX 0.68 切替中')
        label = tk.Label(status, text='準備しています…', padx=30, pady=25); label.pack()
        status.protocol('WM_DELETE_WINDOW', lambda: None)
        def progress(message):
            label.configure(text=message); status.update_idletasks()
        runtime = load_runtime(source)
        runtime.ensure_stopped()
        try:
            subprocess.run([sys.executable, '-c', 'import fitz, reportlab, PIL'], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            progress('初回の準備中です。インターネットに接続してお待ちください…')
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(source / 'requirements.txt')], check=True)
        report = install(selected, source, progress)
        status.destroy()
        messagebox.showinfo('0.68への切替完了', '会社情報・下請け・仕入先・社印を引き継ぎました。\n0.68を起動します。最初にパスワードを設定してください。\n\n元データのバックアップ：\n'+report['backup'])
        subprocess.Popen(['wscript.exe', str(Path(selected) / '起動.vbs')], cwd=selected)
    except Exception as error:
        messagebox.showerror('切替を完了できませんでした', str(error)+'\n\n元のフォルダ、または隣のLOGIBOX-before-0.68-masters-*を保管してください。')
    finally:
        window.destroy()


if __name__ == '__main__':
    main()
