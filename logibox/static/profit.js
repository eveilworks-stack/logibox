// 案件・下請け単位の都度精算。利益と元金を分け、支払合計を記録する。
let PROFIT=null,PROFIT_EDIT=null,PROFIT_KEY=null,PROFIT_PRINCIPAL_MODE=false;
const profitMethods={normal:'通常精算',full:'都度精算'};
const profitPart=p=>p.profit_amount??(p.amount-(p.principal_amount||0));
async function openProfit(cid,sid){
 PROFIT=await api('GET',`/api/cases/${cid}/subs/${sid}/profit`);
 PROFIT_EDIT=null;PROFIT_PRINCIPAL_MODE=false;PROFIT_KEY=crypto.randomUUID();
 let dialog=document.getElementById('profit-dialog');
 if(!dialog){dialog=document.createElement('dialog');dialog.id='profit-dialog';dialog.style.cssText='width:min(1120px,94vw);max-height:92vh;border:1px solid #acc3dc;border-radius:12px;color:#18375a';document.body.append(dialog)}
 renderProfit();if(!dialog.open)dialog.showModal();
}
function profitForm(p){return `<section id="profit-entry" class="card"><div class="g">
 <label class="f">精算方法<select id="ps-method" onchange="toggleProfitForm()">${Object.entries(profitMethods).map(([k,v])=>`<option value="${k}" ${k===(p?.method||(PROFIT_PRINCIPAL_MODE?'full':PROFIT.method))?'selected':''}>${v}</option>`).join('')}</select></label>
 <div id="ps-individual" style="display:contents">
 ${fld('今回の利益精算額（未精算利益全額・円）','ps-amount',p?profitPart(p):PROFIT.remaining,{num:1}).replace('<input','<input readonly')}
 <div class="f"><span>元金の返還</span><label class="funding-check"><input style="width:18px;height:18px;min-height:18px;flex:none;padding:0" type="checkbox" id="ps-include-principal" onchange="toggleProfitForm()" ${p?.principal_amount||PROFIT_PRINCIPAL_MODE?'checked':''}> 返還可能な元金も精算する</label></div>
 <div id="ps-principal-amount" class="f"><span>今回の元金返還額</span><strong id="ps-principal-value"></strong></div>
 <div class="f"><span>今回の支払合計（利益＋元金）</span><strong id="ps-total" style="border:1px solid #a7bfd9;padding:10px"></strong></div>
 ${fld('精算予定日','ps-planned',p?.planned_date||'',{type:'date'})}
 <label class="f">登録内容<select id="ps-stage" onchange="toggleProfitForm()"><option value="plan" ${!p?.paid_date?'selected':''}>支払予定のみ</option><option value="paid" ${p?.paid_date?'selected':''}>支払完了（実績）</option></select></label>
 <div id="ps-paid-field">${fld('実際の支払日（過去日可）','ps-paid',p?.paid_date||'',{type:'date'})}</div>
 <label class="f">支払方法<select id="ps-payment"><option value="bank">銀行振込</option><option value="cash" ${p?.payment_method==='cash'?'selected':''}>現金手渡し</option></select></label>
 ${fld('都度精算の理由・備考','ps-note',p?.note||'',{area:true})}</div>
 ${p?fld('修正理由（必須）','ps-reason',''):''}</div>
 <p id="ps-normal" class="m">月末締め・翌月15日払い。個別支払は登録しません。</p>
 <p id="ps-principal-note" class="m">元金は対象案件・下請けの返還可能額を全額精算します。充当可能額を重複加算しません。充当済み・一時預かり金・他の支払予定に使用中の元金は対象外です。</p>
 <p class="m">${PROFIT.haak_received?'':'都度精算はハークの実入金確認完了後に登録できます。'}</p>
 <div class="acts"><button id="ps-save" onclick="saveProfit(this)">保存</button>${p||PROFIT_PRINCIPAL_MODE?'<button class="s" onclick="PROFIT_EDIT=null;PROFIT_PRINCIPAL_MODE=false;renderProfit()">編集を閉じる</button>':''}</div></section>`}
