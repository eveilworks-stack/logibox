let monthly;
function foldFields(parent,label,ids){
 const fields=ids.map(id=>document.getElementById(id)).filter(Boolean).map(el=>el.closest('.case-maker')||el.closest('.f'));
 if(!fields.length)return;
 const details=document.createElement('details');
 details.className='optional-fields';
 const summary=document.createElement('summary');
 summary.textContent=label;details.append(summary);
 const grid=document.createElement('div');grid.className='g';grid.append(...fields);details.append(grid);
 parent.append(details);
 return details;
}
function simplifyCaseEditor(){
 const basic=$('#c_name').closest('.card');
 foldFields(basic,'詳細設定',['c_start','c_maker','c_lim']);
 setupBasicSummary(basic);
 const flow=$('#panel-basic .card');
 const rows=[...flow.querySelectorAll('.row')];
 const details=document.createElement('details');
 details.innerHTML='<summary>計算の詳細</summary>';
 details.append(...rows.slice(0,3),flow.querySelector('.m'));
 flow.querySelector('h2').textContent='利益';
 flow.append(details);
 if(!CUR.v.purchases.length){
  const heading=flow.querySelector('h2');
  const message=document.createElement('p');
  message.className='m';
  message.textContent='未計算（仕入未登録）。仕入を登録すると利益を表示します。';
  flow.replaceChildren(heading,message);
 }
 flow.before(basic);
 for(const slot of [1,2]){
  const card=document.getElementById('s'+slot+'_id').closest('.card');
  if(!card.querySelector('.funding-panel'))foldFields(card,'詳細設定',['s'+slot+'_fee']);
  document.getElementById('s'+slot+'_set').closest('.f').hidden=true;

 }
 setupSubTabs();
 const haak=$('#h_amt').closest('.card');
 foldFields(haak,'詳細設定',['h_bd','h_items','h_note']);
 const button=document.createElement('button');
 button.textContent='ハーク請求書を作成';
 button.type='button';
 button.onclick=()=>{selectCaseTab('docs');$('#doc_type').value='haak_invoice';invalidateDocumentPreview()};
 if(!CUR.v.case.deleted_at)haak.append(button);
 const form=$('#document_form');
 if(form){
  foldFields(form,'詳細を編集',['doc_method','doc_note','document_item_names']);
  documentFields();
 }
 if(CUR.a.length){
  const list=document.createElement('div');list.className='card';
  list.innerHTML='<h2>受領書類</h2>'+CUR.a.map(file=>attachmentRow(file,true)).join('');
  $('#panel-docs').append(list);
 }
}
function setupBasicSummary(card){
 const heading=card.querySelector('h2');heading.textContent='基本情報';
 const editor=document.createElement('div');editor.id='basic-editor';editor.hidden=true;
 editor.append(...[...card.children].filter(el=>el!==heading));
 const cs=CUR.v.case;
 const summary=document.createElement('dl');summary.className='company-summary';
 summary.innerHTML=[['案件名',cs.name],['案件番号',cs.case_no],['予定利益率',cs.planned_rate_pct==null?'未登録':cs.planned_rate_pct+'%'],['開始日',cs.start_date],['納期',cs.due_date],['メーカー',cs.maker],['税込仕入限度額',cs.purchase_limit_tax_in==null?'未設定':Y(cs.purchase_limit_tax_in)]]
  .map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value??'')||'未登録'}</dd></div>`).join('');
 card.append(summary,editor);
 if(cs.deleted_at)return;
 const edit=document.createElement('button');edit.type='button';edit.className='s';edit.textContent='基本情報を編集';edit.setAttribute('aria-controls',editor.id);edit.setAttribute('aria-expanded','false');
 const setEditing=active=>{summary.hidden=active;edit.hidden=active;editor.hidden=!active;edit.setAttribute('aria-expanded',String(active));};
 edit.onclick=()=>{setEditing(true);$('#c_name').focus()};
 const cancel=document.createElement('button');cancel.type='button';cancel.className='s';cancel.textContent='キャンセル';
 cancel.onclick=()=>{for(const input of editor.querySelectorAll('input,textarea'))input.value=input.defaultValue;for(const detail of editor.querySelectorAll('details'))detail.open=false;setEditing(false);edit.focus()};
 editor.querySelector('.acts').append(cancel);
 const actions=document.createElement('div');actions.className='acts';actions.append(edit);card.append(actions);
}
function documentFields(){
 const type=$('#doc_type')?.value;
 if(!type)return;
 for(const id of ['doc_received','doc_amount','doc_method'])document.getElementById(id).closest('.f').hidden=type!=='receipt';
 $('#doc_sub').closest('.f').hidden=type==='haak_invoice';
 $('#doc_due').closest('.f').hidden=type==='receipt';
 $('#document_item_names').closest('.f').hidden=type!=='principal_invoice';
}
function simplifySettings(migrationCount){
 if(settingsTab==='suppliers')selectSettingsTab('company');
 $('#settings-tab-suppliers').remove();
 $('#settings-panel-suppliers').remove();
 if(!migrationCount){
  $('#settings-tab-migration').hidden=true;
  if(settingsTab==='migration')selectSettingsTab('company');
 }
 const backup=$('#settings-panel-backup');
 const db=backup.querySelector('button[onclick*="backup\')"]');
 if(db){const detail=document.createElement('details');detail.innerHTML='<summary>詳細操作</summary>';db.before(detail);detail.append(db)}
 const company=$('#settings-panel-company');
 const fields=company.querySelector('.g'), actions=company.querySelector('.acts');
 if(!$('#co_name').value.trim())return;
 const summary=document.createElement('dl');
 summary.className='company-summary';
 const fieldsToShow=['co_name','co_invoice_type','co_no','co_contact_name','co_postal_code','co_phone','co_addr','co_email'];
 const bankFields=['co_bank_branch','co_bank_account','co_bank_holder'];
 if($('#co_bank').value&&!bankFields.some(id=>G(id))){
  fieldsToShow.push('co_bank');
 }else{
  fieldsToShow.push(...bankFields);
 }
 summary.innerHTML=fieldsToShow.map(id=>{
  const input=document.getElementById(id);
  const label=id==='co_bank'?'振込先':input.closest('.f').firstChild.textContent;
  const value=input.value?(input.selectedOptions?.[0]?.textContent||input.value):'未登録';
  return `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
 }).join('');
 const edit=document.createElement('button');edit.className='s';edit.textContent='会社情報を編集';
 edit.onclick=()=>{summary.hidden=true;edit.hidden=true;fields.hidden=false;actions.hidden=false;$('#co_name').focus()};
 company.querySelector('.section-head').append(edit);
 fields.before(summary);fields.hidden=true;actions.hidden=true;
}
const settlementLabels={not_requested:'依頼前',requested:'依頼済み',received:'請求書受領済',mismatch:'金額相違',paid:'支払済み'};
const localToday = () => new Date().toLocaleDateString('sv-SE');
const caseLink = (id, no, name, tab='') =>
 `<a href="#/case/${id}${tab?'/'+tab:''}">${esc(no)} ${esc(name)}</a>`;

