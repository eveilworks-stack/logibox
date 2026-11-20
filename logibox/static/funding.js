function fundingPanel(cid,slot,s,data={}){
 const mode=s?.funding_method||((data.principal_sources||[]).length?'carry':(data.custody_sources||[]).length?'hold':'cash');
 return `<section class="funding-panel" id="funding-${slot}">
 <h3>下請け元金の登録・充当</h3><p class="funding-case"><b>${esc(CUR.v.case.case_no||'案件番号未登録')}</b> ${esc(CUR.v.case.name)}<br><span>${esc(s?.sub_name||data.sub_name||'下請けを選択してください')}</span></p>
 <div id="funding-summary-${slot}" class="funding-summary" aria-live="polite"></div>
 <label class="f">入力方法<select id="s${slot}_funding" onchange="toggleFunding(${slot})">${[['cash','① 新規入金のみ'],['carry','② 他案件から充当'],['hold','③ 一時預かり金から充当'],['mixed','④ 併用（複数の資金）']].map(([k,l])=>`<option value="${k}" ${mode===k?'selected':''}>${l}</option>`).join('')}</select></label>
 ${cashPanel(slot,s,data,mode)}
 <section id="funding-input-${slot}" ${mode==='cash'?'hidden':''}><div id="funding-entries-${slot}">${fundingCandidates(slot,data,mode)}</div>
 <p class="m funding-empty" ${fundingSources(data).some(x=>mode==='mixed'||(mode==='carry'?x.source_type==='principal':x.source_type!=='principal'))?'hidden':''}>利用可能な資金がありません。実入金・回収確認と履歴・詳細を確認してください。</p>
 <details class="funding-consent"><summary>充当の承諾・備考（保存時に入力）</summary><div class="g">${fld('承諾日',`s${slot}_consent_date`,new Date().toLocaleDateString('sv-SE'),{type:'date'})}${fld('承諾方法',`s${slot}_consent_method`,'')}${fld('承諾記録',`s${slot}_consent_record`,'')}${fld('備考',`s${slot}_allocation_note`,'')}</div></details></section>
 <div id="funding-totals-${slot}" class="funding-totals" aria-live="polite"></div>
 ${pendingPlansPanel(slot,data)}
 ${fundingDetails(cid,slot,s,data)}
 </section>`;
}
function fundingSources(data){return [...(data?.principal_sources||[]),...(data?.custody_sources||[])];}
function fundingCandidates(slot,data,mode){return fundingSources(data).map(x=>{
 const type=x.source_type||'principal',key=type+':'+(type==='principal'?x.case_id:x.id),hidden=mode==='cash'||(mode==='carry'&&type!=='principal')||(mode==='hold'&&type==='principal');
 return `<div class="funding-entry" data-source="${key}" data-type="${type}" data-balance="${x.available}" data-request-key="${crypto.randomUUID()}" ${hidden?'hidden':''}>
 <label><input type="checkbox" name="selected" ${data.locked?'disabled':''} onchange="selectFundingSource(this,${slot})"><span><b>${esc(x.case_no||'案件番号未登録')} ${esc(x.name)}</b><small>${type==='principal'?'他案件の元金':'一時預かり金'+(x.reason?'・'+esc(x.reason):'')} ／ 利用可能 ${Y(x.available)}</small></span></label>
 <label class="f">充当額（円）<input name="carry_over" inputmode="numeric" disabled oninput="fundingTotals(${slot})" aria-label="${esc(x.case_no||'元案件')}の充当額"></label></div>`;
 }).join('');}
