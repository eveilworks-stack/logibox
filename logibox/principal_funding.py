"""下請けタブ用の明示的な元金解放確認と既存精算への充当登録。"""
import datetime
import json
import secrets
import sys
import sqlite3
import principal_balance
import funding_ledger


def runtime():
    main = sys.modules.get('__main__')
    if hasattr(main, 'r_sub_put') and hasattr(main, 'DBP'):
        return main
    import server
    return server


def source_line(c, cid, sid):
    return c.execute('''SELECT l.*,s.status,s.period FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id WHERE l.case_id=? AND s.sub_id=?''', (cid, sid)).fetchone()


def available_pool(c, sid):
    """ダッシュボード・候補・保存で共用する、実資金の未使用残高。納期順では絞らない。"""
    principal, custody, unavailable = [], [], []
    for row in c.execute('''SELECT cs.*,a.case_no,a.name,r.amount AS released FROM case_subs cs
        JOIN cases a ON a.id=cs.case_id LEFT JOIN principal_release r
        ON r.case_id=cs.case_id AND r.sub_id=cs.sub_id
        WHERE cs.sub_id=? AND a.deleted_at IS NULL ORDER BY a.case_no,a.id''', (sid,)):
        item = dict(case_id=row['case_id'], case_no=row['case_no'], name=row['name'])
        value = principal_balance.values(c, row)
        line = source_line(c, row['case_id'], sid)
        legacy = line and line['carry_over'] and not line['carry_to_case_id'] and not c.execute(
            'SELECT 1 FROM custody_funds WHERE line_id=?', (line['id'],)).fetchone()
        amount = value['available'] if row['tax_mode'] == runtime().SUB_TAX_MODE else 0
        if legacy:
            # 旧方式の預かりはGETで書き換えず、実際の選択・保存時に同じ明細を台帳へ引き継ぐ。
            held = line['carry_over']
            eligible = (row['tax_mode'] == runtime().SUB_TAX_MODE and line['consent_date'] and
                        line['consent_date'] <= datetime.date.today().isoformat() and
                        value['recovered'] - value['refunded'] - value['transferred'] >= held)
            if eligible:
                custody.append(dict(item, id=line['id'], source_type='legacy_hold',
                                    amount=held, balance=held, available=held,
                                    reason='既存の一時預かり金', occurred_on=line['consent_date']))
            amount = max(0, amount - held)
        if row['released'] is not None:
            used = (line['carry_over'] if line else 0) + funding_ledger.outgoing(c, row['case_id'], sid)
            amount = min(amount, max(0, row['released'] - used))
        if amount:
            principal.append(dict(item, available=amount, source_type='principal'))
        elif not legacy:
            unavailable.append(dict(item, reason='実入金・回収未確認、使用済み、返金済み、精算依頼中、または任意上限に達しています'))
    for fund in funding_ledger.funds(c, sid):
        if fund['balance'] > 0:
            custody.append(dict(fund, available=fund['balance'], source_type='hold'))
    return dict(principal=principal, custody=custody, unavailable=unavailable,
                available=sum(x['available'] for x in principal + custody))