async function viewMonthly(period=$('#settlement-month')?.value||location.hash.split('?')[1]||localToday().slice(0,7)){
 monthly=await api('GET','/api/monthly?period='+encodeURIComponent(period));
 const {settlements,candidates,pay_date,states}=monthly;
 const groups=[...new Map(candidates.filter(x=>x.tax_mode==='consignment').map(x=>[x.sub_id,x.sub_name]))];
 page('#/monthly',`<h1>月次精算</h1>
 <section class="card"><form class="filter-bar" onsubmit="event.preventDefault();act(()=>viewMonthly(G('settlement-month')))">
 ${fld('対象月（納期）','settlement-month',period,{type:'month'})}<button>表示</button></form>
 <p>月末締め　支払期限 <b>${esc(pay_date)}</b></p>
 ${monthly.periods.length?`<label class="f">対象月の履歴<select onchange="if(this.value)act(()=>viewMonthly(this.value))"><option value="">選択してください</option>${monthly.periods.map(x=>`<option ${x===period?'selected':''}>${esc(x)}</option>`).join('')}</select></label>`:''}</section>
 ${candidates.length?`<section class="card"><h2>未登録の精算候補</h2><div class="table-scroll"><table class="data-table"><thead><tr><th>案件</th><th>下請け</th><th>元金確認済 / 担当元金</th><th>下請け利益</th><th>進捗</th></tr></thead><tbody>
 ${candidates.map(x=>`<tr><td>${caseLink(x.case_id,x.settlement_no||x.mgmt_no,x.case_name,'sub')}</td><td>下請け${x.slot} ${esc(x.sub_name)}</td><td class="amount">${Y(x.principal_confirmed)} / ${Y(x.principal_tax_in)}</td><td class="amount">${Y(x.sub_profit)}</td><td>${x.tax_mode!=='consignment'?'元金設定の移行対象':x.haak_paid_date?'ハーク入金済':'ハーク未入金'}</td></tr>`).join('')}</tbody></table></div>
 <div class="acts">${groups.map(([id,name])=>`<button onclick="createSettlement(${id})">${esc(name)}の明細を登録</button>`).join('')}</div></section>`:''}
 ${settlements.map(s=>`<section class="card"><div class="row"><span><b>${esc(s.sub_name)}</b>　<span class="t ${s.status==='paid'?'g':s.status==='mismatch'?'w':''}">${states[s.status]}</span></span><span class="amount">${Y(s.total)}</span><button class="s" onclick="openSettlement(${s.id})">明細・履歴</button></div>
 <p class="m">${s.lines.length}案件　期日 ${esc(s.pay_date)}${s.paid_at?'　完了 '+esc(s.paid_at):''}</p></section>`).join('')}
 ${!settlements.length&&!candidates.length?'<p class="card">この月に該当する精算はありません。</p>':''}`);
}

