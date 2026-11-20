"""元金充当の資金確認・変更前バックアップ・履歴用データ・削除阻害理由。"""
from contextlib import closing
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import funding_ledger


def available_principal(c, sub, settlement_id, cutoff, visiting=None):
    """確定資金と、同じ月次精算内の時系列に沿った連続充当だけを認める。"""
    visiting = set() if visiting is None else set(visiting)
    key = (sub['case_id'], sub['sub_id'])
    if key in visiting:
        raise ValueError('循環する元金充当があります。充当元と充当先を確認してください')
    visiting.add(key)
    incoming = c.execute('''SELECT l.*,s.status,s.paid_at,s.sub_id FROM settlement_lines l
                            JOIN settlements s ON s.id=l.settlement_id
                            WHERE l.carry_to_case_id=? AND s.sub_id=? AND l.carry_over>0''', key).fetchall()
    extra = funding_ledger.incoming(c,*key,cutoff)
    reserved = sum(row['carry_over'] for row in incoming) + funding_ledger.incoming(c,*key)
    cash = max(0, sub['principal_tax_in'] - reserved) if (
        sub['principal_received_date'] and sub['principal_received_date'] <= cutoff) else 0
    explicit_cash = funding_ledger.cash(c,*key,cutoff)
    if funding_ledger.cash(c,*key):
        cash = explicit_cash
    credited = extra
    for line in incoming:
        if line['status'] == 'paid' and line['paid_at'] and line['paid_at'] <= cutoff:
            credited += line['carry_over']
        elif line['settlement_id'] == int(settlement_id):
            source = c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',
                               (line['case_id'], sub['sub_id'])).fetchone()
            if source and available_principal(c, source, settlement_id, cutoff, visiting) >= line['principal']:
                credited += line['carry_over']
    return min(sub['principal_tax_in'], cash + credited)


def snapshot(c, settlement_id, line_id):
    row = c.execute('''SELECT l.*,s.sub_id,s.period,s.status,s.paid_at,sc.name AS sub_name,
                       a.mgmt_no AS source_no,a.name AS source_name,a.case_no AS source_case_no,
                       t.mgmt_no AS target_no,t.name AS target_name,t.case_no AS target_case_no,
                       cs.principal_received_date FROM settlement_lines l
                       JOIN settlements s ON s.id=l.settlement_id JOIN subcontractors sc ON sc.id=s.sub_id
                       JOIN cases a ON a.id=l.case_id LEFT JOIN cases t ON t.id=l.carry_to_case_id
                       LEFT JOIN case_subs cs ON cs.case_id=l.case_id AND cs.sub_id=s.sub_id
                       WHERE l.id=? AND l.settlement_id=?''', (line_id, settlement_id)).fetchone()
    return dict(row) if row else None


def backup_database(c):
    """呼出側のBEGIN IMMEDIATE中、変更を加える前に別の読取接続から複製する。"""
    path = next((row[2] for row in c.execute('PRAGMA database_list') if row[1] == 'main'), None)
    if not path:
        raise ValueError('バックアップできる保存済みDBが必要です')
    source_path = Path(path).resolve()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target_path = source_path.with_name(f'before-carry-edit-{stamp}-{secrets.token_hex(4)}.db')
    temporary = target_path.with_suffix('.tmp')
    try:
        with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True, timeout=5)) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('充当変更前のバックアップを検証できませんでした')
        os.replace(temporary, target_path)
        return target_path.name
    finally:
        if temporary.exists():
            temporary.unlink()


def change_history(c, line_id):
    result = []
    line = c.execute('SELECT carry_uid FROM settlement_lines WHERE id=?', (line_id,)).fetchone()
    if not line:
        return result
    for row in c.execute('SELECT * FROM carry_change_history WHERE line_uid=? ORDER BY id DESC', (line['carry_uid'],)):
        item = dict(row)
        item['before'] = json.loads(item.pop('before_json'))
        item['after'] = json.loads(item.pop('after_json'))
        result.append(item)
    return result