def options(c, cid, sid):
    s = runtime()
    today = datetime.date.today().isoformat()
    pool = available_pool(c, sid)
    principal_sources = [x for x in pool['principal'] if x['case_id'] != int(cid)]
    custody_sources = [x for x in pool['custody'] if x['case_id'] != int(cid)]
    # sourcesは旧APIとの互換用。新画面は資金種別を分けた一覧を使用する。
    sources = principal_sources + [x for x in custody_sources if x['source_type'] == 'legacy_hold']
    incoming = []
    for row in c.execute('''SELECT l.*,a.case_no,a.name,s.sub_id,s.period,s.status,s.paid_at FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id JOIN cases a ON a.id=l.case_id
        WHERE l.carry_to_case_id=? AND s.sub_id=? AND l.carry_over>0''', (cid, sid)):
        item = dict(row)
        item['carry_history'] = s.carry_management.change_history(c, row['id'])
        incoming.append(item)
    history = []
    for row in c.execute('SELECT * FROM carry_change_history WHERE sub_id=? ORDER BY id DESC',(sid,)):
        before,after = json.loads(row['before_json']),json.loads(row['after_json'])
        if any(int(x.get('carry_to_case_id') or 0)==int(cid) or int(x.get('case_id') or 0)==int(cid) for x in (before,after)):
            history.append(dict(changed_at=row['changed_at'],reason=row['reason'],before=before,after=after))
    release = c.execute('SELECT * FROM principal_release WHERE case_id=? AND sub_id=?', (cid, sid)).fetchone()
    outgoing = source_line(c,cid,sid)
    used = (outgoing['carry_over'] if outgoing else 0) + funding_ledger.outgoing(c,cid,sid)
    sub = c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    balance = principal_balance.values(c,sub) if sub else None
    remaining = balance['available'] if balance else 0
    if release:
        remaining = min(remaining,max(0,release['amount']-used))
    if outgoing and outgoing['status']!='not_requested':
        remaining=0
    account_history = [dict(x) for x in c.execute('SELECT * FROM principal_account_history WHERE case_id=? AND sub_id=? ORDER BY id DESC',(cid,sid))]
    ledger_history = funding_ledger.history(c,'e.sub_id=? AND (e.source_case_id=? OR e.target_id=?)',(sid,cid,cid))
    ledger_incoming = [x for x in ledger_history if x['target_id']==int(cid) and x['kind'] in ('principal','hold') and not x['cancelled']]
    cash_history = [dict(x) for x in c.execute('SELECT * FROM principal_cash_entries WHERE case_id=? AND sub_id=? ORDER BY id',(cid,sid))]
    holds = funding_ledger.funds(c,sid)
    locked = bool(outgoing and (outgoing['status']!='not_requested' or outgoing['carry_over'])) or bool(funding_ledger.outgoing(c,cid,sid))
    capacity = 0 if locked else max(0, (sub['principal_tax_in'] if sub else 0) - s.incoming_carry(c,cid,sid) - funding_ledger.received_cash(c,cid,sid))
    plans = funding_ledger.cash_plans(c,cid,sid)
    credited = s.incoming_carry(c,cid,sid,today)
    master=c.execute('SELECT name FROM subcontractors WHERE id=?',(sid,)).fetchone()
    return dict(sub_name=master['name'] if master else '',sources=sources,principal_sources=principal_sources,custody_sources=custody_sources,
                pool_available=pool['available'],available=min(capacity,sum(x['available'] for x in principal_sources+custody_sources)),
                capacity=capacity,locked=locked,confirmed_allocated=credited,confirmed_total=s.principal_confirmed(c,sub) if sub else 0,
                cash_plans=plans,cash_planned=sum(x['amount'] for x in plans if x['status']=='planned'),
                unavailable=[x for x in pool['unavailable'] if x['case_id']!=int(cid)],incoming=incoming,history=history,release=dict(release) if release else None,
                custody=holds,ledger_history=ledger_history,ledger_incoming=ledger_incoming,cash_history=cash_history,
                cash_received=funding_ledger.cash(c,cid,sid) if cash_history else max(0,(sub['principal_tax_in'] if sub and sub['principal_received_date'] and sub['principal_received_date']<=today else 0)-s.incoming_carry(c,cid,sid)),
                balance=balance,account_history=account_history,
                source_allocated=used,source_remaining=remaining)


