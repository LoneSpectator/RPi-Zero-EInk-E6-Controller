'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = null, contents = [], schedules = [], lastSnapshot = null;
let serverTimeBase = null, serverTimeReceivedAt = 0;
let toastTimer;
const selectedContents = new Set();
let libraryDeleting = false, deleteTargets = [];
let libraryRenaming = false, renameContentId = null;
let statusAvailable = false, statusRequest = 0;
let displaySubmitting = false, displayAwaitingStatus = false;
const saving = {image:false, text:false};

function syncDisplayControls() {
  const reason = !statusAvailable ? '无法确认屏幕状态，暂不能提交刷新，正在重新连接。'
    : displaySubmitting ? '正在提交刷新，请稍候。'
    : displayAwaitingStatus || state.display_busy ? '屏幕正在刷新或等待刷新，完成后才能再次提交。' : '';
  $('refresh-notice').textContent=reason; $('refresh-notice').hidden=!reason;
  for(const type of ['image','text']) {
    $(type+'-save').disabled=!previews[type] || saving[type];
    $(type+'-display').disabled=!previews[type] || saving[type] || Boolean(reason);
    $(type+'-display').title=reason;
  }
  document.querySelectorAll('[data-display]').forEach(button=>{
    button.disabled=Boolean(reason); button.title=reason;
  });
}
const previews = {image:null, text:null};
const versions = {image:0, text:0};
const urls = {};
const names = {overview:'概览',image:'图片',text:'文本',library:'内容库',schedule:'排程'};
const labels = {pending:'等待刷新',running:'刷新中',done:'已完成',failed:'失败',idle:'待机',unknown:'待确认',refreshing:'刷新中',error:'刷新失败'};

