"""LOGIBOX PDFには名前を整えた現在版の読込PDFと正式発行PDFを置く。"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import zipfile

LOCK = threading.Lock()
LABELS = {'principal_invoice': '元金請求書', 'haak_invoice': 'ハーク請求書', 'receipt': '領収書'}
ATTACHMENT_LABELS = {'purchase_invoice': '仕入請求書', 'purchase_receipt': '仕入領収書', 'other': '添付書類'}


def safe(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '＿', str(value)).strip(' .')[:60] or '未設定'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    return value


def day(value):
    parts = str(value or '').split('-')
    month = parts[1] if len(parts) > 1 and parts[1].isdigit() and 1 <= int(parts[1]) <= 12 else '××'
    date = parts[2] if len(parts) > 2 and parts[2].isdigit() and 1 <= int(parts[2]) <= 31 else '××'
    return f'{month.zfill(2)}／{date.zfill(2)}'


def case_name(row):
    return f"{safe(row['case_no'] or 'SP00000')},{day(row['start_date'])}-{day(row['due_date'])},{safe(row['mgmt_no'])}"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(128 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def atomic_write(path, raw):
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.remove(temporary)


def inside(path, root):
    return path.resolve().is_relative_to(root.resolve())


def sync(c, files, docs, root):
    root, files, docs = Path(root), Path(files), Path(docs)
    manifest_file = docs.parent / 'pdf_export_manifest.json'
    with LOCK:
        for originals in (files, docs):
            if inside(root, originals) or inside(originals, root):
                raise ValueError('PDFコピー保存先は、原本フォルダと重ならない場所を指定してください')
        root.mkdir(parents=True, exist_ok=True)
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(manifest_file.read_text(encoding='utf-8')) if manifest_file.exists() else {'version': 1, 'roots': {}}
        if not isinstance(state, dict) or not isinstance(state.get('roots'), dict):
            raise ValueError('PDFコピーの管理情報を確認してください')
        previous = state['roots'].get(str(root.resolve()), {})
        cases = {r['id']: r for r in c.execute('SELECT * FROM cases')}
        wanted, known = [], set()

        def source(folder, stored, required=True):
            if not stored:return None
            if Path(stored).name != stored or '/' in stored or '\\' in stored:
                raise ValueError('PDFの原本保存先情報を確認してください')
            path = folder / stored
            if path.suffix.lower() != '.pdf':return None
            if not path.is_file():
                if required:raise ValueError('PDF原本が見つかりません：' + stored)
                return None
            value = digest(path)
            known.add(value)
            return path, value

        def add(key, folder, stored, stem):
            found = source(folder, stored)
            if found:wanted.append(dict(key=key, source=found[0], digest=found[1], stem=stem))

        for row in c.execute("SELECT * FROM attachments WHERE mime='application/pdf' ORDER BY id"):
            add(f"attachment:{row['id']}", files, row['stored_name'],
                case_name(cases[row['case_id']]) + '_' + ATTACHMENT_LABELS[row['kind']])
        for row in c.execute('SELECT * FROM issued_documents ORDER BY id'):
            snapshot = json.loads(row['snapshot_json'])
            number = snapshot.get('doc_no')
            stem = safe(number.removesuffix('.pdf')) if isinstance(number, str) and number else (
                case_name(cases[row['case_id']]) + '_' + LABELS[row['doc_type']]
                + (f"_下請け{row['sub_id']}" if row['sub_id'] else ''))
            add(f"issued:{row['id']}", docs, row['file'], stem)
        for row in c.execute('''SELECT i.*,s.period,sc.name AS sub_name FROM sub_invoices i
                                JOIN settlements s ON s.id=i.settlement_id
                                JOIN subcontractors sc ON sc.id=s.sub_id
                                WHERE i.id=(SELECT MAX(n.id) FROM sub_invoices n WHERE n.settlement_id=i.settlement_id)
                                ORDER BY i.id'''):
            add(f"received:{row['settlement_id']}", docs, row['file'],
                f"{row['period']}_{safe(row['sub_name'])}_受領請求書")
        # 旧版の原本は保持し、旧形式の副本を識別するためだけに参照する。
        for table, field, folder in [('attachment_revisions', 'stored_name', files),
                                     ('issued_document_history', 'file', docs), ('sub_invoices', 'file', docs)]:
            for row in c.execute(f'SELECT {field} FROM {table}'):
                source(folder, row[field], required=False)

        def owned(path):
            if not path.is_file() or path.is_symlink() or not inside(path, root):return False
            value = digest(path)
            return any(item.get('name') == path.name and item.get('digest') == value for item in previous.values())

        assigned, current, replaced = set(), {}, []
        for item in wanted:
            index = 1
            while True:
                name = item['stem'] + (f'_{index}' if index > 1 else '') + '.pdf'
                target = root / name
                if name.casefold() not in assigned and inside(target, root) and not target.is_symlink():
                    if not target.exists() or (target.is_file() and (digest(target) == item['digest'] or owned(target))):break
                index += 1
            assigned.add(name.casefold())
            item['target'] = target
            item['previous_digest'] = digest(target) if target.is_file() else None
            current[item['key']] = dict(name=name, digest=item['digest'])
            if item['previous_digest'] and item['previous_digest'] != item['digest']:replaced.append(target)

        remove = []
        final_names = {item['name'].casefold() for item in current.values()}
        for item in previous.values():
            name = item.get('name', '')
            if not name or Path(name).name != name or '/' in name or '\\' in name:
                raise ValueError('PDFコピーの管理ファイル名を確認してください')
            path = root / name
            if name.casefold() not in final_names and owned(path):remove.append(path)
        # 旧自動出力の構造・ファイル名・原本の内容がすべて一致する副本だけを移行する。
        for directory, dirs, names in os.walk(root, followlinks=False):
            parent = Path(directory)
            dirs[:] = [name for name in dirs if not (parent/name).is_symlink()
                       and not (hasattr(parent/name, 'is_junction') and (parent/name).is_junction())]
            for name in names:
                path = parent / name
                rel = path.relative_to(root)
                match = re.search(r'_([0-9a-f]{12})\.pdf$', name)
                if (len(rel.parts) == 3 and rel.parts[1] in ('読込PDF', '発行PDF') and match
                        and name.startswith(rel.parts[0] + '_') and inside(path, root) and not path.is_symlink()):
                    value = digest(path)
                    if value in known and value.startswith(match[1]):remove.append(path)
        removed_paths = sorted(set(remove))
        changed_paths = sorted(set([*removed_paths, *replaced]))
        expected = {path: digest(path) for path in changed_paths}
        if changed_paths:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = docs.parent / f'before-pdf-folder-{stamp}-{secrets.token_hex(4)}.zip'
            with zipfile.ZipFile(backup, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in changed_paths:
                    if not inside(path, root):raise ValueError('PDFコピーの移行先がフォルダ外を指しています')
                    archive.write(path, path.relative_to(root).as_posix())
            with zipfile.ZipFile(backup) as archive:
                if archive.testzip() is not None:raise ValueError('PDFコピー変更前のバックアップを確認できませんでした')

        copied = 0
        for item in wanted:
            target = item['target']
            if not target.exists() or digest(target) != item['digest']:
                if target.exists() and digest(target) != item['previous_digest']:
                    raise ValueError('保存先のPDFが処理中に変更されました。もう一度保存してください')
                raw = item['source'].read_bytes()
                if hashlib.sha256(raw).hexdigest() != item['digest']:
                    raise ValueError('PDF原本が処理中に変更されました。もう一度保存してください')
                atomic_write(target, raw)
                copied += 1
        for path in removed_paths:
            if not inside(path, root):raise ValueError('PDFコピーの削除先がフォルダ外を指しています')
            if path.is_file() and digest(path) == expected[path]:path.unlink()
        old_case_dirs = {path.parent.parent for path in removed_paths if len(path.relative_to(root).parts) == 3}
        for case_dir in old_case_dirs:
            for kind in ('読込PDF', '発行PDF'):
                path = case_dir / kind
                if path.is_dir() and inside(path, root) and not path.is_symlink() and not any(path.iterdir()):path.rmdir()
            if inside(case_dir, root) and not any(case_dir.iterdir()):case_dir.rmdir()
        state['roots'][str(root.resolve())] = current
        atomic_write(manifest_file, json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'))
        return copied
