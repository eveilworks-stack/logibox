"""案件別の証憑フォルダと一覧・一式PDFを安全に同期する。"""
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
import evidence_documents

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


def case_folder(row):
    date = row['due_date'] or row['start_date'] or ''
    year = date[:4] + '年' if re.match(r'^\d{4}-', date) else '年度未設定'
    return str(Path(year) / (safe(row['mgmt_no']) + '_' + safe(row['case_no'] or '案件番号未登録')[:24]
                            + '_' + safe(row['name'])[:28]))


def checked_target(root, name):
    rel = Path(name)
    if (not name or rel.is_absolute() or '..' in rel.parts or '\\' in name
            or any(':' in part for part in rel.parts)):
        raise ValueError('PDFコピーの管理ファイル名を確認してください')
    path = root / rel
    for parent in [path, *path.parents]:
        if parent == root: break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('証憑保存先に外部リンクがあります')
    if not inside(path, root):
        raise ValueError('証憑保存先がフォルダ外を指しています')
    return path


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
            if path.suffix.lower() not in ('.pdf','.png','.jpg','.jpeg'):return None
            if not path.is_file():
                if required:raise ValueError('PDF原本が見つかりません：' + stored)
                return None
            value = digest(path)
            known.add(value)
            return path, value

        groups = {cid: [] for cid in cases}
        references = {cid: [] for cid in cases}
        def add(key, folder, stored, stem, cid=None, label=None):
            found = source(folder, stored)
            if found:
                item = dict(key=key, source=found[0], digest=found[1], stem=stem,
                            suffix=found[0].suffix.lower(), label=label or Path(stem).name)
                wanted.append(item)
                if cid is not None: groups[cid].append(item)
                return item

        for row in c.execute('SELECT * FROM attachments ORDER BY id'):
            category = '01_仕入' if row['kind'] != 'other' else '04_その他'
            label = ATTACHMENT_LABELS[row['kind']] + '_' + safe(Path(row['original_name']).stem)[:45]
            add(f"attachment:{row['id']}", files, row['stored_name'],
                str(Path(case_folder(cases[row['case_id']])) / category / label), row['case_id'], label)
        for row in c.execute('SELECT * FROM issued_documents ORDER BY id'):
            snapshot = json.loads(row['snapshot_json'])
            number = snapshot.get('doc_no')
            label = LABELS[row['doc_type']] + '_' + safe(snapshot.get('recipient') or number or row['id'])[:28]
            stem = (safe(number.removesuffix('.pdf'))[:36] + '_' + label if isinstance(number, str) and number else label)
            category = '03_ハーク' if row['doc_type']=='haak_invoice' else '02_下請け'
            add(f"issued:{row['id']}", docs, row['file'],
                str(Path(case_folder(cases[row['case_id']])) / category / stem), row['case_id'], label)
        for row in c.execute('''SELECT i.*,s.period,sc.name AS sub_name FROM sub_invoices i
                                JOIN settlements s ON s.id=i.settlement_id
                                JOIN subcontractors sc ON sc.id=s.sub_id
                                WHERE i.id=(SELECT MAX(n.id) FROM sub_invoices n WHERE n.settlement_id=i.settlement_id)
                                ORDER BY i.id'''):
            item = add(f"received:{row['settlement_id']}", docs, row['file'],
                       str(Path('月次精算') / safe(row['period']) / (safe(row['sub_name'])+'_受領請求書')))
            if item:
                for line in c.execute('SELECT case_id FROM settlement_lines WHERE settlement_id=?', (row['settlement_id'],)):
                    if line['case_id'] in references: references[line['case_id']].append(item)
        # 旧版の原本は保持し、旧形式の副本を識別するためだけに参照する。
        for table, field, folder in [('attachment_revisions', 'stored_name', files),
                                     ('issued_document_history', 'file', docs), ('sub_invoices', 'file', docs)]:
            for row in c.execute(f'SELECT {field} FROM {table}'):
                source(folder, row[field], required=False)

        def owned(path):
            if not path.is_file() or path.is_symlink() or not inside(path, root):return False
            value = digest(path)
            return any(item.get('name') == path.relative_to(root).as_posix() and item.get('digest') == value for item in previous.values())

        assigned, current, replaced = set(), {}, []
        def assign(item):
            index = 1
            while True:
                name = item['stem'] + (f'_{index}' if index > 1 else '') + item.get('suffix', '.pdf')
                name = Path(name).as_posix()
                target = checked_target(root, name)
                if name.casefold() not in assigned:
                    if not target.exists() or (target.is_file() and (digest(target) == item['digest'] or owned(target))):break
                index += 1
            assigned.add(name.casefold())
            item['target'] = target
            item['previous_digest'] = digest(target) if target.is_file() else None
            current[item['key']] = dict(name=name, digest=item['digest'])
            if item['previous_digest'] and item['previous_digest'] != item['digest']:replaced.append(target)

        for item in wanted: assign(item)
        for cid, case in cases.items():
            folder = Path(case_folder(case))
            entries = sorted(groups[cid], key=lambda item: item['target'].relative_to(root).as_posix())
            for entry in entries:
                entry['relative'] = entry['target'].relative_to(root/folder).as_posix()
            refs = [os.path.relpath(item['target'], root/folder).replace(os.sep, '/') for item in references[cid]]
            # Covers contain only stable case metadata and current-document contents.
            signature = hashlib.sha256(json.dumps([dict(case),[(x['relative'],x['digest'],x['label']) for x in entries],refs],
                                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            outputs = None
            for i, name in enumerate(('00_証憑一覧', '案件証憑一式')):
                key = f"case:{cid}:{i}"
                old = previous.get(key, {})
                old_path = checked_target(root, old['name']) if old.get('name') else None
                if old.get('input') == signature and old_path and old_path.is_file() and digest(old_path)==old.get('digest'):
                    raw = old_path.read_bytes()
                else:
                    if outputs is None: outputs = evidence_documents.create(case, entries, refs)
                    raw = outputs[i]
                item = dict(key=key,raw=raw,digest=hashlib.sha256(raw).hexdigest(),stem=str(folder/name))
                assign(item)
                current[key]['input'] = signature
                wanted.append(item)

        remove = []
        final_names = {item['name'].casefold() for item in current.values()}
        for item in previous.values():
            name = item.get('name', '')
            path = checked_target(root, name)
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
                raw = item['raw'] if 'raw' in item else item['source'].read_bytes()
                if hashlib.sha256(raw).hexdigest() != item['digest']:
                    raise ValueError('PDF原本が処理中に変更されました。もう一度保存してください')
                checked_target(root, target.relative_to(root).as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
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
        state['version'] = 2
        state['roots'][str(root.resolve())] = current
        atomic_write(manifest_file, json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'))
        return copied
