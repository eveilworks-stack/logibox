"""Reasoned, atomic reversal of mistaken subcontractor participation and funding."""
import datetime
import hashlib
import json
import funding_ledger as ledger


def init(c):
    c.execute('''CREATE TABLE IF NOT EXISTS sub_participation_resets (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
        sub_id INTEGER NOT NULL REFERENCES subcontractors(id), slot INTEGER NOT NULL,
        mode TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
        before_json TEXT NOT NULL, backup_file TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS document_cancellations (
        doc_id INTEGER PRIMARY KEY REFERENCES issued_documents(id),
        reset_id INTEGER NOT NULL REFERENCES sub_participation_resets(id),
        reason TEXT NOT NULL, cancelled_at TEXT NOT NULL)''')
    for table in ('sub_participation_resets','document_cancellations'):
        for action in ('UPDATE','DELETE'):
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'reset history is immutable'); END''')


def plan(c, cid, slot, mode):
    s=ledger.runtime()
    if mode not in ('reset','allocations'):
        raise s.Err('操作を選択してください')
    s.case_view(c,int(cid),True)
    sub=c.execute('SELECT * FROM case_subs WHERE case_id=? AND slot=?',(cid,slot)).fetchone()
    if not sub: raise s.Err('対象の案件参加はありません。画面を開き直してください',409)
    sid=sub['sub_id']
    def rows(sql,args=(cid,sid)):
        return [dict(r) for r in c.execute(sql,args)]
    incoming=[dict(r) for r in ledger.active(c,'e.target_id=? AND e.sub_id=?',(cid,sid))]
    legacy=rows('''SELECT l.*,s.status,s.sub_id FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
        WHERE l.carry_to_case_id=? AND s.sub_id=? AND l.carry_over>0''')
    own=rows('''SELECT l.*,s.status FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
        WHERE l.case_id=? AND s.sub_id=?''')
    docs=rows('''SELECT d.* FROM issued_documents d WHERE d.case_id=? AND d.sub_id=?
        AND NOT EXISTS(SELECT 1 FROM document_cancellations x WHERE x.doc_id=d.id)''')
    sources=rows('''SELECT l.*,s.status FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
        WHERE s.sub_id=? AND l.case_id IN (SELECT source_case_id FROM funding_events WHERE target_id=?)''',(sid,cid))
    state=dict(sub=dict(sub),incoming=incoming,legacy=legacy,own=own,docs=docs,sources=sources,
        cash=rows('SELECT * FROM principal_cash_entries WHERE case_id=? AND sub_id=?'),
        plans=ledger.cash_plans(c,cid,sid),
        accounts=rows('SELECT * FROM principal_accounts WHERE case_id=? AND sub_id=?'),
        releases=rows('SELECT * FROM principal_release WHERE case_id=? AND sub_id=?'),
        outgoing=[dict(r) for r in ledger.active(c,'e.source_case_id=? AND e.sub_id=?',(cid,sid))],
        custody=rows('SELECT * FROM custody_funds WHERE case_id=? AND sub_id=?'),mode=mode)
    reasons=[]
    if mode=='reset' and c.execute('SELECT 1 FROM profit_payments WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone():
        reasons.append('利益精算履歴があるため案件参加をリセットできません。利益精算の編集から訂正してください。')
    if sub['principal_received_date'] or state['cash']:
        reasons.append('元金の実入金記録があります。実入金の訂正を先に確認してください。')
    if state['accounts'] or state['custody']:
        reasons.append('元金の回収・返金または預かり金の記録があります。関連取引を先に確認してください。')
    if state['outgoing'] or any(r['carry_over'] or r['status']!='not_requested' for r in own):
        reasons.append('この案件からの後続充当、または依頼済み・支払済みの精算があります。先に関連取引を確認してください。')
    if any(r['status']!='not_requested' for r in legacy+sources):
        reasons.append('充当元の精算が依頼済み以降です。先に精算の状態を確認してください。')
    if any(r['doc_type']=='receipt' for r in docs):
        reasons.append('発行済み領収書があります。受領済み取引の訂正を先に確認してください。')
    if mode=='allocations' and not incoming and not legacy:
        reasons.append('解除する充当がありません。')
    token=hashlib.sha256(json.dumps(state,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    name=c.execute('SELECT name FROM subcontractors WHERE id=?',(sid,)).fetchone()[0]
    return dict(can_reset=not reasons,reasons=reasons,token=token,sub_name=name,
        allocated=sum(r['amount'] for r in incoming)+sum(r['carry_over'] for r in legacy),
        invoices=[dict(id=r['id'],number=json.loads(r['snapshot_json']).get('doc_no',''),sent_date=r['sent_date'])
                  for r in docs if r['doc_type']=='principal_invoice'],state=state)


def preview(c,b,q,cid,slot):
    result=plan(c,int(cid),int(slot),q.get('mode',['reset'])[0])
    result.pop('state')
    result['history']=[dict(r) for r in c.execute('''SELECT id,slot,mode,reason,created_at FROM sub_participation_resets
        WHERE case_id=? AND slot=? ORDER BY id DESC''',(cid,slot))]
    return result


def apply(c,b,q,cid,slot):
    s=ledger.runtime()
    c.execute('BEGIN IMMEDIATE')
    reason=s.txt(b,'reason','解除・リセット理由',True,500)
    result=plan(c,int(cid),int(slot),b.get('mode','reset'))
    if b.get('token')!=result['token']:
        raise s.Err('関連記録が更新されました。画面を開き直して確認してください',409)
    if not result['can_reset']:
        raise s.Err('\n'.join(result['reasons']),409)
    backup=ledger.backup(c)
    state=result['state']; sid=state['sub']['sub_id']
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    reset_id=c.execute('''INSERT INTO sub_participation_resets(case_id,sub_id,slot,mode,reason,created_at,before_json,backup_file)
        VALUES(?,?,?,?,?,?,?,?)''',(cid,sid,slot,state['mode'],reason,now,json.dumps(state,ensure_ascii=False),backup)).lastrowid
    body=dict(consent_date=ledger.today(),consent_method='操作記録',consent_record=reason,note=reason)
    for e in state['incoming']:
        ledger.record(c,'cancel',e['source_case_id'],sid,e['amount'],body,backup,
            fund=e['fund_id'],target=int(cid),reverses=e['id'])
    for line in state['legacy']:
        before=s.carry_management.snapshot(c,line['settlement_id'],line['id'])
        c.execute('''UPDATE settlement_lines SET carry_over=0,carry_to_case_id=NULL,consent_date=NULL,
            consent_record=NULL,consent_method=NULL,carry_confirmed_date=NULL,carry_revision=carry_revision+1 WHERE id=?''',(line['id'],))
        after=s.carry_management.snapshot(c,line['settlement_id'],line['id'])
        c.execute('''INSERT INTO carry_change_history(line_id,line_uid,settlement_id,sub_id,changed_at,reason,
            before_json,after_json,backup_file) VALUES(?,?,?,?,?,?,?,?,?)''',
            (line['id'],line['carry_uid'],line['settlement_id'],sid,now,reason,json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False),backup))
    for doc in state['docs']:
        if doc['doc_type']=='principal_invoice':
            c.execute('INSERT INTO document_cancellations(doc_id,reset_id,reason,cancelled_at) VALUES(?,?,?,?)',(doc['id'],reset_id,reason,now))
    if state['mode']=='reset':
        for p in state['plans']:
            if p['status']=='planned':
                c.execute('''INSERT INTO principal_cash_plan_actions(plan_id,kind,note,created_at,backup_file)
                    VALUES(?,'cancelled',?,?,?)''',(p['id'],reason,now,backup))
        for line in state['own']:
            c.execute('DELETE FROM settlement_lines WHERE id=?',(line['id'],))
        c.execute('DELETE FROM principal_release WHERE case_id=? AND sub_id=?',(cid,sid))
        c.execute('DELETE FROM case_subs WHERE case_id=? AND slot=?',(cid,slot))
    else:
        c.execute("UPDATE case_subs SET funding_method='cash' WHERE case_id=? AND slot=?",(cid,slot))
    s.principal_balance.validate_case(c,int(cid))
    return dict(ok=True,id=reset_id,allocated=result['allocated'])