def release(c, b, cid, slot):
    s = runtime()
    c.execute('BEGIN IMMEDIATE')
    s.case_view(c, int(cid), True)
    sub = c.execute('SELECT * FROM case_subs WHERE case_id=? AND slot=?', (cid, slot)).fetchone()
    if not sub:
        raise s.Err('先に下請けと担当元金を保存してください')
    amount = s.yen(b, 'amount', '充当へ回せる確認済額', True)
    record = s.txt(b, 'record', '残高の確認記録', True, 500)
    line = source_line(c, cid, sub['sub_id'])
    used = (line['carry_over'] if line else 0) + funding_ledger.outgoing(c,cid,sub['sub_id'])
    if amount < used or amount > principal_balance.values(c,sub)['available'] + used:
        raise s.Err('確認済額は実際の確認済元金以内、充当済額以上で指定してください')
    if line and line['status'] != 'not_requested':
        raise s.Err('依頼済み・支払済みの元金は新しく充当へ回せません', 409)
    old = c.execute('SELECT * FROM principal_release WHERE case_id=? AND sub_id=?', (cid, sub['sub_id'])).fetchone()
    revision = old['revision'] if old else 0
    if s.yen(b, 'expected_revision', '確認時の版番号', True) != revision:
        raise s.Err('残高確認が更新されました。画面を開き直してください', 409)
    if old and old['amount'] == amount and old['record'] == record:
        return dict(ok=True)
    try:
        s.carry_management.backup_database(c)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise s.Err('変更前のDBバックアップに失敗したため、確認済額は変更していません',409) from exc
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    c.execute('''INSERT INTO principal_release(case_id,sub_id,amount,confirmed_at,record,revision)
        VALUES(?,?,?,?,?,?) ON CONFLICT(case_id,sub_id) DO UPDATE SET amount=excluded.amount,
        confirmed_at=excluded.confirmed_at,record=excluded.record,revision=excluded.revision''',
        (cid,sub['sub_id'],amount,now,record,revision+1))
    after = dict(c.execute('SELECT * FROM principal_release WHERE case_id=? AND sub_id=?', (cid,sub['sub_id'])).fetchone())
    c.execute('INSERT INTO principal_release_history(case_id,sub_id,changed_at,before_json,after_json) VALUES(?,?,?,?,?)',
        (cid,sub['sub_id'],now,json.dumps(dict(old) if old else None,ensure_ascii=False),json.dumps(after,ensure_ascii=False)))
    return dict(ok=True)


def allocate(c, b, cid, sid):
    """呼出側の下請け保存トランザクション内で全行を一括登録する。"""
    s = runtime()
    entries = b.get('allocations', [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise s.Err('充当明細を確認してください')
    seen = set()
    mode = b.get('funding_method', 'cash')
    if mode not in ('cash','carry','mixed'):
        raise s.Err('元金の充当方法を選択してください')
    if entries and mode == 'cash':
        raise s.Err('他案件から充当または併用を選択してください')
    for entry in entries:
        if not isinstance(entry, dict):
            raise s.Err('充当明細を確認してください')
        source = s.yen(entry,'source_id','充当元案件',True)
        amount = s.yen(entry,'carry_over','充当額',True)
        if source in seen or not amount:
            raise s.Err('同じ充当元の重複または0円の充当は登録できません')
        seen.add(source)
        candidate = next((x for x in options(c,cid,sid)['sources'] if x['case_id']==source),None)
        if not candidate or amount > candidate['available']:
            raise s.Err('充当元の確認済み残高が不足しています。画面を更新してください',409)
        line = source_line(c,source,sid)
        if line and line['carry_to_case_id']:
            raise s.Err('既存充当の編集は明細から行ってください。分割追加は新しい充当画面から登録してください',409)
        if not line:
            row = c.execute('''SELECT a.due_date,a.planned_rate_pct,cs.* FROM cases a
                JOIN case_subs cs ON cs.case_id=a.id WHERE a.id=? AND cs.sub_id=?''',(source,sid)).fetchone()
            period,pay_date = s.settlement_period(row['due_date'][:7])
            settlement = c.execute('SELECT id FROM settlements WHERE sub_id=? AND period=?',(sid,period)).fetchone()
            settlement_id = settlement['id'] if settlement else c.execute(
                'INSERT INTO settlements(sub_id,period,pay_date) VALUES(?,?,?)',(sid,period,pay_date)).lastrowid
            c.execute('''INSERT INTO settlement_lines(settlement_id,case_id,principal,sub_profit,carry_uid)
                VALUES(?,?,?,?,?)''',(settlement_id,source,row['principal_tax_in'],
                s.calc.sub_profit(row['principal_tax_in'],row['planned_rate_pct'],row['fee_rate_pct']),secrets.token_hex(16)))
            line = source_line(c,source,sid)
        body = dict(entry,target_id=int(cid),expected_revision=line['carry_revision'])
        body['consent_method'] = s.txt(entry,'consent_method','承諾方法',True,80)
        s.r_carry(c,body,{},str(line['settlement_id']),str(line['id']))
    c.execute('UPDATE case_subs SET funding_method=? WHERE case_id=? AND sub_id=?',(mode,cid,sid))
