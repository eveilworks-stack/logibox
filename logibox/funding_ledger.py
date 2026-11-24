"""追記式の元金充当・預かり金・実入金台帳。既存明細は保持して合算する。"""
import datetime
import sqlite3
import json


def runtime():
    import principal_funding
    return principal_funding.runtime()


def today():
    return datetime.date.today().isoformat()


def init(c):
    c.execute('''CREATE TABLE IF NOT EXISTS funding_requests (
        token TEXT PRIMARY KEY, created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS custody_funds (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
        sub_id INTEGER NOT NULL REFERENCES subcontractors(id),
        line_id INTEGER UNIQUE REFERENCES settlement_lines(id),
        reason TEXT NOT NULL, occurred_on TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
        consent_date TEXT NOT NULL, consent_method TEXT NOT NULL, consent_record TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS funding_events (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('principal','hold','refund','cancel','created')),
        source_case_id INTEGER NOT NULL REFERENCES cases(id), sub_id INTEGER NOT NULL REFERENCES subcontractors(id),
        fund_id INTEGER REFERENCES custody_funds(id), target_id INTEGER REFERENCES cases(id),
        amount INTEGER NOT NULL CHECK(amount>0), event_date TEXT NOT NULL,
        consent_date TEXT NOT NULL, consent_method TEXT NOT NULL, consent_record TEXT NOT NULL,
        note TEXT NOT NULL, reverses INTEGER UNIQUE REFERENCES funding_events(id),
        created_at TEXT NOT NULL, backup_file TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS funding_event_notifications (
        event_id INTEGER PRIMARY KEY REFERENCES funding_events(id), settlement_id INTEGER NOT NULL,
        notified_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_cash_entries (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
        sub_id INTEGER NOT NULL REFERENCES subcontractors(id), amount INTEGER NOT NULL CHECK(amount>0),
        received_on TEXT NOT NULL, method TEXT NOT NULL CHECK(method IN ('bank','hand')),
        receipt_checked INTEGER NOT NULL, stamp_checked INTEGER NOT NULL,
        note TEXT NOT NULL, created_at TEXT NOT NULL, backup_file TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_cash_plans (
        id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
        sub_id INTEGER NOT NULL REFERENCES subcontractors(id), amount INTEGER NOT NULL CHECK(amount>0),
        expected_on TEXT, method TEXT NOT NULL CHECK(method IN ('bank','hand')),
        note TEXT NOT NULL, created_at TEXT NOT NULL, backup_file TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS principal_cash_plan_actions (
        plan_id INTEGER PRIMARY KEY REFERENCES principal_cash_plans(id),
        kind TEXT NOT NULL CHECK(kind IN ('confirmed','cancelled')),
        cash_entry_id INTEGER UNIQUE REFERENCES principal_cash_entries(id),
        note TEXT NOT NULL, created_at TEXT NOT NULL, backup_file TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS funding_event_labels (
        event_id INTEGER PRIMARY KEY REFERENCES funding_events(id), sub_name TEXT NOT NULL,
        source_case_no TEXT, source_name TEXT NOT NULL, target_case_no TEXT, target_name TEXT)''')
    c.execute('''INSERT OR IGNORE INTO funding_event_labels
        SELECT e.id,sc.name,a.case_no,a.name,t.case_no,t.name FROM funding_events e
        JOIN cases a ON a.id=e.source_case_id JOIN subcontractors sc ON sc.id=e.sub_id
        LEFT JOIN cases t ON t.id=e.target_id''')
    for table in ('custody_funds','funding_events','principal_cash_entries','principal_cash_plans',
                  'principal_cash_plan_actions','funding_event_labels'):
        for action in ('UPDATE','DELETE'):
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'funding ledger is immutable'); END''')
    c.execute('CREATE INDEX IF NOT EXISTS funding_source ON funding_events(source_case_id,sub_id)')
    c.execute('CREATE INDEX IF NOT EXISTS funding_target ON funding_events(target_id,sub_id)')


def active(c, where='1', args=(), cutoff=None):
    return c.execute('''SELECT e.* FROM funding_events e WHERE e.kind IN ('principal','hold')
        AND (? IS NULL OR e.event_date<=?)
        AND NOT EXISTS(SELECT 1 FROM funding_events r WHERE r.reverses=e.id
        AND (? IS NULL OR r.event_date<=?)) AND '''+where, (cutoff,cutoff,cutoff,cutoff)+args).fetchall()


def outgoing(c,cid,sid,cutoff=None):
    return sum(x['amount'] for x in active(c,'e.kind=\'principal\' AND e.source_case_id=? AND e.sub_id=?',(cid,sid),cutoff))


def incoming(c,cid,sid,cutoff=None):
    return sum(x['amount'] for x in active(c,'e.target_id=? AND e.sub_id=?',(cid,sid),cutoff))


def cash(c,cid,sid,cutoff=None):
    return c.execute('''SELECT COALESCE(SUM(amount),0) FROM principal_cash_entries
        WHERE case_id=? AND sub_id=? AND (? IS NULL OR received_on<=?)''',(cid,sid,cutoff,cutoff)).fetchone()[0]


def received_cash(c,cid,sid):
    sub=c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    if not sub: return 0
    if c.execute('SELECT 1 FROM principal_cash_entries WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone():
        return cash(c,cid,sid,today())
    return max(0,sub['principal_tax_in']-runtime().incoming_carry(c,cid,sid)) if (
        sub['principal_received_date'] and sub['principal_received_date']<=today()) else 0


def cash_plans(c,cid,sid):
    return [dict(x) for x in c.execute('''SELECT p.*,COALESCE(a.kind,'planned') AS status
        FROM principal_cash_plans p LEFT JOIN principal_cash_plan_actions a ON a.plan_id=p.id
        WHERE p.case_id=? AND p.sub_id=? ORDER BY p.id''',(cid,sid))]


def pending_cash(c,cid,sid):
    return sum(x['amount'] for x in cash_plans(c,cid,sid) if x['status']=='planned')


def check_capacity(c,cid,sid):
    sub=c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    if not sub or runtime().incoming_carry(c,cid,sid)+received_cash(c,cid,sid)+pending_cash(c,cid,sid)>sub['principal_tax_in']:
        raise runtime().Err('入金・充当・未確認の入金予定の合計が担当元金を超えています',409)


def backup(c):
    s=runtime()
    try:
        return s.carry_management.backup_database(c)
    except (OSError,ValueError,sqlite3.Error) as exc:
        raise s.Err('変更前バックアップに失敗したため台帳は更新していません',409) from exc


def confirmations(b):
    s=runtime()
    date=s.dt(b,'consent_date','承諾日')
    if not date or date>today():
        raise s.Err('承諾日を確認してください')
    return date,s.txt(b,'consent_method','承諾方法',True,80),s.txt(b,'consent_record','承諾記録',True,500)


def record(c,kind,cid,sid,amount,b,backup_file,fund=None,target=None,reverses=None):
    consent=confirmations(b)
    date=s_date(b,'event_date','確定・返金日') if b.get('event_date') else today()
    if kind in ('principal','hold','cancel') and date!=today():
        raise runtime().Err('充当・取消の確定日は操作日で登録してください')
    note=runtime().txt(b,'note','記録・理由',n=500) or ''
    eid = c.execute('''INSERT INTO funding_events(kind,source_case_id,sub_id,fund_id,target_id,amount,
        event_date,consent_date,consent_method,consent_record,note,reverses,created_at,backup_file)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (kind,cid,sid,fund,target,amount,date,*consent,note,reverses,
         datetime.datetime.now(datetime.timezone.utc).isoformat(),backup_file)).lastrowid
    c.execute('''INSERT INTO funding_event_labels SELECT e.id,sc.name,a.case_no,a.name,t.case_no,t.name
        FROM funding_events e JOIN cases a ON a.id=e.source_case_id
        JOIN subcontractors sc ON sc.id=e.sub_id LEFT JOIN cases t ON t.id=e.target_id WHERE e.id=?''',(eid,))
    return eid


