#!/usr/bin/env python3
"""LOGIBOX(フェーズ1)。Python標準ライブラリ+SQLite。既定は自分のPC内(127.0.0.1)だけで動く。"""
import argparse, base64, csv, datetime, hashlib, io, json, os, re, secrets, sqlite3, tempfile, threading, time, unicodedata, webbrowser, zipfile
from contextlib import closing
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import calc
import attachments
import documents
import line_case_reader
import purchase_reader
import pdf_archive
import settlement_documents
import carry_management
import principal_funding
import principal_balance
import funding_ledger
import case_summary
from auth_security import hash_pw, check_pw, data_lock
from app_version import DISPLAY_VERSION, VERSION, SCHEMA_VERSION, runtime_identity

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('APP_DATA') or os.path.join(BASE, 'data')
os.makedirs(DATA, exist_ok=True)
DBP = os.path.join(DATA, 'app.db')
SEAL_SEED = os.path.join(BASE, 'bundled_company_seal.png')
FILES = os.path.join(DATA, 'files')
DOCS = os.path.join(DATA, 'documents')
SESS = {}
DOC_DRAFTS = {}
DOC_DRAFT_LOCK = threading.Lock()
DOC_DRAFT_TTL = 15 * 60
DOC_DRAFT_LIMIT = 24
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SUB_TAX_MODE = 'consignment'
RUNTIME = runtime_identity()
SUB_DETAILS = {'postal_code': ('郵便番号', 12), 'address': ('住所', 200),
               'phone': ('電話番号', 30), 'email': ('メールアドレス', 100),
               'contact_name': ('担当者', 80), 'bank_name': ('銀行名', 80),
               'bank_branch': ('支店名', 80), 'account_type': ('口座種別', 20),
               'account_number': ('口座番号', 30), 'account_holder': ('口座名義', 100)}
COMPANY_DETAILS = {
    'postal_code': ('郵便番号', 12), 'phone': ('電話番号', 30),
    'email': ('メールアドレス', 100), 'contact_name': ('担当者', 80),
    'bank_branch': ('銀行・支店', 100), 'bank_account': ('口座種別・番号', 100),
    'bank_holder': ('口座名義', 100), 'invoice_type': ('請求書の種類', 20),
}


class Err(Exception):
    def __init__(self, msg, code=400):
        self.msg, self.code = msg, code


def db():
    c = sqlite3.connect(DBP)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    return c


def init():
    with closing(db()) as c:
        init_schema(c)
        sync_pdf_archive(c)


def pdf_archive_root(c):
    default_base = BASE if os.path.abspath(DBP) == os.path.join(BASE, 'data', 'app.db') else os.path.dirname(DBP)
    value = os.environ.get('LOGIBOX_PDF_DIR') or getset(c, 'pdf_archive_dir') or os.path.join(default_base, 'LOGIBOX PDF')
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))


def sync_pdf_archive(c):
    root = pdf_archive_root(c)
    error_file = os.path.join(os.path.dirname(DBP), 'PDF保存エラー.txt')
    try:
        pdf_archive.sync(c, FILES, DOCS, root)
        if os.path.exists(error_file):
            os.remove(error_file)
    except Exception as exc:
        with open(error_file, 'w', encoding='utf-8') as out:
            out.write('原本はアプリ内に保存されています。コピーを再試行するにはアプリを再起動してください。\n' + str(exc))


def r_pdf_archive_settings(c, b, q):
    error_file = os.path.join(os.path.dirname(DBP), 'PDF保存エラー.txt')
    error = None
    if os.path.isfile(error_file):
        with open(error_file, encoding='utf-8') as source:
            error = source.read()
    return dict(directory=getset(c, 'pdf_archive_dir') or '', effective_directory=pdf_archive_root(c),
                environment_override=bool(os.environ.get('LOGIBOX_PDF_DIR')), error=error,
                originals_directory=os.path.abspath(DOCS))


def r_pdf_archive_settings_put(c, b, q):
    value = txt(b, 'directory', 'PDFコピー保存先', n=1000)
    if value and (not os.path.isabs(value) or any(ord(ch) < 32 for ch in value)):
        raise Err('PDFコピー保存先は絶対パスで指定してください')
    setset(c, 'pdf_archive_dir', value or '')
    sync_pdf_archive(c)
    return r_pdf_archive_settings(c, {}, {})