function toast(message, error=false) {
  $('toast').textContent = message; $('toast').classList.toggle('error',error); $('toast').hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,error?6500:3500);
}
async function api(path, options={}) {
  const response = await fetch(path,options);
  if(!response.ok){
    let message = `请求失败 (${response.status})`;
    try {const body=await response.json();message=typeof body.detail==='string'?body.detail:JSON.stringify(body.detail);} catch (_) {}
    throw new Error(message);
  }
  return response.headers.get('content-type')?.includes('application/json') ? response.json() : response.blob();
}
const json = body => ({headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function formatTime(timestamp, options={}) {
  if(!timestamp)return '—';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:state?.timezone||'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,...options}).format(new Date(timestamp*1000));
}
function page() {
  const key=location.hash.slice(1) in names ? location.hash.slice(1) : 'overview';
  document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==`page-${key}`);
  document.querySelectorAll('nav a').forEach(el=>el.classList.toggle('active',el.dataset.page===key));
  $('breadcrumb').textContent=names[key];
  if(key==='library')refreshContents().catch(e=>toast(e.message,true));
  if(key==='schedule')refreshSchedules().catch(e=>toast(e.message,true));
}
window.addEventListener('hashchange',page);
function serverNow(){
  return serverTimeBase===null?null:serverTimeBase+(performance.now()-serverTimeReceivedAt);
}
function clock() {
  if(!statusAvailable || serverTimeBase===null){
    $('clock-date').textContent='----/--/--';$('clock').textContent='--:--:--';
    $('clock-date').removeAttribute('datetime');$('clock').removeAttribute('datetime');
    $('header-clock').classList.add('status-unknown');$('header-clock').title='服务器时间不可用';
    return;
  }
  const date=new Date(serverNow());
  $('clock').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:state.timezone,hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(date);
  const calendar=new Intl.DateTimeFormat('sv-SE',{timeZone:state.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
  $('clock-date').textContent=calendar; $('clock-date').dateTime=calendar;
  $('clock').dateTime=date.toISOString();
  $('header-clock').classList.remove('status-unknown');
  $('header-clock').title='服务器时间 · '+new Intl.DateTimeFormat('zh-CN',{timeZone:state.timezone,weekday:'long'}).format(date)+' · '+state.timezone;
}
function updateHeaderBattery(percent=null,voltage=null,message=''){
  const gauge=$('header-battery-gauge'), level=$('header-battery-level'), known=Number.isFinite(percent);
  const value=known?Math.round(Math.min(100,Math.max(0,percent))):null;
  const text=known?`${value}%`:'—%';
  if($('header-battery').textContent!==text)$('header-battery').textContent=text;
  gauge.classList.toggle('status-unknown',!known);
  gauge.setAttribute('role',known?'meter':'img');
  gauge.setAttribute('aria-label',known?'电池电量':'电量未知');
  if(known){
    gauge.setAttribute('aria-valuenow',String(value));gauge.setAttribute('aria-valuemin','0');gauge.setAttribute('aria-valuemax','100');
    gauge.setAttribute('aria-valuetext',`电量 ${value}%`);
  }else{
    for(const name of ['aria-valuenow','aria-valuemin','aria-valuemax','aria-valuetext'])gauge.removeAttribute(name);
  }
  const details=[];
  if(known)details.push(`电量 ${value}%`);
  if(Number.isFinite(voltage))details.push(`电压 ${voltage.toFixed(3)} V`);
  if(!known && message)details.push(message);
  gauge.title=details.join(' · ')||'电量未知';
  level.style.strokeDashoffset=String(known?100-value:100);
  level.style.opacity=known && value>0?'1':'0';
}
function updateHeaderStatus(){
  const set=(id,text,kind='',title='')=>{
    const el=$(id);
    if(el.textContent!==text)el.textContent=text;
    el.className='connection'+(kind?' '+kind:'');el.title=title;
  };
  if(!statusAvailable || !state){
    set('connection','连接异常','status-error');
    set('header-screen','屏幕状态未知','status-unknown');
    updateHeaderBattery(null,null,'连接异常，无法获取电量');clock();
    return;
  }
  set('connection',state.driver==='mock'?'模拟设备已连接':'设备服务已连接');
  const screen=state.running || state.screen.status==='refreshing'?'refreshing':state.pending?'pending':state.screen.status;
  const text={refreshing:'屏幕刷新中',pending:'屏幕等待刷新',idle:'屏幕待机',error:'屏幕刷新失败',unknown:'屏幕状态待确认'};
  const kind=screen==='error'?'status-error':screen==='refreshing'||screen==='pending'?'status-active':screen==='idle'?'':'status-unknown';
  const detail=state.screen.error||(state.running?`正在刷新：${state.running.title}`:state.pending?`${state.pending} 项内容等待刷新`:'刷新队列空闲');
  set('header-screen',text[screen]||'屏幕状态未知',kind,detail);
  const battery=state.battery.available && Number.isFinite(state.battery.percent);
  updateHeaderBattery(battery?state.battery.percent:null,state.battery.available?state.battery.voltage:null,state.battery.message||'');
}
async function refreshStatus() {
  const request=++statusRequest;
  try {
    const next=await api('/api/status',{signal:AbortSignal.timeout(8000)});
    if(request!==statusRequest)return;
    const serverTime=Date.parse(next.now);
    if(!Number.isFinite(serverTime))throw new Error('服务器时间不可用');
    state=next;serverTimeBase=serverTime;serverTimeReceivedAt=performance.now();
    statusAvailable=true;displayAwaitingStatus=false;syncDisplayControls();clock();
    updateHeaderStatus();
    $('mode-note').textContent=state.driver==='mock'?'当前为模拟模式：可验证全部内容与排程功能，不会驱动物理屏幕，电量不模拟。':'';
    $('screen-error').textContent=state.screen.error||'';
    $('current-title').textContent=state.screen.title||'还没有显示内容';
    $('last-refresh').textContent=state.screen.updated_at?'成功下发于 '+formatTime(state.screen.updated_at):'等待第一次刷新';
    if(state.screen.snapshot && lastSnapshot!==state.screen.snapshot){
      $('current-screen').src='/api/screen?v='+encodeURIComponent(state.screen.snapshot);$('current-screen').hidden=false;$('screen-empty').hidden=true;lastSnapshot=state.screen.snapshot;
    }
    $('upcoming').innerHTML=state.upcoming.length?state.upcoming.map(s=>`<div class="upcoming-item"><div class="schedule-time">${escapeHTML(formatTime(s.next_run,{month:undefined,day:undefined}))}<small>${escapeHTML(formatTime(s.next_run,{hour:undefined,minute:undefined}))}</small></div><div><strong>${escapeHTML(s.content_title)}</strong><p>${escapeHTML(s.title)} · ${s.kind==='cron'?'循环':'单次'}</p></div></div>`).join(''):'<div class="empty-state">把下一帧交给时间<br><a class="text-link" href="#schedule">创建你的第一个排程 ↗</a></div>';
    $('job-history').innerHTML=state.jobs.length?state.jobs.map(j=>`<div class="history-row"><span>${escapeHTML(j.title)}${j.error?`<small class="error-text"> ${escapeHTML(j.error)}</small>`:''}</span><span class="muted">${j.scheduled_for?'定时':'手动'}</span><time class="muted">${escapeHTML(formatTime(j.created_at))}</time><span class="job-state ${j.status==='failed'?'failed':''}">${escapeHTML(labels[j.status])}</span></div>`).join(''):'<div class="empty-state">还没有刷新记录</div>';
    $('schedule-tz').textContent=state.timezone;
  } catch(e){if(request!==statusRequest)return; statusAvailable=false; syncDisplayControls(); updateHeaderStatus(); if(!state)toast(e.message,true);}
}
async function refreshContents(){contents=await api('/api/contents');$('library-count').textContent=contents.length;renderLibrary();}
function filteredContents(){
  const query=$('library-search').value.trim().toLowerCase(),filter=$('library-filter').value;
  return contents.filter(c=>c.title.toLowerCase().includes(query)&&(filter==='all'||filter===c.kind));
}
function syncLibrarySelection(){
  const visible=filteredContents(), count=selectedContents.size;
  $('library-delete').hidden=!count;
  $('library-delete').textContent=`删除（${count}）`;
  $('library-delete').disabled=libraryDeleting;
  $('library-select-all').checked=visible.length>0 && count===visible.length;
  $('library-select-all').indeterminate=count>0 && count<visible.length;
  $('library-select-all').disabled=libraryDeleting || !visible.length;
  $('library-search').disabled=libraryDeleting;$('library-filter').disabled=libraryDeleting;
  document.querySelectorAll('[data-select-content]').forEach(box=>{
    box.checked=selectedContents.has(box.dataset.selectContent);box.disabled=libraryDeleting;
    box.closest('tr').classList.toggle('selected',box.checked);
  });
  for(const id of ['confirm-delete','cancel-delete','close-delete'])$(id).disabled=libraryDeleting;
  $('confirm-delete').textContent=libraryDeleting?'正在删除…':'确认删除';
}
function renderLibrary(){
  const filtered=filteredContents(), visibleIds=new Set(filtered.map(c=>c.id));
  for(const id of selectedContents)if(!visibleIds.has(id))selectedContents.delete(id);
  $('library-summary').textContent=`共 ${filtered.length} 份内容`;
  $('library-rows').innerHTML=filtered.map(c=>`<tr data-content-id="${c.id}"><td class="library-check"><input type="checkbox" data-select-content="${c.id}" aria-label="选择 ${escapeHTML(c.title)}"></td><td><div class="library-content"><img src="/api/contents/${c.id}/image" loading="lazy" alt="${escapeHTML(c.title)}"><div class="library-details"><strong>${escapeHTML(c.title)}</strong><small>${c.kind==='text'?'文本':'图片'}</small><small class="library-mobile-date">创建 ${escapeHTML(formatTime(c.created_at))}<br>${c.last_displayed?'最近显示 '+escapeHTML(formatTime(c.last_displayed)):'尚未显示'}</small></div></div></td><td class="library-date">${escapeHTML(formatTime(c.created_at))}</td><td class="library-date">${c.last_displayed?escapeHTML(formatTime(c.last_displayed)):'尚未显示'}</td><td class="library-row-actions"><div class="library-action-group"><button class="button primary library-action" data-display="${c.id}">显示</button><button class="button secondary library-action" data-schedule="${c.id}">排程</button><button class="button secondary library-action" data-rename="${c.id}">重命名</button></div></td></tr>`).join('');
  $('library-list').hidden=!filtered.length;$('library-empty').hidden=Boolean(filtered.length);
  $('library-empty').innerHTML=contents.length?'没有符合条件的内容':'这里还没有内容<br><a href="#image" class="text-link">上传第一张照片 ↗</a>';
  syncLibrarySelection();syncDisplayControls();
}
$('library-search').addEventListener('input',renderLibrary);$('library-filter').addEventListener('change',renderLibrary);
$('library-rows').addEventListener('change',e=>{
  const box=e.target.closest('[data-select-content]');if(!box || libraryDeleting)return;
  if(box.checked)selectedContents.add(box.dataset.selectContent);else selectedContents.delete(box.dataset.selectContent);
  syncLibrarySelection();
});
$('library-select-all').addEventListener('change',e=>{
  for(const c of filteredContents())if(e.target.checked)selectedContents.add(c.id);else selectedContents.delete(c.id);
  syncLibrarySelection();
});
$('library-rows').addEventListener('click',async e=>{
  const button=e.target.closest('button');if(!button)return;
  try{
    if(button.dataset.display)await displayContent(button.dataset.display);
    if(button.dataset.schedule)await openSchedule(null,button.dataset.schedule);
    if(button.dataset.rename)openRename(button.dataset.rename);
  }catch(err){toast(err.message,true);}
});
$('library-delete').addEventListener('click',()=>{
  if(libraryDeleting)return;
  deleteTargets=filteredContents().filter(c=>selectedContents.has(c.id));
  if(!deleteTargets.length)return;
  $('delete-message').textContent=`确认删除选中的 ${deleteTargets.length} 份内容？删除后无法恢复。`;
  $('delete-names').innerHTML=deleteTargets.map(c=>`<li>${escapeHTML(c.title)}</li>`).join('');
  $('delete-error').textContent='';$('delete-dialog').showModal();
});
for(const id of ['close-delete','cancel-delete'])$(id).addEventListener('click',()=>{if(!libraryDeleting)$('delete-dialog').close();});
$('delete-dialog').addEventListener('cancel',e=>{if(libraryDeleting)e.preventDefault();});
$('confirm-delete').addEventListener('click',async()=>{
  if(libraryDeleting || !deleteTargets.length)return;
  const ids=deleteTargets.map(c=>c.id);libraryDeleting=true;syncLibrarySelection();
  try{
    const result=await api('/api/contents/delete',{method:'POST',...json({content_ids:ids})});
    for(const id of result.deleted_ids)selectedContents.delete(id);
    for(const type of ['image','text'])if(result.deleted_ids.includes(previews[type]?.id))invalidate(type);
    $('delete-dialog').close();deleteTargets=[];
    await refreshContents();toast(`已删除 ${result.deleted_ids.length} 份内容`);
  }catch(e){$('delete-error').textContent=e.message;}
  finally{libraryDeleting=false;syncLibrarySelection();}
});
function syncRenameControls(){
  for(const id of ['rename-title','confirm-rename','cancel-rename','close-rename'])$(id).disabled=libraryRenaming;
  $('confirm-rename').textContent=libraryRenaming?'正在保存…':'保存';
}
function openRename(id){
  const item=contents.find(c=>c.id===id);if(!item)return;
  renameContentId=id;$('rename-title').value=item.title;$('rename-error').textContent='';
  syncRenameControls();$('rename-dialog').showModal();$('rename-title').focus();$('rename-title').select();
}
for(const id of ['close-rename','cancel-rename'])$(id).addEventListener('click',()=>{if(!libraryRenaming)$('rename-dialog').close();});
$('rename-dialog').addEventListener('cancel',e=>{if(libraryRenaming)e.preventDefault();});
$('rename-form').addEventListener('submit',async e=>{
  e.preventDefault();if(libraryRenaming || !renameContentId)return;
  const title=$('rename-title').value.trim();
  if(!title){$('rename-error').textContent='内容名称不能为空';$('rename-title').focus();return;}
  libraryRenaming=true;syncRenameControls();$('rename-error').textContent='';
  try{
    const item=await api(`/api/contents/${renameContentId}`,{method:'PATCH',...json({title})});
    for(const type of ['image','text'])if(previews[type]?.id===item.id){
      $(type+'-title').value=item.title;
      if(type==='image')previews[type].payload.set('title',item.title);else previews[type].payload.title=item.title;
    }
    contents=contents.map(c=>c.id===item.id?item:c);renderLibrary();
    $('rename-dialog').close();renameContentId=null;
    await Promise.all([refreshContents(),refreshSchedules(),refreshStatus()]);toast('内容名称已修改');
  }catch(err){if($('rename-dialog').open)$('rename-error').textContent=err.message;else toast(err.message,true);}
  finally{libraryRenaming=false;syncRenameControls();}
});
function invalidate(type){versions[type]++;previews[type]=null;syncDisplayControls();}
let selectedFile=null;
function selectFile(file){selectedFile=file;$('file-label').textContent=file?file.name:'点击选择，或拖入一张图片';if(file&&!$('image-title').value)$('image-title').value=file.name.replace(/\.[^.]+$/,'');invalidate('image');}
$('image-file').addEventListener('change',e=>selectFile(e.target.files[0]));
$('drop-zone').addEventListener('dragover',e=>{e.preventDefault();$('drop-zone').classList.add('dragging');});
$('drop-zone').addEventListener('dragleave',()=>$('drop-zone').classList.remove('dragging'));
$('drop-zone').addEventListener('drop',e=>{e.preventDefault();$('drop-zone').classList.remove('dragging');selectFile(e.dataTransfer.files[0]);});
document.querySelectorAll('[name="image-mode"]').forEach(el=>el.addEventListener('change',()=>invalidate('image')));
$('image-title').addEventListener('input',()=>invalidate('image'));
function imagePayload(){
  if(!selectedFile)throw new Error('请先选择图片');
  if(selectedFile.size>12*1024*1024)throw new Error('图片不能超过 12 MiB');
  const title=$('image-title').value.trim();if(!title)throw new Error('请填写内容名称');
  const form=new FormData();form.append('file',selectedFile);form.append('mode',document.querySelector('[name="image-mode"]:checked').value);form.append('title',title);return form;
}
// The browser editor emits a constrained document, never arbitrary HTML.
let selection=null;
const editor=$('text-editor');
document.addEventListener('selectionchange',()=>{const s=getSelection();if(s.rangeCount&&editor.contains(s.anchorNode)&&editor.contains(s.focusNode))selection=s.getRangeAt(0).cloneRange();});
function restoreSelection(){editor.focus();if(selection&&editor.contains(selection.commonAncestorContainer)){const s=getSelection();s.removeAllRanges();s.addRange(selection);}}
function command(name,value=null){restoreSelection();document.execCommand(name,false,value);invalidate('text');}
document.querySelectorAll('[data-command]').forEach(b=>{b.addEventListener('mousedown',e=>e.preventDefault());b.addEventListener('click',()=>command(b.dataset.command));});
$('font-family').addEventListener('change',e=>command('fontName',e.target.value));
$('font-size').addEventListener('change',e=>{command('fontSize','7');editor.querySelectorAll('font[size="7"]').forEach(f=>{f.removeAttribute('size');f.style.fontSize=e.target.value+'px';});invalidate('text');});
const colorNames={'#000000':'黑','#ffffff':'白','#ff0000':'红','#ffff00':'黄','#0000ff':'蓝','#00ff00':'绿'};
$('text-colors').innerHTML=Object.entries(colorNames).map(([c,n])=>`<button type="button" style="background:${c}" data-color="${c}" aria-label="${n}色文字" title="${n}色"></button>`).join('');
$('text-colors').addEventListener('mousedown',e=>e.preventDefault());
$('text-colors').addEventListener('click',e=>{if(e.target.dataset.color)command('foreColor',e.target.dataset.color);});
editor.addEventListener('input',()=>invalidate('text'));
editor.addEventListener('paste',e=>{e.preventDefault();command('insertText',e.clipboardData.getData('text/plain'));});
editor.addEventListener('drop',e=>e.preventDefault());
['text-title','text-background','text-padding'].forEach(id=>$(id).addEventListener('input',()=>invalidate('text')));
function textPayload(){
  const paragraphs=[];
  const nodes=[];
  function gather(node){
    if(node.nodeType===Node.TEXT_NODE){if(node.textContent)nodes.push(node);return;}
    if(node.nodeName==='BR'){nodes.push(node);return;}
    const block=node!==editor&&['P','DIV'].includes(node.nodeName);
    if(block&&nodes.length)flush(node.parentElement);
    const start=paragraphs.length;
    node.childNodes.forEach(gather);
    if(block){if(nodes.length)flush(node);else if(start===paragraphs.length)paragraphs.push({align:getComputedStyle(node).textAlign==='center'?'center':getComputedStyle(node).textAlign==='right'?'right':'left',runs:[{text:'',size:32}]});}
  }
  function flush(block){
    const align=getComputedStyle(block).textAlign;
    const runs=nodes.splice(0).map(n=>{
      const s=getComputedStyle(n.nodeType===Node.TEXT_NODE?n.parentElement:n);
      const font=s.fontFamily.split(',')[0].replaceAll(/["']/g,'').trim();
      const rgb=(s.color.match(/\d+/g)||['0','0','0']).slice(0,3).map(Number);
      let color='#'+rgb.map(x=>x.toString(16).padStart(2,'0')).join('');if(!(color in colorNames))color='#000000';
      return {text:n.nodeName==='BR'?'\n':n.textContent,color,font:['heiti','songti','kaiti','source-sans','source-serif'].includes(font)?font:'source-sans',size:Math.min(120,Math.max(12,Math.round(parseFloat(s.fontSize)))),bold:Number(s.fontWeight)>=600,italic:s.fontStyle==='italic'};
    });
    paragraphs.push({align:align==='center'?'center':align==='right'?'right':'left',runs});
  }
  gather(editor);if(nodes.length)flush(editor);
  if(!paragraphs.length||!editor.textContent.trim())throw new Error('请先输入文字');
  const title=$('text-title').value.trim();
  return {title,background:$('text-background').value,padding:Number($('text-padding').value),paragraphs};
}
async function preview(type){
  const button=$(type+'-preview');button.disabled=true;
  try{
    invalidate(type);const version=versions[type];const payload=type==='image'?imagePayload():textPayload();
    const blob=await api('/api/preview/'+type,{method:'POST',...(type==='image'?{body:payload}:json(payload))});
    if(version!==versions[type])return;
    if(urls[type])URL.revokeObjectURL(urls[type]);urls[type]=URL.createObjectURL(blob);
    $(type+'-result').src=urls[type];$(type+'-result').hidden=false;$(type+'-placeholder').hidden=true;
    previews[type]={payload,id:null,version};syncDisplayControls();
  }catch(e){toast(e.message,true);}finally{button.disabled=false;}
}
async function save(type,display){
  const preview=previews[type];if(!preview){toast('请先重新生成预览',true);return;}
  if(saving[type])return;
  saving[type]=true;syncDisplayControls();
  try{
    const persist=async()=>{
      if(!preview.id){const result=await api('/api/contents/'+type,{method:'POST',...(type==='image'?{body:preview.payload}:json(preview.payload))});preview.id=result.id;}
      return preview.id;
    };
    if(display)await requestDisplay(persist);else {await persist();toast('内容已保存，可在内容库和排程中使用');}
    await refreshContents();
  }catch(e){toast(e.message,true);}finally{saving[type]=false;syncDisplayControls();}
}
async function requestDisplay(resolveId){
  if(displaySubmitting || displayAwaitingStatus)throw new Error('正在提交刷新，请稍候');
  displaySubmitting=true;syncDisplayControls();
  try{
    // Preflight before saving a new content snapshot. The backend rechecks
    // atomically so another tab or a schedule cannot race this request.
    const latest=await api('/api/status');
    if(latest.display_busy)throw new Error('屏幕正在刷新或等待刷新，请完成后再提交');
    const id=await resolveId();
    const result=await api(`/api/contents/${id}/display`,{method:'POST'});
    displayAwaitingStatus=true; ++statusRequest; // Discard polls begun before acceptance.
    toast('已加入刷新队列，可在概览查看结果');
    return result;
  }finally{
    await refreshStatus();
    displaySubmitting=false;syncDisplayControls();
  }
}
async function displayContent(id){return requestDisplay(async()=>id);}
for(const type of ['image','text']){$(type+'-preview').addEventListener('click',()=>preview(type));$(type+'-save').addEventListener('click',()=>save(type,false));$(type+'-display').addEventListener('click',()=>save(type,true));}
async function refreshSchedules(){schedules=await api('/api/schedules');renderSchedules();}
function renderSchedules(){
  $('schedule-list').innerHTML=schedules.length?`<table><thead><tr><th>排程 / 内容</th><th>执行规则</th><th>下次执行</th><th>状态</th><th>操作</th></tr></thead><tbody>${schedules.map(s=>`<tr><td><strong>${escapeHTML(s.title)}</strong><small>${escapeHTML(s.content_title)}</small></td><td>${s.kind==='cron'?`<code>${escapeHTML(s.cron)}</code>`:escapeHTML(formatTime(s.run_at,{year:'numeric'}))}<small>${s.kind==='cron'?'循环排程':'单次排程'}</small></td><td>${escapeHTML(formatTime(s.next_run,{year:'numeric'}))}</td><td><span class="job-state">${s.enabled?'已启用':'已停用'}</span></td><td><button class="text-button" data-edit="${s.id}">编辑</button><button class="text-button danger" data-remove="${s.id}">删除</button></td></tr>`).join('')}</tbody></table>`:'<div class="empty-state">还没有排程<br>先在图片或文本页面保存一份内容，再安排它的显示时间。</div>';
}
function localDatetime(ts){
  const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:state?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(ts*1000));return parts.replace(' ','T');
}
// Convert device-local wall time to an instant, independently of browser timezone.
function zonedISO(value){
  if(!value)throw new Error('请选择执行时间');
  const target=Date.parse(value+'Z');let guess=target;
  for(let i=0;i<4;i++){
    const p=new Intl.DateTimeFormat('sv-SE',{timeZone:state?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(guess));
    const wall=Date.parse(p.replace(' ','T')+'Z');const delta=target-wall;if(delta===0)return new Date(guess).toISOString();guess+=delta;
  }
  throw new Error('此本地时间在设备时区中不存在，请选择其他时间');
}
function scheduleKind(){const once=$('schedule-kind').value==='once';$('once-field').hidden=!once;$('cron-field').hidden=once;}
$('schedule-kind').addEventListener('change',scheduleKind);
async function openSchedule(existing=null,contentId=null){
  await refreshContents();if(!contents.length)throw new Error('请先在图片或文本页面保存一份内容');
  $('schedule-form').reset();$('schedule-error').textContent='';
  $('schedule-content').innerHTML=contents.map(c=>`<option value="${c.id}">${escapeHTML(c.title)}</option>`).join('');
  $('schedule-id').value=existing?.id||'';$('schedule-dialog-title').textContent=existing?'编辑排程':'新建排程';
  $('schedule-title').value=existing?.title||'';$('schedule-content').value=existing?.content_id||contentId||contents[0].id;
  $('schedule-kind').value=existing?.kind||'cron';$('schedule-cron').value=existing?.cron||'*/30 * * * *';
  if(serverTimeBase===null){await refreshStatus();if(serverTimeBase===null)throw new Error('无法获取服务器时间，请稍后重试');}
  $('schedule-once').value=localDatetime(existing?.run_at||Math.ceil((serverNow()+60000)/60000)*60);
  $('schedule-enabled').checked=existing?Boolean(existing.enabled):true;scheduleKind();$('schedule-dialog').showModal();
}
$('new-schedule').addEventListener('click',()=>openSchedule().catch(e=>toast(e.message,true)));
['close-dialog','cancel-dialog'].forEach(id=>$(id).addEventListener('click',()=>$('schedule-dialog').close()));
$('schedule-form').addEventListener('submit',async e=>{
  e.preventDefault();const submit=e.submitter;submit.disabled=true;
  try{
    const kind=$('schedule-kind').value;
    const body={title:$('schedule-title').value,content_id:$('schedule-content').value,kind,enabled:$('schedule-enabled').checked,cron:kind==='cron'?$('schedule-cron').value:null,run_at:kind==='once'?zonedISO($('schedule-once').value):null};
    const id=$('schedule-id').value;await api('/api/schedules'+(id?'/'+id:''),{method:id?'PUT':'POST',...json(body)});
    $('schedule-dialog').close();toast('排程已保存');await refreshSchedules();await refreshStatus();
  }catch(err){$('schedule-error').textContent=err.message;}finally{submit.disabled=false;}
});
$('schedule-list').addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  try{if(b.dataset.edit)await openSchedule(schedules.find(s=>s.id===b.dataset.edit));
    if(b.dataset.remove&&confirm('删除此排程？已进入刷新队列的任务仍会执行。')){await api('/api/schedules/'+b.dataset.remove,{method:'DELETE'});await refreshSchedules();await refreshStatus();toast('排程已删除');}
  }catch(err){toast(err.message,true);}
});
syncDisplayControls();page();refreshStatus();refreshContents().catch(e=>toast(e.message,true));
setInterval(clock,1000);
// Recursive timer avoids overlapping requests when hardware telemetry is slow.
async function poll(){await refreshStatus();if(location.hash==='#schedule')await refreshSchedules().catch(()=>{});setTimeout(poll,state?.display_busy || displayAwaitingStatus ? 1000 : 4000);}setTimeout(poll,4000);