function createSettlement(sub_id){
 if(!confirm('対象月の精算明細を登録しますか？'))return;
 act(async()=>{await api('POST','/api/settlements',{period:monthly.period,sub_id});await viewMonthly(monthly.period)},'明細を登録しました');
}

function openSettlement(id){
 const s=monthly.settlements.find(x=>x.id===id);
 const draft=s.status==='not_requested';
 const upload=['requested','received','mismatch'].includes(s.status);
 const pay=s.status==='received'||(s.status==='requested'&&s.total===0);
 const dialog=openDocDialog('settlement-dialog',`
 <header class="editor-head editor-title"><div><h2 id="settlement-title">${esc(s.sub_name)}　${esc(s.period)}の精算</h2><span class="t">${monthly.states[s.status]}</span></div><button class="s" type="button" onclick="this.closest('dialog').close()">閉じる</button></header>
 <div class="editor-body">
 <p>支払期限 <b>${esc(s.pay_date)}</b>　振込額 <b class="amount">${Y(s.total)}</b></p>
 ${s.request_needs_revision?'<p class="purchase-warning" role="alert">充当内容が依頼時点から変更されています。送付済み文面・発行済みPDFは変更していません。下請けへ訂正内容を連絡し、受領請求書の金額・明細も確認してください。</p>':''}
 <p class="m">依頼前 → 依頼済み → 請求書受領済 → 支払済み。文面・ひな形の生成だけでは依頼済みになりません。実際に送付した後で「依頼済みにする」を押してください。受領PDFが金額相違の場合は訂正版を登録します。</p>
 ${s.lines.map(line=>`<section class="card"><h3>${caseLink(line.case_id,line.case_no||'案件番号未登録',line.case_name,'sub')}</h3>
 ${row('元金確認済 / 担当元金',Y(line.principal_confirmed)+' / '+Y(line.principal))}
 ${line.principal_available!==line.principal_confirmed?row('同月内の連続充当を含む確認可能額',Y(line.principal_available)):''}
 ${row('元金の現金入金日',esc(line.principal_received_date||'―'))}
 ${line.refunded?row('個別返金済み元金（今回の振込額から控除）',Y(line.refunded)):''}${row('下請け利益',Y(line.sub_profit))}${row(line.temporary_hold?'一時預かり額':'充当額',Y(line.carry_over))}${row('請求方式',line.profit_only?'利益のみの請求':'元金残額＋利益')}${row('振込額',Y(line.payout),true)}
 ${line.carry_over?`<p>${line.temporary_hold?'元金一時預かり（充当先なし）':'充当先 '+caseLink(line.carry_to_case_id,line.target_case_no||'案件番号未登録',line.target_name,'sub')}　承諾日 ${esc(line.consent_date||'―')}　承諾方法 ${esc(line.consent_method||'―')}</p><p>${esc(line.consent_record||'')}</p>`:''}
 ${line.hold_consent_pending?'<p class="purchase-warning" role="status">元金は一時預かり（了承待ち）です。LINEで了承を得たら「編集」から承諾日を入力して保存してください。</p>':''}
 ${line.carry_over?`<div class="acts"><button class="s" type="button" onclick="editCarry(${line.id})">編集</button></div>`:''}
 ${draft||line.carry_over?`<details id="carry-edit-${line.id}"><summary>元金の充当・一時預かりを編集</summary><form onsubmit="saveCarry(event,${s.id},${line.id})"><input type="hidden" name="expected_revision" value="${line.carry_revision}"><div class="g">
 <label class="f">元金の取扱い・充当先<select name="target_id" onchange="changeCarryTarget(this)"><option value="">案件を選択</option><option value="hold" ${line.temporary_hold?'selected':''}>一時預かり（充当先を解除）</option>${s.targets.filter(target=>target.id!==line.case_id&&target.due_date>line.due_date).map(target=>`<option value="${target.id}" ${target.id===line.carry_to_case_id?'selected':''}>${esc(target.case_no||'案件番号未登録')} ${esc(target.name)}（${Y(target.principal_tax_in)}）</option>`).join('')}</select></label>
 <label class="f">充当・一時預かり額（円、0円で解除・返金）<input name="carry_over" inputmode="numeric" value="${line.carry_over}" ${s.status==='paid'?'readonly':''} required></label>
 <label class="f">承諾日<input name="consent_date" type="date" value="${esc(line.consent_date||'')}"></label>
 <label class="f">承諾方法（一時預かりはLINE）<input name="consent_method" maxlength="80" value="${esc(line.consent_method||(line.temporary_hold?'LINE':''))}"></label>
 <label class="f">承諾記録（一時預かりは任意）<input name="consent_record" maxlength="500" value="${esc(line.consent_record||'')}" placeholder="承諾者・LINEの日時など"></label>
 <label class="f">変更理由<input name="change_reason" maxlength="500" value="${line.temporary_hold?'一時預かりの承諾情報を更新':''}" required placeholder="充当先案件の選択誤りを訂正、など"></label>
 </div><p class="m">充当先がなくなった場合は「一時預かり」を選び、元金を預かる金額を入力してください。全額預かりなら利益のみの振込です。了承依頼文を送付し、LINEで実際の了承を得てから承諾日を入力して保存してください。承諾方法はLINEが初期値で、承諾記録は任意です。0円で解除すると元金も返金する計算になります。支払済みの金額は変更できません。</p><div class="acts"><button class="s" type="button" onclick="previewHoldConsent(this,${s.id},${line.id})">一時預かりの了承依頼文を表示</button><button class="s">充当・一時預かりを保存</button></div><div class="hold-consent" hidden><textarea readonly rows="9" aria-label="一時預かりの了承依頼文"></textarea><button class="s" type="button" onclick="copyHoldConsent(this)">了承依頼文をコピー</button><p class="m">文面の表示・コピーでは承諾済みとして保存されません。</p></div></form></details>`:''}
 ${carryHistoryBlock(line)}</section>`).join('')}
 <section class="card"><h3>請求依頼文面・PDFひな形</h3>
 ${draft?`<form onsubmit="saveSettlementRequest(event,${s.id})"><div class="g">
 <label class="f">宛先担当者（空欄で会社宛）<input name="request_contact" maxlength="80" value="${esc(s.request_contact)}"></label>
 <label class="f">敬称<select name="request_honorific"><option ${s.request_honorific==='御中'?'selected':''}>御中</option><option ${s.request_honorific==='様'?'selected':''}>様</option></select></label>
 <label class="f">税率（請求書全体に適用）<select name="invoice_tax_rate"><option value="10" ${s.invoice_tax_rate===10?'selected':''}>10%</option><option value="8" ${s.invoice_tax_rate===8?'selected':''}>8%</option></select></label>
 <label class="f">請求内容・業務内容<textarea name="invoice_description" maxlength="500" required>${esc(s.invoice_description)}</textarea></label>
 </div><button class="s">依頼内容を保存して文面・ひな形に反映</button><p class="m">編集後は保存してからコピー・PDF取得してください。税込の依頼合計から税額を1回だけ計算します。</p></form>`:'<p class="m">依頼時点の宛名・税率・業務内容を保持しています。</p>'}
 ${row('税抜金額',Y(s.amount_ex_tax))}${row('消費税（'+s.request_document.tax_rate+'%）',Y(s.tax_amount))}
 <textarea readonly rows="10" id="request-text">${esc(s.request_text)}</textarea>
 <div class="acts"><button class="s" type="button" onclick="copySettlementRequest()">文面をコピー</button>
 <button class="s" type="button" onclick="showSettlementTemplate(${s.id})">請求書PDFひな形を確認</button>
 <button class="s" type="button" onclick="downloadFile('/api/settlements/${s.id}/invoice-template')">PDFひな形を保存</button>
 ${draft?`<button onclick="changeSettlement(${s.id},'requested')">依頼済みにする</button><button class="d" onclick="deleteSettlement(${s.id})">依頼前の明細を削除</button>`:''}
 ${s.status==='requested'&&!s.invoices.length?`<button class="s" onclick="changeSettlement(${s.id},'not_requested')">依頼前に戻す</button>`:''}</div></section>
 ${upload?`<section class="card"><h3>下請けから受領した請求書</h3><form onsubmit="uploadSettlementInvoice(event,${s.id})">
 <div class="g"><label class="f">請求書PDF<input name="pdf" type="file" accept="application/pdf,.pdf" required></label>
 <label class="f">受領日<input name="received_date" type="date" value="${localToday()}" required></label>
 <label class="f">請求書番号<input name="invoice_no" maxlength="80"></label>
 <label class="f">税込請求額（読取不可・訂正時のみ）<input name="amount_tax_in" inputmode="numeric"></label></div>
 <label><input name="confirm_amount" type="checkbox" style="width:auto;min-height:auto">手入力額を原本と照合済み</label>
 <div class="acts"><button>登録して金額を照合</button></div></form></section>`:''}
 ${s.status==='mismatch'?'<p role="alert">請求書の金額が依頼額と異なります。原本を確認し、訂正版のPDFを登録してください。金額一致までは支払済みにできません。</p>':''}
 ${s.invoices.length?`<section class="card"><h3>受領PDFの履歴</h3><p class="m" style="overflow-wrap:anywhere">PDFコピー先：${esc(s.archive_directory)}（直下に現在版を保存。旧版はアプリ内の履歴で保管）</p>${s.invoices.map((invoice,index)=>`<div class="row"><button class="s" onclick="showSettlementInvoice(${invoice.id})">${esc(invoice.original_name||'受領PDF')}</button><span>${esc(invoice.received_date||'')} ${esc(invoice.invoice_no||'')}　${Y(invoice.amount_tax_in)}${index===0?'（最新版）':''}</span></div><details><summary>アプリ内の原本保存パス</summary><p style="overflow-wrap:anywhere">${esc(invoice.stored_path)}</p></details>`).join('')}</section>`:''}
 ${pay?`<section class="card"><h3>支払・充当完了</h3>${fld('完了日','settlement-paid',localToday(),{type:'date'})}<div class="acts"><button onclick="changeSettlement(${s.id},'paid')">支払済みにする</button></div></section>`:''}
 </div>`);
 dialog.setAttribute('aria-labelledby','settlement-title');
}