def init_schema(c):
    existing = c.execute("SELECT 1 FROM sqlite_master WHERE name='cases'").fetchone()
    version = c.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError('このDBには新しいバージョンのアプリが必要です。')
    if not existing:
        with open(os.path.join(BASE, 'schema.sql'), encoding='utf-8') as schema:
            c.executescript(schema.read())
    elif version < SCHEMA_VERSION:
        backup = r_backup_full(c, {}, {})[1]
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(DBP), prefix='before-upgrade-', suffix='.zip', delete=False) as file:
            file.write(backup)
    c.execute('BEGIN IMMEDIATE')
    funding_ledger.init(c)
    c.execute('CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)')
    # 既存データに対しても安全にフェーズ2の追加テーブルを作成
    c.execute("""CREATE TABLE IF NOT EXISTS attachments (id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE RESTRICT, purchase_id INTEGER REFERENCES purchases(id) ON DELETE SET NULL, kind TEXT NOT NULL CHECK(kind IN ('purchase_invoice','purchase_receipt','other')), original_name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE, mime TEXT NOT NULL, bytes INTEGER NOT NULL, created_at TEXT NOT NULL)""")
    c.execute('''CREATE TABLE IF NOT EXISTS purchase_invoice_details (
        purchase_id INTEGER PRIMARY KEY REFERENCES purchases(id) ON DELETE CASCADE,
        original TEXT NOT NULL, reviewed TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS attachment_revisions (
        id INTEGER PRIMARY KEY, attachment_id INTEGER NOT NULL REFERENCES attachments(id),
        original_name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
        mime TEXT NOT NULL, bytes INTEGER NOT NULL, created_at TEXT NOT NULL)''')
    c.execute("INSERT OR IGNORE INTO company(id, name) VALUES (1, '')")
    for table, definitions in (
        ('company', tuple(key + ' TEXT' for key in COMPANY_DETAILS)),
        ('subcontractors', tuple(key + ' TEXT' for key in SUB_DETAILS)),
        ('settlements', ('requested_at TEXT', 'paid_at TEXT', 'invoice_description TEXT',
                         'request_contact TEXT', 'request_honorific TEXT',
                         'invoice_tax_rate INTEGER NOT NULL DEFAULT 10', 'request_snapshot_json TEXT')),
        ('settlement_lines', ('consent_date TEXT', 'consent_record TEXT', 'carry_revision INTEGER NOT NULL DEFAULT 0', 'carry_uid TEXT', 'consent_method TEXT', 'carry_confirmed_date TEXT', 'carry_created_at TEXT')),
        ('case_subs', ('funding_method TEXT NOT NULL DEFAULT \'cash\'',)),
        ('haak_billings', ('actual_paid_amount INTEGER CHECK(actual_paid_amount>=0)',)),
        ('sub_invoices', ('original_name TEXT', 'detected_amount INTEGER')),
        ('issued_document_history', ('sent_date TEXT',)),
    ):
        columns = {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}
        for definition in definitions:
            if definition.split()[0] not in columns:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_accounts (
        case_id INTEGER NOT NULL REFERENCES cases(id), sub_id INTEGER NOT NULL REFERENCES subcontractors(id),
        recovered INTEGER NOT NULL CHECK(recovered>=0), refunded INTEGER NOT NULL CHECK(refunded>=0),
        record_date TEXT NOT NULL, note TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(case_id,sub_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_account_history (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, sub_id INTEGER NOT NULL,
        changed_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, backup_file TEXT NOT NULL)''')
    for action in ('UPDATE','DELETE'):
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS account_history_no_{action.lower()}
            BEFORE {action} ON principal_account_history
            BEGIN SELECT RAISE(ABORT,'principal account history is immutable'); END''')
    c.execute('''CREATE TABLE IF NOT EXISTS carry_change_history (
        id INTEGER PRIMARY KEY, line_id INTEGER NOT NULL, line_uid TEXT NOT NULL,
        settlement_id INTEGER NOT NULL, sub_id INTEGER NOT NULL,
        changed_at TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        backup_file TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS carry_history_line ON carry_change_history(line_id,id)')
    c.execute('CREATE INDEX IF NOT EXISTS carry_history_uid ON carry_change_history(line_uid,id)')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_release (
        case_id INTEGER NOT NULL REFERENCES cases(id), sub_id INTEGER NOT NULL REFERENCES subcontractors(id),
        amount INTEGER NOT NULL CHECK(amount>=0), confirmed_at TEXT NOT NULL, record TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(case_id,sub_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_release_history (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, sub_id INTEGER NOT NULL,
        changed_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS carry_notifications (
        line_uid TEXT PRIMARY KEY, settlement_id INTEGER NOT NULL, notified_at TEXT NOT NULL)''')
    c.execute('''INSERT OR IGNORE INTO carry_notifications(line_uid,settlement_id,notified_at)
        SELECT l.carry_uid,s.id,COALESCE(s.requested_at,'legacy') FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id
        WHERE l.carry_uid IS NOT NULL AND l.carry_over>0 AND s.request_snapshot_json IS NOT NULL''')
    for action in ('UPDATE', 'DELETE'):
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS release_history_no_{action.lower()}
            BEFORE {action} ON principal_release_history
            BEGIN SELECT RAISE(ABORT,'release history is immutable'); END''')
    c.execute('UPDATE settlement_lines SET carry_uid=lower(hex(randomblob(16))) WHERE carry_uid IS NULL')
    c.execute('UPDATE settlement_lines SET carry_confirmed_date=consent_date WHERE carry_over>0 AND carry_confirmed_date IS NULL')
    c.execute('''INSERT OR IGNORE INTO carry_notifications(line_uid,settlement_id,notified_at)
        SELECT l.carry_uid,s.id,COALESCE(s.requested_at,'legacy') FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id
        WHERE l.carry_uid IS NOT NULL AND l.carry_over>0 AND s.request_snapshot_json IS NOT NULL''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS carry_history_no_update BEFORE UPDATE ON carry_change_history
                 BEGIN SELECT RAISE(ABORT,'carry history is immutable'); END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS carry_history_no_delete BEFORE DELETE ON carry_change_history
                 BEGIN SELECT RAISE(ABORT,'carry history is immutable'); END''')
    c.execute('''CREATE TABLE IF NOT EXISTS settlement_numbers (
        case_number TEXT NOT NULL, sub_id INTEGER NOT NULL, branch INTEGER NOT NULL CHECK(branch BETWEEN 1 AND 99),
        number TEXT NOT NULL UNIQUE, PRIMARY KEY(case_number,sub_id), UNIQUE(case_number,branch))''')
    for row in c.execute('SELECT case_id,sub_id FROM case_subs ORDER BY case_id,slot,id').fetchall():
        settlement_number(c, row['case_id'], row['sub_id'], create=True)
    for row in c.execute('SELECT l.case_id,s.sub_id FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id ORDER BY l.id').fetchall():
        settlement_number(c, row['case_id'], row['sub_id'], create=True)
    for row in c.execute('SELECT mgmt_no FROM cases').fetchall():
        match = re.fullmatch(r'(\d{2})-(\d{4})', row['mgmt_no'])
        if match:
            year = datetime.date.today().year // 100 * 100 + int(match[1])
            c.execute('INSERT OR IGNORE INTO mgmt_counters(year) VALUES(?)', (year,))
            c.execute('UPDATE mgmt_counters SET last_seq=MAX(last_seq,?) WHERE year=?', (int(match[2]), year))
    c.execute("""CREATE TRIGGER IF NOT EXISTS immutable_case_number BEFORE UPDATE OF mgmt_no ON cases
                 WHEN NEW.mgmt_no<>OLD.mgmt_no BEGIN SELECT RAISE(ABORT,'immutable case number'); END""")
    c.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
    if not existing and os.path.isfile(SEAL_SEED):
        setset(c, 'company_seal_png', bundled_company_seal())
    seal = getset(c, 'company_seal_png')
    if seal:
        import company_seal
        try:
            company_seal.save(c, seal)
        except (ValueError, OSError):
            # Invalid legacy data remains available for explicit recovery.
            pass
    c.commit()


def settlement_number(c, cid, sid, create=False):
    case = c.execute('SELECT mgmt_no FROM cases WHERE id=?', (int(cid),)).fetchone()
    if not case:
        raise Err('案件が見つかりません', 404)
    number = case['mgmt_no']
    row = c.execute('SELECT number FROM settlement_numbers WHERE case_number=? AND sub_id=?', (number, int(sid))).fetchone()
    if row:
        return row['number']
    if not create:
        return None
    branch = c.execute('SELECT COALESCE(MAX(branch),0)+1 FROM settlement_numbers WHERE case_number=?', (number,)).fetchone()[0]
    if branch > 99:
        raise Err('この案件の下請け枝番は上限99に達しています')
    value = f'{number}-{branch:02d}'
    c.execute('INSERT INTO settlement_numbers VALUES(?,?,?,?)', (number, int(sid), branch, value))
    return value


def getset(c, k):
    r = c.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone()
    return r['v'] if r else None


def setset(c, k, v):
    c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v))


def bundled_company_seal():
    """Only the release's fixed PNG may be restored; never copy another DB."""
    from PIL import Image
    try:
        with open(SEAL_SEED, 'rb') as source:
            raw = source.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('image too large')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'PNG' or not (1 <= image.width <= 4096 and 1 <= image.height <= 4096):
                raise ValueError('invalid image')
            image.verify()
        return base64.b64encode(raw).decode('ascii')
    except Exception as exc:
        raise Err('同梱の社印PNGを確認できません。社印同梱の修正版をすべて展開してください。') from exc


# ---- 入力チェック ----
def txt(d, k, label, req=False, n=300):
    v = d.get(k)
    v = '' if v is None else str(v).strip()
    if req and not v:
        raise Err(label + 'を入力してください')
    if len(v) > n:
        raise Err(label + 'が長すぎます')
    return v or None


def yen(d, k, label, req=False):
    v = d.get(k)
    if v is None or str(v).strip() == '':
        if req:
            raise Err(label + 'を入力してください')
        return None
    try:
        n = int(str(v).replace(',', '').replace('¥', '').strip())
    except ValueError:
        raise Err(label + 'は整数の金額で入力してください')
    if n < 0 or n > 10**12:
        raise Err(label + 'の金額が正しくありません')
    return n


def pct(d, k, label, default=None):
    v = d.get(k)
    if v is None or str(v).strip() == '':
        if default is None:
            raise Err(label + 'を入力してください')
        return default
    try:
        f = float(str(v).replace('%', ''))
    except ValueError:
        raise Err(label + 'は数字で入力してください')
    if not 0 <= f <= 100:
        raise Err(label + 'は0〜100で入力してください')
    return f


def dt(d, k, label):
    v = txt(d, k, label, n=10)
    if v is None:
        return None
    try:
        if not DATE.match(v):
            raise ValueError
        datetime.date.fromisoformat(v)
    except ValueError:
        raise Err(label + 'の日付が正しくありません')
    return v


def default_settle(due):
    """精算予定日の自動値 = 納期の翌月15日(月末締め翌月15日払い)。"""
    if not due:
        return None
    d = datetime.date.fromisoformat(due)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return f'{y}-{m:02d}-15'


# ---- 案件の集計 ----
def case_view(c, cid, need_active=False):
    r = c.execute('SELECT * FROM cases WHERE id=?', (cid,)).fetchone()
    if not r:
        raise Err('案件が見つかりません', 404)
    if need_active and r['deleted_at']:
        raise Err('削除ボックスの案件は編集できません。先に復元してください')
    case = dict(r)
    purchases = [dict(x) for x in c.execute(
        '''SELECT p.*, s.name AS supplier_name, d.original AS invoice_original, d.reviewed AS invoice_details
           FROM purchases p JOIN suppliers s ON s.id=p.supplier_id
           LEFT JOIN purchase_invoice_details d ON d.purchase_id=p.id
           WHERE p.case_id=? ORDER BY p.id''', (cid,))]
    for purchase in purchases:
        for key in ('invoice_original', 'invoice_details'):
            if purchase[key] is not None:
                purchase[key] = json.loads(purchase[key])
    ptotal = sum(p['amount_tax_in'] for p in purchases)
    rate = case['planned_rate_pct']
    subs = []
    for x in c.execute('SELECT cs.*, sc.name AS sub_name FROM case_subs cs JOIN subcontractors sc ON sc.id=cs.sub_id WHERE cs.case_id=? ORDER BY slot', (cid,)):
        d = dict(x)
        d['settlement_no'] = settlement_number(c, cid, d['sub_id'])
        pr = d['principal_tax_in']
        d['sub_profit'] = calc.sub_profit(pr, rate, d['fee_rate_pct'])
        d['fee'] = calc.sub_fee(pr, d['fee_rate_pct'])
        d['payout'] = calc.sub_payout(pr, d['sub_profit'])
        d['principal_ex'], d['principal_tax'] = calc.tax_split(pr)
        d['settle_date_auto'] = default_settle(case['due_date'])
        subs.append(d)
    h = c.execute('SELECT * FROM haak_billings WHERE case_id=?', (cid,)).fetchone()
    haak = dict(h) if h else {}
    planned = calc.haak_planned_amount(ptotal, rate) if ptotal else 0
    conf = haak.get('confirmed_amount_tax_in')
    basis = conf if conf is not None else planned
    pr = calc.case_profit(basis, ptotal, [dict(principal=s['principal_tax_in'], fee_rate=s['fee_rate_pct']) for s in subs], rate)
    over = basis - planned
    calcd = dict(purchase_total=ptotal, planned_amount=planned, basis_amount=basis,
                 confirmed=conf is not None, total_profit=pr['total_profit'],
                 sub_profit_total=pr['sub_profit_total'], fee_total=pr['fee_total'],
                 own_profit=pr['own_profit'], over_planned=over,
                 own_base=pr['own_profit'] - pr['fee_total'] - over)
    return dict(case=case, purchases=purchases, subs=subs, haak=haak, calc=calcd)


def new_mgmt_no(c):
    y = datetime.date.today().year
    c.execute('INSERT OR IGNORE INTO mgmt_counters(year) VALUES (?)', (y,))
    c.execute('UPDATE mgmt_counters SET last_seq = last_seq + 1 WHERE year = ?', (y,))
    seq = c.execute('SELECT last_seq FROM mgmt_counters WHERE year = ?', (y,)).fetchone()[0]
    if seq > 9999:
        raise Err('この年の案件管理番号は上限9999に達しています')
    return f'{y % 100:02d}-{seq:04d}'


def case_fields(b):
    return dict(name=txt(b, 'name', '案件名', True, 100), case_no=txt(b, 'case_no', '案件番号', n=50),
                start_date=dt(b, 'start_date', '開始日'), due_date=dt(b, 'due_date', '納期'),
                planned_rate_pct=pct(b, 'planned_rate_pct', '予定利益率'),
                maker=txt(b, 'maker', 'メーカー', n=line_case_reader.MAKER_MAX_LENGTH),
                purchase_limit_tax_in=yen(b, 'purchase_limit_tax_in', '税込仕入限度額'))


# ---- API ----
def r_dashboard(c, b, q):
    ids = [r['id'] for r in c.execute('SELECT id FROM cases WHERE deleted_at IS NULL ORDER BY id')]
    tot = dict(cases=len(ids), purchase=0, haak_billed=0, haak_paid=0, haak_unpaid=0,
               haak_unconfirmed_cases=0, haak_planned_total=0,
               principal=0, principal_confirmed=0, own=0, own_provisional=0,
               total_profit=0, sub_profit=0, sub_unsettled=0, sub_unsettled_profit=0)
    principals = {}
    progress = dict.fromkeys(('登録', '仕入済', '請求額確定', '入金済'), 0)
    todo = []
    today = datetime.date.today().isoformat()
    for i in ids:
        v = case_view(c, i)
        cs, cc = v['case'], v['calc']
        progress[case_progress(v)] += 1
        tot['purchase'] += cc['purchase_total']
        h = v['haak']
        haak_received = h.get('paid_date') and h['paid_date'] <= today
        actual_received = (h.get('actual_paid_amount') if h.get('actual_paid_amount') is not None else cc['basis_amount']) if haak_received else 0
        if not haak_received:
            tot['haak_planned_total'] += cc['planned_amount']
            if v['purchases'] and not cc['confirmed']:
                tot['haak_unconfirmed_cases'] += 1
        if cc['confirmed']:
            tot['own'] += cc['own_profit']
            tot['total_profit'] += cc['total_profit']
            tot['sub_profit'] += cc['sub_profit_total']
            tot['haak_billed'] += cc['basis_amount']
            tot['haak_paid'] += actual_received
            unpaid = max(0,cc['basis_amount']-actual_received)
            tot['haak_unpaid'] += unpaid
            if unpaid:
                todo.append(dict(id=i, tab='haak', text=f"{cs['mgmt_no']} ハーク入金確認 {unpaid:,}円", due=h.get('due_date')))
        else:
            tot['own_provisional'] += cc['own_profit']
            if cc['purchase_total']:
                todo.append(dict(id=i, tab='haak', text=f"{cs['mgmt_no']} {cs['name']}　ハークの確定額を確認", due=cs['due_date']))
        for s in v['subs']:
            confirmed = principal_confirmed(c, s, today)
            balance = principal_balance.values(c, s)
            tot['principal'] += balance['balance']
            tot['principal_confirmed'] += confirmed
            group = principals.setdefault(s['sub_id'], dict(name=s['sub_name'], principal=0, available=0))
            group['principal'] += balance['balance']
            group['available'] += balance['available']
            if confirmed < s['principal_tax_in']:
                todo.append(dict(id=i, tab='sub', text=f"{s['settlement_no']} {s['sub_name']}の元金確認 {s['principal_tax_in'] - confirmed:,}円", due=None))
            settlement = c.execute(
                '''SELECT l.*, s.period, s.status, s.pay_date FROM settlement_lines l
                   JOIN settlements s ON s.id=l.settlement_id WHERE l.case_id=? AND s.sub_id=?''',
                (i, s['sub_id'])).fetchone()
            if settlement and settlement['status'] == 'paid':
                continue
            amount = calc.sub_payout(settlement['principal'], settlement['sub_profit'], settlement['carry_over']) if settlement else s['payout']
            amount -= balance['refunded'] + funding_ledger.outgoing(c,i,s['sub_id'])
            due = settlement['pay_date'] if settlement else s['settle_date_auto']
            period = cs['due_date'][:7] if cs['due_date'] else ''
            tot['sub_unsettled'] += amount
            tot['sub_unsettled_profit'] += settlement['sub_profit'] if settlement else s['sub_profit']
            todo.append(dict(id=i, tab='sub', period=period, text=f"{s['settlement_no']} {s['sub_name']}へ精算 {amount:,}円", due=due))
        for p in v['purchases']:
            if p['paid_amount'] < p['amount_tax_in']:
                todo.append(dict(id=i, tab='purchase', text=f"{cs['mgmt_no']} {p['supplier_name']}へ仕入の支払 {p['amount_tax_in'] - p['paid_amount']:,}円", due=p['due_date']))
    todo.sort(key=lambda t: (t['due'] is None, t['due'] or ''))
    for fund in funding_ledger.funds(c):
        group=principals.setdefault(fund['sub_id'],dict(name=fund['sub_name'],principal=0,available=0))
        group['principal']+=fund['balance']
        group['available']+=fund['balance']
        tot['principal']+=fund['balance']
    for sid, group in principals.items():
        group['available'] = principal_funding.available_pool(c,sid)['available']
    return dict(totals=tot, progress=progress, todo=todo, principals=list(principals.values()))


def case_progress(v):
    if case_summary.haak_paid(v['haak']):
        return '入金済'
    if v['calc']['confirmed']:
        return '請求額確定'
    return '仕入済' if v['purchases'] else '登録'


def r_cases(c, b, q):
    out = []
    for r in c.execute('SELECT id FROM cases WHERE deleted_at IS NULL ORDER BY id DESC'):
        v = case_view(c, r['id'])
        cs = v['case']
        out.append(dict(id=cs['id'], mgmt_no=cs['mgmt_no'], name=cs['name'], due_date=cs['due_date'], case_no=cs['case_no'],
                        subs=len(v['subs']), purchase_total=v['calc']['purchase_total'], own_profit=v['calc']['own_profit'],
                        confirmed=v['calc']['confirmed'], progress=case_progress(v), planned_rate_pct=cs['planned_rate_pct'],
                        total_profit=v['calc']['total_profit'], sub_profit=v['calc']['sub_profit_total'],
                        haak_amount=v['haak'].get('confirmed_amount_tax_in'),
                        haak_paid=v['haak'].get('paid_date'), principal=sum(s['principal_tax_in'] for s in v['subs']),
                        principal_confirmed=sum(principal_confirmed(c, s) for s in v['subs']),
                        purchased_unpaid=sum(p['amount_tax_in']-p['paid_amount'] for p in v['purchases'])))
    return out


def duplicate_case_warning(c, case_no, exclude_id=None):
    if not case_no:
        return None
    key = line_case_reader.normalized(case_no)
    rows = c.execute('SELECT id, case_no, mgmt_no, deleted_at FROM cases WHERE case_no IS NOT NULL ORDER BY id')
    for row in rows:
        if row['id'] != exclude_id and line_case_reader.normalized(row['case_no']) == key:
            location = '（削除ボックス内）' if row['deleted_at'] else ''
            return f"案件番号「{case_no}」は管理番号 {row['mgmt_no']}{location} で登録済みです。"
    return None


def r_case_line_preview(c, b, q):
    text = b.get('text')
    if not isinstance(text, str) or not text.strip():
        raise Err('LINE文章を貼り付けてください')
    if len(text) > 20000:
        raise Err('LINE文章は20,000文字以内で入力してください')
    fields, warnings = line_case_reader.parse_line_notice(text)
    duplicate = duplicate_case_warning(c, fields['case_no'])
    if not any(value is not None for value in fields.values()) and not warnings:
        warnings.append('読み取れる項目がありませんでした。貼り付けた文章を確認してください。')
    return dict(fields=fields, warnings=warnings, duplicate=duplicate)


def r_case_new(c, b, q):
    f = case_fields(b)
    # 照合と登録を同じ書き込みトランザクションで直列化する。
    c.execute('BEGIN IMMEDIATE')
    warning = duplicate_case_warning(c, f['case_no'])
    if warning:
        raise Err(warning, 409)
    no = new_mgmt_no(c)
    cur = c.execute('INSERT INTO cases(mgmt_no,case_no,name,start_date,due_date,planned_rate_pct,maker,purchase_limit_tax_in) VALUES (?,?,?,?,?,?,?,?)',
                    (no, f['case_no'], f['name'], f['start_date'], f['due_date'], f['planned_rate_pct'], f['maker'], f['purchase_limit_tax_in']))
    return dict(id=cur.lastrowid, mgmt_no=no)


def r_case_get(c, b, q, cid):
    view = case_view(c, int(cid))
    view['principal_items_text'] = principal_items_text(view['purchases'])
    view['settlement_history'] = [dict(row) for row in c.execute(
        '''SELECT l.principal, l.sub_profit, l.carry_over, s.period, s.pay_date, s.status,
                  s.paid_at, s.sub_id, sc.name AS sub_name
           FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
           JOIN subcontractors sc ON sc.id=s.sub_id
           WHERE l.case_id=? ORDER BY s.period DESC, s.id''', (cid,))]
    for line in view['settlement_history']:
        line['settlement_no'] = settlement_number(c, cid, line['sub_id'])
    for sub in view['subs']:
        sub['principal_confirmed'] = principal_confirmed(c, sub)
        sub['carry_reserved'] = incoming_carry(c, cid, sub['sub_id'])
        sub['funding'] = principal_funding.options(c, int(cid), sub['sub_id'])
    view['documents'] = case_documents(c, cid)
    return case_summary.enrich(view)


def r_case_put(c, b, q, cid):
    f = case_fields(b)
    c.execute('BEGIN IMMEDIATE')
    current = case_view(c, int(cid), True)['case']
    if any(current[key] != f[key] for key in ('due_date', 'planned_rate_pct')):
        if c.execute('SELECT 1 FROM settlement_lines WHERE case_id=? OR carry_to_case_id=?', (cid, cid)).fetchone():
            raise Err('精算明細または充当先に登録済みのため、納期と予定利益率は変更できません', 409)
    if line_case_reader.normalized(f['case_no'] or '') != line_case_reader.normalized(current['case_no'] or ''):
        warning = duplicate_case_warning(c, f['case_no'], int(cid))
        if warning:
            raise Err(warning, 409)
    c.execute('UPDATE cases SET case_no=?,name=?,start_date=?,due_date=?,planned_rate_pct=?,maker=?,purchase_limit_tax_in=? WHERE id=?',
              (f['case_no'], f['name'], f['start_date'], f['due_date'], f['planned_rate_pct'], f['maker'], f['purchase_limit_tax_in'], int(cid)))
    return dict(ok=True)


def r_trash_move(c, b, q, cid):
    c.execute('BEGIN IMMEDIATE')
    case_view(c, int(cid), True)
    if b.get('cancel_to_hold') is True:
        return cancel_case_to_hold(c, b, cid)
    reasons = carry_management.deletion_reasons(c, cid)
    if reasons:
        raise Err('関連記録があるため削除できません：\n' + '\n'.join(reasons), 409)
    c.execute("UPDATE cases SET deleted_at=datetime('now','localtime') WHERE id=?", (int(cid),))
    return dict(ok=True)


def cancel_case_to_hold(c, b, cid):
    plan = carry_management.cancellation_plan(c, int(cid))
    if not plan['can_cancel']:
        raise Err('見送り処理ができません：\n' + '\n'.join(plan['reasons']), 409)
    if b.get('expected_token') != plan['token']:
        raise Err('案件または充当内容が更新されました。画面を開き直して確認してください', 409)
    try:
        backup = carry_management.backup_database(c)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise Err('変更前のDBバックアップに失敗したため、見送り処理は行っていません', 409) from exc
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state = plan['state']
    for line in state['incoming']:
        before = carry_management.snapshot(c, line['settlement_id'], line['id'])
        before['cancelled_case_settings'] = state
        # 元金の預かりは維持。旧案件への承諾を新たな預かり承諾として扱わない。
        c.execute('''UPDATE settlement_lines SET carry_to_case_id=NULL,consent_date=NULL,
                     consent_method='LINE',consent_record=NULL,carry_revision=carry_revision+1
                     WHERE id=?''', (line['id'],))
        after = carry_management.snapshot(c, line['settlement_id'], line['id'])
        c.execute('''INSERT INTO carry_change_history(line_id,line_uid,settlement_id,sub_id,changed_at,
                     reason,before_json,after_json,backup_file) VALUES(?,?,?,?,?,?,?,?,?)''',
                  (line['id'], line['carry_uid'], line['settlement_id'], line['sub_id'], now,
                   f"充当先案件の見送り：{state['case']['case_no'] or state['case']['mgmt_no']}。元金を一時預かり（了承待ち）へ変更",
                   json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), backup))
    c.execute('DELETE FROM settlement_lines WHERE case_id=?', (cid,))
    for settlement_id in {line['settlement_id'] for line in state['lines']}:
        c.execute('''DELETE FROM settlements WHERE id=? AND status='not_requested'
                     AND NOT EXISTS(SELECT 1 FROM settlement_lines WHERE settlement_id=?)
                     AND NOT EXISTS(SELECT 1 FROM sub_invoices WHERE settlement_id=?)''',
                  (settlement_id, settlement_id, settlement_id))
    c.execute('DELETE FROM case_subs WHERE case_id=?', (cid,))
    c.execute("UPDATE cases SET deleted_at=datetime('now','localtime') WHERE id=?", (cid,))
    return dict(ok=True, hold_total=plan['hold_total'], consent_pending=bool(plan['hold_total']))


def r_restore(c, b, q, cid):
    c.execute('UPDATE cases SET deleted_at=NULL WHERE id=?', (int(cid),))
    return dict(ok=True)


def r_purge(c, b, q, cid):
    c.execute('BEGIN IMMEDIATE')
    r = c.execute('SELECT * FROM cases WHERE id=?', (int(cid),)).fetchone()
    if not r or not r['deleted_at']:
        raise Err('削除ボックスにある案件だけ完全に削除できます')
    if (b.get('confirm') or '').strip() != r['mgmt_no']:
        raise Err('管理番号が一致しません')
    reasons = carry_management.deletion_reasons(c, cid)
    if reasons:
        raise Err('関連記録があるため完全削除できません：\n' + '\n'.join(reasons), 409)
    try:
        c.execute('DELETE FROM cases WHERE id=?', (int(cid),))
    except sqlite3.IntegrityError:
        raise Err('精算の明細に入っている案件は完全に削除できません')
    return dict(ok=True)


def r_case_deletion_check(c, b, q, cid):
    case_view(c, int(cid), True)
    reasons = carry_management.deletion_reasons(c, cid)
    cancellation = carry_management.cancellation_plan(c, int(cid))
    cancellation.pop('state')
    return dict(can_delete=not reasons, reasons=reasons, cancellation=cancellation)


def r_trash(c, b, q):
    return [dict(id=r['id'], mgmt_no=r['mgmt_no'], name=r['name'], deleted_at=r['deleted_at'])
            for r in c.execute('SELECT * FROM cases WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC')]


def purchase_fields(b, c=None):
    amt = yen(b, 'amount_tax_in', '税込請求額', True)
    paid = yen(b, 'paid_amount', '支払済み額') or 0
    if paid > amt:
        raise Err('支払済み額が請求額を超えています')
    try:
        sid = int(b.get('supplier_id'))
    except (TypeError, ValueError):
        raise Err('仕入先を選んでください')
    if c is not None and not c.execute('SELECT 1 FROM suppliers WHERE id=?', (sid,)).fetchone():
        raise Err('選択した仕入先が見つかりません。仕入先を選び直すか、新規登録してください')
    return (sid, amt, dt(b, 'invoice_date', '請求日'), dt(b, 'due_date', '支払期限'), paid, dt(b, 'paid_date', '支払日'), txt(b, 'items_text', '品名', n=3000))


def r_purchase_new(c, b, q, cid):
    case_view(c, int(cid), True)
    reviewed = reviewed_invoice(b['invoice_details']) if 'invoice_details' in b else None
    c.execute('INSERT INTO purchases(case_id,supplier_id,amount_tax_in,invoice_date,due_date,paid_amount,paid_date,items_text) VALUES (?,?,?,?,?,?,?,?)',
              (int(cid),) + purchase_fields(b, c))
    pid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    if reviewed is not None:
        c.execute('INSERT INTO purchase_invoice_details(purchase_id,original,reviewed) VALUES (?,?,?)',
                  (pid, '{}', json.dumps(reviewed, ensure_ascii=False)))
    return dict(ok=True, id=pid)


def purchase_case(c, pid):
    r = c.execute('SELECT case_id FROM purchases WHERE id=?', (int(pid),)).fetchone()
    if not r:
        raise Err('仕入が見つかりません', 404)
    case_view(c, r['case_id'], True)


def r_purchase_put(c, b, q, pid):
    purchase_case(c, pid)
    c.execute('UPDATE purchases SET supplier_id=?,amount_tax_in=?,invoice_date=?,due_date=?,paid_amount=?,paid_date=?,items_text=? WHERE id=?',
              purchase_fields(b, c) + (int(pid),))
    if 'invoice_details' in b:
        reviewed = reviewed_invoice(b['invoice_details'])
        c.execute('UPDATE purchase_invoice_details SET reviewed=? WHERE purchase_id=?',
                  (json.dumps(reviewed, ensure_ascii=False), int(pid)))
    return dict(ok=True)


def r_purchase_del(c, b, q, pid):
    purchase_case(c, pid)
    c.execute('DELETE FROM purchases WHERE id=?', (int(pid),))
    return dict(ok=True)


def r_sub_put(c, b, q, cid, slot):
    c.execute('BEGIN IMMEDIATE')
    if b.get('allocations'):
        funding_ledger.backup(c)
    v = case_view(c, int(cid), True)
    try:
        sid = int(b.get('sub_id'))
    except (TypeError, ValueError):
        raise Err('下請けを選んでください')
    pr = yen(b, 'principal_tax_in', '担当元金(税込)', True)
    fee = pct(b, 'fee_rate_pct', '手数料率', default=2.0)
    if fee > v['case']['planned_rate_pct']:
        raise Err('手数料率が予定利益率より大きくなっています')
    existing = next((sub for sub in v['subs'] if sub['slot'] == int(slot)), None)
    if existing:
        release = c.execute('SELECT amount FROM principal_release WHERE case_id=? AND sub_id=?', (cid,existing['sub_id'])).fetchone()
        account = c.execute('SELECT 1 FROM principal_accounts WHERE case_id=? AND sub_id=?',(cid,existing['sub_id'])).fetchone()
        if (release or account) and (sid != existing['sub_id'] or pr != existing['principal_tax_in'] or
                        dt(b,'principal_received_date','元金の入金日') != existing['principal_received_date']):
            raise Err('充当可能額の確認記録があるため、下請け・担当元金・現金入金日は変更できません',409)
    mode = b.get('tax_mode', SUB_TAX_MODE)
    if mode != SUB_TAX_MODE:
        raise Err('元金の扱いは販売委託型（固定）です')
    if existing and existing['tax_mode'] != SUB_TAX_MODE and 'tax_mode' not in b:
        raise Err('この下請けは元金の扱いの移行対象です。既存の設定を確認してください。', 409)
    if existing and funding_ledger.cash(c,cid,existing['sub_id']):
        if sid!=existing['sub_id'] or dt(b,'principal_received_date','元金の入金日')!=existing['principal_received_date']:
            raise Err('入金台帳のある下請け・旧入金日は変更できません',409)
    vals = (sid, pr, fee, mode, dt(b, 'principal_received_date', '元金の入金日'), dt(b, 'settle_date', '精算予定日'))
    if existing and settlement_link(c, cid, existing['sub_id']):
        if vals[:4] != tuple(existing[key] for key in ('sub_id', 'principal_tax_in', 'fee_rate_pct', 'tax_mode')):
            raise Err('精算または充当に使用した下請け・担当元金・手数料率は変更できません', 409)
        if vals[4] != existing['principal_received_date']:
            frozen = c.execute("""SELECT 1 FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
                                  WHERE s.sub_id=? AND l.case_id=? AND s.status<>'not_requested'""", (sid, cid)).fetchone()
            if frozen:
                raise Err('依頼済みの明細の元金入金日は変更できません', 409)
    try:
        if existing:
            c.execute('UPDATE case_subs SET sub_id=?,principal_tax_in=?,fee_rate_pct=?,tax_mode=?,principal_received_date=?,settle_date=? WHERE case_id=? AND slot=?', vals + (int(cid), int(slot)))
        else:
            c.execute('INSERT INTO case_subs(sub_id,principal_tax_in,fee_rate_pct,tax_mode,principal_received_date,settle_date,case_id,slot) VALUES (?,?,?,?,?,?,?,?)', vals + (int(cid), int(slot)))
    except sqlite3.IntegrityError:
        raise Err('同じ下請けは1案件に1回だけ登録できます')
    settlement_number(c, cid, sid, create=True)
    principal_balance.validate_case(c,int(cid))
    if 'funding_method' in b or 'allocations' in b:
        entries=b.get('allocations',[])
        if isinstance(entries,list) and entries and all(isinstance(e,dict) and 'source_type' not in e and 'request_key' not in e for e in entries):
            principal_funding.allocate(c,b,int(cid),sid)
        else:
            funding_ledger.allocate(c, b, int(cid), sid)
    funding_ledger.save_cash(c,b,int(cid),sid)
    funding_ledger.check_capacity(c,int(cid),sid)
    if incoming_carry(c,int(cid),sid)+funding_ledger.cash(c,int(cid),sid)>pr:
        raise Err('入金・充当の合計が担当元金を超えています',409)
    return dict(ok=True)


def r_funding_options(c, b, q, cid):
    case_view(c, int(cid), True)
    sid = yen({'sub_id': q.get('sub_id', [''])[0]}, 'sub_id', '下請け', True)
    result = principal_funding.options(c, int(cid), sid)
    cutoff = dt({'date': q.get('issued_date', [None])[0]}, 'date', '発行日')
    if result['invoice'] is not None:
        result['invoice'] = funding_ledger.invoice_values(c,int(cid),sid,cutoff)
    return result


def r_principal_release(c, b, q, cid, slot):
    return principal_funding.release(c, b, int(cid), int(slot))


def r_principal_account(c,b,q,cid,slot):
    return principal_balance.save(c,b,int(cid),int(slot))


def r_sub_mode_migrations(c, b, q):
    rows = c.execute(
        '''SELECT cases.id AS case_id, cases.mgmt_no, cases.case_no, cases.name AS case_name,
                  cases.deleted_at, cs.slot, cs.tax_mode, sc.name AS sub_name
           FROM case_subs cs JOIN cases ON cases.id=cs.case_id
           JOIN subcontractors sc ON sc.id=cs.sub_id
           WHERE cs.tax_mode<>? ORDER BY cases.id, cs.slot''', (SUB_TAX_MODE,))
    return [dict(row) for row in rows]


def r_sub_del(c, b, q, cid, slot):
    c.execute('BEGIN IMMEDIATE')
    case_view(c, int(cid), True)
    sub = c.execute('SELECT sub_id FROM case_subs WHERE case_id=? AND slot=?', (cid, slot)).fetchone()
    if sub and c.execute('SELECT 1 FROM principal_accounts WHERE case_id=? AND sub_id=?',(cid,sub['sub_id'])).fetchone():
        raise Err('回収・返金記録がある下請けは外せません',409)
    if sub and settlement_link(c, cid, sub['sub_id']):
        raise Err('精算明細または充当に使用した下請けは外せません', 409)
    if sub and c.execute('SELECT 1 FROM principal_release WHERE case_id=? AND sub_id=?', (cid,sub['sub_id'])).fetchone():
        raise Err('充当可能額の確認記録がある下請けは外せません', 409)
    c.execute('DELETE FROM case_subs WHERE case_id=? AND slot=?', (int(cid), int(slot)))
    return dict(ok=True)


def r_haak_put(c, b, q, cid):
    c.execute('BEGIN IMMEDIATE')
    case_view(c, int(cid), True)
    if b.get('require_actual_paid_amount') and b.get('paid_date'):
        if not yen(b, 'actual_paid_amount', 'ハーク実入金累計額', True):
            raise Err('入金確認日を登録する場合は、実際の入金額を入力してください')
    vals = (yen(b, 'confirmed_amount_tax_in', 'ハークから連絡された確定額'), 1 if b.get('confirmed_checked') else 0,
            dt(b, 'billing_date', '請求日'), dt(b, 'due_date', '支払期限'), dt(b, 'paid_date', '入金日'),
            txt(b, 'items_text', '品名', n=3000), txt(b, 'note', '備考', n=500))
    if c.execute('SELECT 1 FROM haak_billings WHERE case_id=?', (int(cid),)).fetchone():
        c.execute('UPDATE haak_billings SET confirmed_amount_tax_in=?,confirmed_checked=?,billing_date=?,due_date=?,paid_date=?,items_text=?,note=? WHERE case_id=?', vals + (int(cid),))
    else:
        c.execute('INSERT INTO haak_billings(confirmed_amount_tax_in,confirmed_checked,billing_date,due_date,paid_date,items_text,note,case_id) VALUES (?,?,?,?,?,?,?,?)', vals + (int(cid),))
    if 'actual_paid_amount' in b:
        actual = yen(b,'actual_paid_amount','ハーク実入金累計額')
        c.execute('UPDATE haak_billings SET actual_paid_amount=? WHERE case_id=?',(actual,int(cid)))
    principal_balance.validate_case(c,int(cid))
    return dict(ok=True)


BANK_FIELDS = ('bank_branch', 'bank_account', 'bank_holder')
BANK_SNAPSHOT_FIELDS = ('bank_account_id', 'issuer_bank_info', 'issuer_bank_branch',
                        'issuer_bank_account', 'issuer_bank_holder')


def company_bank_accounts(c):
    company = dict(c.execute('SELECT * FROM company WHERE id=1').fetchone())
    first = dict(id='1', label='口座1（標準）', bank_info=company['bank_info'],
                 **{key: company[key] for key in BANK_FIELDS})
    extra = json.loads(getset(c, 'company_extra_bank_accounts') or '[]')
    return [first] + extra


def apply_document_bank(c, data, account_id):
    account = next((a for a in company_bank_accounts(c) if a['id'] == str(account_id)), None)
    if not account or (account['id'] != '1' and not all(account.get(k) for k in BANK_FIELDS)):
        raise Err('振込先口座を設定から登録し、選択してください')
    data['bank_account_id'] = account['id']
    data['issuer_bank_info'] = account.get('bank_info') or ' '.join(account.get(k) or '' for k in BANK_FIELDS).strip()
    for key in BANK_FIELDS:
        data['issuer_' + key] = account.get(key)


def r_master(c, b, q):
    company = dict(c.execute('SELECT * FROM company WHERE id=1').fetchone())
    seal = c.execute("SELECT v FROM settings WHERE k='company_seal_png'").fetchone()
    company['bank_accounts'] = company_bank_accounts(c)
    company['seal_png'] = seal['v'] if seal else None
    company['bundled_seal_available'] = os.path.isfile(SEAL_SEED)
    return dict(company=company,
                subs=[dict(x) for x in c.execute('SELECT * FROM subcontractors ORDER BY id')],
                suppliers=[dict(x) for x in c.execute('SELECT * FROM suppliers ORDER BY id')])


def r_company_seal_restore(c, b, q):
    if b.get('confirmed') is not True:
        raise Err('同梱の社印を復元することを確認してください')
    encoded = bundled_company_seal()
    import company_seal
    if getset(c, 'company_seal_png') == company_seal.encoded_png(encoded):
        return dict(ok=True, unchanged=True)
    backup = r_backup_full(c, {}, {})[1]
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(DBP), prefix='before-seal-restore-', suffix='.zip', delete=False) as file:
        file.write(backup)
        file.flush()
        os.fsync(file.fileno())
        backup_path = file.name
    with zipfile.ZipFile(backup_path) as archive:
        if archive.testzip() is not None:
            raise Err('バックアップの検証に失敗したため、社印は変更していません')
    import company_seal
    company_seal.save(c, encoded)
    return dict(ok=True, backup=os.path.basename(backup_path))


def r_company_seal_upload(c, b, q):
    import company_seal
    encoded = txt(b, 'png_base64', '社印PNG', True, 1400000)
    try:
        company_seal.encoded_png(encoded)
    except Exception as exc:
        raise Err('有効な社印PNG（1MB以下）を選択してください') from exc
    if len(base64.b64decode(encoded)) > 1000000:
        raise Err('社印PNGは1MB以下を選択してください')
    raw = r_backup_full(c, {}, {})[1]
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(DBP), prefix='before-seal-upload-', suffix='.zip', delete=False) as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
        name = out.name
    with zipfile.ZipFile(name) as archive:
        if archive.testzip() is not None:
            raise Err('バックアップを検証できないため社印を変更していません')
    company_seal.save(c, encoded)
    return dict(ok=True, backup=os.path.basename(name))


def r_company_put(c, b, q):
    c.execute('BEGIN IMMEDIATE')
    current = dict(c.execute('SELECT * FROM company WHERE id=1').fetchone())
    details = {key: txt(b, key, label, n=limit) if key in b else current[key]
               for key, (label, limit) in COMPANY_DETAILS.items()}
    if details['invoice_type'] not in (None, 'qualified', 'standard'):
        raise Err('請求書の種類を選択してください')
    if details['invoice_type'] == 'qualified' and not re.fullmatch(r'T[0-9]{13}', b.get('invoice_no') or ''):
        raise Err('登録番号をTと13桁の数字で入力してください')
    for key, value in details.items():
        c.execute(f'UPDATE company SET {key}=? WHERE id=1', (value,))
    if any(key in b for key in ('bank_branch', 'bank_account', 'bank_holder')):
        combined = ' '.join(filter(None, (details['bank_branch'], details['bank_account'], details['bank_holder'])))
        b = dict(b, bank_info=combined or b.get('bank_info'))
    c.execute('UPDATE company SET name=?,address=?,invoice_no=?,bank_info=? WHERE id=1',
              (txt(b, 'name', '会社名', True, 100) , txt(b, 'address', '住所'), txt(b, 'invoice_no', '登録番号', n=20), txt(b, 'bank_info', '振込先', n=300)))
    if 'extra_bank_accounts' in b:
        entries = b['extra_bank_accounts']
        if not isinstance(entries, list) or len(entries) > 2:
            raise Err('追加口座は2件まで登録できます')
        saved = []
        for i, entry in enumerate(entries, 2):
            if not isinstance(entry, dict):
                raise Err('口座情報が正しくありません')
            account = {key: txt(entry, key, label, n=limit)
                       for key, (label, limit) in COMPANY_DETAILS.items() if key in BANK_FIELDS}
            if not any(account.values()):
                continue
            if not all(account.values()):
                raise Err('追加口座の銀行・支店、口座種別・番号、口座名義をすべて入力してください')
            account.update(id=str(i), label=txt(entry, 'label', '口座の表示名', n=50) or f'口座{i}')
            saved.append(account)
        setset(c, 'company_extra_bank_accounts', json.dumps(saved, ensure_ascii=False))
    return dict(ok=True)


def sub_fields(b):
    return (txt(b, 'name', '下請け名', True, 100), txt(b, 'invoice_no', '登録番号', n=20), txt(b, 'bank_info', '振込先', n=300), pct(b, 'default_fee_rate_pct', '標準の手数料率', default=2.0))


def r_subm_new(c, b, q):
    c.execute('BEGIN IMMEDIATE')
    cursor = c.execute('INSERT INTO subcontractors(name,invoice_no,bank_info,default_fee_rate_pct) VALUES (?,?,?,?)', sub_fields(b))
    save_sub_details(c, b, cursor.lastrowid)
    return dict(ok=True)


def r_subm_put(c, b, q, sid):
    c.execute('BEGIN IMMEDIATE')
    current = c.execute('SELECT * FROM subcontractors WHERE id=?', (int(sid),)).fetchone()
    if not current:
        raise Err('下請けが見つかりません')
    b = dict(b)
    b.setdefault('default_fee_rate_pct', current['default_fee_rate_pct'])
    c.execute('UPDATE subcontractors SET name=?,invoice_no=?,bank_info=?,default_fee_rate_pct=? WHERE id=?', sub_fields(b) + (int(sid),))
    save_sub_details(c, b, int(sid))
    return dict(ok=True)


def save_sub_details(c, b, sid):
    for key, (label, limit) in SUB_DETAILS.items():
        if key in b:
            c.execute(f'UPDATE subcontractors SET {key}=? WHERE id=?', (txt(b, key, label, n=limit), sid))
    bank_keys = ('bank_name', 'bank_branch', 'account_type', 'account_number', 'account_holder')
    if any(key in b for key in bank_keys):
        saved = c.execute('SELECT * FROM subcontractors WHERE id=?', (sid,)).fetchone()
        combined = ' '.join(saved[key] for key in bank_keys if saved[key])
        if combined:
            c.execute('UPDATE subcontractors SET bank_info=? WHERE id=?', (combined, sid))


def r_supplier_new(c, b, q):
    try:
        cursor = c.execute('INSERT INTO suppliers(name,invoice_no) VALUES (?,?)', (txt(b, 'name', '仕入先名', True, 100), txt(b, 'invoice_no', '登録番号', n=20)))
    except sqlite3.IntegrityError:
        raise Err('同じ名前の仕入先がすでにあります')
    return dict(ok=True, id=cursor.lastrowid)


def r_password(c, b, q):
    if not check_pw(str(b.get('current') or ''), getset(c, 'pw')):
        raise Err('現在のパスワードが違います')
    new = str(b.get('new') or '')
    if len(new) < 8:
        raise Err('新しいパスワードは8文字以上にしてください')
    setset(c, 'pw', hash_pw(new))
    return dict(ok=True)


def r_backup(c, b, q):
    fd, tmp = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    dst = sqlite3.connect(tmp)
    c.backup(dst)
    dst.close()
    data = open(tmp, 'rb').read()
    os.remove(tmp)
    return ('file', data, 'backup_' + datetime.datetime.now().strftime('%Y%m%d_%H%M') + '.db')



# ---- 仕入書類の読取り（登録には利用者の確認が必要） ----
def read_purchase(b):
    mime = txt(b, 'mime', 'ファイル形式', True, 60)
    if mime != 'application/pdf':
        raise Err('読取り対象はPDFです。写真は添付保存できます')
    try:
        raw = attachments.decode_pdf(b.get('data') or '', mime)
        return raw, purchase_reader.read_pdf(raw)
    except ValueError as e:
        raise Err(str(e)) from e


def purchase_number_warning(case, result):
    found = result['fields'].get('case_no')
    current = case['case_no']
    if found and current and line_case_reader.normalized(found) != line_case_reader.normalized(current):
        return f'選択案件の番号 {current} とPDF記載の {found} が異なります。別案件の可能性があります'
    return None


def purchase_case_selection(c, result):
    active = [dict(row) for row in c.execute(
        'SELECT id, mgmt_no, case_no, name FROM cases WHERE deleted_at IS NULL ORDER BY id DESC')]
    found = result['fields'].get('case_no')
    if found:
        key = line_case_reader.normalized(found)
        matches = [case for case in active if line_case_reader.normalized(case['case_no'] or '') == key]
        if matches:
            return dict(mode='matched' if len(matches) == 1 else 'ambiguous', cases=matches)
    return dict(mode='unmatched' if found else 'unread', cases=active)


def r_purchase_read(c, b, q, cid=None):
    case = case_view(c, int(cid), True)['case'] if cid is not None else None
    _, result = read_purchase(b)
    selection = purchase_case_selection(c, result)
    warning = purchase_number_warning(case, result) if case is not None else None
    if warning:
        result['warnings'].append(warning)
    return dict(result, case_selection=selection, case_mismatch=bool(warning))


def reviewed_invoice(data):
    if not isinstance(data, dict):
        raise Err('請求書の入力形式が正しくありません')
    fields = dict(
        invoice_number=txt(data, 'invoice_number', '請求書番号', n=100),
        case_no=txt(data, 'case_no', 'PDFの案件番号', n=50),
        case_name=txt(data, 'case_name', 'PDFの案件名', n=200),
        delivery_date=dt(data, 'delivery_date', '納品日'),
        amount_ex_tax_as_printed=yen(data, 'amount_ex_tax_as_printed', '確認した税抜金額'),
        tax_as_printed=yen(data, 'tax_as_printed', '確認した消費税額'),
        bank_info=txt(data, 'bank_info', '振込先', n=1000),
    )
    items = data.get('items', [])
    if not isinstance(items, list) or len(items) > 250 or any(not isinstance(item, dict) for item in items):
        raise Err('商品明細は250行以内で入力してください')
    fields['items'] = [
        dict(name=txt(item, 'name', '商品名', n=300), maker=txt(item, 'maker', 'メーカー', n=150),
             quantity=yen(item, 'quantity', '数量'), unit=txt(item, 'unit', '単位', n=30),
             unit_price=yen(item, 'unit_price', '単価'), amount_ex_tax=yen(item, 'amount_ex_tax', '商品金額'))
        for item in items
    ]
    return fields


def r_purchase_import(c, b, q, cid):
    cid = int(cid)
    upload = b.get('attachment')
    if not isinstance(upload, dict):
        raise Err('仕入請求書PDFを選択してください')
    name = txt(upload, 'filename', 'ファイル名', True, 150)
    raw, result = read_purchase(upload)
    reviewed = reviewed_invoice(b.get('invoice_details'))
    c.execute('BEGIN IMMEDIATE')
    case_view(c, cid, True)
    selection = purchase_case_selection(c, result)
    if selection['mode'] in ('matched', 'ambiguous'):
        if not any(case['id'] == cid for case in selection['cases']):
            raise Err('PDFの案件番号と一致する案件があります。読み取り直して保存先を確認してください。', 409)
    elif b.get('confirm_unmatched_case') is not True:
        raise Err('PDFの案件番号と一致する案件がありません。保存先の案件を選んで確認してください。', 409)
    reject_duplicate_attachment(c, cid, raw, 'application/pdf')
    purchase = dict(b)
    purchase.pop('invoice_details', None)
    saved = r_purchase_new(c, purchase, q, cid)
    original = {'fields': result['fields'], 'items': result['items']}
    c.execute('INSERT INTO purchase_invoice_details(purchase_id,original,reviewed) VALUES (?,?,?)',
              (saved['id'], json.dumps(original, ensure_ascii=False), json.dumps(reviewed, ensure_ascii=False)))
    saved['attachment_id'] = save_attachment(c, cid, saved['id'], 'purchase_invoice', name, 'application/pdf', raw)
    return saved


# ---- フェーズ2: 添付・帳票 ----
LEGACY_ITEM_SUFFIX = re.compile(
    r' / [0-9,]+(?:Lot|個|箱|式|本|点|セット) / [0-9,]+円[（(]税抜[）)]$'
)


def saved_product_names(invoice):
    names = []
    for item in invoice.get('items', ()):
        name = (item.get('name') or '').strip()
        maker = (item.get('maker') or '').strip()
        if name:
            names.append(f'{maker}　{name}' if maker and not name.startswith(maker) else name)
    return names


def principal_items_text(purchases):
    names = []
    for purchase in purchases:
        reviewed = saved_product_names(purchase.get('invoice_details') or {})
        original = saved_product_names(purchase.get('invoice_original') or {})
        if reviewed or original:
            names.extend(reviewed or original)
        else:
            for line in (purchase.get('items_text') or '').splitlines():
                name = LEGACY_ITEM_SUFFIX.sub('', line.strip())
                if name:
                    names.append(name)
    return '\n'.join(names)


def get_attachment(c, aid):
    row = c.execute('SELECT * FROM attachments WHERE id=?', (int(aid),)).fetchone()
    if row is None:
        raise Err('ファイルが見つかりません', 404)
    return row


def attachment_digest(row):
    try:
        return attachments.file_digest(FILES, row['stored_name'])
    except OSError:
        return None


def reject_duplicate_attachment(c, cid, raw, mime, replacing=None):
    digest = attachments.content_digest(raw)
    sql = ('SELECT a.*, cases.mgmt_no FROM attachments a JOIN cases ON cases.id=a.case_id'
           ' WHERE a.mime=? AND a.bytes=?')
    args = (mime, len(raw))
    if mime != 'application/pdf':
        sql += ' AND a.case_id=?'
        args += (cid,)
    for row in c.execute(sql, args):
        if row['id'] != replacing and attachment_digest(row) == digest:
            raise Err(f'同じ書類が案件 {row["mgmt_no"]} に「{row["original_name"]}」として添付済みです。', 409)


def save_attachment(c, cid, pid, kind, name, mime, raw, previous=None):
    stored = attachments.store(FILES, raw, attachments.ALLOWED[mime])
    now = datetime.datetime.now().isoformat(timespec='seconds')
    try:
        if previous is None:
            cur = c.execute(
                'INSERT INTO attachments(case_id,purchase_id,kind,original_name,stored_name,mime,bytes,created_at) VALUES (?,?,?,?,?,?,?,?)',
                (cid, pid, kind, name, stored, mime, len(raw), now))
            aid = cur.lastrowid
        else:
            aid = previous['id']
            c.execute('''INSERT INTO attachment_revisions(attachment_id,original_name,stored_name,mime,bytes,created_at)
                         SELECT id, original_name, stored_name, mime, bytes, created_at FROM attachments WHERE id=?''', (aid,))
            c.execute('UPDATE attachments SET original_name=?, stored_name=?, mime=?, bytes=?, created_at=? WHERE id=?',
                      (name, stored, mime, len(raw), now, aid))
        c.commit()
    except Exception:
        c.rollback()
        os.remove(attachments.safe_path(FILES, stored))
        raise
    return aid


def attachment_upload(body):
    mime = txt(body, 'mime', 'ファイル形式', True, 60)
    name = txt(body, 'filename', 'ファイル名', True, 150)
    try:
        raw = attachments.decode_pdf(body.get('data') or '', mime)
        with purchase_reader.open_pdf(raw):
            pass
    except ValueError as e:
        raise Err(str(e)) from e
    return name, mime, raw


def r_attach(c, b, q, cid):
    cid = int(cid)
    kind = b.get('kind')
    if kind not in ('purchase_invoice', 'purchase_receipt', 'other'):
        raise Err('添付の種類を選択してください')
    pid = b.get('purchase_id')
    if pid in (None, ''):
        pid = None
    else:
        try:
            pid = int(pid)
        except (TypeError, ValueError) as e:
            raise Err('仕入IDが不正です') from e
    name, mime, raw = attachment_upload(b)
    c.execute('BEGIN IMMEDIATE')
    case_view(c, cid, True)
    if pid is not None and not c.execute('SELECT 1 FROM purchases WHERE id=? AND case_id=?', (pid, cid)).fetchone():
        raise Err('この案件の仕入を選択してください')
    reject_duplicate_attachment(c, cid, raw, mime)
    if pid is not None and kind == 'purchase_invoice' and c.execute(
            "SELECT 1 FROM attachments WHERE purchase_id=? AND kind='purchase_invoice'", (pid,)).fetchone():
        raise Err('この仕入には請求書が添付済みです。差し替えを選択してください。', 409)
    return dict(ok=True, id=save_attachment(c, cid, pid, kind, name, mime, raw))


def r_attachment_replace(c, b, q, aid):
    version = txt(b, 'version', '差し替え対象', True, 80)
    name, mime, raw = attachment_upload(b)
    c.execute('BEGIN IMMEDIATE')
    current = get_attachment(c, aid)
    cid, pid, kind = current['case_id'], current['purchase_id'], current['kind']
    case_view(c, cid, True)
    if b.get('confirmed') is not True or current['stored_name'] != version:
        raise Err('現在の書類を確認してから差し替えてください。', 409)
    reject_duplicate_attachment(c, cid, raw, mime, replacing=current['id'])
    if attachment_digest(current) == attachments.content_digest(raw):
        return dict(ok=True, id=current['id'])
    return dict(ok=True, id=save_attachment(c, cid, pid, kind, name, mime, raw, previous=current))


def r_attachments(c, b, q, cid):
    case_view(c, int(cid))
    rows = c.execute('SELECT * FROM attachments WHERE case_id=? ORDER BY purchase_id IS NULL, id DESC', (int(cid),))
    seen = set()
    result = []
    for row in rows:
        digest = attachment_digest(row)
        if digest is not None:
            if digest in seen:
                continue
            seen.add(digest)
        fields = ('id', 'purchase_id', 'kind', 'original_name', 'mime', 'bytes', 'created_at')
        result.append(dict({key: row[key] for key in fields}, version=row['stored_name']))
    return result


def attachment_data(c, aid):
    row = get_attachment(c, aid)
    if row['mime'] not in attachments.ALLOWED:
        raise Err('このファイル形式は表示できません', 415)
    try:
        return row, attachments.load(FILES, row['stored_name'])
    except FileNotFoundError as e:
        raise Err('保存ファイルが見つかりません', 404) from e


def r_attachment_download(c, b, q, aid):
    row, raw = attachment_data(c, aid)
    return ('file', raw, 'attachment' + attachments.ALLOWED[row['mime']], row['mime'])


def pdf_preview(raw, q, name):
    if 'info' not in q and 'page' not in q:
        return ('inline', raw, name, 'application/pdf')
    try:
        with purchase_reader.open_pdf(raw) as pdf:
            if 'info' in q:
                return dict(pages=len(pdf))
            number = int(q['page'][0])
            if not 1 <= number <= len(pdf):
                raise Err('指定のページが見つかりません', 404)
            return ('inline', attachments.render_page(pdf[number - 1]), 'preview.png', 'image/png')
    except (ValueError, RuntimeError) as e:
        raise Err('このPDFをプレビューできません') from e


def r_attachment_preview(c, b, q, aid):
    row, raw = attachment_data(c, aid)
    mime = row['mime']
    if mime == 'application/pdf':
        return pdf_preview(raw, q, 'preview.pdf')
    return ('inline', raw, 'preview' + attachments.ALLOWED[mime], mime)


def document_context(c,case_id,doc_type,sub_id=None):
    view=case_view(c,int(case_id),True)
    cs=view['case']; company=dict(c.execute('SELECT * FROM company WHERE id=1').fetchone())
    result=dict(mgmt_no=cs['mgmt_no'],case_no=cs['case_no'],case_name=cs['name'],case_due_date=cs['due_date'],
                issuer_name=company['name'],issuer_address=company['address'],issuer_bank_info=company['bank_info'],issuer_invoice_no=company['invoice_no'])
    result['issuer_address'] = ' '.join(filter(None, (
        '〒' + company['postal_code'] if company['postal_code'] else None, company['address'])))
    result['issuer_contact'] = ' / '.join(filter(None, (company['phone'], company['email'], company['contact_name'])))
    for key in ('postal_code', 'phone', 'email', 'contact_name',
                'bank_branch', 'bank_account', 'bank_holder'):
        result['issuer_' + key] = company[key]
    result['issuer_street_address'] = company['address']
    seal = c.execute("SELECT v FROM settings WHERE k='company_seal_png'").fetchone()
    result['issuer_seal_png'] = seal['v'] if seal else None
    if company['invoice_type'] == 'standard':
        result['issuer_invoice_no'] = None
    if not company['name']:
        raise Err('設定で発行者名を登録してください')
    if doc_type in ('principal_invoice','receipt'):
        try: sub_id=int(sub_id)
        except (ValueError,TypeError):raise Err('下請けを選んでください')
        r=c.execute('SELECT cs.*,sc.name AS sub_name FROM case_subs cs JOIN subcontractors sc ON cs.sub_id=sc.id WHERE cs.case_id=? AND cs.sub_id=?',(int(case_id),sub_id)).fetchone()
        if not r:raise Err('案件に登録されていない下請けです')
        result['case_mgmt_no'] = cs['mgmt_no']
        result['settlement_no'] = settlement_number(c, case_id, sub_id)
        result['mgmt_no'] = result['settlement_no']
        result.update(recipient=r['sub_name'],slot=r['slot'],principal=r['principal_tax_in'],tax_mode=r['tax_mode'])
        if doc_type == 'principal_invoice':
            result['items_text'] = principal_items_text(view['purchases'])
    else:
        h=view['haak']
        if h.get('confirmed_amount_tax_in') is None or not h.get('confirmed_checked'):
            raise Err('ハーク確定額と「確認済み」を登録してから請求書を発行してください')
        result.update(recipient='請求先',haak_amount=h['confirmed_amount_tax_in'],
                      billing_date=h.get('billing_date'),due_date=h.get('due_date'),items_text=h.get('items_text'),
                      product_details=principal_items_text(view['purchases']) or h.get('items_text') or '',note=h.get('note'))
    return result

def document_payload(c, b, case_id, doc_type, sub_id=None, existing=None):
    if doc_type not in ('principal_invoice','receipt','haak_invoice'):
        raise Err('書類種類が正しくありません')
    base=document_context(c,case_id,doc_type,sub_id)
    today=datetime.date.today().isoformat()
    issued_date=dt(b,'issued_date','発行日') or today
    if doc_type == 'principal_invoice' and not existing:
        rules = funding_ledger.invoice_date_rules(c,int(case_id),int(sub_id))
        issued_date = dt(b,'issued_date','発行日') or rules['default_issued_date']
    if doc_type=='principal_invoice':
        funding = (json.loads(existing['snapshot_json']) if existing and not b.get('recalculate_principal')
                   else funding_ledger.invoice_values(c,int(case_id),int(sub_id),issued_date))
        if 'invoice_amount' not in funding:  # 旧版の帳票も金額を維持して編集可能
            funding = dict(funding, invoice_amount=existing['amount_tax_in'], allocated_principal=funding.get('carried_amount',0), allocated_hold=0)
        amount = funding['invoice_amount']
        base.update(funding)
    elif doc_type=='haak_invoice':
        amount=base['haak_amount']
    else:
        amount=yen(b,'amount_tax_in','受領金額',True)
        if not dt(b,'received_date','受領日'):
            raise Err('領収書には実際の受領日が必要です')
    if existing and b.get('amount_tax_in') is None and doc_type=='receipt': amount=existing['amount_tax_in']
    carry=yen(b,'carried_amount','振替充当済額') or 0
    paid=yen(b,'paid_amount','現金入金済額') or 0
    if doc_type=='principal_invoice':
        carry = base['allocated_principal'] + base['allocated_hold']
        paid = 0  # 実入金は請求額計算に含めない
    if doc_type == 'principal_invoice':
        items = txt(b, 'items_text', '参考商品明細', n=200000) if 'items_text' in b else base['items_text']
    else:
        items = txt(b, 'items_text', '品名明細', n=1200) or base.get('items_text')
    data=dict(base,issued_date=issued_date,amount_tax_in=amount,
              due_date=dt(b,'due_date','支払期限') or base.get('due_date'),
              received_date=dt(b,'received_date','受領日'),
              item_name=txt(b,'item_name','品名',n=120) or base['case_name'],
              items_text=items,
              note=txt(b,'note','備考',n=300) or base.get('note'),
              payment_method=txt(b,'payment_method','受領方法',n=40) or '銀行振込',
              carried_amount=carry,paid_amount=paid,
              payment_terms=txt(b,'payment_terms','支払条件',n=100) or '指定期日までにお振込み',
              valid_until=dt(b,'valid_until','有効期限'))
    if not existing and doc_type != 'receipt':
        apply_document_bank(c, data, b.get('bank_account_id') or '1')
    return data

def _doc_no(c,case_id,doc_type,sub_id):
    mgmt=c.execute('SELECT mgmt_no FROM cases WHERE id=?',(int(case_id),)).fetchone()['mgmt_no']
    prefix={'principal_invoice':'PI','receipt':'RC','haak_invoice':'HI'}[doc_type]
    if sub_id:
        slot=c.execute('SELECT slot FROM case_subs WHERE case_id=? AND sub_id=?',(int(case_id),int(sub_id))).fetchone()['slot']
        suffix=f'-{slot}'
    else: suffix=''
    count=c.execute('SELECT COUNT(*) FROM issued_documents WHERE case_id=? AND doc_type=? AND (sub_id=? OR (sub_id IS NULL AND ? IS NULL))',(int(case_id),doc_type,sub_id,sub_id)).fetchone()[0]
    return prefix+'-'+mgmt+suffix+(f'-{count+1}' if count else '')

def prepare_new_document(c, b, cid):
    typ = b.get('doc_type')
    sid = b.get('sub_id') if typ != 'haak_invoice' else None
    doc = document_payload(c, b, cid, typ, sid)
    sid = int(sid) if sid else None
    doc['doc_no'] = _doc_no(c, cid, typ, sid)
    return typ, sid, doc


def render_new_document(typ, doc):
    try:
        return documents.render(typ, doc)
    except Exception as e:
        raise Err('PDF生成に失敗しました: ' + str(e)) from e


def load_doc_draft(token, session):
    with DOC_DRAFT_LOCK:
        draft = DOC_DRAFTS.get(token)
        if draft is not None and draft['expires'] <= time.monotonic():
            DOC_DRAFTS.pop(token)
            draft = None
        if draft is None or draft['session'] != session:
            raise Err('仮プレビューが見つかりません。仮プレビューを作成し直してください。', 404)
        return draft


def r_doc_preview(c, b, q, cid, *, session):
    c.execute('BEGIN')
    typ, _, doc = prepare_new_document(c, b, cid)
    return store_doc_draft(session, cid, b, doc, render_new_document(typ, doc))


def store_doc_draft(session, cid, form, doc, pdf, **metadata):
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with DOC_DRAFT_LOCK:
        for key, draft in list(DOC_DRAFTS.items()):
            if draft['expires'] <= now or (draft['session'] == session and draft['cid'] == cid):
                DOC_DRAFTS.pop(key)
        while len(DOC_DRAFTS) >= DOC_DRAFT_LIMIT:
            DOC_DRAFTS.pop(next(iter(DOC_DRAFTS)))
        DOC_DRAFTS[token] = dict(session=session, cid=cid, form=form, doc=doc, pdf=pdf,
                                 expires=now + DOC_DRAFT_TTL, **metadata)
    return dict(token=token)


def r_doc_preview_file(c, b, q, token, *, session):
    return pdf_preview(load_doc_draft(token, session)['pdf'], q, 'draft.pdf')


def r_doc_new(c, b, q, cid, *, session):
    token = txt(b, 'preview_token', '仮プレビュー', n=80)
    c.execute('BEGIN IMMEDIATE')
    if token:
        draft = load_doc_draft(token, session)
        if draft['cid'] != cid or 'document_id' in draft:
            raise Err('対象案件の仮プレビューを確認してください。', 409)
        typ, sid, doc = prepare_new_document(c, draft['form'], cid)
        if doc != draft['doc']:
            raise Err('帳票に反映する内容が更新されました。仮プレビューを作成し直してください。', 409)
        pdf = draft['pdf']
    else:
        # 既存のAPI呼出しでも同じ発行処理を使用する。
        typ, sid, doc = prepare_new_document(c, b, cid)
        pdf = render_new_document(typ, doc)
    if typ == 'haak_invoice' and c.execute(
            "SELECT 1 FROM issued_documents WHERE case_id=? AND doc_type='haak_invoice'", (cid,)).fetchone():
        raise Err('ハーク請求書は登録済みです。発行済み帳票の「編集・再発行」を使用してください。', 409)
    if typ == 'principal_invoice':
        if c.execute("SELECT 1 FROM issued_documents WHERE case_id=? AND sub_id=? AND doc_type='principal_invoice'",(int(cid),sid)).fetchone():
            raise Err('発行済みの元金請求書があります。未入金・一部入金の残高を確認し、必要な場合は履歴・編集から訂正・再発行してください。二重発行はできません。',409)
        if not doc['amount_tax_in']:
            raise Err('今回の請求額は0円です。充当済みのため請求書を発行できません。',409)
    stored = attachments.store(DOCS, pdf, '.pdf')
    try:
        cur = c.execute(
            """INSERT INTO issued_documents(doc_type,case_id,sub_id,revision,issued_date,amount_tax_in,snapshot_json,file,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (typ, int(cid), sid, 1, doc['issued_date'], doc['amount_tax_in'], json.dumps(doc, ensure_ascii=False),
             stored, datetime.datetime.now().isoformat(timespec='seconds')))
        c.commit()
    except Exception:
        c.rollback()
        os.remove(attachments.safe_path(DOCS, stored))
        raise
    if token:
        with DOC_DRAFT_LOCK:
            DOC_DRAFTS.pop(token, None)
    return dict(id=cur.lastrowid, revision=1, doc_no=doc['doc_no'])


def r_docs(c,b,q,cid):
    case_view(c,int(cid))
    return case_documents(c, cid)


def case_documents(c, cid):
    out=[]
    for r in c.execute('SELECT * FROM issued_documents WHERE case_id=? ORDER BY id DESC',(int(cid),)):
        d=dict(r);d['snapshot']=json.loads(d.pop('snapshot_json'))
        stored=d.pop('file',None)
        try:
            d['pdf_available']=bool(stored) and os.path.isfile(attachments.safe_path(DOCS,stored))
        except ValueError:
            d['pdf_available']=False
        if d['doc_type']=='principal_invoice':
            received = funding_ledger.received_cash(c,int(cid),d['sub_id'])
            d['cash_received'] = received
            d['unpaid_amount'] = max(0,d['amount_tax_in']-received)
        out.append(d)
    return out

def issued_document(c, did):
    r=c.execute('SELECT * FROM issued_documents WHERE id=?',(int(did),)).fetchone()
    if not r:raise Err('書類が見つかりません',404)
    return r


def edited_document(c, r, b):
    prev=json.loads(r['snapshot_json'])
    merged=dict(prev);merged.update(b)
    doc=document_payload(c,merged,r['case_id'],r['doc_type'],r['sub_id'],existing=r)
    if r['doc_type']=='principal_invoice' and not b.get('recalculate_principal'):
        for key in ('principal','amount_tax_in','allocated_principal','allocated_hold','allocations','invoice_amount','excess','allocation_as_of','carried_amount','paid_amount'):
            if key in prev: doc[key]=prev[key]
        if doc['issued_date'] != prev['issued_date']:
            rows = funding_ledger.recorded_allocation_consents(c,prev.get('allocations', []))
            funding_ledger.validate_invoice_date(doc['issued_date'], rows)
            doc['allocations'] = rows
    if r['doc_type'] != 'receipt':
        for key in BANK_SNAPSHOT_FIELDS:
            if key in prev: doc[key] = prev[key]
        if b.get('bank_account_id') not in (None, '', 'keep'):
            apply_document_bank(c, doc, b['bank_account_id'])
    doc['doc_no']=prev['doc_no']
    return doc


def r_doc_edit_preview(c, b, q, did, *, session):
    c.execute('BEGIN')
    r = issued_document(c, did)
    doc = edited_document(c, r, b)
    return store_doc_draft(session, str(r['case_id']), b, doc, render_new_document(r['doc_type'], doc),
                           document_id=did, revision=r['revision'], sent=r['sent_date'])


def r_doc_put(c,b,q,did,*,session=None):
    c.execute('BEGIN IMMEDIATE')
    r=issued_document(c, did)
    if r['sent_date'] and not b.get('confirm_sent_edit'):
        raise Err('送付済みです。確認のうえ再編集を承認してください')
    token = txt(b, 'preview_token', '仮プレビュー', n=80)
    if token:
        draft = load_doc_draft(token, session)
        if (draft.get('document_id'), draft.get('revision'), draft.get('sent')) != (did, r['revision'], r['sent_date']):
            raise Err('発行状態が更新されました。仮プレビューを作成し直してください', 409)
        doc = edited_document(c, r, draft['form'])
        if doc != draft['doc']:
            raise Err('帳票に反映する内容が更新されました。仮プレビューを作成し直してください', 409)
        pdf = draft['pdf']
    else:
        doc = edited_document(c, r, b)
        pdf = render_new_document(r['doc_type'], doc)
    newfile=attachments.store(DOCS,pdf,'.pdf')
    now=datetime.datetime.now().isoformat(timespec='seconds')
    c.execute('INSERT INTO issued_document_history(doc_id,revision,saved_at,amount_tax_in,snapshot_json,file,sent_date) VALUES(?,?,?,?,?,?,?)',
              (int(did),r['revision'],now,r['amount_tax_in'],r['snapshot_json'],r['file'],r['sent_date']))
    c.execute('UPDATE issued_documents SET revision=?,issued_date=?,amount_tax_in=?,snapshot_json=?,file=?,updated_at=?,sent_date=NULL WHERE id=?',
              (r['revision']+1,doc['issued_date'],doc['amount_tax_in'],json.dumps(doc,ensure_ascii=False),newfile,now,int(did)))
    return dict(ok=True,revision=r['revision']+1)

def r_doc_history(c,b,q,did):
    if not c.execute('SELECT 1 FROM issued_documents WHERE id=?',(int(did),)).fetchone():raise Err('書類が見つかりません',404)
    return [dict(id=r['id'],revision=r['revision'],saved_at=r['saved_at'],amount_tax_in=r['amount_tax_in'],sent_date=r['sent_date']) for r in c.execute('SELECT * FROM issued_document_history WHERE doc_id=? ORDER BY revision DESC',(int(did),))]

def r_doc_download(c,b,q,did):
    r=c.execute('SELECT file FROM issued_documents WHERE id=?',(int(did),)).fetchone()
    if not r: raise Err('書類が見つかりません',404)
    if not r['file']: raise Err('この配布版には発行済みPDFを含めていません。帳票から再発行してください。',404)
    try:raw=attachments.load(DOCS,r['file'])
    except FileNotFoundError:raise Err('書類PDFが見つかりません',404)
    return ('file',raw,'document-'+str(did)+'.pdf','application/pdf')

def r_doc_old_download(c,b,q,hid):
    r=c.execute('SELECT file FROM issued_document_history WHERE id=?',(int(hid),)).fetchone()
    if not r: raise Err('履歴が見つかりません',404)
    if not r['file']: raise Err('この配布版には旧版PDFを含めていません。',404)
    try:raw=attachments.load(DOCS,r['file'])
    except FileNotFoundError:raise Err('旧版PDFが見つかりません',404)
    return ('file',raw,'revision-'+str(hid)+'.pdf','application/pdf')

def r_doc_issued_preview(c, b, q, did):
    _, pdf, name, _ = r_doc_download(c, b, q, did)
    return pdf_preview(pdf, q, name)


def r_doc_history_preview(c, b, q, hid):
    _, pdf, name, _ = r_doc_old_download(c, b, q, hid)
    return pdf_preview(pdf, q, name)


def r_doc_sent(c,b,q,did):
    sent = dt(b,'sent_date','送付日')
    c.execute('UPDATE issued_documents SET sent_date=? WHERE id=?',(sent,int(did)))
    if not c.execute('SELECT changes()').fetchone()[0]:raise Err('書類が見つかりません',404)
    return dict(ok=True)

def r_backup_full(c,b,q):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as out:
        fd,tmp=tempfile.mkstemp(suffix='.db');os.close(fd)
        dst=sqlite3.connect(tmp)
        try:c.backup(dst);dst.close();out.write(tmp,'app.db')
        finally:
            dst.close();os.remove(tmp)
        for folder,prefix in [(FILES,'files'),(DOCS,'documents')]:
            if os.path.isdir(folder):
                for name in os.listdir(folder):
                    path=attachments.safe_path(folder,name)
                    if os.path.isfile(path):out.write(path,prefix+'/'+name)
    return ('file',stream.getvalue(),'full_backup_'+datetime.datetime.now().strftime('%Y%m%d_%H%M')+'.zip','application/zip')


SETTLEMENT_STATES = {
    'not_requested': '依頼前', 'requested': '依頼済み', 'received': '請求書受領済',
    'mismatch': '金額相違', 'paid': '支払済み',
}


def settlement_link(c, cid, sid):
    if c.execute('SELECT 1 FROM principal_cash_plans WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone():
        return True
    if funding_ledger.history(c,'e.sub_id=? AND (e.source_case_id=? OR e.target_id=?)',(sid,cid,cid)) or funding_ledger.cash(c,cid,sid):
        return True
    return c.execute(
        '''SELECT 1 FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
           WHERE s.sub_id=? AND (l.case_id=? OR l.carry_to_case_id=?)''', (sid, cid, cid)).fetchone()


def incoming_carry(c, cid, sid, cutoff=None):
    return funding_ledger.incoming(c,cid,sid,cutoff) + c.execute(
        '''SELECT COALESCE(SUM(l.carry_over),0) FROM settlement_lines l
           JOIN settlements s ON s.id=l.settlement_id
           WHERE l.carry_to_case_id=? AND s.sub_id=?
             AND (? IS NULL OR (s.status='paid' AND s.paid_at<=?))''',
        (cid, sid, cutoff, cutoff)).fetchone()[0]


def principal_confirmed(c, sub, cutoff=None):
    cutoff = cutoff or datetime.date.today().isoformat()
    reserved = incoming_carry(c, sub['case_id'], sub['sub_id'])
    credited = incoming_carry(c, sub['case_id'], sub['sub_id'], cutoff)
    paid = sub['principal_received_date']
    cash = funding_ledger.cash(c,sub['case_id'],sub['sub_id'],cutoff)
    if not funding_ledger.cash(c,sub['case_id'],sub['sub_id']):
        cash = max(0, sub['principal_tax_in'] - reserved) if paid and paid <= cutoff else 0
    return min(sub['principal_tax_in'], credited + cash)


def settlement_period(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
        raise Err('対象月を指定してください')
    first = dt({'month': value + '-01'}, 'month', '対象月')
    if not 1900 <= int(value[:4]) <= 9998:
        raise Err('対象月の年が範囲外です')
    return value, default_settle(first)


def settlement_candidates(c, period, sid=None):
    rows = c.execute(
        '''SELECT cs.*, a.mgmt_no, a.case_no, a.name AS case_name, a.due_date,
                  a.planned_rate_pct, sc.name AS sub_name, h.paid_date AS haak_paid_date
           FROM case_subs cs JOIN cases a ON a.id=cs.case_id
           JOIN subcontractors sc ON sc.id=cs.sub_id
           LEFT JOIN haak_billings h ON h.case_id=a.id
           WHERE a.deleted_at IS NULL AND substr(a.due_date,1,7)=?
             AND (? IS NULL OR cs.sub_id=?)
             AND NOT EXISTS (SELECT 1 FROM settlement_lines l JOIN settlements s
                             ON s.id=l.settlement_id WHERE l.case_id=a.id AND s.sub_id=cs.sub_id)
           ORDER BY a.due_date, a.mgmt_no, cs.slot''', (period, sid, sid))
    out = []
    for row in rows:
        detail = dict(row)
        detail['settlement_no'] = settlement_number(c, row['case_id'], row['sub_id'])
        detail['principal_confirmed'] = principal_confirmed(c, row)
        detail['sub_profit'] = calc.sub_profit(row['principal_tax_in'], row['planned_rate_pct'], row['fee_rate_pct'])
        out.append(detail)
    return out


def settlement_view(c, sid):
    row = c.execute(
        '''SELECT s.*, sc.name AS sub_name, sc.contact_name AS sub_contact FROM settlements s
           JOIN subcontractors sc ON sc.id=s.sub_id WHERE s.id=?''', (sid,)).fetchone()
    if not row:
        raise Err('精算が見つかりません', 404)
    view = dict(row)
    lines = c.execute(
        '''SELECT l.*, a.mgmt_no, a.case_no, a.name AS case_name, a.start_date, a.due_date, cs.slot,
                  target.mgmt_no AS target_no, target.case_no AS target_case_no, target.name AS target_name
           FROM settlement_lines l JOIN cases a ON a.id=l.case_id
           LEFT JOIN case_subs cs ON cs.case_id=l.case_id AND cs.sub_id=?
           LEFT JOIN cases target ON target.id=l.carry_to_case_id
           WHERE l.settlement_id=? ORDER BY a.mgmt_no''', (row['sub_id'], sid))
    view['lines'] = []
    for line in lines:
        item = dict(line)
        item['settlement_no'] = settlement_number(c, line['case_id'], row['sub_id'])
        item['payout'] = calc.sub_payout(line['principal'], line['sub_profit'], line['carry_over'])
        item['profit_only'] = line['carry_over'] > 0 and line['carry_over'] == line['principal']
        item['temporary_hold'] = line['carry_over'] > 0 and line['carry_to_case_id'] is None
        item['hold_consent_pending'] = item['temporary_hold'] and not line['consent_date']
        sub = c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',
                        (line['case_id'], row['sub_id'])).fetchone()
        item['principal_confirmed'] = principal_confirmed(c, sub) if sub else 0
        item['principal_available'] = carry_management.available_principal(
            c, sub, sid, datetime.date.today().isoformat()) if sub else 0
        balance = principal_balance.values(c,sub) if sub else dict(refunded=0,recovered=0)
        item['refunded'] = balance['refunded']
        item['ledger_outgoing'] = funding_ledger.outgoing(c,line['case_id'],row['sub_id'])
        item['payout'] -= balance['refunded'] + item['ledger_outgoing']
        item['profit_only'] = line['carry_over'] + item['ledger_outgoing'] == line['principal'] and line['principal']>0
        item['recovered_available'] = min(item['principal_available'], balance['recovered'])
        item['principal_received_date'] = sub['principal_received_date'] if sub else None
        item['carry_history'] = carry_management.change_history(c, line['id'])
        view['lines'].append(item)
    view['total'] = sum(line['payout'] for line in view['lines'])
    view['invoices'] = [dict(invoice) for invoice in c.execute(
        '''SELECT id, received_date, invoice_no, amount_tax_in, original_name, detected_amount, file
           FROM sub_invoices WHERE settlement_id=? ORDER BY id DESC''', (sid,))]
    for invoice in view['invoices']:
        invoice['stored_path'] = os.path.abspath(attachments.safe_path(DOCS, invoice.pop('file')))
    view['targets'] = [dict(target) for target in c.execute(
        '''SELECT a.id, a.mgmt_no, a.case_no, a.name, a.due_date, cs.principal_tax_in
           FROM cases a JOIN case_subs cs ON cs.case_id=a.id
           WHERE a.deleted_at IS NULL AND cs.sub_id=?
             AND cs.tax_mode=? ORDER BY a.due_date, a.mgmt_no''',
        (row['sub_id'], SUB_TAX_MODE))]
    company = dict(c.execute('SELECT * FROM company WHERE id=1').fetchone())
    sub = dict(c.execute('SELECT * FROM subcontractors WHERE id=?', (row['sub_id'],)).fetchone())
    view['request_contact'] = row['request_contact'] if row['request_contact'] is not None else row['sub_contact'] or ''
    view['request_honorific'] = row['request_honorific'] or ('様' if view['request_contact'] else '御中')
    view['invoice_description'] = row['invoice_description'] or '販売委託業務の精算'
    ex, tax = calc.tax_split(view['total'], row['invoice_tax_rate'])
    document = dict(period=row['period'], pay_date=row['pay_date'], total=view['total'],
                    amount_ex_tax=ex, tax_amount=tax, tax_rate=row['invoice_tax_rate'],
                    sub_name=row['sub_name'], contact=view['request_contact'], honorific=view['request_honorific'],
                    description=view['invoice_description'], company=company, subcontractor=sub, lines=view['lines'])
    document['carry_details'] = [dict(item) for item in c.execute('''
        SELECT l.*,a.case_no,a.name AS case_name,t.case_no AS target_case_no,t.name AS target_name
        FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
        JOIN cases a ON a.id=l.case_id JOIN cases t ON t.id=l.carry_to_case_id
        WHERE s.sub_id=? AND l.carry_over>0
        AND substr(COALESCE(l.carry_confirmed_date,l.consent_date),1,7)=?
        AND NOT EXISTS(SELECT 1 FROM carry_notifications n WHERE n.line_uid=l.carry_uid)
        ORDER BY l.id''',(row['sub_id'],row['period']))]
    document['funding_events'] = funding_ledger.period_events(c,row['sub_id'],row['period'])
    if row['request_snapshot_json']:
        document = json.loads(row['request_snapshot_json'])
    view['request_document'] = document
    frozen = {item['id']: item for item in document['lines']}
    view['request_needs_revision'] = bool(row['request_snapshot_json']) and (
        document['total'] != view['total'] or any(
            item['id'] not in frozen or any(item.get(key) != frozen[item['id']].get(key)
                for key in ('carry_to_case_id', 'carry_over', 'consent_date', 'consent_record', 'consent_method'))
            for item in view['lines']))
    generated_text = settlement_documents.request_text(document)
    view['request_text_generated'] = generated_text
    view['request_text_basis'] = hashlib.sha256(generated_text.encode('utf-8')).hexdigest()
    saved_text = document.get('edited_request_text')
    if not row['request_snapshot_json']:
        stored = getset(c, 'settlement_request_text:' + str(row['id']))
        saved_text = json.loads(stored) if stored else None
    view['request_text_custom'] = saved_text is not None
    view['request_text_stale'] = bool(saved_text and saved_text['basis'] != view['request_text_basis'])
    view['request_text'] = saved_text['text'] if saved_text else generated_text
    view['request_text_revision'] = hashlib.sha256(json.dumps(saved_text, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    if saved_text:
        document['edited_request_text'] = saved_text
    view['amount_ex_tax'], view['tax_amount'] = document['amount_ex_tax'], document['tax_amount']
    view['archive_directory'] = pdf_archive_root(c)
    return view


def r_settlement_request(c, b, q, settlement_id):
    c.execute('BEGIN IMMEDIATE')
    view = settlement_draft(c, settlement_id)
    description = txt(b, 'invoice_description', '請求内容', True, 500)
    contact = txt(b, 'request_contact', '宛先担当者', n=80) or ''
    honorific = b.get('request_honorific')
    if honorific not in ('様', '御中') or (contact and honorific != '様'):
        raise Err('担当者を指定する場合は敬称「様」を選んでください')
    rate = yen(b, 'invoice_tax_rate', '税率', True)
    if rate not in (8, 10):
        raise Err('税率は8または10を指定してください')
    c.execute('''UPDATE settlements SET invoice_description=?,request_contact=?,request_honorific=?,
                 invoice_tax_rate=? WHERE id=?''', (description, contact, honorific, rate, view['id']))
    return settlement_view(c, settlement_id)


def r_settlement_request_text(c, b, q, settlement_id):
    c.execute('BEGIN IMMEDIATE')
    view = settlement_draft(c, settlement_id)
    if b.get('expected_revision') != view['request_text_revision'] or b.get('basis') != view['request_text_basis']:
        raise Err('依頼文または精算内容が更新されています。画面を開き直して確認してください', 409)
    key = 'settlement_request_text:' + str(view['id'])
    if b.get('reset') is True:
        c.execute('DELETE FROM settings WHERE k=?', (key,))
    else:
        if not isinstance(b.get('text'), str):
            raise Err('請求依頼文を入力してください')
        text = txt(b, 'text', '請求依頼文', True, 20000)
        if any(ord(ch) < 32 and ch not in '\r\n\t' for ch in text):
            raise Err('請求依頼文に使用できない文字があります')
        setset(c, key, json.dumps(dict(text=text, basis=view['request_text_basis']), ensure_ascii=False))
    return settlement_view(c, settlement_id)


def r_settlement_template(c, b, q, settlement_id):
    view = settlement_view(c, settlement_id)
    if not view['request_document']['company']['name']:
        raise Err('設定で自社の会社名を登録してください')
    if any(line['payout'] < 0 for line in view['lines']):
        raise Err('各案件の請求額が0円以上であることを確認してください')
    try:
        raw = settlement_documents.render(view['request_document'])
    except ValueError as exc:
        raise Err(str(exc)) from exc
    kind = 'inline' if q.get('preview') == ['1'] else 'file'
    return kind, raw, f"invoice-template-{view['period']}-{view['id']}.pdf", 'application/pdf'


def r_settlement_template_preview(c, b, q, settlement_id):
    result = r_settlement_template(c, b, {}, settlement_id)
    return pdf_preview(result[1], q, result[2])


def r_monthly(c, b, q):
    period, due = settlement_period(q.get('period', [datetime.date.today().strftime('%Y-%m')])[0])
    settlements = [settlement_view(c, row['id']) for row in c.execute(
        'SELECT id FROM settlements WHERE period=? ORDER BY sub_id', (period,))]
    periods = [row[0] for row in c.execute(
        '''SELECT period FROM settlements UNION SELECT substr(due_date,1,7) FROM cases
           WHERE due_date IS NOT NULL AND deleted_at IS NULL ORDER BY 1 DESC''')]
    return dict(period=period, pay_date=due, settlements=settlements, periods=periods,
                candidates=settlement_candidates(c, period), states=SETTLEMENT_STATES)


def r_settlement_create(c, b, q):
    period, pay_date = settlement_period(b.get('period'))
    sid = yen(b, 'sub_id', '下請け', True)
    c.execute('BEGIN IMMEDIATE')
    old = c.execute('SELECT * FROM settlements WHERE sub_id=? AND period=?', (sid, period)).fetchone()
    if old and old['status'] != 'not_requested':
        raise Err('この月の精算は依頼済みです。追加分を登録するには依頼を取り消してください', 409)
    candidates = [row for row in settlement_candidates(c, period, sid) if row['tax_mode'] == SUB_TAX_MODE]
    if not candidates:
        raise Err('この月に追加できる販売委託型の案件がありません')
    settlement_id = old['id'] if old else c.execute(
        'INSERT INTO settlements(sub_id,period,pay_date) VALUES(?,?,?)', (sid, period, pay_date)).lastrowid
    c.executemany('INSERT INTO settlement_lines(settlement_id,case_id,principal,sub_profit,carry_uid) VALUES(?,?,?,?,?)',
                  ((settlement_id, row['case_id'], row['principal_tax_in'], row['sub_profit'], secrets.token_hex(16)) for row in candidates))
    return settlement_view(c, settlement_id)


def settlement_draft(c, settlement_id):
    view = settlement_view(c, settlement_id)
    if view['status'] != 'not_requested':
        raise Err('依頼前の精算だけ編集できます', 409)
    return view


def r_settlement_delete(c, b, q, settlement_id):
    c.execute('BEGIN IMMEDIATE')
    view = settlement_draft(c, settlement_id)
    if any(line['carry_over'] or funding_ledger.outgoing(c,line['case_id'],view['sub_id']) or c.execute('SELECT 1 FROM custody_funds WHERE line_id=?',(line['id'],)).fetchone() for line in view['lines']):
        raise Err('元金充当がある精算は削除できません。明細の編集・取消を使用してください', 409)
    if c.execute(
        '''SELECT 1 FROM settlement_lines l JOIN case_subs cs ON cs.case_id=l.carry_to_case_id
           WHERE l.settlement_id=? AND cs.sub_id=? AND cs.principal_received_date IS NOT NULL''',
        (settlement_id, view['sub_id'])).fetchone():
        raise Err('充当先の残額が入金済みのため、精算明細を削除できません')
    c.execute('DELETE FROM settlement_lines WHERE settlement_id=?', (settlement_id,))
    c.execute('DELETE FROM settlements WHERE id=?', (settlement_id,))
    c.execute('DELETE FROM settings WHERE k=?', ('settlement_request_text:' + str(view['id']),))
    return dict(ok=True)


def r_carry(c, b, q, settlement_id, line_id):
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    view = settlement_view(c, settlement_id)
    line = next((line for line in view['lines'] if line['id'] == int(line_id)), None)
    if not line:
        raise Err('明細が見つかりません', 404)
    if b.get('expected_revision') is not None and yen(b, 'expected_revision', '編集時の版番号', True) != line['carry_revision']:
        raise Err('別の操作で充当内容が更新されました。画面を開き直して確認してください', 409)
    if not line['carry_over'] and view['status'] != 'not_requested':
        raise Err('依頼済み以降に新しく充当することはできません。既存の充当記録を選んでください', 409)
    if c.execute('SELECT 1 FROM custody_funds WHERE line_id=?',(line_id,)).fetchone():
        raise Err('台帳登録済みの預かり金は預かり金台帳から充当・返金してください',409)
    amount = yen(b, 'carry_over', '充当額', True)
    if view['status'] == 'paid' and amount != line['carry_over']:
        raise Err('支払済みの充当額は変更できません。充当先・承諾情報のみ訂正できます', 409)
    if amount > line['principal'] or line['principal'] + line['sub_profit'] - amount < 0:
        raise Err('入金確認済みの元金まで充当できます')
    target_id = consent_date = consent = method = None
    if amount:
        principal_balance.validate_case(c,line['case_id'])
        if line['recovered_available'] - line['refunded'] - funding_ledger.outgoing(c,line['case_id'],view['sub_id']) < amount:
            raise Err('入金確認済みの元金まで充当できます')
        release = c.execute('SELECT amount FROM principal_release WHERE case_id=? AND sub_id=?',
                            (line['case_id'], view['sub_id'])).fetchone()
        if release and amount > release['amount']:
            raise Err('充当へ回せる確認済額を超えています')
        holding = b.get('target_id') == 'hold'
        consent_date = dt(b, 'consent_date', '承諾日')
        consent = txt(b, 'consent_record', '承諾記録', not holding, 500)
        method = txt(b, 'consent_method', '承諾方法', n=80) if 'consent_method' in b else line['consent_method']
        if not consent_date or consent_date > datetime.date.today().isoformat():
            raise Err('承諾日を確認してください')
        if holding:
            method = method or 'LINE'
    else:
        holding = False
    if amount and not holding:
        target_id = yen(b, 'target_id', '充当先の案件', True)
        target = c.execute(
            '''SELECT cs.*, a.due_date FROM case_subs cs JOIN cases a ON a.id=cs.case_id
               WHERE a.deleted_at IS NULL AND cs.case_id=? AND cs.sub_id=?''', (target_id, view['sub_id'])).fetchone()
        if (not target or target_id == line['case_id'] or not target['due_date'] or not line['due_date'] or
                target['due_date'] <= line['due_date'] or target['tax_mode'] != SUB_TAX_MODE):
            raise Err('同じ下請けで、充当元より納期が後の販売委託型案件を選んでください（同月内も可）')
        capital_changes = target_id != line['carry_to_case_id'] or amount != line['carry_over']
        if capital_changes and (target['principal_received_date'] or c.execute(
            '''SELECT 1 FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
               WHERE l.case_id=? AND s.sub_id=? AND s.status<>'not_requested' ''', (target_id, view['sub_id'])).fetchone()):
            raise Err('元金入金済みまたは依頼済みの案件には充当できません')
        other = incoming_carry(c, target_id, view['sub_id'])
        if line['carry_to_case_id'] == target_id:
            other -= line['carry_over']
        if other + amount + funding_ledger.cash(c,target_id,view['sub_id']) > target['principal_tax_in']:
            raise Err('充当額の合計が充当先の担当元金を超えています')
    capital_changes = target_id != line['carry_to_case_id'] or amount != line['carry_over']
    if line['carry_to_case_id'] and capital_changes:
        if funding_ledger.outgoing(c,line['carry_to_case_id'],view['sub_id']):
            raise Err('変更前の充当先に後続の充当があります',409)
        old_target = c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',
                              (line['carry_to_case_id'], view['sub_id'])).fetchone()
        if old_target and (old_target['principal_received_date'] or funding_ledger.cash(c,line['carry_to_case_id'],view['sub_id'])):
            raise Err('変更前の充当先に元金の現金入金記録があります。残額入金との整合を先に確認してください', 409)
        if c.execute('''SELECT 1 FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
                        WHERE l.case_id=? AND s.sub_id=? AND
                        (s.status<>'not_requested' OR l.carry_over>0)''',
                     (line['carry_to_case_id'], view['sub_id'])).fetchone():
            raise Err('変更前の充当先が依頼済み・支払済み、または次の充当元になっています。後続記録を先に確認してください', 409)
    fields = ('carry_over', 'carry_to_case_id', 'consent_date', 'consent_record', 'consent_method')
    values = (amount, target_id, consent_date, consent, method)
    if tuple(line[key] for key in fields) == values:
        return view
    before = carry_management.snapshot(c, settlement_id, line_id)
    reason = txt(b, 'change_reason', '変更理由', n=500) or '元金充当の登録・編集'
    try:
        backup = carry_management.backup_database(c)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise Err('変更前のDBバックアップに失敗したため、充当は変更していません：' + str(exc), 409) from exc
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    allocation_date = datetime.date.today().isoformat() if amount and (not line['carry_over'] or target_id != line['carry_to_case_id']) else line['carry_confirmed_date']
    c.execute('''UPDATE settlement_lines SET carry_over=?,carry_to_case_id=?,consent_date=?,consent_record=?,consent_method=?,
                 carry_revision=carry_revision+1,carry_confirmed_date=?,carry_created_at=COALESCE(carry_created_at,?)
                 WHERE id=? AND settlement_id=?''', values + (allocation_date,now,line_id,settlement_id))
    if view['invoices'] and view['status'] != 'paid':
        total = settlement_view(c,settlement_id)['total']
        c.execute('UPDATE settlements SET status=? WHERE id=?',
                  ('received' if view['invoices'][0]['amount_tax_in'] == total else 'mismatch', settlement_id))
    after = carry_management.snapshot(c, settlement_id, line_id)
    c.execute('''INSERT INTO carry_change_history(line_id,line_uid,settlement_id,sub_id,changed_at,reason,
                 before_json,after_json,backup_file) VALUES(?,?,?,?,?,?,?,?,?)''',
              (line_id, line['carry_uid'], settlement_id, view['sub_id'], datetime.datetime.now(datetime.timezone.utc).isoformat(),
               reason, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), backup))
    return settlement_view(c, settlement_id)


def r_settlement_status(c, b, q, settlement_id):
    c.execute('BEGIN IMMEDIATE')
    view = settlement_view(c, settlement_id)
    status = b.get('status')
    today = datetime.date.today().isoformat()
    if (view['status'], status) == ('not_requested', 'requested'):
        if view['request_text_stale']:
            raise Err('精算内容が変わっています。編集した請求依頼文を確認・保存してから依頼済みにしてください', 409)
        if any(line['hold_consent_pending'] for line in view['lines']):
            raise Err('一時預かりの了承待ちです。LINEで了承を得てから承諾日を登録してください')
        if any(row['tax_mode'] == SUB_TAX_MODE for row in settlement_candidates(c, view['period'], view['sub_id'])):
            raise Err('対象月に未登録の案件があります。先に対象月の明細を追加してください')
        if not view['lines'] or any(line['principal_available'] < line['principal'] or line['payout'] < 0
                                    for line in view['lines']):
            raise Err('各案件の元金入金と振込額を確認してください')
        if today[:7] <= view['period']:
            raise Err('対象月の月末締め後に請求依頼を記録できます')
        c.execute('UPDATE settlements SET status=?,requested_at=?,request_snapshot_json=? WHERE id=?',
                  (status, today, json.dumps(view['request_document'], ensure_ascii=False), settlement_id))
        for event in view['request_document'].get('funding_events', []):
            c.execute('INSERT OR IGNORE INTO funding_event_notifications(event_id,settlement_id,notified_at) VALUES(?,?,?)',(event['id'],settlement_id,today))
        for item in view['request_document'].get('carry_details', []):
            c.execute('INSERT OR IGNORE INTO carry_notifications(line_uid,settlement_id,notified_at) VALUES(?,?,?)',
                      (item['carry_uid'],settlement_id,today))
    elif (view['status'], status) == ('requested', 'not_requested') and not view['invoices']:
        c.execute('UPDATE settlements SET status=?,requested_at=NULL,request_snapshot_json=NULL WHERE id=?', (status, settlement_id))
    elif status == 'paid' and (view['status'] == 'received' or
                               (view['status'] == 'requested' and view['total'] == 0)):
        if any(line['hold_consent_pending'] for line in view['lines']):
            raise Err('一時預かりの了承待ちです。LINEで了承を得てから承諾日を登録してください')
        paid = dt(b, 'paid_at', '支払・充当完了日')
        earliest = view['invoices'][0]['received_date'] if view['invoices'] else view['requested_at']
        if not paid or paid > today or (earliest and paid < earliest):
            raise Err('支払・充当完了日は請求書受領日以降の日付を指定してください')
        if view['total'] and (not view['invoices'] or view['invoices'][0]['amount_tax_in'] != view['total']):
            raise Err('請求書と依頼金額が一致していません')
        c.execute('UPDATE settlements SET status=?,paid_at=? WHERE id=?', (status, paid, settlement_id))
    else:
        raise Err('この状態では操作できません', 409)
    return settlement_view(c, settlement_id)


def settlement_invoice_amount(raw):
    with purchase_reader.open_pdf(raw) as pdf:
        if len(pdf) > 20:
            raise Err('請求書PDFは20ページ以内にしてください')
        text = unicodedata.normalize('NFKC', '\n'.join(page.get_text(sort=True) for page in pdf))
    amounts = re.findall(
        r'(?:今回\s*御?請求額|ご?請求(?:金額|額|合計)|税込合計|合計\s*\(税込\))'
        r'\s*[:：]?\s*(?:[¥￥\\]\s*)?([0-9][0-9,]*)(?=\s|円|$)', text)
    values = {int(amount.replace(',', '')) for amount in amounts}
    return next(iter(values)) if len(values) == 1 else None


def r_sub_invoice(c, b, q, settlement_id):
    name, _, raw = attachment_upload(b)
    amount = settlement_invoice_amount(raw)
    if amount is not None and amount > 10**12:
        raise Err('読み取った請求額が範囲外です')
    verified = yen(b, 'amount_tax_in', '原本確認後の請求額')
    if verified is not None and not b.get('confirm_amount'):
        raise Err('手入力する場合は原本の金額を確認してください')
    if verified is None:
        verified = amount
    if verified is None:
        raise Err('請求額を特定できません。原本の税込請求額を確認して入力してください')
    received = dt(b, 'received_date', '受領日')
    number = txt(b, 'invoice_no', '請求書番号', n=80)
    c.execute('BEGIN IMMEDIATE')
    view = settlement_view(c, settlement_id)
    if view['status'] not in ('requested', 'received', 'mismatch'):
        raise Err('請求依頼後の未払い精算に登録できます', 409)
    if not received or received > datetime.date.today().isoformat() or (
        view['requested_at'] and received < view['requested_at']
    ):
        raise Err('依頼日以降の請求書受領日を入力してください')
    file = attachments.store(DOCS, raw, '.pdf')
    c.execute('''INSERT INTO sub_invoices(settlement_id,received_date,invoice_no,amount_tax_in,file,original_name,detected_amount)
                 VALUES(?,?,?,?,?,?,?)''', (settlement_id, received, number, verified, file, name, amount))
    c.execute('UPDATE settlements SET status=? WHERE id=?',
              ('received' if verified == view['total'] else 'mismatch', settlement_id))
    return settlement_view(c, settlement_id)


def r_sub_invoice_preview(c, b, q, invoice_id):
    row = c.execute('SELECT file FROM sub_invoices WHERE id=?', (invoice_id,)).fetchone()
    if not row:
        raise Err('受領PDFが見つかりません', 404)
    path = attachments.safe_path(DOCS, row['file'])
    if not os.path.isfile(path):
        raise Err('受領PDFファイルが見つかりません', 404)
    with open(path, 'rb') as file:
        return pdf_preview(file.read(), q, f'sub-invoice-{invoice_id}.pdf')


def csv_file(name, header, rows):
    def safe(value):
        if isinstance(value, str):
            visible = unicodedata.normalize('NFKC', value).lstrip()
            if value.startswith(('\t', '\r', '\n')) or visible.startswith(('=', '+', '-', '@')):
                return "'" + value
        return value

    buf = io.StringIO(newline='')
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(header)
    writer.writerows([safe(value) for value in row] for row in rows)
    return 'file', buf.getvalue().encode('utf-8-sig'), name + '.csv', 'text/csv; charset=utf-8'


def report_cases(c):
    return [case_view(c, r['id']) for r in c.execute('SELECT id FROM cases WHERE deleted_at IS NULL ORDER BY mgmt_no')]


def report_settlements(c):
    return [settlement_view(c, row['id']) for row in c.execute('SELECT id FROM settlements ORDER BY period,sub_id')]


def transaction_rows(c, cases, settlements):
    suppliers = {row['id']: row['invoice_no'] for row in c.execute('SELECT id,invoice_no FROM suppliers')}
    subs = {row['id']: row['invoice_no'] for row in c.execute('SELECT id,invoice_no FROM subcontractors')}
    for v in cases:
        a, h = v['case'], v['haak']
        for p in v['purchases']:
            original = p['invoice_original'] or {}
            rates = sorted({item['tax_rate'] for item in original.get('items', []) if item.get('tax_rate') is not None})
            tax_rates = ' / '.join(f'{rate}%' for rate in rates)
            registration = original.get('fields', {}).get('supplier_invoice_no') or suppliers[p['supplier_id']]
            yield (p['invoice_date'], a['mgmt_no'], '仕入請求', p['supplier_name'], p['amount_tax_in'],
                   registration, p['due_date'], p['paid_date'], p['paid_amount'], tax_rates, None)
        if h.get('confirmed_amount_tax_in') is not None:
            yield (h.get('billing_date'), a['mgmt_no'], 'ハーク請求', 'ハーク', h['confirmed_amount_tax_in'],
                   None, h.get('due_date'), h.get('paid_date'),
                   (h.get('actual_paid_amount') if h.get('actual_paid_amount') is not None else h['confirmed_amount_tax_in']) if h.get('paid_date') else 0, '10%', None)
        for s in v['subs']:
            cash_entries=c.execute('SELECT * FROM principal_cash_entries WHERE case_id=? AND sub_id=? ORDER BY id',(a['id'],s['sub_id'])).fetchall()
            if cash_entries:
                for payment in cash_entries:
                    yield (payment['received_on'],a['mgmt_no'],'下請け元金（現金）',s['sub_name'],payment['amount'],subs[s['sub_id']],None,payment['received_on'],payment['amount'],None,s['settlement_no'])
                continue
            reserved = incoming_carry(c, a['id'], s['sub_id'])
            yield (s['principal_received_date'], a['mgmt_no'], '下請け元金（現金）', s['sub_name'],
                   s['principal_tax_in'] - reserved, subs[s['sub_id']], None, s['principal_received_date'],
                   s['principal_tax_in'] - reserved if s['principal_received_date'] else 0,
                   '10%' if s['tax_mode'] == SUB_TAX_MODE else None, s['settlement_no'])
    for s in settlements:
        for line in s['lines']:
            yield (s['period'] + '-01', line['mgmt_no'], '下請け精算', s['sub_name'], line['payout'],
                   s['request_document']['subcontractor'].get('invoice_no') or subs[s['sub_id']],
                   s['pay_date'], s['paid_at'], line['payout'] if s['paid_at'] else 0,
                   f"{s['request_document']['tax_rate']}%", line['settlement_no'])
    for history in c.execute('''SELECT h.*,a.mgmt_no,sc.name FROM principal_account_history h
        JOIN cases a ON a.id=h.case_id JOIN subcontractors sc ON sc.id=h.sub_id ORDER BY h.id'''):
        before, after = json.loads(history['before_json']), json.loads(history['after_json'])
        refund = after['refunded'] - before.get('refunded',0)
        if refund:
            yield (after['record_date'],history['mgmt_no'],'下請け精算',history['name'],refund,
                   subs[history['sub_id']],None,after['record_date'],refund,None,
                   settlement_number(c,history['case_id'],history['sub_id']))

    for refund in c.execute('''SELECT e.*,a.mgmt_no,sc.name FROM funding_events e JOIN cases a ON a.id=e.source_case_id
        JOIN subcontractors sc ON sc.id=e.sub_id WHERE e.kind='refund' ORDER BY e.id'''):
        yield (refund['event_date'],refund['mgmt_no'],'下請け精算',refund['name'],refund['amount'],
               subs[refund['sub_id']],None,refund['event_date'],refund['amount'],None,
               settlement_number(c,refund['source_case_id'],refund['sub_id']))


def balances_at(c, cases, settlements, cutoff):
    custody=funding_ledger.funds(c,cutoff=cutoff)
    managed={x['line_id']:x for x in custody}
    for fund in custody:
        if fund['balance']:
            yield ('元金一時預かり',fund['case_no'] or '案件番号未登録',fund['name'],fund['sub_name'],fund['balance'],None)
    lines = {(line['case_id'], s['sub_id']): (s, line) for s in settlements for line in s['lines']}
    for v in cases:
        a, h = v['case'], v['haak']
        number, name = a['mgmt_no'], a['name']
        if h.get('confirmed_amount_tax_in') is not None and h.get('billing_date') and h['billing_date'] <= cutoff:
            received = (h.get('actual_paid_amount') if h.get('actual_paid_amount') is not None else h['confirmed_amount_tax_in']) if h.get('paid_date') and h['paid_date'] <= cutoff else 0
            unpaid = max(0,h['confirmed_amount_tax_in']-received)
            if unpaid:
                yield ('ハーク未入金', number, name, 'ハーク', unpaid, None)
        for p in v['purchases']:
            if not p['invoice_date'] or p['invoice_date'] > cutoff:
                continue
            amount = p['amount_tax_in'] - (p['paid_amount'] if p['paid_date'] and p['paid_date'] <= cutoff else 0)
            if amount:
                yield ('仕入未払い', number, name, p['supplier_name'], amount, None)
            delivery = (p['invoice_details'] or {}).get('delivery_date') or a['due_date']
            if delivery and delivery > cutoff:
                yield ('納期未到来の仕入', number, name, p['supplier_name'], p['amount_tax_in'], None)
        for s in v['subs']:
            settlement, line = lines.get((a['id'], s['sub_id']), ({}, {}))
            paid = settlement.get('paid_at')
            if paid and paid <= cutoff:
                if line.get('temporary_hold') and line.get('id') not in managed:
                    yield ('元金一時預かり', number, name, s['sub_name'], line['carry_over'], s['settlement_no'])
                continue
            received = max(0,principal_confirmed(c,s,cutoff)-funding_ledger.outgoing(c,a['id'],s['sub_id'],cutoff)-(managed.get(line.get('id'),{}).get('amount',0)))
            if received:
                yield ('精算前の確認済元金', number, name, s['sub_name'], received, s['settlement_no'])
            if a['due_date'] and a['due_date'] <= cutoff:
                payout = line.get('payout', s['payout'])
                if payout:
                    yield ('下請け未精算', number, name, s['sub_name'], payout, s['settlement_no'])



def r_export(c, b, q, report):
    period = q.get('period', [''])[0]
    if report == 'funding':
        if period:
            settlement_period(period+'-01' if re.fullmatch(r'\d{4}',period) else period)
        rows = funding_ledger.export_history(c)
        labels={'created':'一時預かり発生','principal':'他案件元金充当','hold':'一時預かり金充当','refund':'一時預かり金返金','cancel':'充当取消','legacy_change':'旧元金充当の変更履歴','legacy':'旧元金充当明細'}
        return csv_file(report, ('履歴ID','区分','元案件番号','元案件名','下請け','預かり金ID','充当先案件番号','充当先案件名','金額','確定日','承諾日','承諾方法','承諾記録','記録','取消元ID'),
            ((x['id'],labels[x['kind']],x['case_no'],x['name'],x['sub_name'],x['fund_id'],x['target_case_no'],x['target_name'],x['amount'],x['event_date'],x['consent_date'],x['consent_method'],x['consent_record'],x['note'],x['reverses']) for x in rows if not period or x['event_date'].startswith(period)))
    if period:
        if re.fullmatch(r'\d{4}', period):
            settlement_period(period + '-01')
        else:
            settlement_period(period)

    def in_period(date):
        return (not period) or bool(date and date.startswith(period))

    cases = report_cases(c)
    settlements = report_settlements(c)
    if report == 'transactions':
        rows = (row for row in transaction_rows(c, cases, settlements) if in_period(row[0]))
        return csv_file(report, ('取引日', '案件管理番号', '区分', '取引先', '税込金額', '登録番号',
                                 '期限', '入出金日', '入出金額', '税率', '精算管理番号'), rows)
    if report == 'cashflow':
        months = {}
        keys = ('ハーク請求', '下請け元金（現金）', '仕入請求', '下請け精算')
        for _, _, kind, _, _, _, _, paid, amount, _, _ in transaction_rows(c, cases, settlements):
            if paid and in_period(paid):
                groups = months.setdefault(paid[:7], dict.fromkeys(keys, 0))
                groups[kind] += amount
        return csv_file(report, ('入出金月', 'ハーク入金', '下請け元金入金', '仕入支払', '下請け精算支払', '差引'),
                        ((month, *(d[key] for key in keys), d[keys[0]] + d[keys[1]] - d[keys[2]] - d[keys[3]])
                         for month, d in sorted(months.items())))
    if report == 'profit':
        rows = ((a['mgmt_no'], a['case_no'], a['name'], a['due_date'], k['purchase_total'], k['basis_amount'],
                 '確定' if k['confirmed'] else '見込', k['total_profit'], k['sub_profit_total'], k['own_profit'])
                for a, k in ((v['case'], v['calc']) for v in cases) if in_period(a['due_date']))
        return csv_file(report, ('管理番号', '案件番号', '案件名', '納期', '仕入額', 'ハーク請求額',
                                 '確定区分', '全体利益', '下請け利益', '自社利益'), rows)
    if report == 'settlements':
        rows = ((s['period'], s['sub_name'], line['slot'], line['mgmt_no'], line['case_name'], line['principal'],
                 line['sub_profit'], line['carry_over']+line.get('ledger_outgoing',0), line['target_no'], line['consent_date'],
                 line['consent_record'], line['payout'], s['pay_date'], SETTLEMENT_STATES[s['status']], s['paid_at'], line['settlement_no'],
                 line['case_no'], line['target_case_no'], '利益のみ' if line['profit_only'] else '元金残額＋利益',
                 s['request_document']['tax_rate'], '一時預かり' if line['temporary_hold'] else '別案件充当' if line['carry_over']+line.get('ledger_outgoing',0) else '返金',
                 line['carry_over'] if line['temporary_hold'] else 0, line['consent_method'],
                 ' / '.join((e['target_case_no'] or '案件番号未登録')+' '+(e['target_name'] or '')+' '+str(e['amount'])+'円 '+e['event_date'] for e in funding_ledger.history(c,"e.source_case_id=? AND e.sub_id=? AND e.kind='principal'",(line['case_id'],s['sub_id'])) if not e['cancelled']))
                for s in settlements if in_period(s['period']) for line in s['lines'])
        return csv_file(report, ('対象月', '下請け', '下請け枠', '案件管理番号', '案件名', '元金', '下請け利益',
                                 '充当額', '充当先', '承諾日', '承諾記録', '振込額', '支払期限', '状態', '完了日', '精算管理番号',
                                 '案件番号', '充当先案件番号', '請求方式', '依頼税率', '元金の取扱い', '一時預かり額', '承諾方法', '分割充当先・金額・確定日'), rows)
    cutoff = dt({'at': q.get('at', [''])[0]}, 'at', '期末日')
    if not cutoff:
        raise Err('期末日を指定してください')
    rows = ((cutoff, *row) for row in balances_at(c, cases, settlements, cutoff))
    return csv_file(report, ('期末日', '区分', '案件管理番号', '案件名', '取引先', '金額', '精算管理番号'), rows)


def r_evidence_bundle(c, b, q, cid):
    case = case_view(c, int(cid))['case']
    root = pdf_archive_root(c)
    pdf_archive.sync(c, FILES, DOCS, root)
    with open(os.path.join(os.path.dirname(DOCS), 'pdf_export_manifest.json'), encoding='utf-8') as source:
        state = json.load(source)
    item = state['roots'][str(Path(root).resolve())][f'case:{int(cid)}:1']
    path = pdf_archive.checked_target(Path(root), item['name'])
    if pdf_archive.digest(path) != item['digest']:
        raise Err('証憑一式PDFが変更されました。もう一度作成してください', 409)
    return 'file', path.read_bytes(), f'evidence-{case["mgmt_no"]}.pdf', 'application/pdf'


def r_evidence(c, b, q, cid):
    case = case_view(c, int(cid))['case']
    stream = io.BytesIO()
    manifest = []
    types = {'principal_invoice': '元金請求書', 'receipt': '元金領収書', 'haak_invoice': 'ハーク請求書'}
    group = case['mgmt_no']
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as out:
        def add(folder, stored, label, original=None):
            path = attachments.safe_path(folder, stored) if stored else None
            name = f'{group}/{label}.pdf'
            present = bool(path and os.path.isfile(path))
            if present:
                out.write(path, name)
            manifest.append((name, original, '収録' if present else 'ファイルなし'))

        for a in c.execute("SELECT * FROM attachments WHERE case_id=? AND mime='application/pdf'", (cid,)):
            add(FILES, a['stored_name'], f"仕入等/添付-{a['id']}", a['original_name'])
            for old in c.execute("SELECT * FROM attachment_revisions WHERE attachment_id=? AND mime='application/pdf'", (a['id'],)):
                add(FILES, old['stored_name'], f"仕入等/履歴/添付-{a['id']}-{old['id']}", old['original_name'])
        for doc in c.execute('SELECT * FROM issued_documents WHERE case_id=?', (cid,)):
            label = f"{types[doc['doc_type']]}-{doc['id']}"
            add(DOCS, doc['file'], f"発行/{label}-v{doc['revision']}")
            for old in c.execute('SELECT * FROM issued_document_history WHERE doc_id=?', (doc['id'],)):
                add(DOCS, old['file'], f"発行/履歴/{label}-v{old['revision']}")
        for invoice in c.execute(
            '''SELECT i.* FROM sub_invoices i JOIN settlement_lines l ON l.settlement_id=i.settlement_id
               WHERE l.case_id=?''', (cid,)):
            add(DOCS, invoice['file'], f"月次精算/下請け受領-{invoice['id']}", invoice['original_name'])
        out.writestr(f'{group}/証憑一覧.csv', csv_file('manifest', ('ZIP内のファイル', '原本名', '状態'), manifest)[1])
    return 'file', stream.getvalue(), f'evidence-{group}.zip', 'application/zip'


ROUTES = [
    ('GET', r'/api/cases/(\d+)/deletion-check', r_case_deletion_check),
    ('GET', r'/api/settings/pdf-archive', r_pdf_archive_settings),
    ('PUT', r'/api/settings/pdf-archive', r_pdf_archive_settings_put),
    ('GET', r'/api/export/(transactions|cashflow|profit|settlements|balances)', r_export),
    ('GET', r'/api/cases/(\d+)/evidence', r_evidence),
    ('GET', r'/api/cases/(\d+)/evidence-bundle', r_evidence_bundle),
    ('GET', r'/api/monthly', r_monthly), ('POST', r'/api/settlements', r_settlement_create),
    ('DELETE', r'/api/settlements/(\d+)', r_settlement_delete),
    ('PUT', r'/api/settlements/(\d+)/lines/(\d+)', r_carry),
    ('PUT', r'/api/settlements/(\d+)/status', r_settlement_status),
    ('PUT', r'/api/settlements/(\d+)/request', r_settlement_request),
    ('PUT', r'/api/settlements/(\d+)/request-text', r_settlement_request_text),
    ('GET', r'/api/settlements/(\d+)/invoice-template', r_settlement_template),
    ('GET', r'/api/settlements/(\d+)/invoice-template/preview', r_settlement_template_preview),
    ('POST', r'/api/settlements/(\d+)/invoice', r_sub_invoice),
    ('GET', r'/api/sub-invoices/(\d+)/preview', r_sub_invoice_preview),
    ('GET', r'/api/dashboard', r_dashboard), ('GET', r'/api/cases', r_cases), ('POST', r'/api/cases', r_case_new),
    ('POST', r'/api/cases/line-preview', r_case_line_preview),
    ('GET', r'/api/cases/(\d+)', r_case_get), ('PUT', r'/api/cases/(\d+)', r_case_put),
    ('POST', r'/api/cases/(\d+)/trash', r_trash_move), ('POST', r'/api/cases/(\d+)/restore', r_restore),
    ('POST', r'/api/cases/(\d+)/purge', r_purge), ('GET', r'/api/trash', r_trash),
    ('POST', r'/api/cases/(\d+)/purchases', r_purchase_new), ('PUT', r'/api/purchases/(\d+)', r_purchase_put),
    ('DELETE', r'/api/purchases/(\d+)', r_purchase_del),
    ('PUT', r'/api/cases/(\d+)/subs/([12])', r_sub_put), ('DELETE', r'/api/cases/(\d+)/subs/([12])', r_sub_del),
    ('GET', r'/api/cases/(\d+)/funding-options', r_funding_options),
    ('POST', r'/api/cases/(\d+)/subs/(\d+)/custody', funding_ledger.create_hold),
    ('POST', r'/api/custody/(\d+)/refund', funding_ledger.refund),
    ('POST', r'/api/funding-events/(\d+)/cancel', funding_ledger.cancel),
    ('POST', r'/api/cash-plans/(\d+)/action', funding_ledger.cash_plan_action),
    ('PUT', r'/api/cases/(\d+)/subs/([12])/principal-release', r_principal_release),
    ('PUT', r'/api/cases/(\d+)/subs/([12])/principal-account', r_principal_account),
    ('PUT', r'/api/cases/(\d+)/haak', r_haak_put),
    ('POST', r'/api/purchase-read', r_purchase_read),
    ('POST', r'/api/cases/(\d+)/purchase-read', r_purchase_read),
    ('POST', r'/api/cases/(\d+)/purchase-import', r_purchase_import),
    ('POST', r'/api/cases/(\d+)/attachments', r_attach), ('GET', r'/api/cases/(\d+)/attachments', r_attachments),
    ('GET', r'/api/attachments/(\d+)/file', r_attachment_download),
    ('GET', r'/api/attachments/(\d+)/preview', r_attachment_preview),
    ('PUT', r'/api/attachments/(\d+)', r_attachment_replace),
    ('POST', r'/api/cases/(\d+)/document-preview', r_doc_preview),
    ('GET', r'/api/document-previews/([a-zA-Z0-9_-]+)/preview', r_doc_preview_file),
    ('POST', r'/api/cases/(\d+)/documents', r_doc_new), ('GET', r'/api/cases/(\d+)/documents', r_docs),
    ('PUT', r'/api/documents/(\d+)', r_doc_put), ('GET', r'/api/documents/(\d+)/file', r_doc_download),
    ('POST', r'/api/documents/(\d+)/edit-preview', r_doc_edit_preview),
    ('GET', r'/api/documents/(\d+)/history', r_doc_history), ('GET', r'/api/document-history/(\d+)/file', r_doc_old_download),
    ('GET', r'/api/documents/(\d+)/preview', r_doc_issued_preview),
    ('GET', r'/api/document-history/(\d+)/preview', r_doc_history_preview),
    ('PUT', r'/api/documents/(\d+)/sent', r_doc_sent),
    ('GET', r'/api/backup/full', r_backup_full),
    ('GET', r'/api/sub-mode-migrations', r_sub_mode_migrations),
    ('GET', r'/api/master', r_master), ('PUT', r'/api/company', r_company_put),
    ('POST', r'/api/company/seal/restore', r_company_seal_restore),
    ('POST', r'/api/company/seal', r_company_seal_upload),
    ('POST', r'/api/subcontractors', r_subm_new), ('PUT', r'/api/subcontractors/(\d+)', r_subm_put),
    ('POST', r'/api/suppliers', r_supplier_new), ('POST', r'/api/password', r_password), ('GET', r'/api/backup', r_backup),
]


class H(BaseHTTPRequestHandler):
    loopback = True
    port = 0

    def log_message(self, *a):
        pass

    def out(self, code, body, ctype='application/json; charset=utf-8', extra=(), *, preview=False):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'SAMEORIGIN' if preview else 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        policy = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        if preview:
            policy = "default-src 'none'; frame-ancestors 'self'"
        self.send_header('Content-Security-Policy', policy)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def js(self, obj, code=200, extra=()):
        self.out(code, json.dumps(obj, ensure_ascii=False).encode(), extra=extra)

    def sid(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'sid':
                expiry, credential = SESS.get(v, (0, None))
                if expiry > time.time():
                    with closing(db()) as connection:
                        current = getset(connection, 'pw')
                    if current and secrets.compare_digest(credential, hashlib.sha256(current.encode()).hexdigest()):
                        return v
                SESS.pop(v, None)
        return None

    def handle_any(self, method):
        u = urlparse(self.path)
        try:
            if self.loopback and (self.headers.get('Host') or '') not in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
                raise Err('許可されていないアクセスです', 403)
            if method == 'GET' and u.path == '/api/runtime':
                return self.js(RUNTIME)
            if method == 'GET' and u.path in ('/', '/workflow.js', '/funding.js', '/static/logibox.svg'):
                name, mime = {'/': ('index.html', 'text/html'), '/workflow.js': ('workflow.js', 'text/javascript'),
                              '/funding.js': ('funding.js', 'text/javascript'),
                              '/static/logibox.svg': ('logibox.svg', 'image/svg+xml')}[u.path]
                with open(os.path.join(BASE, 'static', name), 'rb') as page:
                    content = page.read()
                    if name == 'index.html':
                        content = content.replace(b'__APP_VERSION__', DISPLAY_VERSION.encode())
                        content = content.replace(b'__RELEASE_VERSION__', VERSION.encode())
                        content = content.replace(b'__APP_BUILD__', RUNTIME['build'].encode())
                    return self.out(200, content, mime + '; charset=utf-8')
            if not u.path.startswith('/api/'):
                raise Err('見つかりません', 404)
            body = {}
            if method != 'GET':
                if self.headers.get('X-Requested-With') != 'app':
                    raise Err('不正なリクエストです', 403)
                n = int(self.headers.get('Content-Length') or 0)
                upload = re.fullmatch(r'/api/(?:purchase-read|cases/\d+/(?:attachments|purchase-read|purchase-import)|attachments/\d+|settlements/\d+/invoice)', u.path)
                limit = 12_000_000 if upload else 1_000_000
                if n < 0 or n > limit:
                    raise Err('データが大きすぎます')
                raw = self.rfile.read(n) if n else b''
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise Err('リクエストの形式が正しくありません')
            c = db()
            try:
                if u.path == '/api/state' and method == 'GET':
                    return self.js(dict(setup=bool(getset(c, 'pw')), logged=bool(self.sid())))
                if u.path == '/api/setup' and method == 'POST':
                    if getset(c, 'pw'):
                        raise Err('すでに設定済みです', 403)
                    pw = str(body.get('password') or '')
                    if len(pw) < 8:
                        raise Err('パスワードは8文字以上にしてください')
                    stored = hash_pw(pw)
                    setset(c, 'pw', stored)
                    c.commit()
                    return self.login_ok(stored)
                if u.path == '/api/login' and method == 'POST':
                    stored = getset(c, 'pw')
                    if not stored or not check_pw(str(body.get('password') or ''), stored):
                        time.sleep(1)
                        raise Err('パスワードが違います', 401)
                    return self.login_ok(stored)
                if u.path == '/api/logout' and method == 'POST':
                    SESS.pop(self.sid(), None)
                    return self.js(dict(ok=True), extra=[('Set-Cookie', 'sid=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')])
                session = self.sid()
                if not session:
                    raise Err('ログインしてください', 401)
                for m, pat, fn in ROUTES:
                    mt = re.fullmatch(pat, u.path)
                    if m == method and mt:
                        context = {'session': session} if fn in (r_doc_new, r_doc_preview, r_doc_preview_file, r_doc_edit_preview, r_doc_put) else {}
                        res = fn(c, body, parse_qs(u.query), *mt.groups(), **context)
                        c.commit()
                        if method in ('POST', 'PUT', 'DELETE'):
                            sync_pdf_archive(c)
                        if isinstance(res, tuple) and res[0] in ('file', 'inline'):
                            mime = res[3] if len(res) > 3 else 'application/octet-stream'
                            preview = res[0] == 'inline'
                            kind = 'inline' if preview else 'attachment'
                            return self.out(200, res[1], mime, [('Content-Disposition', f'{kind}; filename="{res[2]}"')],
                                            preview=preview)
                        return self.js(res)
                raise Err('見つかりません', 404)
            except sqlite3.IntegrityError:
                c.rollback()
                raise Err('保存できませんでした(登録内容を確認してください)')
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()
        except Err as e:
            self.js(dict(error=e.msg), e.code)
        except (ValueError, json.JSONDecodeError):
            self.js(dict(error='入力内容が正しくありません'), 400)
        except Exception:
            self.js(dict(error='アプリ内でエラーが起きました'), 500)

    def login_ok(self, stored):
        t = secrets.token_urlsafe(32)
        SESS[t] = (time.time() + 12 * 3600, hashlib.sha256(stored.encode()).hexdigest())
        self.js(dict(ok=True), extra=[('Set-Cookie', f'sid={t}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200')])

    def do_GET(self): self.handle_any('GET')
    def do_POST(self): self.handle_any('POST')
    def do_PUT(self): self.handle_any('PUT')
    def do_DELETE(self): self.handle_any('DELETE')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    if os.path.isfile(os.path.join(BASE, 'UPDATE_ONLY.txt')) and not os.path.isfile(DBP):
        raise RuntimeError('これは更新専用パッケージです。update.batで利用中フォルダを更新してください。新しいDBは作成しません。')
    with data_lock(DATA):
        run_server(a)


def run_server(a):
    H.loopback = a.host in ('127.0.0.1', 'localhost')
    H.port = a.port
    srv = ThreadingHTTPServer((a.host, a.port), H)
    try:
        init()
    except Exception:
        srv.server_close()
        raise
    url = f'http://127.0.0.1:{a.port}/?v={VERSION}&build={RUNTIME["build"]}'
    print('起動しました:', url, '(終了するときはこの画面を閉じます)')
    if not a.no_browser and H.loopback:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == '__main__':
    main()
