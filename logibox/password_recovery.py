"""Offline administrator recovery. Never imports/initializes the application DB."""
from contextlib import closing
import ctypes
import datetime
import getpass
import errno
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import tempfile
import zipfile

from auth_security import data_lock, hash_pw

ROOT = Path(__file__).resolve().parent
from app_version import VERSION


def require_administrator():
    if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError('Windowsの管理者確認が必要です。「パスワード再設定.vbs」から開いてください。')


def ensure_stopped():
    """Check port ownership locally, without a timed network connection.

    Windows may take longer than a short connect timeout to refuse an unused
    loopback port. An exclusive bind detects legacy servers too, without
    treating that timeout as a network restriction. Never enable SO_REUSEADDR.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', 8766))
    except OSError as error:
        code = getattr(error, 'winerror', None) or error.errno
        if code in (errno.EADDRINUSE, 10048):
            raise RuntimeError(
                'LOGIBOXのポート8766が使用中です。LOGIBOXを終了するか、'
                'PCを再起動し、LOGIBOXを開かずにもう一度実行してください。') from error
        raise RuntimeError(
            'ポート8766の停止確認ができませんでした。元データは変更していません。'
            '\nLOGIBOXを終了して再実行してください。解消しない場合は、'
            '次のエラー内容をお知らせください。\n' + str(error)) from error


def connect_existing(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.name.lower() != 'app.db':
        raise ValueError('現在使用しているdataフォルダのapp.dbを選んでください。')
    return sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)


def inspect_database(connection):
    if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise ValueError('DBの整合性を確認できませんでした。変更せずに中止します。')
    if connection.execute('PRAGMA user_version').fetchone()[0] not in (6, 7, 8, 9, 10, 11, 12, 13):
        raise ValueError('対応するLOGIBOXのDB（スキーマ6〜13）を選んでください。DBの移行や初期化は行いません。')
    required = {'settings': {'k', 'v'}, 'cases': {'id', 'name', 'mgmt_no'},
                'company': {'id', 'name'}, 'issued_documents': {'id'}}
    for table, columns in required.items():
        actual = {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}
        if not columns <= actual:
            raise ValueError('LOGIBOXのDBとして確認できませんでした。変更せずに中止します。')
    row = connection.execute("SELECT v FROM settings WHERE k='pw'").fetchone()
    if not row or not isinstance(row[0], str) or not row[0]:
        raise ValueError('設定済みパスワードがありません。初期設定は通常の起動画面から行ってください。')
    company = connection.execute('SELECT name FROM company WHERE id=1').fetchone()
    count = connection.execute('SELECT count(*) FROM cases').fetchone()[0]
    return row[0], (company[0] if company else '') or '（未登録）', count


def add_tree(archive, source, prefix, excluded=()):
    if source.is_symlink():
        raise ValueError('リンクされた保存フォルダには対応していません。管理者にバックアップを依頼してください。')
    if not source.exists():
        return
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise ValueError('保存先にリンクがあります。完全なバックアップを確認できないため中止します。')
        for name in files:
            item = Path(directory) / name
            relative = item.relative_to(source)
            if relative.as_posix() not in excluded:
                archive.write(item, (Path(prefix) / relative).as_posix())


def create_backup(db_path, backup_dir, app_root):
    """Called under data lock + SQLite RESERVED writer lock; failure aborts reset."""
    data = db_path.parent
    backup_dir = Path(backup_dir).resolve(strict=True)
    if not backup_dir.is_dir():
        raise ValueError('バックアップ先フォルダを選んでください。')
    trees = [(data, 'data'), (app_root / 'document_templates', 'document_templates'),
             (app_root / 'fonts', 'fonts'), (app_root / 'LOGIBOX PDF', 'LOGIBOX PDF')]
    for source, _ in trees:
        if backup_dir == source.resolve() or source.resolve() in backup_dir.parents:
            raise ValueError('バックアップ先はdata・帳票テンプレート・PDF・fontsの外を選んでください。')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    result = backup_dir / f'LOGIBOX-before-password-reset-{stamp}-{secrets.token_hex(4)}.zip'
    partial = result.with_suffix('.zip.partial')
    try:
        with tempfile.TemporaryDirectory(prefix='logibox-recovery-') as temporary:
            snapshot = Path(temporary) / 'app.db'
            with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('バックアップDBの整合性を確認できませんでした。')
            with zipfile.ZipFile(partial, 'x', zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, 'data/app.db')
                for source, prefix in trees:
                    add_tree(archive, source, prefix,
                             ('app.db', 'app.db-wal', 'app.db-shm', 'app.db-journal', '.logibox-auth.lock') if prefix == 'data' else ())
                archive.writestr('recovery-backup-info.json', json.dumps({
                    'version': VERSION, 'created_at': datetime.datetime.now().astimezone().isoformat(),
                    'database': str(db_path), 'application': str(app_root),
                    'windows_administrator': getpass.getuser(),
                    'purpose': 'Password recovery backup; contains the OLD credential.'
                }, ensure_ascii=False, indent=2))
            with zipfile.ZipFile(partial) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError('バックアップZIPの検証に失敗しました。')
            partial.rename(result)
        return result
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def reset_password(db_path, new_password, confirmation, backup_dir, app_root=ROOT):
    require_administrator()  # Enforced in the writer, including direct Python invocation.
    if new_password != confirmation:
        raise ValueError('新しいパスワードが一致しません。')
    if not 8 <= len(new_password) <= 1024 or not new_password.strip():
        raise ValueError('新しいパスワードは8～1024文字で入力してください。')
    db_path = Path(db_path).resolve(strict=True)
    app_root = Path(app_root).resolve(strict=True)
    if not (app_root / 'document_templates').is_dir() or not (app_root / 'schema.sql').is_file():
        raise ValueError('復旧ツールをLOGIBOXのアプリフォルダに配置してください。')
    ensure_stopped()
    with data_lock(db_path.parent), closing(connect_existing(db_path)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            previous, _, _ = inspect_database(connection)
            backup = create_backup(db_path, backup_dir, app_root)
            ensure_stopped()
            updated = connection.execute("UPDATE settings SET v=? WHERE k='pw' AND v=?",
                                         (hash_pw(new_password), previous))
            if updated.rowcount != 1:
                raise RuntimeError('パスワードが別の操作で変更されました。変更せずに中止します。')
            connection.commit()
            return backup
        except BaseException:
            connection.rollback()
            raise


def main():
    import argparse
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    parser = argparse.ArgumentParser(description='LOGIBOX Windows administrator recovery')
    parser.add_argument('--data', type=Path, required=True)
    args = parser.parse_args()
    window = tk.Tk()
    window.withdraw()
    try:
        require_administrator()
    except PermissionError as error:
        messagebox.showerror('LOGIBOX パスワード再設定', str(error), parent=window)
        window.destroy()
        return 1
    window.title('LOGIBOX ver.0.6.2 パスワード再設定（Windows管理者用）')
    window.minsize(740, 520)
    body = ttk.Frame(window, padding=22)
    body.pack(fill='both', expand=True)
    body.columnconfigure(1, weight=1)
    ttk.Label(body, text='管理者によるパスワード再設定', font=('Yu Gothic UI', 17, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 12))
    ttk.Label(body, text='PCを再起動し、LOGIBOXを起動していない状態で操作してください。\nバックアップの作成・検証後、パスワードのみ変更します。', wraplength=690).grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 14))
    database = tk.StringVar(value=str(args.data.resolve() / 'app.db'))
    backup_dir = tk.StringVar(value=str(ROOT.parent))
    first, second = tk.StringVar(), tk.StringVar()
    consent = tk.BooleanVar(value=False)
    info = tk.StringVar(value='対象DBを確認してください。')

    def inspect():
        try:
            with closing(connect_existing(database.get())) as connection:
                _, company, count = inspect_database(connection)
            info.set(f'会社：{company}　／　案件数：{count}件')
        except Exception as error:
            info.set(str(error))

    def browse_db():
        chosen = filedialog.askopenfilename(parent=window, title='使用中のdata/app.dbを選択', initialdir=args.data, filetypes=[('LOGIBOX DB', 'app.db')])
        if chosen:
            database.set(chosen)
            inspect()

    def browse_backup():
        chosen = filedialog.askdirectory(parent=window, title='バックアップ先（dataの外）を選択', mustexist=True)
        if chosen:
            backup_dir.set(chosen)

    for row, label, variable, browse in ((2, '対象のDB', database, browse_db), (4, 'バックアップ先', backup_dir, browse_backup)):
        ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=7)
        ttk.Entry(body, textvariable=variable).grid(row=row, column=1, sticky='ew')
        ttk.Button(body, text='参照…', command=browse).grid(row=row, column=2, padx=(8, 0))
    ttk.Label(body, textvariable=info, wraplength=690).grid(row=3, column=0, columnspan=3, sticky='w', pady=5)
    for row, label, variable in ((5, '新しいパスワード', first), (6, 'もう一度入力', second)):
        ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=9)
        ttk.Entry(body, textvariable=variable, show='●').grid(row=row, column=1, sticky='ew')
    ttk.Label(body, text='8～1024文字。Windowsのパスワードとは別の、LOGIBOX用のパスワードです。', wraplength=690).grid(row=7, column=0, columnspan=3, sticky='w', pady=7)
    ttk.Checkbutton(body, text='私はこのPCとLOGIBOXの管理を許可されており、対象DBを確認しました。', variable=consent).grid(row=8, column=0, columnspan=3, sticky='w', pady=12)
    status = tk.StringVar()
    ttk.Label(body, textvariable=status, wraplength=690).grid(row=10, column=0, columnspan=3, sticky='w', pady=10)

    def submit():
        if not consent.get():
            messagebox.showerror('確認が必要です', '管理の許可と対象DBを確認し、チェックを入れてください。', parent=window)
            return
        if first.get() != second.get() or not 8 <= len(first.get()) <= 1024 or not first.get().strip():
            messagebox.showerror('入力を確認してください', '8～1024文字の同じパスワードを2回入力してください。', parent=window)
            return
        inspect()
        if not messagebox.askokcancel('再設定の最終確認', f'対象：{database.get()}\n{info.get()}\n\nバックアップを保存してパスワードを変更します。よろしいですか？', parent=window):
            return
        button.configure(state='disabled')
        status.set('バックアップを作成・検証しています。完了するまで閉じないでください。')
        window.update_idletasks()
        try:
            backup = reset_password(database.get(), first.get(), second.get(), backup_dir.get())
        except Exception as error:
            first.set('')
            second.set('')
            status.set('再設定できませんでした。パスワードは変更していません。')
            messagebox.showerror('再設定を中止しました', str(error), parent=window)
        else:
            first.set('')
            second.set('')
            status.set('再設定が完了しました。この画面を閉じ、起動.vbsからログインしてください。')
            messagebox.showinfo('再設定が完了しました', f'新しいパスワードでログインしてください。\n案件・請求書・会社情報・帳票テンプレートは保持しました。\n\nバックアップ：\n{backup}', parent=window)
            window.destroy()
            return
        button.configure(state='normal')

    button = ttk.Button(body, text='バックアップを作成して再設定', command=submit)
    button.grid(row=9, column=0, columnspan=2, sticky='w', pady=8)
    ttk.Button(body, text='閉じる', command=window.destroy).grid(row=9, column=2, sticky='e')
    inspect()
    window.deiconify()
    window.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