function reloadSettlement(id){
 $('.settlement-dialog')?.close();
 return viewMonthly(monthly.period).then(()=>openSettlement(id));
}
function deleteSettlement(id){
 if(!confirm('依頼前の精算明細と充当予定を削除しますか？'))return;
 act(async()=>{
  await api('DELETE',`/api/settlements/${id}`);
  $('.settlement-dialog')?.close();
  await viewMonthly(monthly.period);
 },'精算明細を削除しました');
}
function changeCarryTarget(select){
 const form=select.form;
 for(const name of ['consent_date','consent_record'])form.elements[name].value='';
 form.elements.consent_method.value=select.value==='hold'?'LINE':'';
 if(select.value==='hold'&&!form.elements.change_reason.value)form.elements.change_reason.value='充当予定案件の見送りにより元金を一時預かり';
 form.querySelector('.hold-consent')?.setAttribute('hidden','');
}
function previewHoldConsent(button,id,lineId){
 const s=monthly.settlements.find(item=>item.id===id),line=s.lines.find(item=>item.id===lineId),form=button.form;
 const amount=Number(form.elements.carry_over.value);
 if(form.elements.target_id.value!=='hold'||!Number.isSafeInteger(amount)||amount<=0||amount>line.principal){toast('一時預かりを選び、元金以内の預かり額を入力してください');return;}
 const field=form.querySelector('.hold-consent textarea');
 field.value=`${s.sub_name} 御中\n${line.case_no||'案件番号未登録'} ${line.case_name}の精算について、ご確認をお願いいたします。\n当初は支払期限${s.pay_date}に元金${Y(line.principal)}と利益${Y(line.sub_profit)}をお振り込みする予定でしたが、充当予定の案件が見送りとなったため、充当先の指定を解除いたします。\nご要望により、元金のうち${Y(amount)}を当社で一時お預かりし、今回は${amount===line.principal?'利益のみの':'元金残額と利益の'}${Y(line.principal+line.sub_profit-amount)}をお振り込みする内容でよろしいでしょうか。\n預かり元金の返金時期・今後の充当先は、別途ご相談のうえ決定いたします。別案件へ充当する場合は、改めて了承を確認いたします。\n上記の一時預かりについて、了承の旨をご返信いただけますと幸いです。`;
 field.parentElement.hidden=false;
}
function copyHoldConsent(button){
 const field=button.parentElement.querySelector('textarea');field.select();
 if(navigator.clipboard)act(()=>navigator.clipboard.writeText(field.value),'了承依頼文をコピーしました');
 else toast('文面を選択しました。Ctrl+Cでコピーしてください');
}
function saveCarry(event,id,line){
 event.preventDefault();
 if(!confirm('変更前のDBをバックアップし、既存の充当明細を更新します。内容を保存しますか？'))return;
 const values=Object.fromEntries(new FormData(event.currentTarget));
 act(async()=>{await api('PUT',`/api/settlements/${id}/lines/${line}`,values);await reloadSettlement(id)},'充当・一時預かりを保存しました');
}
function editCarry(lineId){
 const editor=document.getElementById('carry-edit-'+lineId);
 editor.open=true;editor.scrollIntoView({block:'nearest'});
 editor.querySelector('select').focus();
}
function carryHistoryBlock(line){
 if(!line.carry_history?.length)return '';
 const describe=value=>`${value.carry_over?(value.carry_to_case_id?(value.target_case_no||'案件番号未登録'):'一時預かり（充当先なし）'):'充当・預かりなし'} ${value.target_name||''} ／ ${Y(value.carry_over)} ／ 承諾日 ${value.consent_date||(value.carry_over&&!value.carry_to_case_id?'了承待ち':'―')} ／ 承諾方法 ${value.consent_method||'―'} ／ 承諾記録 ${value.consent_record||'―'}`;
 return `<details><summary>元金充当の変更履歴（${line.carry_history.length}件）</summary>${line.carry_history.map(change=>`<section class="card"><p><b>${esc(new Date(change.changed_at).toLocaleString('ja-JP'))}</b> ${esc(change.reason)}</p><p style="overflow-wrap:anywhere">変更前：${esc(describe(change.before))}</p><p style="overflow-wrap:anywhere">変更後：${esc(describe(change.after))}</p><p class="m" style="overflow-wrap:anywhere">変更前DB：${esc(change.backup_file)}</p></section>`).join('')}</details>`;
}
function saveSettlementRequest(event,id){
 event.preventDefault();
 const values=Object.fromEntries(new FormData(event.currentTarget));
 act(async()=>{await api('PUT',`/api/settlements/${id}/request`,values);await reloadSettlement(id)},'依頼内容を保存しました');
}
function showSettlementTemplate(id){
 act(()=>showDocumentPreview({title:'請求書PDFひな形（下請け本人による確認・発行用）',url:`/api/settlements/${id}/invoice-template/preview`,download:`/api/settlements/${id}/invoice-template`}));
}
function changeSettlement(id,status){
 if(!confirm('精算の状態を変更しますか？'))return;
 const paid_at=status==='paid'?G('settlement-paid'):undefined;
 act(async()=>{await api('PUT',`/api/settlements/${id}/status`,{status,paid_at});await reloadSettlement(id)},'状態を更新しました');
}
function uploadSettlementInvoice(event,id){
 event.preventDefault();
 const form=event.currentTarget, data=new FormData(form);
 const {pdf,...values}=Object.fromEntries(data);
 act(async()=>{
  const body=await attachmentBody(pdf);
  await api('POST',`/api/settlements/${id}/invoice`,{...values,...body});
  await reloadSettlement(id);
 },'請求書を照合しました');
}
function showSettlementInvoice(id){
 const url=`/api/sub-invoices/${id}/preview`;
 act(()=>showDocumentPreview({title:'下請けからの受領請求書',url,download:url}));
}
function copySettlementRequest(){
 const field=$('#request-text');
 field.select();
 if(navigator.clipboard)act(()=>navigator.clipboard.writeText(field.value),'文面をコピーしました');
 else toast('文面を選択しました。Ctrl+Cでコピーしてください');
}

