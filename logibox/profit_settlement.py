"""案件ごとの都度精算。利益と元金は成分別に管理し、一括で予定・支払を記録する。"""
import datetime
import json
import hashlib
import secrets
from pathlib import Path
import calc
import case_summary
import settlement_principal
from principal_funding import runtime

METHODS = {'normal':'通常精算', 'full':'都度精算'}
STATES = ('未精算','精算予定','一部精算済','精算完了')


def init(c):
    c.execute('''CREATE TABLE IF NOT EXISTS profit_preferences (
        case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
        sub_id INTEGER NOT NULL REFERENCES subcontractors(id) ON DELETE RESTRICT,
        method TEXT NOT NULL DEFAULT 'normal', PRIMARY KEY(case_id,sub_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS profit_payments (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
        sub_id INTEGER NOT NULL REFERENCES subcontractors(id) ON DELETE RESTRICT,
        method TEXT NOT NULL CHECK(method IN ('full','partial')), amount INTEGER NOT NULL CHECK(amount>0),
        planned_date TEXT, paid_date TEXT, payment_method TEXT NOT NULL,
        note TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 1, request_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS profit_monthly_payments (
        line_id INTEGER PRIMARY KEY REFERENCES settlement_lines(id) ON DELETE RESTRICT,
        case_id INTEGER NOT NULL, sub_id INTEGER NOT NULL, amount INTEGER NOT NULL,
        paid_date TEXT NOT NULL, deducted INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS profit_history (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, sub_id INTEGER NOT NULL,
        payment_id INTEGER, changed_at TEXT NOT NULL, reason TEXT NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS profit_documents (
        id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES profit_payments(id),
        revision INTEGER NOT NULL, number TEXT NOT NULL UNIQUE, issued_date TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, pdf BLOB NOT NULL, UNIQUE(payment_id,revision))''')
    columns={r[1] for r in c.execute('PRAGMA table_info(profit_payments)')}
    if 'principal_amount' not in columns:
        c.execute('ALTER TABLE profit_payments ADD COLUMN principal_amount INTEGER NOT NULL DEFAULT 0 CHECK(principal_amount>=0 AND principal_amount<=amount)')
    if 'principal_balance_after' not in columns:
        c.execute('ALTER TABLE profit_payments ADD COLUMN principal_balance_after INTEGER CHECK(principal_balance_after>=0)')
    columns={r[1] for r in c.execute('PRAGMA table_info(profit_monthly_payments)')}
    for definition in ('principal_amount INTEGER', 'principal_deducted INTEGER', 'principal_reserved INTEGER'):
        if definition.split()[0] not in columns:
            c.execute('ALTER TABLE profit_monthly_payments ADD COLUMN '+definition)
    for table in ('profit_history','profit_documents','profit_monthly_payments'):
        for action in ('UPDATE','DELETE'):
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable profit history'); END''')
    c.execute('''INSERT OR IGNORE INTO profit_monthly_payments(line_id,case_id,sub_id,amount,paid_date)
        SELECT l.id,l.case_id,s.sub_id,l.sub_profit,COALESCE(s.paid_at,s.pay_date)
        FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id WHERE s.status='paid' ''')


def audit(c,cid,sid,pid,before,after,reason):
    c.execute('INSERT INTO profit_history(case_id,sub_id,payment_id,changed_at,reason,before_json,after_json) VALUES(?,?,?,?,?,?,?)',
        (cid,sid,pid,datetime.datetime.now(datetime.timezone.utc).isoformat(),reason,
         json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False)))


def profit_amount(payment):
    return payment['amount']-payment.get('principal_amount',0) if isinstance(payment,dict) else payment['amount']-payment['principal_amount']


