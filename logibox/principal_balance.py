"""案件・下請け単位の回収元金と未返還元金。利益と現金入金を混同しない。"""
import datetime
import json
import sqlite3
import funding_ledger


def account(c, cid, sid):
    row = c.execute('SELECT * FROM principal_accounts WHERE case_id=? AND sub_id=?', (cid, sid)).fetchone()
    return dict(row) if row else dict(recovered=None, refunded=0, revision=0)


def haak_cash(c, cid):
    h = c.execute('SELECT * FROM haak_billings WHERE case_id=?', (cid,)).fetchone()
    today = datetime.date.today().isoformat()
    if not h or not h['paid_date'] or h['paid_date'] > today or not h['confirmed_checked']:
        return 0, False
    billed = h['confirmed_amount_tax_in'] or 0
    paid = h['actual_paid_amount'] if h['actual_paid_amount'] is not None else billed
    return max(0, paid), billed > 0 and paid >= billed


def values(c, sub, allow_monthly=False, exclude_payment=None):
    import principal_funding
    s = principal_funding.runtime()
    cid, sid = sub['case_id'], sub['sub_id']
    a = account(c, cid, sid)
    cash, full = haak_cash(c, cid)
    funded = s.principal_confirmed(c, sub)
    if full and sum(s.principal_confirmed(c,x) for x in c.execute('SELECT * FROM case_subs WHERE case_id=?',(cid,))) > cash:
        full = False  # 回収不足の既存案件は自動配賦せず、下請け別の確認を必要とする
    recovered = min(funded, (a['recovered'] if a['recovered'] is not None else funded if full else 0)) if cash else 0
    recovered = min(recovered, cash)
    line = principal_funding.source_line(c, cid, sid)
    extra = funding_ledger.outgoing(c,cid,sid)
    managed = 0
    if line and c.execute('SELECT 1 FROM custody_funds WHERE line_id=?',(line['id'],)).fetchone():
        managed = line['carry_over']
    transferred = (line['carry_over'] if line and line['carry_to_case_id'] else 0) + extra
    held = line['carry_over'] if line and not line['carry_to_case_id'] else 0
    import settlement_principal
    individual=settlement_principal.totals(c,cid,sid,exclude_payment)
    refunded = a['refunded'] + individual['paid']
    # 精算支払済みの元金返還分。既に個別返金した分を二重に控除しない。
    final_refund = max(0, line['principal'] - line['carry_over'] - refunded - extra) if line and line['status']=='paid' else 0
    if line and line['status']=='paid':
        columns={r[1] for r in c.execute('PRAGMA table_info(profit_monthly_payments)')}
        if 'principal_amount' in columns:
            frozen=c.execute('SELECT principal_amount FROM profit_monthly_payments WHERE line_id=?',(line['id'],)).fetchone()
            if frozen and frozen['principal_amount'] is not None: final_refund=frozen['principal_amount']
    explicit_cash = funding_ledger.cash(c,cid,sid)
    cash_owned = explicit_cash if explicit_cash else (
        max(0,sub['principal_tax_in']-s.incoming_carry(c,cid,sid))
        if sub['principal_received_date'] and sub['principal_received_date']<=datetime.date.today().isoformat() else 0)
    owned = min(sub['principal_tax_in'],cash_owned+s.incoming_carry(c,cid,sid))
    balance = max(0, owned - transferred - refunded - final_refund - managed)
    available = max(0, min(balance, recovered - transferred - refunded - final_refund - managed))
    available=max(0,available-individual['planned'])
    if not allow_monthly and line and line['status'] not in ('not_requested','paid'):
        available = 0  # 請求依頼中の返還予定元金を再使用しない
    return dict(balance=balance, available=available, recovered=recovered, refunded=refunded,
                transferred=transferred, held=held, account=a, individual_refunded=individual['paid'], principal_reserved=individual['planned'])


def validate_case(c, cid):
    import principal_funding
    s = principal_funding.runtime()
    cash, full = haak_cash(c,cid)
    total = 0
    for sub in c.execute('SELECT * FROM case_subs WHERE case_id=?',(cid,)).fetchall():
        v = values(c, sub)
        a = v['account']
        raw = a['recovered'] if a['recovered'] is not None else v['recovered']
        total += raw
        line = principal_funding.source_line(c,cid,sub['sub_id'])
        reserved = (line['carry_over'] if line else 0) + funding_ledger.outgoing(c,cid,sub['sub_id']) + v['principal_reserved']
        if raw > s.principal_confirmed(c,sub) or raw > cash or v['refunded'] + reserved > v['recovered']:
            raise s.Err('回収確認済み元金が不足します。既存の充当・返金を先に確認してください',409)
    if total > cash:
        raise s.Err('下請け別の回収元金合計がハークの実入金額を超えています',409)


def save(c, b, cid, slot):
    import principal_funding
    s = principal_funding.runtime()
    c.execute('BEGIN IMMEDIATE')
    s.case_view(c,int(cid),True)
    sub = c.execute('SELECT * FROM case_subs WHERE case_id=? AND slot=?',(cid,slot)).fetchone()
    if not sub:
        raise s.Err('先に下請けを保存してください')
    old = account(c,cid,sub['sub_id'])
    if s.yen(b,'expected_revision','版番号',True) != old['revision']:
        raise s.Err('残高が更新されています。画面を開き直してください',409)
    line = principal_funding.source_line(c,cid,sub['sub_id'])
    if line and line['status']!='not_requested':
        raise s.Err('依頼済み・支払済みの元金は変更できません',409)
    recovered = s.yen(b,'recovered','回収確認済み元金累計',True)
    refunded = s.yen(b,'refunded','個別返金済み元金累計',True)
    date = s.dt(b,'record_date','確認・返金日')
    note = s.txt(b,'note','確認記録・変更理由',True,500)
    if not date or date > datetime.date.today().isoformat():
        raise s.Err('確認・返金日を確認してください')
    try:
        backup = s.carry_management.backup_database(c)
    except (OSError,ValueError,sqlite3.Error) as exc:
        raise s.Err('変更前バックアップに失敗したため回収・返金を変更していません',409) from exc
    c.execute('''INSERT INTO principal_accounts(case_id,sub_id,recovered,refunded,record_date,note,revision)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(case_id,sub_id) DO UPDATE SET recovered=excluded.recovered,
        refunded=excluded.refunded,record_date=excluded.record_date,note=excluded.note,revision=excluded.revision''',
        (cid,sub['sub_id'],recovered,refunded,date,note,old['revision']+1))
    validate_case(c,cid)
    after = account(c,cid,sub['sub_id'])
    c.execute('INSERT INTO principal_account_history(case_id,sub_id,changed_at,before_json,after_json,backup_file) VALUES(?,?,?,?,?,?)',
        (cid,sub['sub_id'],datetime.datetime.now(datetime.timezone.utc).isoformat(),json.dumps(old,ensure_ascii=False),json.dumps(after,ensure_ascii=False),backup))
    return dict(ok=True)