async function savePdfArchiveSettings(){
 await api('PUT','/api/settings/pdf-archive',{directory:G('pdf-archive-directory')});
 await viewSettings();
}

async function viewExports(){
 const cases=await api('GET','/api/cases');
 const reports=[['transactions','取引一覧CSV'],['cashflow','月次収支CSV'],['profit','案件別利益CSV'],['settlements','下請け別精算CSV'],['funding','元金・預かり金履歴CSV']];
 page('#/exports',`<div class="exports-page"><h1>帳票・データ出力</h1><div class="exports-grid">
 <section class="card"><h2>CSV出力</h2>
 <div class="g">${fld('対象年（任意）','export-year','',{ph:'例 2026'})}${fld('対象月（任意・年より優先）','export-month','',{type:'month'})}</div>
 <div class="acts">${reports.map(([key,name])=>`<button onclick="downloadReport('${key}')">${name}</button>`).join('')}</div></section>
 <section class="card"><h2>期末残高一覧</h2>${fld('期末日','report-cutoff',localToday(),{type:'date'})}
 <div class="acts"><button onclick="downloadReport('balances')">期末残高CSV</button></div></section>
 <section class="card export-evidence"><h2>案件の証憑PDF</h2>
 ${fld('案件','evidence-case','',{sel:'<option value="">案件を選択</option>'+opts(cases.map(x=>({...x,name:x.mgmt_no+' '+x.name})))})}
 <div class="acts"><button class="s" onclick="openEvidenceCase()">発行済み帳票を確認</button><button onclick="downloadEvidence()">証憑PDF一式をダウンロード</button></div></section></div></div>`);
}
function downloadReport(kind){
 const period=G('export-month')||G('export-year').trim();
 const cutoff=G('report-cutoff');
 if(kind==='balances'&&!cutoff){toast('期末日を指定してください',true);return}
 if(kind!=='balances'&&period&&!/^\d{4}(?:-\d{2})?$/.test(period)){toast('対象年を西暦4桁で入力してください',true);return}
 const params=new URLSearchParams(kind==='balances'?{at:cutoff}:{period});
 downloadFile('/api/export/'+kind+'?'+params);
}
function openEvidenceCase(){
 const id=G('evidence-case');
 if(id)location.hash='#/case/'+id+'/docs';
 else toast('案件を選択してください',true);
}
function downloadEvidence(){
 const id=G('evidence-case');
 if(id)downloadFile('/api/cases/'+id+'/evidence');
 else toast('案件を選択してください',true);
}
function downloadFile(path){
 act(async()=>{
  const response=await fetch(path);
  if(!response.ok)throw new Error((await response.json()).error);
  const url=URL.createObjectURL(await response.blob());
  const link=document.createElement('a');
  link.href=url;
  link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||'download';
  link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 });
}