def s_date(b,key,label):
    value=runtime().dt(b,key,label)
    if not value or value>today():
        raise runtime().Err(label+'を確認してください')
    return value


def funds(c,sid=None,cutoff=None):
    result=[]
    for r in c.execute('''SELECT f.*,a.case_no,a.name,sc.name AS sub_name FROM custody_funds f
        JOIN cases a ON a.id=f.case_id JOIN subcontractors sc ON sc.id=f.sub_id
        WHERE (? IS NULL OR f.sub_id=?) ORDER BY f.id''',(sid,sid)):
        if cutoff and r['occurred_on']>cutoff:
            continue
        item=dict(r)
        used=sum(x['amount'] for x in active(c,'e.fund_id=?',(r['id'],),cutoff))
        refunded=c.execute("SELECT COALESCE(SUM(amount),0) FROM funding_events WHERE fund_id=? AND kind='refund' AND (? IS NULL OR event_date<=?)",(r['id'],cutoff,cutoff)).fetchone()[0]
        item.update(balance=r['amount']-used-refunded,allocated=used,refunded=refunded)
        item['history']=history(c,'e.fund_id=?',(r['id'],))
        result.append(item)
    return result


def history(c,where='1',args=()):
    return [dict(x) for x in c.execute('''SELECT e.*,
        COALESCE(labels.source_case_no,a.case_no) AS case_no,
        COALESCE(labels.source_name,a.name) AS name,
        COALESCE(labels.target_case_no,t.case_no) AS target_case_no,
        COALESCE(labels.target_name,t.name) AS target_name,COALESCE(labels.sub_name,sc.name) AS sub_name,
        EXISTS(SELECT 1 FROM funding_events r WHERE r.reverses=e.id) AS cancelled
        FROM funding_events e JOIN cases a ON a.id=e.source_case_id
        LEFT JOIN cases t ON t.id=e.target_id JOIN subcontractors sc ON sc.id=e.sub_id
        LEFT JOIN funding_event_labels labels ON labels.event_id=e.id
        WHERE '''+where+' ORDER BY e.id',args)]