def summary(c,cid,sid,exclude=None):
    Err,default_settle=runtime().Err,runtime().default_settle
    row=c.execute('''SELECT cs.*,a.case_no,a.name AS case_name,a.planned_rate_pct,a.due_date,
        a.deleted_at,sc.name AS sub_name FROM case_subs cs JOIN cases a ON a.id=cs.case_id
        JOIN subcontractors sc ON sc.id=cs.sub_id WHERE cs.case_id=? AND cs.sub_id=?''',(cid,sid)).fetchone()
    if not row: raise Err('案件と下請けの組合せが見つかりません',404)
    d=dict(row)
    h=c.execute('SELECT * FROM haak_billings WHERE case_id=?',(cid,)).fetchone()
    d['confirmed']=bool(h and h['confirmed_checked'] and h['confirmed_amount_tax_in'] is not None)
    d['haak_received']=bool(h and case_summary.haak_paid(dict(h)))
    d['profit']=calc.sub_profit(d['principal_tax_in'],d['planned_rate_pct'],d['fee_rate_pct']) if d['confirmed'] else 0
    d['contract_rate']=str(__import__('decimal').Decimal(str(d['planned_rate_pct']))-__import__('decimal').Decimal(str(d['fee_rate_pct'])))
    payments=[dict(x) for x in c.execute('SELECT * FROM profit_payments WHERE case_id=? AND sub_id=? ORDER BY id',(cid,sid))]
    active=[x for x in payments if not x['cancelled'] and x['id']!=exclude]
    d['full_paid']=sum(profit_amount(x) for x in active if x['paid_date'] and x['method']=='full')
    d['partial_paid']=sum(profit_amount(x) for x in active if x['paid_date'] and x['method']=='partial')
    d['individual_paid']=d['full_paid']+d['partial_paid']
    d['monthly_paid']=c.execute('SELECT COALESCE(SUM(amount),0) FROM profit_monthly_payments WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()[0]
    frozen=c.execute('''SELECT l.sub_profit,s.status FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id WHERE l.case_id=? AND s.sub_id=?''',(cid,sid)).fetchone()
    if frozen:
        d['profit']=frozen['sub_profit']
        if frozen['status']=='paid': d['confirmed']=True
    d['paid']=d['individual_paid']+d['monthly_paid']
    d['planned']=sum(profit_amount(x) for x in active if not x['paid_date'])
    d['remaining']=d['profit']-d['paid']
    d['available']=max(0,d['remaining']-d['planned'])
    monthly=c.execute('''SELECT s.status,s.pay_date FROM settlements s JOIN settlement_lines l ON l.settlement_id=s.id
        WHERE l.case_id=? AND s.sub_id=?''',(cid,sid)).fetchone()
    d['monthly_due']=monthly['pay_date'] if monthly else default_settle(d['due_date'])
    d['monthly_scheduled']=d['available'] if not monthly or monthly['status']!='paid' else 0
    d['status']='精算完了' if d['confirmed'] and d['remaining']==0 else '一部精算済' if d['paid'] else '精算予定' if d['planned'] else '未精算'
    pref=c.execute('SELECT method FROM profit_preferences WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    d['method']=pref[0] if pref else 'normal'
    own=settlement_principal.totals(c,cid,sid,exclude)
    import principal_balance
    balance=principal_balance.values(c,row,exclude_payment=exclude)
    d['principal_paid']=own['paid']
    d['principal_planned']=own['planned']
    d['principal_balance']=balance['balance']
    d['principal_available']=settlement_principal.available(c,row,exclude)
    d['principal_manual_refunded']=balance['account']['refunded']
    for payment in payments:
        payment['profit_amount']=profit_amount(payment)
    d['payments']=payments
    d['monthly_history']=[dict(x) for x in c.execute('SELECT * FROM profit_monthly_payments WHERE case_id=? AND sub_id=?',(cid,sid))]
    d['history']=[dict(x) for x in c.execute('SELECT * FROM profit_history WHERE case_id=? AND sub_id=? ORDER BY id DESC',(cid,sid))]
    d['documents']=[dict(x) for x in c.execute('''SELECT d.id,d.payment_id,d.revision,d.number,d.issued_date FROM profit_documents d
        JOIN profit_payments p ON p.id=d.payment_id WHERE p.case_id=? AND p.sub_id=? ORDER BY d.id DESC''',(cid,sid))]
    return d


def r_get(c,b,q,cid,sid): return summary(c,cid,sid)


def r_list(c,b,q):
    rows=[summary(c,x['case_id'],x['sub_id']) for x in c.execute('''SELECT cs.case_id,cs.sub_id FROM case_subs cs
        JOIN cases a ON a.id=cs.case_id WHERE a.deleted_at IS NULL ORDER BY cs.sub_id,a.due_date''')]
    groups=[]
    for sub in c.execute('SELECT id,name FROM subcontractors ORDER BY id'):
        items=[x for x in rows if x['sub_id']==sub['id']]
        groups.append(dict(id=sub['id'],name=sub['name'],profit=sum(x['profit'] for x in items),
            full_paid=sum(x['full_paid'] for x in items),remaining=sum(x['remaining'] for x in items),
            monthly_scheduled=sum(x['monthly_scheduled'] for x in items),
            principal_balance=sum(x['principal_balance'] for x in items),
            principal_paid=sum(x['principal_paid'] for x in items)))
    return dict(rows=rows,subcontractors=groups)


def r_save(c,b,q,cid,sid):
    Err,dt=runtime().Err,runtime().dt
    c.execute('BEGIN IMMEDIATE')
    d=summary(c,cid,sid)
    if d['deleted_at']: raise Err('削除ボックスの案件は編集できません')
    method=b.get('method','normal')
    if method not in METHODS: raise Err('精算方法を選択してください')
    pid=b.get('id')
    old=None
    if pid:
        old=c.execute('SELECT * FROM profit_payments WHERE id=? AND case_id=? AND sub_id=?',(pid,cid,sid)).fetchone()
        if not old: raise Err('精算履歴が見つかりません',404)
        old=dict(old)
        if b.get('revision')!=old['revision']: raise Err('履歴が更新されています。開き直してください',409)
        if old['cancelled']: raise Err('取消済みの記録は編集できません')
        if not str(b.get('reason') or '').strip(): raise Err('修正・取消理由を入力してください')
    if b.get('cancel'):
        if not old: raise Err('取消対象を選択してください')
        c.execute('UPDATE profit_payments SET cancelled=1,revision=revision+1 WHERE id=?',(pid,))
        audit(c,cid,sid,pid,old,dict(old,cancelled=1,revision=old['revision']+1),b['reason'])
        return summary(c,cid,sid)
    if method=='normal':
        if old: raise Err('既存の個別支払は取消または訂正してください')
        before=d['method']
        c.execute('INSERT INTO profit_preferences VALUES(?,?,?) ON CONFLICT(case_id,sub_id) DO UPDATE SET method=excluded.method',(cid,sid,method))
        audit(c,cid,sid,None,{'method':before},{'method':method},'精算方法変更')
        return summary(c,cid,sid)
    key=str(b.get('request_key') or '')
    if not old:
        if not 8<=len(key)<=150: raise Err('再送防止キーがありません。開き直してください')
        duplicate=c.execute('SELECT * FROM profit_payments WHERE request_key=?',(key,)).fetchone()
        if duplicate:
            fields=('method','planned_date','paid_date','payment_method','note')
            if (b.get('principal_amount') is not None and b['principal_amount']!=duplicate['principal_amount']) or profit_amount(duplicate)!=b.get('amount') or bool(duplicate['principal_amount'])!=bool(b.get('include_principal',False)) or str(duplicate['case_id'])!=str(cid) or str(duplicate['sub_id'])!=str(sid) or any(str(duplicate[k] or '')!=str(b.get(k) or '') for k in fields):
                raise Err('同じ処理キーで内容を変更できません',409)
            return summary(c,cid,sid)
    if not d['haak_received']: raise Err('ハークからの実入金確認完了後に登録できます')
    amount=b.get('amount')
    if isinstance(amount,bool) or not isinstance(amount,int) or amount<0: raise Err('利益精算額は0円以上の整数を指定してください')
    if not isinstance(b.get('include_principal',False),bool): raise Err('元金返還の選択を確認してください')
    capacity=summary(c,cid,sid,exclude=pid)
    if amount>capacity['available']: raise Err('確定利益の未予約残額を超える精算はできません')
    if method=='full' and not (old and amount==profit_amount(old)) and (amount!=capacity['remaining'] or capacity['planned']): raise Err('都度精算は未予約残額の全額を指定してください')
    principal=(old['principal_amount'] if old and old['principal_amount'] else capacity['principal_available']) if b.get('include_principal') else 0
    if principal>capacity['principal_available']: raise Err('返還可能元金が変更されています。画面を開き直してください',409)
    expected=b.get('principal_amount')
    if expected is not None and (isinstance(expected,bool) or not isinstance(expected,int) or expected!=principal):
        raise Err('元金返還額が変更されています。画面を開き直して金額を確認してください',409)
    if b.get('include_principal') and principal<=0:
        raise Err('対象案件に返還可能な元金がありません。充当済み・預かり金・予約済み・回収状況を確認してください',409)
    if amount+principal<=0: raise Err('未精算利益または返還可能元金がありません',409)
    planned=dt(b,'planned_date','精算予定日'); paid=dt(b,'paid_date','実際の支払日')
    if not planned and not paid: raise Err('精算予定日または実際の支払日を入力してください')
    if paid and paid>datetime.date.today().isoformat(): raise Err('実際の支払日は今日以前を指定してください')
    payment_method=b.get('payment_method')
    if payment_method not in ('bank','cash'): raise Err('支払方法を選択してください')
    note=str(b.get('note') or '').strip()
    if not note or len(note)>2000: raise Err('理由・備考を2000文字以内で入力してください')
    # Same dated amount must be amended through the editor, never silently paid twice.
    if c.execute('''SELECT 1 FROM profit_payments WHERE case_id=? AND sub_id=? AND cancelled=0
        AND amount=? AND COALESCE(paid_date,'')=? AND COALESCE(planned_date,'')=? AND id<>?''',
        (cid,sid,amount+principal,paid or '',planned or '',pid or 0)).fetchone(): raise Err('同じ日付・金額の精算が登録済みです。履歴を確認してください',409)
    values=(method,amount+principal,planned,paid,payment_method,note,principal)
    if old:
        c.execute('UPDATE profit_payments SET method=?,amount=?,planned_date=?,paid_date=?,payment_method=?,note=?,principal_amount=?,revision=revision+1 WHERE id=?',values+(pid,))
    else:
        pid=c.execute('''INSERT INTO profit_payments(case_id,sub_id,method,amount,planned_date,paid_date,payment_method,note,principal_amount,request_key,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(cid,sid)+values+(key,datetime.datetime.now(datetime.timezone.utc).isoformat())).lastrowid
    c.execute('INSERT INTO profit_preferences VALUES(?,?,?) ON CONFLICT(case_id,sub_id) DO UPDATE SET method=excluded.method',(cid,sid,method))
    import principal_balance
    principal_balance.validate_case(c,int(cid))
    closing_balance=summary(c,cid,sid)['principal_balance'] if paid else None
    if old and old['paid_date'] and paid and old['principal_amount']==principal and old['amount']==amount+principal:
        closing_balance=old['principal_balance_after']
    c.execute('UPDATE profit_payments SET principal_balance_after=? WHERE id=?',(closing_balance,pid))
    after=dict(c.execute('SELECT * FROM profit_payments WHERE id=?',(pid,)).fetchone())
    audit(c,cid,sid,pid,old,after,str(b.get('reason') or '利益精算登録'))
    return summary(c,cid,sid)


def record_monthly(c,view,paid):
    for line in view['lines']:
        d=summary(c,line['case_id'],view['sub_id'])
        c.execute('''INSERT INTO profit_monthly_payments(line_id,case_id,sub_id,amount,paid_date,deducted,reserved,principal_amount,principal_deducted,principal_reserved)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(line['id'],line['case_id'],view['sub_id'],max(0,line['sub_profit']-d['individual_paid']-d['planned']),paid,d['individual_paid'],d['planned'],line['principal_payable'],line['refunded'],d['principal_planned']))


def r_document(c,b,q,pid):
    Err,getset=runtime().Err,runtime().getset
    import profit_settlement_documents
    issue=b.get('issue') is True
    if issue: c.execute('BEGIN IMMEDIATE')
    p=c.execute('SELECT * FROM profit_payments WHERE id=?',(pid,)).fetchone()
    if not p or p['cancelled'] or not p['paid_date']: raise Err('支払完了した有効な利益精算を選択してください')
    p=dict(p)
    p['profit_amount']=profit_amount(p)
    d=summary(c,p['case_id'],p['sub_id'])
    number=f"PS-{int(pid):06d}-R{p['revision']:02d}"
    # Prior amount is held internally but excluded from the PDF layout.
    previous=sum(profit_amount(x) for x in d['payments'] if not x['cancelled'] and x['paid_date'] and (x['paid_date'],x['id'])<(p['paid_date'],p['id']))
    previous+=sum(x['amount'] for x in d['monthly_history'] if x['paid_date']<=p['paid_date'])
    snapshot=dict(d,principal_balance_after=p['principal_balance_after'] if p['principal_balance_after'] is not None else d['principal_balance'],payment=p,previous_paid=previous,remaining_after=d['profit']-previous-profit_amount(p),
        number=number,issued_date=datetime.date.today().isoformat(),company=dict(c.execute('SELECT * FROM company WHERE id=1').fetchone()),seal=getset(c,'company_seal_png'))
    # Preview and issue use the same deterministic layout, issuance freezes the snapshot.
    stored=c.execute('SELECT pdf FROM profit_documents WHERE payment_id=? AND revision=?',(pid,p['revision'])).fetchone()
    raw=bytes(stored['pdf']) if stored else profit_settlement_documents.render(snapshot)
    if issue and not stored:
        c.execute('INSERT INTO profit_documents(payment_id,revision,number,issued_date,snapshot_json,pdf) VALUES(?,?,?,?,?,?)',
            (pid,p['revision'],number,snapshot['issued_date'],json.dumps(snapshot,ensure_ascii=False),raw))
    return 'file',raw,number+'.pdf','application/pdf'


def r_document_get(c,b,q,did):
    Err=runtime().Err
    row=c.execute('SELECT * FROM profit_documents WHERE id=?',(did,)).fetchone()
    if not row: raise Err('発行済み精算書が見つかりません',404)
    return 'file',bytes(row['pdf']),row['number']+'.pdf','application/pdf'


def r_monthly_revision(c,b,q,settlement_id):
    Err,settlement_view=runtime().Err,runtime().settlement_view
    c.execute('BEGIN IMMEDIATE')
    before=settlement_view(c,settlement_id)
    if before['status'] not in ('requested','received','mismatch'): raise Err('依頼済み・未払いの精算だけ訂正版を登録できます',409)
    reason=str(b.get('reason') or '').strip()
    if not reason: raise Err('訂正理由を入力してください')
    c.execute('UPDATE settlements SET request_snapshot_json=NULL WHERE id=?',(settlement_id,))
    # Manual request text was based on the old amount; retain it in audit and use the new generated text.
    c.execute('DELETE FROM settings WHERE k=?',('settlement_request_text:'+str(settlement_id),))
    current=settlement_view(c,settlement_id)
    audit(c,0,before['sub_id'],None,before['request_document'],current['request_document'],reason)
    c.execute('UPDATE settlements SET request_snapshot_json=? WHERE id=?',(json.dumps(current['request_document'],ensure_ascii=False),settlement_id))
    return settlement_view(c,settlement_id)