const subSelection = new Map();
function setupSubTabs(){
 const panel=$('#panel-sub');
 const tabs=document.createElement('div');tabs.className='sub-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','下請けの切替');
 for(const slot of [1,2]){
  const card=document.getElementById('s'+slot+'_id').closest('.card');
  card.id='sub-panel-'+slot;card.classList.add('sub-slot');card.setAttribute('role','tabpanel');card.setAttribute('aria-labelledby','sub-tab-'+slot);
  const button=document.createElement('button');button.type='button';button.id='sub-tab-'+slot;button.textContent='下請け'+slot;button.setAttribute('role','tab');button.setAttribute('aria-controls',card.id);
  button.onclick=()=>selectSubSlot(slot);
  button.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?1:e.key==='End'?2:3-slot;selectSubSlot(next);document.getElementById('sub-tab-'+next).focus()}};
  tabs.append(button);
  const grid=card.querySelector('.g');grid.classList.add('sub-input-summary');
 }
 panel.prepend(tabs);
 const docs=document.createElement('button');docs.type='button';docs.className='s';docs.textContent='請求書・領収書';docs.onclick=()=>{selectCaseTab('docs');const slot=subSelection.get(CUR.v.case.id)||1;const sub=CUR.v.subs.find(s=>s.slot===slot);if(sub&&$('#doc_sub'))$('#doc_sub').value=sub.sub_id;invalidateDocumentPreview()};tabs.append(docs);
 const cs=CUR.v.case,info=document.createElement('div');info.className='case-info-strip';
 info.innerHTML=[['管理番号',cs.mgmt_no],['案件番号',cs.case_no],['開始日',cs.start_date],['納期',cs.due_date]].map(([label,value])=>`<div><span>${label}</span><strong>${esc(value||'未登録')}</strong></div>`).join('');
 $('#case-editor .editor-tabs').before(info);
 selectSubSlot(subSelection.get(cs.id)||1);
}
function selectSubSlot(slot){
 subSelection.set(CUR.v.case.id,slot);
 for(const n of [1,2]){const active=n===slot;document.getElementById('sub-panel-'+n).hidden=!active;const button=document.getElementById('sub-tab-'+n);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;}
}