def create_hold(c,b,q,cid,slot):
    s=runtime()
    c.execute('BEGIN IMMEDIATE')
    s.case_view(c,int(cid),True)
    sub=c.execute('SELECT * FROM case_subs WHERE case_id=? AND slot=?',(cid,slot)).fetchone()
    if not sub: raise s.Err('先に下請けを保存してください')
    amount=s.yen(b,'amount','一時預かり金額',True)
    if not amount: raise s.Err('一時預かり金額は1円以上です')
    date=s_date(b,'occurred_on','発生日')
    reason=s.txt(b,'reason','発生理由',True,500)
    consent=confirmations(b)
    import principal_funding
    line=principal_funding.source_line(c,int(cid),sub['sub_id'])
    if line and c.execute('SELECT 1 FROM custody_funds WHERE line_id=?',(line['id'],)).fetchone():
        raise s.Err('この精算明細は台帳へ登録済みです',409)
    file=backup(c)
    if line and line['carry_over'] and not line['carry_to_case_id']:
        if amount!=line['carry_over'] or not line['consent_date']:
            raise s.Err('既存預かり金の金額と承諾を確認してください',409)
        if s.principal_balance.values(c,sub)['recovered'] < amount:
            raise s.Err('実入金・回収確認済み元金が不足しています',409)
    else:
        if line and line['carry_over']:
            raise s.Err('既存の充当明細があるため、この元金は直接分割充当してください',409)
        available=s.principal_balance.values(c,sub)['available']
        if amount>available: raise s.Err('確認済み残高が不足しています',409)
        if not line:
            case=c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone()
            if not case['due_date']: raise s.Err('元案件の納期を保存してください')
            period,pay_date=s.settlement_period(case['due_date'][:7])
            settlement=c.execute('SELECT * FROM settlements WHERE sub_id=? AND period=?',(sub['sub_id'],period)).fetchone()
            if settlement and settlement['status']!='not_requested': raise s.Err('対象月は依頼済みです',409)
            settlement_id=settlement['id'] if settlement else c.execute('INSERT INTO settlements(sub_id,period,pay_date) VALUES(?,?,?)',(sub['sub_id'],period,pay_date)).lastrowid
            c.execute('''INSERT INTO settlement_lines(settlement_id,case_id,principal,sub_profit,carry_uid)
                VALUES(?,?,?,?,lower(hex(randomblob(16))))''',(settlement_id,cid,sub['principal_tax_in'],
                s.calc.sub_profit(sub['principal_tax_in'],case['planned_rate_pct'],sub['fee_rate_pct'])))
            line=principal_funding.source_line(c,int(cid),sub['sub_id'])
        s.r_carry(c,dict(b,target_id='hold',carry_over=amount,expected_revision=line['carry_revision']),{},str(line['settlement_id']),str(line['id']))
    fund=c.execute('''INSERT INTO custody_funds(case_id,sub_id,line_id,reason,occurred_on,amount,
        consent_date,consent_method,consent_record,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (cid,sub['sub_id'],line['id'],reason,date,amount,*consent,datetime.datetime.now(datetime.timezone.utc).isoformat())).lastrowid
    record(c,'created',int(cid),sub['sub_id'],amount,dict(b,event_date=date,note=reason),file,fund=fund)
    return dict(ok=True,id=fund)


def allocate(c,b,cid,sid):
    s=runtime()
    entries=b.get('allocations',[])
    if not isinstance(entries,list) or len(entries)>100: raise s.Err('充当明細を確認してください')
    if b.get('funding_method','cash') not in ('cash','carry','hold','mixed'): raise s.Err('充当方法を確認してください')
    if entries and b.get('funding_method')=='cash': raise s.Err('充当または併用を選択してください')
    seen=set()
    file=backup(c) if entries else ''
    target=c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    line=s.principal_funding.source_line(c,cid,sid)
    if entries and ((line and (line['status']!='not_requested' or line['carry_over'])) or outgoing(c,cid,sid)):
        raise s.Err('依頼済みまたは後続の充当がある案件には追加できません',409)
    for e in entries:
        if not isinstance(e,dict): raise s.Err('充当明細を確認してください')
        claim(c,e)
        kind=e.get('source_type','principal')
        if kind not in ('principal','hold','legacy_hold'): raise s.Err('充当元の区分を確認してください')
        if b.get('funding_method')=='hold' and kind=='principal': raise s.Err('一時預かり金を選んでください')
        source=s.yen(e,'source_id','充当元',True)
        amount=s.yen(e,'carry_over','充当額',True)
        key=(kind,source)
        if key in seen or not amount: raise s.Err('同じ充当元の重複または0円の充当は登録できません')
        seen.add(key)
        if kind=='legacy_hold':
            candidate=next((x for x in s.principal_funding.options(c,cid,sid)['custody_sources']
                            if x['source_type']=='legacy_hold' and x['id']==source),None)
            if not candidate or amount>candidate['available']: raise s.Err('既存預かり金の残高が不足しています',409)
            source=adopt_legacy_hold(c,candidate,e,file)
            kind='hold'
        if kind=='hold':
            fund=next((x for x in funds(c,sid) if x['id']==source),None)
            if not fund or fund['case_id']==cid or amount>fund['balance']: raise s.Err('預かり金の残高が不足しています',409)
            source_case=fund['case_id']
        else:
            candidate=next((x for x in s.principal_funding.options(c,cid,sid)['sources'] if x['case_id']==source),None)
            if not candidate or amount>candidate['available']: raise s.Err('充当元の確認済み残高が不足しています',409)
            oldline=s.principal_funding.source_line(c,source,sid)
            if oldline and oldline['carry_over'] and not oldline['carry_to_case_id'] and not c.execute('SELECT 1 FROM custody_funds WHERE line_id=?',(oldline['id'],)).fetchone():
                raise s.Err('既存の一時預かり金は元案件から台帳へ登録してください',409)
            source_case=source
        if creates_cycle(c,source_case,cid,sid):
            raise s.Err('循環する元金充当は登録できません。充当元を確認してください',409)
        if s.incoming_carry(c,cid,sid)+cash(c,cid,sid)+amount>target['principal_tax_in']:
            raise s.Err('入金・充当の合計が担当元金を超えています')
        if target['principal_received_date'] and not cash(c,cid,sid):
            raise s.Err('既存の現金入金記録があります。残額入金との整合を先に確認してください',409)
        record(c,kind,source_case,sid,amount,e,file,fund=source if kind=='hold' else None,target=cid)
    c.execute('UPDATE case_subs SET funding_method=? WHERE case_id=? AND sub_id=?',(b.get('funding_method','cash'),cid,sid))


def adopt_legacy_hold(c,candidate,b,file):
    """旧精算明細を変更せず、選択した預かり金だけを同一トランザクションで台帳へ引き継ぐ。"""
    line=c.execute('''SELECT l.*,s.sub_id FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
        WHERE l.id=?''',(candidate['id'],)).fetchone()
    confirmations(b)
    fund=c.execute('''INSERT INTO custody_funds(case_id,sub_id,line_id,reason,occurred_on,amount,
        consent_date,consent_method,consent_record,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (line['case_id'],line['sub_id'],line['id'],'旧精算明細の一時預かり金を引継ぎ',line['consent_date'],
         line['carry_over'],line['consent_date'],line['consent_method'] or '',line['consent_record'] or '',
         datetime.datetime.now(datetime.timezone.utc).isoformat())).lastrowid
    record(c,'created',line['case_id'],line['sub_id'],line['carry_over'],
           dict(b,note='旧精算明細の資金・承諾を保持して台帳へ引継ぎ'),file,fund=fund)
    return fund


def creates_cycle(c,source,target,sid):
    edges=[(r['source_case_id'],r['target_id']) for r in active(c,'e.sub_id=?',(sid,))]
    edges += [(r['case_id'],r['carry_to_case_id']) for r in c.execute('''SELECT l.* FROM settlement_lines l
        JOIN settlements s ON s.id=l.settlement_id WHERE s.sub_id=? AND l.carry_over>0 AND l.carry_to_case_id IS NOT NULL''',(sid,))]
    pending=[target];seen=set()
    while pending:
        node=pending.pop()
        if node==source:return True
        if node in seen:continue
        seen.add(node)
        pending.extend(b for a,b in edges if a==node and b is not None)
    return False


def refund(c,b,q,fid):
    s=runtime(); c.execute('BEGIN IMMEDIATE')
    f=next((x for x in funds(c) if x['id']==int(fid)),None)
    if not f: raise s.Err('預かり金が見つかりません',404)
    amount=s.yen(b,'amount','返金額',True)
    if not amount or amount>f['balance']: raise s.Err('返金額は残高以内で指定してください',409)
    date=s_date(b,'event_date','返金日')
    if date < max([f['occurred_on']]+[x['event_date'] for x in f['history']]):
        raise s.Err('返金日はこの預かり金の直近取引日以降にしてください')
    claim(c,b)
    s.txt(b,'note','返金先・振込記録',True,500)
    record(c,'refund',f['case_id'],f['sub_id'],amount,b,backup(c),fund=f['id'])
    return dict(ok=True)


def cancel(c,b,q,eid):
    s=runtime(); c.execute('BEGIN IMMEDIATE')
    e=next((dict(x) for x in active(c,'e.id=?',(eid,))),None)
    if not e: raise s.Err('取消済み、または対象の充当がありません',409)
    target=c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(e['target_id'],e['sub_id'])).fetchone()
    line=s.principal_funding.source_line(c,e['target_id'],e['sub_id'])
    if (target and target['principal_received_date']) or outgoing(c,e['target_id'],e['sub_id']) or (line and (line['status']!='not_requested' or line['carry_over'])):
        raise s.Err('充当先に入金・精算・後続の充当があります。取消できません',409)
    source_line=s.principal_funding.source_line(c,e['source_case_id'],e['sub_id'])
    if e['kind']=='principal' and source_line and source_line['status']!='not_requested':
        raise s.Err('充当元は依頼済みです。取消できません',409)
    s.txt(b,'note','取消理由',True,500)
    record(c,'cancel',e['source_case_id'],e['sub_id'],e['amount'],b,backup(c),fund=e['fund_id'],target=e['target_id'],reverses=e['id'])
    s.principal_balance.validate_case(c,e['target_id'])
    return dict(ok=True)


