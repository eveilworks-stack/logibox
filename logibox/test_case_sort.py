"""Exercise the actual browser comparator with missing dates and progress stages."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser comparator checks')
class CaseSortTests(unittest.TestCase):
    def test_due_dates_progress_ties_and_input_preservation(self):
        html=(Path(__file__).parent/'static/index.html').read_text()
        function=html[html.index('function sortCaseRows('):html.index('async function viewCases(')]
        script=function+r'''
const assert=require('assert');
const rows=[
 {mgmt_no:'26-0001',due_date:'2026-11-30',progress:'入金済'},
 {mgmt_no:'26-0002',due_date:null,progress:'登録'},
 {mgmt_no:'26-0003',due_date:'2026-11-20',progress:'仕入済'},
 {mgmt_no:'26-0004',due_date:'2026-11-20',progress:'請求額確定'},
 {mgmt_no:'26-0005',due_date:'2026-11-25',progress:'登録'}];
const ids=order=>sortCaseRows(rows,order).map(x=>x.mgmt_no.slice(-1));
assert.deepStrictEqual(ids('due_asc'),['4','3','5','1','2']);
assert.deepStrictEqual(ids('due_desc'),['1','5','4','3','2']);
assert.deepStrictEqual(ids('progress_asc'),['5','2','3','4','1']);
assert.deepStrictEqual(ids('progress_desc'),['1','4','3','5','2']);
assert.deepStrictEqual(ids('newest'),['5','4','3','2','1']);
assert.deepStrictEqual(rows.map(x=>x.mgmt_no.slice(-1)),['1','2','3','4','5']);
assert.deepStrictEqual(sortCaseRows([],'due_asc'),[]);
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