def deletion_reasons(c, cid, cancelling=False):
    reasons = []
    if funding_ledger.history(c,'e.source_case_id=? OR e.target_id=?',(cid,cid)) or c.execute('SELECT 1 FROM principal_cash_entries WHERE case_id=?',(cid,)).fetchone():
        reasons.append('元金充当・預かり金・入金台帳の履歴があります')
    if c.execute('SELECT 1 FROM principal_accounts WHERE case_id=?',(cid,)).fetchone():
        reasons.append('元金の回収・返金記録があります')
    for row in c.execute('''SELECT l.id,l.case_id,l.carry_to_case_id,l.carry_over,s.period,s.status,sc.name,
                            a.case_no AS source_no,t.case_no AS target_no,
                            a.name AS source_name,t.name AS target_name FROM settlement_lines l
                            JOIN settlements s ON s.id=l.settlement_id JOIN subcontractors sc ON sc.id=s.sub_id
                            JOIN cases a ON a.id=l.case_id LEFT JOIN cases t ON t.id=l.carry_to_case_id
                            WHERE l.case_id=? OR l.carry_to_case_id=? ORDER BY l.id''', (cid, cid)):
        if row['case_id'] == int(cid):
            if cancelling and row['status'] == 'not_requested' and not row['carry_over']:
                continue
            reasons.append(f"月次精算 {row['period']}／{row['name']}の明細#{row['id']}（充当元 {row['source_no'] or '案件番号未登録'} {row['source_name']}）")
        else:
            if cancelling:
                continue
            reasons.append(f"元金充当 {row['source_no'] or '案件番号未登録'} {row['source_name']} → {row['target_no'] or '案件番号未登録'} {row['target_name']}／{row['name']} "
                           f"{row['carry_over']:,}円（月次精算 {row['period']}、明細#{row['id']}）")
    for row in c.execute('''SELECT p.id,p.amount_tax_in,p.paid_amount,sp.name FROM purchases p
                            JOIN suppliers sp ON sp.id=p.supplier_id WHERE p.case_id=?''', (cid,)):
        reasons.append(f"仕入#{row['id']} {row['name']} 請求{row['amount_tax_in']:,}円・支払済{row['paid_amount']:,}円")
    labels = {'principal_invoice': '元金請求書', 'receipt': '領収書', 'haak_invoice': 'ハーク請求書'}
    for row in c.execute('SELECT id,doc_type FROM issued_documents WHERE case_id=?', (cid,)):
        reasons.append(f"発行済み{labels.get(row['doc_type'], '帳票')} #{row['id']}（旧版履歴を含む）")
    for row in c.execute('SELECT id,original_name FROM attachments WHERE case_id=?', (cid,)):
        reasons.append(f"添付資料#{row['id']} {row['original_name']}")
    if c.execute('SELECT 1 FROM principal_release WHERE case_id=?', (cid,)).fetchone():
        reasons.append('元金の充当可能額の確認記録があります')
    for row in c.execute('''SELECT sc.name,cs.principal_received_date FROM case_subs cs
                            JOIN subcontractors sc ON sc.id=cs.sub_id
                            WHERE cs.case_id=? AND cs.principal_received_date IS NOT NULL''', (cid,)):
        reasons.append(f"{row['name']}の元金入金記録（{row['principal_received_date']}）")
    for row in c.execute('''SELECT * FROM haak_billings WHERE case_id=? AND
                            (confirmed_amount_tax_in IS NOT NULL OR billing_date IS NOT NULL OR paid_date IS NOT NULL)''', (cid,)):
        reasons.append('ハークへの請求・入金記録')
    return reasons


def cancellation_plan(c, cid):
    """見送り時に取り消せる未実行の設定と、残すべき実取引を分けて確認する。"""
    case = dict(c.execute('SELECT * FROM cases WHERE id=?', (cid,)).fetchone())
    subs = [dict(r) for r in c.execute('SELECT * FROM case_subs WHERE case_id=? ORDER BY id', (cid,))]
    lines = [dict(r) for r in c.execute('''SELECT l.*,s.status,s.sub_id FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id WHERE l.case_id=? ORDER BY l.id''', (cid,))]
    incoming = [dict(r) for r in c.execute('''SELECT l.*,s.sub_id,s.status,sc.name AS sub_name FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id JOIN subcontractors sc ON sc.id=s.sub_id
        WHERE l.carry_to_case_id=? ORDER BY l.id''', (cid,))]
    reasons = deletion_reasons(c, cid, cancelling=True)
    if case['deleted_at']:
        reasons.append('この案件はすでに削除ボックスにあります')
    for line in incoming:
        source = c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',
                           (line['case_id'], line['sub_id'])).fetchone()
        if not source or available_principal(c, source, line['settlement_id'], datetime.date.today().isoformat()) < line['carry_over']:
            reasons.append(f"{line['sub_name']}の充当元の確認済元金が不足しています")
    state = dict(case=case, subs=subs, lines=lines, incoming=incoming, reasons=reasons)
    token = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return dict(can_cancel=not reasons, reasons=reasons, token=token,
                hold_total=sum(r['carry_over'] for r in incoming), sub_count=len(subs),
                draft_line_count=len(lines), state=state)
