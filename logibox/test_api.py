import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
tmp = tempfile.mkdtemp()
env = dict(os.environ, APP_DATA=tmp)
# Explicitly create a synthetic fixture; the update-only server refuses missing databases.
subprocess.run([sys.executable, '-c', 'import server; server.init()'], env=env, check=True)
p = subprocess.Popen([sys.executable, 'server.py', '--port', '8799', '--no-browser'], env=env, stdout=subprocess.DEVNULL)
for attempt in range(150):
    if p.poll() is not None:
        raise RuntimeError('test server exited during startup')
    try:
        urllib.request.urlopen('http://127.0.0.1:8799/api/state', timeout=.2).close()
        break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(.1)
else:
    raise RuntimeError('test server startup timed out')
cookie = ''
def api(m, u, b=None, raw=False):
    global cookie
    req = urllib.request.Request('http://127.0.0.1:8799' + u, method=m, data=json.dumps(b).encode() if b is not None else None,
                                 headers={'X-Requested-With': 'app', 'Content-Type': 'application/json', 'Cookie': cookie})
    try:
        r = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    sc = r.headers.get('Set-Cookie')
    if sc and 'Max-Age=0' not in sc:
        cookie = sc.split(';')[0]
    d = r.read()
    return r.status, (d if raw else json.loads(d))
try:
    assert api('GET', '/api/state')[1] == {'setup': False, 'logged': False}
    assert api('POST', '/api/setup', {'password': 'short'})[0] == 400
    assert api('POST', '/api/setup', {'password': 'password123'})[0] == 200
    assert api('GET', '/api/cases')[0] == 200
    cookie_ok = cookie
    cookie = ''
    assert api('GET', '/api/cases')[0] == 401
    assert api('POST', '/api/login', {'password': 'wrong'})[0] == 401
    assert api('POST', '/api/login', {'password': 'password123'})[0] == 200
    s, r = api('POST', '/api/cases', {'name': '検証用商品A・商品B', 'planned_rate_pct': 7, 'due_date': '2026-10-31'})
    assert r['mgmt_no'].endswith('-0001'), r
    cid = r['id']
    assert api('POST', '/api/cases', {'name': '', 'planned_rate_pct': 7})[0] == 400
    api('POST', '/api/subcontractors', {'name': '検証下請けA'})
    assert api('POST', f'/api/cases/{cid}/purchases', {'supplier_id': 1, 'amount_tax_in': '35,017,990'})[0] == 200
    assert api('PUT', f'/api/cases/{cid}/subs/1', {'sub_id': 1, 'principal_tax_in': 2000000})[0] == 200
    assert api('PUT', f'/api/cases/{cid}/subs/2', {'sub_id': 1, 'principal_tax_in': 1})[0] == 400
    v = api('GET', f'/api/cases/{cid}')[1]
    assert v['calc']['basis_amount'] == 37469249 and not v['calc']['confirmed'], v['calc']
    assert v['subs'][0]['sub_profit'] == 100000 and v['subs'][0]['fee'] == 40000
    assert v['subs'][0]['settle_date_auto'] == '2026-11-15'
    assert api('PUT', f'/api/cases/{cid}/haak', {'confirmed_amount_tax_in': 37819429})[0] == 200
    c = api('GET', f'/api/cases/{cid}')[1]['calc']
    assert c['own_profit'] == 2701439 and c['over_planned'] == 350180 and c['own_base'] + 40000 + 350180 == 2701439, c
    assert api('GET', '/api/dashboard')[1]['totals']['sub_unsettled'] == 2100000
    assert api('POST', f'/api/cases/{cid}/purge', {'confirm': 'x'})[0] == 400
    check = api('GET', f'/api/cases/{cid}/deletion-check')[1]
    assert not check['can_delete'] and any('仕入#' in reason for reason in check['reasons'])
    assert api('POST', f'/api/cases/{cid}/trash')[0] == 409
    # 財務記録がある案件を残し、関連記録のない案件で削除・復元を確認する。
    cid = api('POST', '/api/cases', {'name': '削除確認用', 'planned_rate_pct': 7})[1]['id']
    assert api('POST', f'/api/cases/{cid}/trash')[0] == 200
    assert api('PUT', f'/api/cases/{cid}', {'name': 'x', 'planned_rate_pct': 7})[0] == 400
    assert len(api('GET', '/api/cases')[1]) == 1 and len(api('GET', '/api/trash')[1]) == 1
    assert api('POST', f'/api/cases/{cid}/restore')[0] == 200
    api('POST', f'/api/cases/{cid}/trash')
    mg = api('GET', '/api/trash')[1][0]['mgmt_no']
    assert api('POST', f'/api/cases/{cid}/purge', {'confirm': mg})[0] == 200
    assert api('POST', '/api/cases', {'name': '次', 'planned_rate_pct': 7})[1]['mgmt_no'].endswith('-0003')
    assert api('GET', '/api/backup', raw=True)[1][:6] == b'SQLite'
    assert api('POST', '/api/password', {'current': 'password123', 'new': 'newpassword1'})[0] == 200
    assert api('POST', '/api/cases', None)[0] == 401
    assert api('POST', '/api/login', {'password': 'newpassword1'})[0] == 200
    assert api('POST', '/api/cases', None)[0] == 400
    source = api('POST','/api/cases',dict(name='HTTP充当元',case_no='HTTP-SP1',due_date='2020-11-20',planned_rate_pct=7))[1]['id']
    target = api('POST','/api/cases',dict(name='HTTP充当先',case_no='HTTP-SP2',due_date='2020-12-20',planned_rate_pct=7))[1]['id']
    assert api('PUT',f'/api/cases/{source}/subs/1',dict(sub_id=1,principal_tax_in=100000,principal_received_date='2020-11-01'))[0]==200
    assert api('PUT',f'/api/cases/{target}/subs/1',dict(sub_id=1,principal_tax_in=100000))[0]==200
    assert api('GET',f'/api/cases/{target}/funding-options?sub_id=1')[1]['sources']==[]
    assert api('PUT',f'/api/cases/{source}/subs/1/principal-release',dict(amount=60000,record='未回収は不可',expected_revision=0))[0]==400
    assert api('PUT',f'/api/cases/{source}/haak',dict(confirmed_amount_tax_in=107000,confirmed_checked=True,paid_date='2020-11-20'))[0]==200
    assert api('PUT',f'/api/cases/{source}/subs/1/principal-release',dict(amount=60000,record='実在資金と充当可能額の確認',expected_revision=0))[0]==200
    assert api('GET',f'/api/cases/{target}/funding-options?sub_id=1')[1]['sources'][0]['available']==60000
    body=dict(sub_id=1,principal_tax_in=100000,funding_method='mixed',allocations=[dict(source_id=source,carry_over=50000,consent_date='2020-11-20',consent_method='LINE',consent_record='担当者の承諾')])
    assert api('PUT',f'/api/cases/{target}/subs/1',body)[0]==200
    assert api('PUT',f'/api/cases/{target}/subs/1',body)[0]==409
    assert api('GET',f'/api/cases/{target}')[1]['subs'][0]['principal_received_date'] is None
    assert b'fundingPanel' in api('GET','/funding.js',raw=True)[1]
    print('API OK (including principal funding)')
finally:
    p.terminate()