def save_cash(c,b,cid,sid):
    s=runtime()
    entry=b.get('cash_entry')
    if not entry: return
    if not isinstance(entry,dict): raise s.Err('入金明細を確認してください')
    if 'confirmed' in entry and not isinstance(entry['confirmed'],bool): raise s.Err('実入金確認の選択を確認してください')
    claim(c,entry)
    if b.get('funding_method','cash') in ('carry','hold'): raise s.Err('新規入金または併用を選んでください')
    amount=s.yen(entry,'amount','入金金額',True)
    planned=entry.get('confirmed') is False
    date=s.dt(entry,'received_on','入金予定日') if planned else s_date(entry,'received_on','入金日')
    method=entry.get('method')
    if not amount or method not in ('bank','hand'): raise s.Err('入金金額・方法を確認してください')
    if not planned and method=='hand' and (entry.get('receipt_checked') is not True or entry.get('stamp_checked') is not True):
        raise s.Err('手渡しの領収書・収入印紙を確認してください')
    sub=c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    line=s.principal_funding.source_line(c,cid,sid)
    if (line and (line['status']!='not_requested' or line['carry_over'])) or outgoing(c,cid,sid):
        raise s.Err('精算・後続の充当がある案件には入金を追加できません',409)
    if cash(c,cid,sid)+s.incoming_carry(c,cid,sid)+amount>sub['principal_tax_in']:
        raise s.Err('入金・充当の合計が担当元金を超えています')
    if sub['principal_received_date'] and not cash(c,cid,sid):
        raise s.Err('旧入金記録があるため、新しい入金を追加できません',409)
    file=backup(c)
    if planned:
        c.execute('''INSERT INTO principal_cash_plans(case_id,sub_id,amount,expected_on,method,note,created_at,backup_file)
            VALUES(?,?,?,?,?,?,?,?)''',(cid,sid,amount,date,method,s.txt(entry,'note','入金予定の備考',n=500) or '',
            datetime.datetime.now(datetime.timezone.utc).isoformat(),file))
        return
    c.execute('''INSERT INTO principal_cash_entries(case_id,sub_id,amount,received_on,method,receipt_checked,
        stamp_checked,note,created_at,backup_file) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (cid,sid,amount,date,method,entry.get('receipt_checked') is True,entry.get('stamp_checked') is True,
         s.txt(entry,'note','入金記録',n=500) or '',datetime.datetime.now(datetime.timezone.utc).isoformat(),file))


def cash_plan_action(c,b,q,pid):
    s=runtime();c.execute('BEGIN IMMEDIATE')
    plan=c.execute('SELECT * FROM principal_cash_plans WHERE id=?',(pid,)).fetchone()
    if not plan or c.execute('SELECT 1 FROM principal_cash_plan_actions WHERE plan_id=?',(pid,)).fetchone():
        raise s.Err('入金予定は確認済み・取消済み、または見つかりません',409)
    s.case_view(c,plan['case_id'],True)
    action=b.get('action')
    file=backup(c);cash_id=None
    if action=='confirmed':
        if b.get('confirmed') is not True: raise s.Err('実入金確認を選択してください')
        entry=dict(b,amount=plan['amount'],method=plan['method'],note=plan['note'],confirmed=True)
        save_cash(c,dict(funding_method='mixed',cash_entry=entry),plan['case_id'],plan['sub_id'])
        cash_id=c.execute('SELECT max(id) FROM principal_cash_entries').fetchone()[0]
    elif action=='cancelled':
        claim(c,b)
        s.txt(b,'note','取消理由',True,500)
    else: raise s.Err('入金予定の操作を確認してください')
    c.execute('''INSERT INTO principal_cash_plan_actions VALUES(?,?,?,?,?,?)''',(pid,action,cash_id,
        s.txt(b,'note','確認・取消記録',n=500) or '',datetime.datetime.now(datetime.timezone.utc).isoformat(),file))
    check_capacity(c,plan['case_id'],plan['sub_id'])
    return dict(ok=True)


def period_events(c,sid,period):
    return history(c,'''e.sub_id=? AND substr(e.event_date,1,7)=? AND NOT EXISTS
        (SELECT 1 FROM funding_event_notifications n WHERE n.event_id=e.id)''',(sid,period))


def export_history(c):
    """新台帳と旧充当の変更履歴を一緒に出力する。金額は履歴であり合算しない。"""
    rows=history(c)
    seen=set()
    for r in c.execute('''SELECT h.*,sc.name AS sub_name FROM carry_change_history h
        JOIN subcontractors sc ON sc.id=h.sub_id ORDER BY h.id'''):
        a=json.loads(r['after_json']);seen.add(r['line_id'])
        rows.append(dict(id='旧履歴-'+str(r['id']),kind='legacy_change',case_no=a.get('source_case_no'),
            name=a.get('source_name'),sub_name=r['sub_name'],fund_id=None,
            target_case_no=a.get('target_case_no'),target_name=a.get('target_name'),amount=a['carry_over'],
            event_date=r['changed_at'][:10],consent_date=a.get('consent_date'),consent_method=a.get('consent_method'),
            consent_record=a.get('consent_record'),note=r['reason'],reverses=None))
    for r in c.execute('''SELECT l.*,a.case_no,a.name,t.case_no AS target_case_no,t.name AS target_name,
        sc.name AS sub_name FROM settlement_lines l JOIN cases a ON a.id=l.case_id
        JOIN settlements s ON s.id=l.settlement_id JOIN subcontractors sc ON sc.id=s.sub_id
        LEFT JOIN cases t ON t.id=l.carry_to_case_id WHERE l.carry_over>0 ORDER BY l.id'''):
        if r['id'] in seen:continue
        rows.append(dict(id='旧明細-'+str(r['id']),kind='legacy',case_no=r['case_no'],name=r['name'],sub_name=r['sub_name'],
            fund_id=None,target_case_no=r['target_case_no'],target_name=r['target_name'],amount=r['carry_over'],
            event_date=r['carry_confirmed_date'] or r['consent_date'] or '',consent_date=r['consent_date'],
            consent_method=r['consent_method'],consent_record=r['consent_record'],note='旧明細（詳細変更履歴なし）',reverses=None))
    return rows


def claim(c,b):
    token=runtime().txt(b,'request_key','重複防止キー',True,100)
    try:
        c.execute('INSERT INTO funding_requests(token,created_at) VALUES(?,?)',(token,datetime.datetime.now(datetime.timezone.utc).isoformat()))
    except sqlite3.IntegrityError as exc:
        raise runtime().Err('この入力は保存済みです。画面を再表示してください',409) from exc


def confirmed_allocations(c, cid, sid, cutoff):
    """元金管理と帳票で共用。実入金・精算支払とは別の確定充当明細。"""
    rows = []
    if cutoff < today():
        for change in c.execute('SELECT before_json FROM carry_change_history WHERE sub_id=? AND substr(changed_at,1,10)>?',(sid,cutoff)):
            before = json.loads(change['before_json'])
            confirmed = before.get('carry_confirmed_date') or before.get('consent_date')
            if (before.get('carry_to_case_id') == int(cid) and before.get('carry_over') and
                    confirmed and confirmed <= cutoff):
                raise runtime().Err('発行日後に変更・取消された充当履歴があります。発行日と訂正内容を確認してください',409)
    for event in active(c, 'e.target_id=? AND e.sub_id=?', (cid, sid), cutoff):
        source = c.execute('SELECT case_no,name FROM cases WHERE id=?', (event['source_case_id'],)).fetchone()
        label = c.execute('SELECT * FROM funding_event_labels WHERE event_id=?', (event['id'],)).fetchone()
        rows.append(dict(kind=event['kind'], id=event['id'], amount=event['amount'],
                         case_no=label['source_case_no'] if label else source['case_no'],
                         name=label['source_name'] if label else source['name'],
                         confirmed_on=event['event_date'], consent_date=event['consent_date'], fund_id=event['fund_id'],
                         record=event['consent_record']))
    # 旧方式は承諾と確定日を持つ保存済み充当のみ。精算の支払待ちは入金と混同しない。
    for row in c.execute("""SELECT l.*,a.case_no,a.name FROM settlement_lines l
            JOIN settlements s ON s.id=l.settlement_id JOIN cases a ON a.id=l.case_id
            WHERE l.carry_to_case_id=? AND s.sub_id=? AND l.carry_over>0
            AND l.consent_date<=? AND COALESCE(l.carry_confirmed_date,l.consent_date)<=?
            AND COALESCE(l.consent_record,'')<>'' """, (cid,sid,cutoff,cutoff)):
        # 可変の旧明細を過去日に再構成できない場合は、後日の内容を混入させず停止する。
        changed = c.execute('SELECT MAX(changed_at) FROM carry_change_history WHERE line_id=?', (row['id'],)).fetchone()[0]
        if cutoff < today() and changed and changed[:10] > cutoff:
            raise runtime().Err('発行日後に変更された充当履歴があります。発行日と訂正内容を確認してください',409)
        rows.append(dict(kind='principal', id='legacy-'+str(row['id']), amount=row['carry_over'],
                         case_no=row['case_no'], name=row['name'],
                         confirmed_on=row['carry_confirmed_date'] or row['consent_date'], consent_date=row['consent_date'],
                         fund_id=None, record=row['consent_record']))
    return rows


def invoice_date_rules(c, cid, sid):
    # A confirmed transfer must have its own consent date. Never invent one.
    missing = c.execute("""SELECT 1 FROM settlement_lines l JOIN settlements s ON s.id=l.settlement_id
        WHERE l.carry_to_case_id=? AND s.sub_id=? AND l.carry_over>0
        AND (l.consent_date IS NULL OR l.consent_date='') LIMIT 1""", (cid,sid)).fetchone()
    events = active(c, 'e.target_id=? AND e.sub_id=?', (cid,sid), today())
    if missing or any(not e['consent_date'] for e in events):
        raise runtime().Err('充当承諾日の記録がありません。承諾日を確認・登録してください',409)
    rows = confirmed_allocations(c,cid,sid,today())
    rows = [r for r in rows if r.get('consent_date') and r['consent_date']<=today() and r.get('record')]
    minimum = max((r['consent_date'] for r in rows), default=None)
    return dict(min_issued_date=minimum, max_issued_date=today(),
                default_issued_date=minimum or today(), allocations=rows)


def validate_invoice_date(value, rows):
    if any(not r.get('consent_date') for r in rows):
        raise runtime().Err('充当承諾日を確認・登録してください',409)
    minimum = max((r['consent_date'] for r in rows), default='')
    if value > today() or (minimum and value < minimum):
        raise runtime().Err('発行日は最新の充当承諾日以降、本日までの日付を選択してください')


def recorded_allocation_consents(c, rows):
    result = []
    for original in rows:
        row = dict(original)
        if not row.get('consent_date'):
            ident = str(row.get('id', ''))
            if ident.startswith('legacy-'):
                saved = c.execute('SELECT consent_date FROM settlement_lines WHERE id=?', (ident[7:],)).fetchone()
            else:
                saved = c.execute('SELECT consent_date FROM funding_events WHERE id=?', (ident,)).fetchone()
            row['consent_date'] = saved['consent_date'] if saved else None
        result.append(row)
    return result


def invoice_values(c, cid, sid, cutoff=None):
    rules = invoice_date_rules(c,cid,sid)
    cutoff = cutoff or rules['default_issued_date']
    if cutoff < today():
        for change in c.execute('SELECT before_json FROM carry_change_history WHERE sub_id=? AND substr(changed_at,1,10)>?', (sid,cutoff)):
            before = json.loads(change['before_json'])
            confirmed = before.get('carry_confirmed_date') or before.get('consent_date')
            if before.get('carry_to_case_id') == int(cid) and before.get('carry_over') and confirmed and confirmed <= cutoff:
                raise runtime().Err('発行日後に変更・取消された充当履歴があります。発行日と訂正内容を確認してください',409)
    sub = c.execute('SELECT * FROM case_subs WHERE case_id=? AND sub_id=?',(cid,sid)).fetchone()
    if not sub:
        raise runtime().Err('案件に登録されていない下請けです')
    rows = rules['allocations']
    validate_invoice_date(cutoff,rows)
    principal = sum(x['amount'] for x in rows if x['kind']=='principal')
    hold = sum(x['amount'] for x in rows if x['kind']=='hold')
    raw = sub['principal_tax_in'] - principal - hold
    return dict(principal=sub['principal_tax_in'], allocated_principal=principal, allocated_hold=hold,
                allocations=rows, invoice_amount=max(0,raw), excess=max(0,-raw), allocation_as_of=cutoff,
                min_issued_date=rules['min_issued_date'], max_issued_date=rules['max_issued_date'],
                default_issued_date=rules['default_issued_date'])
