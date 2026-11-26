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

    def test_header_clicks_restore_order_and_render_all_cases(self):
        html=(Path(__file__).parent/'static/index.html').read_text()
        function=html[html.index('function sortCaseRows('):html.index('const caseInputFields =')]
        script=r'''
const assert=require('assert');
const rows=[
 {id:1,mgmt_no:'26-0001',due_date:'2026-11-30',progress:'入金済'},
 {id:2,mgmt_no:'26-0002',due_date:null,progress:'登録'},
 {id:3,mgmt_no:'26-0003',due_date:'2026-11-20',progress:'仕入済'}];
let EP,READ_PDF,markup,shown,store='newest';
const menu=[['#/cases','案件管理']];
const localStorage={getItem:()=>store,setItem:(key,value)=>store=value};
const buttons=['due','progress'].map(key=>({
 dataset:{caseSortKey:key},sort:'none',indicator:{},
 closest(){return {setAttribute:(name,value)=>this.sort=value}},
 querySelector(){return this.indicator}
}));
const nodes={'#case-rows':{},'#case-pager':{},'#no-cases':{},'#case-profit-filter':{value:''}};
const G=id=>nodes['#'+id].value;
rows.forEach((x,i)=>x.profit_statuses=[i===0?'精算完了':'未精算']);
const $=key=>{assert.ok(nodes[key],key);return nodes[key]};
const document={querySelectorAll:()=>buttons};
const api=async(method,path)=>path==='/api/cases'?rows:{suppliers:[]};
const page=(view,content)=>markup=content;
const purchasePdfTool=()=>'',purchaseForm=()=>'';
const paginate=()=>items=>shown=items.map(x=>x.id);
'''+function+r'''
(async()=>{
 await viewCases();
 assert.ok(!markup.includes('id="case-search"'));
 assert.ok(!markup.includes('id="case-filter"'));
 assert.ok(!markup.includes('id="case-sort"'));
 assert.ok(markup.includes('data-case-sort-key="due"'));
 assert.ok(markup.includes('data-case-sort-key="progress"'));
 assert.deepStrictEqual(shown,[3,2,1]);
 buttons[0].onclick();
 assert.deepStrictEqual(shown,[3,1,2]);
 assert.strictEqual(buttons[0].sort,'ascending');
 assert.strictEqual(buttons[0].indicator.textContent,'↑');
 buttons[0].onclick();
 assert.deepStrictEqual(shown,[1,3,2]);
 assert.strictEqual(buttons[0].sort,'descending');
 buttons[1].onclick();
 assert.deepStrictEqual(shown,[2,3,1]);
 assert.strictEqual(buttons[0].sort,'none');
 buttons[1].onclick();
 assert.deepStrictEqual(shown,[1,3,2]);
 assert.strictEqual(store,'progress_desc');
 await viewCases();
 assert.deepStrictEqual(shown,[1,3,2]);
 assert.strictEqual(buttons[1].sort,'descending');
 store='invalid';await viewCases();assert.deepStrictEqual(shown,[3,2,1]);
 nodes['#case-profit-filter'].value='精算完了';nodes['#case-profit-filter'].onchange();assert.deepStrictEqual(shown,[1]);
 nodes['#case-profit-filter'].value='';
 rows.length=0;await viewCases();assert.strictEqual(nodes['#no-cases'].hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