function selectedFundingEntries(slot){return [...document.querySelectorAll(`#funding-entries-${slot} .funding-entry`)].filter(x=>!x.hidden&&x.querySelector('[name=selected]').checked);}
function fundingYen(value){return Number(String(value||'').replaceAll(',',''));}
function selectFundingSource(input,slot){const box=input.closest('.funding-entry'),amount=box.querySelector('[name=carry_over]');amount.disabled=!input.checked;if(input.checked&&!amount.value){const data=CUR.funding[slot]||{},other=selectedFundingEntries(slot).filter(x=>x!==box).reduce((n,x)=>n+fundingYen(x.querySelector('[name=carry_over]').value),0);amount.value=Math.max(0,Math.min(Number(box.dataset.balance),fundingYen(G(`s${slot}_pr`))-(data.confirmed_total||0)-(data.cash_planned||0)-other));}fundingTotals(slot);}
function toggleFunding(slot){const mode=G(`s${slot}_funding`);document.getElementById(`funding-input-${slot}`).hidden=mode==='cash';document.getElementById(`funding-cash-${slot}`).hidden=['carry','hold'].includes(mode);for(const x of document.querySelectorAll(`#funding-entries-${slot} .funding-entry`)){x.hidden=mode==='cash'||(mode==='carry'&&x.dataset.type!=='principal')||(mode==='hold'&&x.dataset.type==='principal');}const empty=document.querySelector(`#funding-input-${slot} .funding-empty`);if(empty)empty.hidden=[...document.querySelectorAll(`#funding-entries-${slot} .funding-entry`)].some(x=>!x.hidden);fundingTotals(slot);}
function cashPanel(slot,s,data,mode){return `<section class="funding-cash" id="funding-cash-${slot}" data-request-key="${crypto.randomUUID()}" ${['carry','hold'].includes(mode)?'hidden':''}><div class="g">
 <label class="f">入金方法<select id="s${slot}_cash_method" onchange="toggleCashConfirmation(${slot})"><option value="bank">銀行振込</option><option value="hand">手渡し</option></select></label>
 ${fld('金額（円）',`s${slot}_cash_amount`,'',{num:1}).replace('<input',`<input oninput="fundingTotals(${slot})"`)}${fld('入金予定日／確認日',`s${slot}_cash_date`,'',{type:'date'})}${fld('備考',`s${slot}_cash_note`,'')}</div>
 <label class="funding-check"><input type="checkbox" id="s${slot}_cash_confirmed" onchange="toggleCashConfirmation(${slot})">実入金を確認済み</label>
 <div id="cash-hand-${slot}" hidden><label class="funding-check"><input type="checkbox" id="s${slot}_receipt">領収書を確認</label><label class="funding-check"><input type="checkbox" id="s${slot}_stamp">収入印紙の要否・貼付を確認</label></div><p class="m">未確認なら入金予定として保存し、未充足額は減りません。</p></section>`;}