function startPrincipalSettlement(){PROFIT_PRINCIPAL_MODE=true;PROFIT_EDIT=null;renderProfit()}
function renderProfit(){
 const d=PROFIT,p=PROFIT_EDIT,dialog=document.getElementById('profit-dialog');
 const completed=d.status==='精算完了'&&!p&&!PROFIT_PRINCIPAL_MODE;
 dialog.innerHTML=`<div class="section-head"><h2>都度精算・利益精算</h2><button class="s" onclick="document.getElementById('profit-dialog').close()">閉じる</button></div>
 <p><b>${esc(d.case_no||'案件番号未登録')} ${esc(d.case_name)}</b> ／ ${esc(d.sub_name)} ／ 利益 <span class="t">${esc(d.status)}</span></p>
 <div class="g"><div>${row('担当元金',Y(d.principal_tax_in))}${row('元金残高',Y(d.principal_balance))}${row('返還可能元金',Y(d.principal_available))}</div>
 <div>${row('契約利益率',esc(d.contract_rate)+'％')}${row('確定利益額',Y(d.profit))}${row('利益精算済み累計額',Y(d.paid))}</div>
 <div style="border:1px solid #a7bfd9;padding:8px">${row('未精算利益',Y(d.remaining),1)}${row('別途支払予定（利益）',Y(d.planned))}${row('別途返還予定（元金）',Y(d.principal_planned))}${row('月次支払予定利益',Y(d.monthly_scheduled))}</div></div>
 ${completed?`<p class="m">利益精算完了。元金返還は別に管理します。修正は履歴の編集ボタンから操作してください。</p>${d.principal_available>0?'<button class="s" id="ps-start-principal" onclick="startPrincipalSettlement()">残っている元金を精算する</button>':''}`:profitForm(p)}
 <details ${p?'':'open'}><summary>精算結果と履歴</summary><div class="table-scroll"><table class="data-table compact-table"><thead><tr><th>状態・方法</th><th>利益／元金／合計</th><th>予定日／実際の支払日</th><th>備考</th><th>操作</th></tr></thead><tbody>
 ${d.payments.map(x=>`<tr><td>${x.cancelled?'取消済':x.paid_date?'支払済':'精算予定'}<br>${profitMethods[x.method]||'旧精算履歴'}</td><td>利益 ${Y(profitPart(x))}<br>元金 ${Y(x.principal_amount||0)}<br>合計 ${Y(x.amount)}</td><td>${esc(x.planned_date||'―')}<br>${esc(x.paid_date||'未払い')}</td><td>${esc(x.note)}</td><td>${x.cancelled?'':`<button class="s" onclick="editProfit(${x.id})">編集${x.paid_date?'':'・支払完了'}</button> <button class="s" onclick="cancelProfit(${x.id})">取消</button>${x.paid_date?` <a target="_blank" href="/api/profit-payments/${x.id}/pdf?preview=1">PDFプレビュー</a> <button class="s" onclick="issueProfit(${x.id},this)">発行</button>`:''}`}</td></tr>`).join('')}
 ${d.monthly_history.map(x=>`<tr><td>通常精算・支払済</td><td>利益 ${Y(x.amount)}${x.principal_amount!=null?'<br>元金 '+Y(x.principal_amount):''}</td><td>${esc(x.paid_date)}</td><td>月次精算の実績</td><td>月次精算履歴を参照</td></tr>`).join('')}</tbody></table></div></details>
 <details><summary>発行済みPDF・変更履歴</summary>${d.documents.map(x=>`<p>${esc(x.number)} ／ ${esc(x.issued_date)} <a target="_blank" href="/api/profit-documents/${x.id}/pdf?preview=1">閲覧</a> <a href="/api/profit-documents/${x.id}/pdf">ダウンロード</a></p>`).join('')}${d.history.map(x=>`<p>${esc(new Date(x.changed_at).toLocaleString('ja-JP'))} ${esc(x.reason)}<br>${profitHistoryChange(x)}</p>`).join('')}</details>`;
 toggleProfitForm();
}
function toggleProfitForm(){
 if(!document.getElementById('ps-method'))return;
 const individual=G('ps-method')!=='normal',old=PROFIT_EDIT;
 const profit=old?profitPart(old):PROFIT.remaining;
 const principalAvailable=PROFIT.principal_available+(old?.principal_amount||0);
 const checkbox=document.getElementById('ps-include-principal');checkbox.disabled=principalAvailable<=0;
 const principal=checkbox.checked?(old?.principal_amount||principalAvailable):0;
 document.getElementById('ps-individual').style.display=individual?'contents':'none';
 document.getElementById('ps-normal').hidden=individual;
 document.getElementById('ps-principal-note').hidden=!individual;
 document.getElementById('ps-paid-field').hidden=G('ps-stage')!=='paid';
 document.getElementById('ps-amount').value=profit;
 document.getElementById('ps-principal-value').textContent=Y(principal);
 document.getElementById('ps-principal-value').dataset.amount=principal;
 document.getElementById('ps-principal-amount').hidden=!checkbox.checked;
 document.getElementById('ps-total').textContent=Y(profit+principal);
 document.getElementById('ps-save').disabled=individual&&(!PROFIT.haak_received||profit+principal<=0);
}
function editProfit(id){PROFIT_EDIT=PROFIT.payments.find(x=>x.id===id);PROFIT_PRINCIPAL_MODE=false;renderProfit()}
async function saveProfit(button){
 button.disabled=true;
 try{const p=PROFIT_EDIT;
 const b={method:G('ps-method'),amount:fundingYen(G('ps-amount')),include_principal:document.getElementById('ps-include-principal').checked,principal_amount:Number(document.getElementById('ps-principal-value').dataset.amount||0),
 planned_date:G('ps-planned'),paid_date:G('ps-stage')==='paid'?G('ps-paid'):'',payment_method:G('ps-payment'),note:G('ps-note'),request_key:PROFIT_KEY,
 ...(p?{id:p.id,revision:p.revision,reason:G('ps-reason')}:{})};
 PROFIT=await api('POST',`/api/cases/${PROFIT.case_id}/subs/${PROFIT.sub_id}/profit`,b);
 PROFIT_KEY=crypto.randomUUID();PROFIT_EDIT=null;PROFIT_PRINCIPAL_MODE=false;renderProfit();toast('精算を保存しました')
 }catch(e){toast(e.message,true);button.disabled=false}
}
async function cancelProfit(id){const p=PROFIT.payments.find(x=>x.id===id),reason=prompt('取消理由を入力してください');if(!reason)return;try{PROFIT=await api('POST',`/api/cases/${PROFIT.case_id}/subs/${PROFIT.sub_id}/profit`,{id,revision:p.revision,cancel:true,reason});PROFIT_EDIT=null;PROFIT_PRINCIPAL_MODE=false;renderProfit()}catch(e){toast(e.message,true)}}
async function issueProfit(id,button){button.disabled=true;try{const response=await fetch(`/api/profit-payments/${id}/pdf`,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'app'},body:JSON.stringify({issue:true})});if(!response.ok)throw Error((await response.json()).error);await response.blob();await openProfit(PROFIT.case_id,PROFIT.sub_id);toast('精算書を発行しました。発行済みPDFから閲覧できます')}catch(e){toast(e.message,true);button.disabled=false}}
async function openProfitList(sid=null){
 const data=await api('GET','/api/profit-settlements');
 let dialog=document.getElementById('profit-list');if(!dialog){dialog=document.createElement('dialog');dialog.id='profit-list';dialog.style.cssText='width:min(1100px,94vw);max-height:90vh;border:1px solid #acc3dc;border-radius:12px';document.body.append(dialog)}
 dialog.innerHTML=`<div class="section-head"><h2>下請け別・利益／元金精算一覧</h2><button class="s" onclick="document.getElementById('profit-list').close()">閉じる</button></div><div class="acts"><label>利益の状態 <select id="profit-filter"><option value="">すべて</option>${['未精算','精算予定','精算完了'].map(x=>`<option>${x}</option>`).join('')}</select></label><a href="/api/export/individual-settlements">都度精算明細CSV</a></div><div id="profit-list-rows"></div>`;
 const draw=()=>{const rows=data.rows.filter(x=>(!sid||x.sub_id===sid)&&(!G('profit-filter')||x.status===G('profit-filter')));document.getElementById('profit-list-rows').innerHTML=data.subcontractors.filter(x=>!sid||x.id===sid).map(x=>`<div class="card"><b>${esc(x.name)}</b><p>確定利益 ${Y(x.profit)} ／ 都度精算済利益 ${Y(x.full_paid)} ／ 未精算利益 ${Y(x.remaining)} ／ 月次支払予定利益 ${Y(x.monthly_scheduled)}<br>元金残高 ${Y(x.principal_balance)} ／ 都度返還済み元金 ${Y(x.principal_paid)}</p>${rows.filter(r=>r.sub_id===x.id).map(r=>`<div class="row"><span>${esc(r.case_no||'案件番号未登録')} ${esc(r.case_name)} ／ 利益 ${esc(r.status)} ／ 未精算利益 ${Y(r.remaining)} ／ 元金残高 ${Y(r.principal_balance)}</span><button class="s" onclick="openProfit(${r.case_id},${r.sub_id})">都度精算・利益精算</button></div>`).join('')}</div>`).join('')};document.getElementById('profit-filter').onchange=draw;draw();if(!dialog.open)dialog.showModal()
}
async function reviseProfitMonthly(id){const reason=prompt('訂正理由を入力してください。旧依頼文は履歴に保持し、現在の金額で訂正版を登録します。');if(!reason)return;try{await api('POST',`/api/settlements/${id}/profit-revision`,{reason});await route();toast('訂正版を登録しました。下請けへの連絡と受領請求書の再確認を行ってください')}catch(e){toast(e.message,true)}}
function profitHistoryChange(x){const describe=value=>{const p=JSON.parse(value);if(!p)return '新規';return [profitMethods[p.method]||'',p.amount!=null?'利益 '+Y(profitPart(p))+' ／ 元金 '+Y(p.principal_amount||0)+' ／ 合計 '+Y(p.amount):'',p.cancelled?'取消済':p.paid_date?'支払済 '+p.paid_date:p.planned_date?'予定 '+p.planned_date:''].filter(Boolean).map(esc).join(' ／ ')};return describe(x.before_json)+' → '+describe(x.after_json)}
