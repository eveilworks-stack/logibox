"""Data-preserving in-place update. Run using the NEW distribution's Python."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

from auth_security import data_lock
from app_version import VERSION
from password_recovery import ensure_stopped

SOURCE = Path(__file__).resolve().parent


def verified_files(source):
    manifest = json.loads((source / 'SOURCE_MANIFEST.json').read_text(encoding='utf-8-sig'))
    if manifest['version'] != VERSION:
        raise RuntimeError('配布物のバージョンが一致しません。')
    result = []
    for name, expected in manifest['sha256'].items():
        rel = Path(name)
        if (rel.is_absolute() or '..' in rel.parts or '\\' in name or ':' in name
                or any(x.lower() in ('data', 'logibox pdf', '.git', '__pycache__') for x in rel.parts)
                or rel.suffix.lower() not in ('.py', '.ps1', '.vbs', '.bat', '.sql', '.txt', '.md', '.ico', '.html', '.js', '.svg', '.ttf', '.pdf')):
            raise RuntimeError('配布対象外のファイルです: ' + name)
        path = source / rel
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('配布ファイルの照合に失敗しました: ' + name)
        result.append(rel)
    return result + [Path('SOURCE_MANIFEST.json')]


def update(target, source=SOURCE):
    target, source = Path(target).resolve(), Path(source).resolve()
    if target == source or target in source.parents or source in target.parents:
        raise RuntimeError('新版は更新先と別のフォルダへ展開してください。')
    if os.environ.get('APP_DATA'):
        raise RuntimeError('APP_DATAが指定されています。対象データの取り違えを防ぐため自動更新を中止しました。管理者が保存先とバックアップを確認してください。')
    data = target / 'data'
    if not (data / 'app.db').is_file():
        raise RuntimeError('更新先にdata/app.dbがありません。利用中のフォルダを指定してください。新規DBは作成しません。')
    files = verified_files(source)
    ensure_stopped()
    # Legacy launchers also hold this lock for the server lifetime.
    with data_lock(data):
        # Never overwrite a destination symlink or junction leading outside target.
        for rel in files:
            destination = target / rel
            if not destination.resolve().is_relative_to(target) or destination.is_symlink():
                raise RuntimeError('更新先に外部へのリンクがあります: ' + str(rel))
        for rel in files:
            cache = (target / rel).parent / '__pycache__'
            if not cache.resolve().is_relative_to(target):
                raise RuntimeError('外部へのキャッシュリンクがあるため更新を中止しました。')
        backup_dir = Path(tempfile.mkdtemp(prefix='LOGIBOX-before-' + VERSION + '-', dir=target.parent))
        backup = backup_dir / 'full-installation.zip'
        print('バックアップ先:', backup, flush=True)
        with zipfile.ZipFile(backup, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(target.rglob('*')):
                if path.is_file() and path.name != '.logibox-auth.lock':
                    archive.write(path, path.relative_to(target).as_posix())
        with zipfile.ZipFile(backup) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('バックアップの照合に失敗したため更新しません。')
            # Check every data file byte-for-byte before touching application files.
            for path in data.rglob('*'):
                if path.is_file() and path.name != '.logibox-auth.lock':
                    saved = archive.read(path.relative_to(target).as_posix())
                    if hashlib.sha256(saved).digest() != hashlib.sha256(path.read_bytes()).digest():
                        raise RuntimeError('バックアップ中にデータが変わったため更新しません。')
        try:
            for rel in files:
                destination = target / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as staged:
                    pending = Path(staged.name)
                    staged.write((source / rel).read_bytes())
                try:
                    os.replace(pending, destination)
                finally:
                    pending.unlink(missing_ok=True)
            # Invalidate only bytecode for replaced application modules, after backup.
            for rel in files:
                if rel.suffix == '.py':
                    cache = (target / rel).parent / '__pycache__'
                    for bytecode in cache.glob(rel.stem + '.*.pyc'):
                        bytecode.unlink()
        except Exception as exc:
            raise RuntimeError(f'更新未完了です。起動せずバックアップを管理者へ渡してください: {backup}') from exc
    print(f'ver.{VERSION}へ更新しました。dataは変更していません。')
    print('更新先の起動.vbsを開き、デスクトップのショートカットを更新してください。')
    return backup


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', help='利用中のlogiboxフォルダ（data/app.dbを含む）')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    if args.interactive:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        window = tk.Tk(); window.withdraw()
        selected = filedialog.askdirectory(title='利用中のlogiboxフォルダ（data/app.dbを含む）を選択')
        if not selected:
            raise SystemExit(0)
        try:
            backup = update(selected)
        except Exception as error:
            messagebox.showerror('LOGIBOX 更新中止', str(error)); raise SystemExit(1)
        messagebox.showinfo('LOGIBOX 更新完了',
            f'更新前バックアップ: {backup}\n\n更新先の「起動.vbs」を開いてください。\nデスクトップのアイコンも更新されます。')
    else:
        if not args.target: parser.error('--target または --interactive を指定してください。')
        try:
            update(args.target)
        except Exception as error:
            parser.exit(1, str(error) + '\n')