function toggleCashConfirmation(slot){document.getElementById(`cash-hand-${slot}`).hidden=G(`s${slot}_cash_method`)!=='hand'||!document.getElementById(`s${slot}_cash_confirmed`).checked;fundingTotals(slot);}
function fundingTotals(slot){
 const data=CUR.funding[slot]||{},pr=fundingYen(G(`s${slot}_pr`)),cash=data.cash_received||0,carry=data.confirmed_allocated||0,confirmed=data.confirmed_total||0;
 const selected=selectedFundingEntries(slot),pending=selected.reduce((n,x)=>n+fundingYen(x.querySelector('[name=carry_over]').value),0);
 const visible=!document.getElementById(`funding-cash-${slot}`).hidden,inputCash=visible?fundingYen(G(`s${slot}_cash_amount`)):0,checked=document.getElementById(`s${slot}_cash_confirmed`).checked;
 const available=data.locked?0:Math.min(Math.max(0,pr-confirmed),fundingSources(data).reduce((n,x)=>n+x.available,0));
 document.getElementById(`funding-summary-${slot}`).innerHTML=[['担当元金',pr],['充当可能額',available],['充当済額',carry],['未充足額',Math.max(0,pr-confirmed)]].map(([label,value])=>`<div><span>${label}</span><strong>${Y(value)}</strong></div>`).join('');
 const held=(data.ledger_incoming||[]).filter(x=>x.kind==='hold').reduce((n,x)=>n+x.amount,0);
 document.getElementById(`funding-totals-${slot}`).innerHTML=`<details><summary>金額内訳を見る</summary>${row('他案件から充当（確定）',Y(Math.max(0,carry-held)))}${row('一時預かり金から充当（確定）',Y(held))}${row('新規入金確認済み',Y(cash))}${row('新規入金予定（未確認）',Y((data.cash_planned||0)+(checked?0:inputCash)))}${row('確定済み資金合計',Y(confirmed))}${row('下請け全体の充当可能額',Y(data.pool_available||0))}</details>`+(pending||inputCash?`<p class="funding-preview">今回の入力：充当 ${Y(pending)} ／ ${checked?'確認済み入金':'入金予定'} ${Y(inputCash)}<br>保存後の未充足額（見込み）：${Y(Math.max(0,pr-confirmed-pending-(checked?inputCash:0)))}</p>`:'');
}
function fundingValues(slot){
 const funding_method=G(`s${slot}_funding`),selected=selectedFundingEntries(slot);
 const consent={consent_date:G(`s${slot}_consent_date`),consent_method:G(`s${slot}_consent_method`),consent_record:G(`s${slot}_consent_record`),note:G(`s${slot}_allocation_note`)};
 if(selected.length&&(!consent.consent_date||!consent.consent_method||!consent.consent_record)){document.querySelector(`#funding-${slot} .funding-consent`).open=true;throw new Error('充当の承諾日・方法・記録を入力してください');}
 const allocations=selected.map(box=>{const [source_type,source_id]=box.dataset.source.split(':');return {...consent,source_type,source_id,carry_over:box.querySelector('[name=carry_over]').value,request_key:box.dataset.requestKey};});
 const amount=G(`s${slot}_cash_amount`),cash_entry=!['carry','hold'].includes(funding_method)&&amount?{amount,received_on:G(`s${slot}_cash_date`),method:G(`s${slot}_cash_method`),confirmed:document.getElementById(`s${slot}_cash_confirmed`).checked,receipt_checked:document.getElementById(`s${slot}_receipt`).checked,stamp_checked:document.getElementById(`s${slot}_stamp`).checked,note:G(`s${slot}_cash_note`),request_key:document.getElementById(`funding-cash-${slot}`).dataset.requestKey}:null;
 return {funding_method,allocations,cash_entry};
}
function pendingPlansPanel(slot,data){return (data.cash_plans||[]).filter(x=>x.status==='planned').map(x=>`<details class="funding-plan" id="cash-plan-${x.id}" data-request-key="${crypto.randomUUID()}"><summary>未確認の入金予定 ${Y(x.amount)}・${x.method==='bank'?'銀行振込':'手渡し'}</summary><p>${esc(x.expected_on||'予定日未登録')} ／ ${esc(x.note)}</p><label class="f">実入金日<input type="date" id="plan_${x.id}_date"></label><label class="funding-check"><input type="checkbox" id="plan_${x.id}_confirmed">実入金を確認しました</label>${x.method==='hand'?`<label class="funding-check"><input type="checkbox" id="plan_${x.id}_receipt">領収書を確認</label><label class="funding-check"><input type="checkbox" id="plan_${x.id}_stamp">収入印紙の要否・貼付を確認</label>`:''}<div class="acts"><button type="button" class="s" onclick="confirmCashPlan(${x.id},${slot})">実入金として確定</button><button type="button" class="s" onclick="cancelCashPlan(${x.id},${slot})">予定を取り消す</button></div></details>`).join('');}
function confirmCashPlan(id,slot){act(async()=>{await api('POST',`/api/cash-plans/${id}/action`,{action:'confirmed',confirmed:document.getElementById(`plan_${id}_confirmed`).checked,received_on:G(`plan_${id}_date`),receipt_checked:!!document.getElementById(`plan_${id}_receipt`)?.checked,stamp_checked:!!document.getElementById(`plan_${id}_stamp`)?.checked,request_key:document.getElementById(`cash-plan-${id}`).dataset.requestKey});await rf()},'実入金を確認しました');}
function cancelCashPlan(id,slot){const note=prompt('入金予定の取消理由');if(!note)return;act(async()=>{await api('POST',`/api/cash-plans/${id}/action`,{action:'cancelled',note,request_key:document.getElementById(`cash-plan-${id}`).dataset.requestKey});await rf()},'予定を取り消しました');}
function fundingCashHistory(slot,s,data){return `<details><summary>保存済み入金履歴・入金予定</summary>${(data?.cash_history||[]).map(x=>`<p>${esc(x.received_on)} ／ ${x.method==='hand'?'手渡し':'銀行振込'} ／ ${Y(x.amount)} ／ ${esc(x.note)}</p>`).join('')}${(data?.cash_plans||[]).map(x=>`<p>予定 ${Y(x.amount)} ／ ${esc(x.expected_on||'―')} ／ ${{planned:'未確認',confirmed:'確認済み',cancelled:'取消済み'}[x.status]} ／ ${esc(x.note)}</p>`).join('')}${s?.principal_received_date?`<p>旧入金記録 ${esc(s.principal_received_date)}</p>`:''}</details>`;}
function fundingDetails(cid,slot,s,data){
 const release=data?.release,balance=data?.balance,account=balance?.account,incoming=data?.incoming||[];
 return `<details class="funding-details"><summary>履歴・詳細を見る</summary>${fundingCashHistory(slot,s,data)}
 ${ledgerPanel(cid,slot,s,data)}
 ${incoming.map(x=>`<section class="card"><b>${esc(x.case_no||'案件番号未登録')} ${esc(x.name)}</b><p>${Y(x.carry_over)} ／ 承諾日 ${esc(x.consent_date||'―')} ／ ${esc(x.consent_method||'未登録')}</p><p>${esc(x.consent_record||'')}</p><p class="m">充当確定日 ${esc(x.carry_confirmed_date||'旧記録：未登録')} ／ 作成 ${esc(x.carry_created_at||'旧記録：未登録')}</p><button class="s" type="button" onclick="editFunding(${cid},${slot},${x.settlement_id},${x.id})">編集・取消</button><div id="funding-edit-${x.id}"></div>${carryHistoryBlock(x)}</section>`).join('')}
 ${(data?.history||[]).length?`<details><summary>元金充当履歴（取消・付替えを含む）</summary>${data.history.map(h=>`<p><b>${esc(new Date(h.changed_at).toLocaleString('ja-JP'))}</b> ${esc(h.reason)}</p><p>変更前：${esc(fundingHistoryLabel(h.before))}</p><p>変更後：${esc(fundingHistoryLabel(h.after))}</p>`).join('')}</details>`:''}
 ${s?`<details><summary>ハークからの元金回収・個別返金の確認</summary><p class="m">回収額は利益を除いた、この下請けに帰属する元金のみです。一部入金ではハーク画面に実入金累計額を保存してから登録してください。返金累計は月次精算以外で既に返金した元金です。取消・訂正は累計額を修正し、理由を残します。</p>
 ${row('元金残高',Y(balance?.balance||0))}${row('うち充当可能額',Y(balance?.available||0))}
 ${fld('回収確認済み元金累計（円）',`s${slot}_recovered`,balance?.recovered||0,{num:1})}${fld('個別返金済み元金累計（円）',`s${slot}_refunded`,balance?.refunded||0,{num:1})}${fld('確認・返金日',`s${slot}_account_date`,account?.record_date||new Date().toLocaleDateString('sv-SE'),{type:'date'})}${fld('確認記録・変更理由',`s${slot}_account_note`,'')}
 <button class="s" type="button" onclick="savePrincipalAccount(${cid},${slot},${account?.revision||0})">回収・返金を保存</button>
 ${(data?.account_history||[]).map(h=>{const a=JSON.parse(h.after_json);return `<p>${esc(new Date(h.changed_at).toLocaleString('ja-JP'))} ／ ${esc(a.record_date)} ／ 回収 ${Y(a.recovered)} ／ 返金 ${Y(a.refunded)} ／ ${esc(a.note)}</p>`}).join('')}</details>
 <details><summary>充当額の任意上限（既存の確認記録）</summary><p class="m">ハークから回収確認済みの元金に、追加の上限を設定できます。未回収元金は指定しても充当できません。</p>
 ${fld('充当へ回せる確認済額（円）',`s${slot}_release`,release?.amount||0,{num:1})}${fld('残高の確認記録',`s${slot}_release_record`,release?.record||'')}
 ${row('この案件からの充当済額',Y(data?.source_allocated||0))}${row('この案件の確認済残高（登録済明細は編集で調整）',Y(data?.source_remaining||0))}
 <button class="s" type="button" onclick="savePrincipalRelease(${cid},${slot},${release?.revision||0})">確認済額を保存</button></details>`:'<p class="m">充当元としての残高確認は下請けを保存後に登録できます。</p>'}
 </details>`;
}
function savePrincipalAccount(cid,slot,revision){act(async()=>{await api('PUT',`/api/cases/${cid}/subs/${slot}/principal-account`,{recovered:G(`s${slot}_recovered`),refunded:G(`s${slot}_refunded`),record_date:G(`s${slot}_account_date`),note:G(`s${slot}_account_note`),expected_revision:revision});await rf()},'回収・返金を保存しました');}
function consentInputs(prefix){return `${fld('承諾日',prefix+'_date','',{type:'date'})}${fld('承諾方法',prefix+'_method','')}${fld('承諾記録',prefix+'_record','')}`;}
function consentValues(prefix){return {consent_date:G(prefix+'_date'),consent_method:G(prefix+'_method'),consent_record:G(prefix+'_record')};}
function ledgerPanel(cid,slot,s,data){
 const labels={created:'預かり発生',principal:'他案件元金充当',hold:'預かり金充当',refund:'返金',cancel:'充当取消'};
 const format=x=>`<p>${esc(labels[x.kind])} ／ ${esc(x.case_no||'案件番号未登録')} ${esc(x.name)} → ${esc(x.target_case_no||'―')} ${esc(x.target_name||'')} ／ ${Y(x.amount)} ／ 確定 ${esc(x.event_date)} ／ 承諾 ${esc(x.consent_date)} ${esc(x.consent_method)} ${esc(x.consent_record)} ／ ${esc(x.note)} ${x.cancelled?'（取消済み）':''}</p>`;
 return `<details><summary>保存済み元金充当・預かり金履歴</summary>${(data?.ledger_history||[]).map(x=>`${format(x)}${['principal','hold'].includes(x.kind)&&!x.cancelled?`<button type="button" class="s" onclick="openLedgerCancel(${x.id},${slot})">訂正・取消</button><div id="ledger-cancel-${slot}-${x.id}"></div>`:''}`).join('')||'<p>履歴はありません</p>'}</details>
 <details><summary>一時預かり金台帳（同一下請け）</summary><a href="/api/export/funding">元金・預かり金履歴CSV</a>${(data?.custody||[]).map(f=>`<section class="card"><b>#${f.id} ${esc(f.case_no||'案件番号未登録')} ${esc(f.name)}</b><p>${esc(f.sub_name)} ／ ${esc(f.reason)} ／ 発生 ${esc(f.occurred_on)}</p>${row('当初金額',Y(f.amount))}${row('残高',Y(f.balance))}${row('充当済',Y(f.allocated))}${row('返金済',Y(f.refunded))}<details><summary>発生・充当・返金履歴</summary>${f.history.map(format).join('')}</details>${f.balance?`<button type="button" class="s" onclick="openCustodyRefund(${f.id},${slot})">返金を記録</button><div id="custody-refund-${slot}-${f.id}"></div>`:''}</section>`).join('')||'<p>台帳登録はありません</p>'}</details>
 ${s?`<details><summary>この元金を一時預かり金へ振替・既存預かり金を台帳へ登録</summary><p class="m">実入金・回収確認済みの元金を振り替えます。発生理由と下請けの承諾を記録してください。台帳登録後はこの台帳から充当・返金します。</p><div class="g">${fld('預かり金額（円）',`hold_${slot}_amount`,'',{num:1})}${fld('発生日',`hold_${slot}_occurred`,'',{type:'date'})}${fld('発生理由',`hold_${slot}_reason`,'')}${consentInputs('hold_'+slot)}</div><button type="button" class="s" onclick="createCustody(${cid},${slot})">預かり金台帳へ登録</button></details>`:''}`;
}
function createCustody(cid,slot){act(async()=>{await api('POST',`/api/cases/${cid}/subs/${slot}/custody`,{amount:G(`hold_${slot}_amount`),occurred_on:G(`hold_${slot}_occurred`),reason:G(`hold_${slot}_reason`),...consentValues('hold_'+slot)});await rf()},'一時預かり金を登録しました');}
function openCustodyRefund(id,slot){document.getElementById(`custody-refund-${slot}-${id}`).dataset.requestKey=crypto.randomUUID();const p=`refund_${slot}_${id}`;document.getElementById(`custody-refund-${slot}-${id}`).innerHTML=`<div class="g">${fld('返金額（円）',p+'_amount','',{num:1})}${fld('返金日',p+'_event','',{type:'date'})}${fld('返金先・振込記録',p+'_note','')}${consentInputs(p)}</div><button type="button" class="s" onclick="saveCustodyRefund(${id},${slot})">返金履歴を保存</button>`;}
function saveCustodyRefund(id,slot){const p=`refund_${slot}_${id}`;act(async()=>{await api('POST',`/api/custody/${id}/refund`,{amount:G(p+'_amount'),event_date:G(p+'_event'),note:G(p+'_note'),request_key:document.getElementById(`custody-refund-${slot}-${id}`).dataset.requestKey,...consentValues(p)});await rf()},'返金履歴を保存しました');}
function openLedgerCancel(id,slot){const p=`cancel_${slot}_${id}`;document.getElementById(`ledger-cancel-${slot}-${id}`).innerHTML=`<p>訂正は取消後に正しい内容を追加してください。元の履歴は残ります。</p><div class="g">${fld('取消理由',p+'_note','')}${consentInputs(p)}</div><button type="button" class="s" onclick="cancelLedger(${id},${slot})">充当を取り消す</button>`;}
function cancelLedger(id,slot){const p=`cancel_${slot}_${id}`;act(async()=>{await api('POST',`/api/funding-events/${id}/cancel`,{note:G(p+'_note'),...consentValues(p)});await rf()},'充当を取り消しました');}
function fundingHistoryLabel(x){return `${x.source_case_no||'案件番号未登録'} ${x.source_name||''} → ${x.carry_over?(x.target_case_no||'案件番号未登録'):'充当なし'} ${x.target_name||''} ／ ${Y(x.carry_over)} ／ 承諾日 ${x.consent_date||'―'} ／ ${x.consent_method||'未登録'} ／ ${x.consent_record||'―'}`;}
const fundingRefreshRequests={};
async function refreshFunding(cid,slot){
 const request=(fundingRefreshRequests[slot]||0)+1;fundingRefreshRequests[slot]=request;
 const sid=G(`s${slot}_id`); if(!sid){CUR.funding[slot]={};document.getElementById(`funding-${slot}`).outerHTML=fundingPanel(cid,slot,null,{});fundingTotals(slot);return;}
 act(async()=>{const data=await api('GET',`/api/cases/${cid}/funding-options?sub_id=${encodeURIComponent(sid)}`);if(CUR.v?.case.id!==cid||fundingRefreshRequests[slot]!==request||G(`s${slot}_id`)!==sid)return;CUR.funding[slot]=data;
 const s=CUR.v.subs.find(x=>x.slot===slot&&String(x.sub_id)===sid);
 document.getElementById(`funding-${slot}`).outerHTML=fundingPanel(cid,slot,s,data);fundingTotals(slot);});
}
function savePrincipalRelease(cid,slot,revision){act(async()=>{await api('PUT',`/api/cases/${cid}/subs/${slot}/principal-release`,{amount:G(`s${slot}_release`),record:G(`s${slot}_release_record`),expected_revision:revision});await rf()},'確認済額を保存しました');}
function editFunding(cid,slot,id,lineId){act(async()=>{
 const period=CUR.funding[slot].incoming.find(x=>x.id===lineId).period;
 const monthlyData=await api('GET',`/api/monthly?period=${encodeURIComponent(period)}`);
 const s=monthlyData.settlements.find(x=>x.id===id),line=s.lines.find(x=>x.id===lineId);
 const host=document.getElementById(`funding-edit-${lineId}`);
 host.innerHTML=`<form><div class="g"><label class="f">充当先案件<select name="target_id" onchange="changeCarryTarget(this)"><option value="">案件を選択</option><option value="hold" ${line.temporary_hold?'selected':''}>一時預かり（充当先を解除）</option>${s.targets.filter(x=>x.id!==line.case_id&&x.due_date>line.due_date).map(x=>`<option value="${x.id}" ${x.id===line.carry_to_case_id?'selected':''}>${esc(x.case_no||'案件番号未登録')} ${esc(x.name)}</option>`).join('')}</select></label><label class="f">充当・一時預かり額（円、0円で解除・返金）<input name="carry_over" value="${line.carry_over}" ${s.status==='paid'?'readonly':''} required></label><label class="f">承諾日<input type="date" name="consent_date" value="${esc(line.consent_date||'')}"></label><label class="f">承諾方法<input name="consent_method" maxlength="80" value="${esc(line.consent_method||'')}"></label><label class="f">承諾記録<input name="consent_record" maxlength="500" value="${esc(line.consent_record||'')}"></label><label class="f">変更理由<input name="change_reason" maxlength="500" required></label></div><p class="m">一時預かりはLINEで了承を得てから承諾日を入力してください。承諾記録は任意です。了承依頼文は月次精算の明細から作成できます。支払済みの金額変更や、後続記録と矛盾する変更はできません。</p><button>充当・一時預かりを保存</button></form>`;
 host.querySelector('form').onsubmit=event=>{event.preventDefault();const values=Object.fromEntries(new FormData(event.currentTarget));act(async()=>{await api('PUT',`/api/settlements/${id}/lines/${lineId}`,{...values,expected_revision:line.carry_revision});await rf()},'充当を更新しました');};
 });}
