'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = { view: 'notes', filter: 'all', notes: [], selected: null, detail: null, app: null, files: [], fileSelection: new Set(), folders: [], folderDraft: [], folderDirty: false, editing: false, seenJobs: new Set(), initialized: false, searchRequest: 0 };
Object.assign(state,{noteMode:'documents',channels:[],channel:null,channelRequest:0,detailRequest:0,historyAnswer:null,reading:new Set(),notesFor:null,collapsedChannel:null,navHTML:''});
try { if (window.localStorage?.getItem('dpt-note-mode')==='timeline') state.noteMode='timeline'; } catch (_) { /* Browser storage is optional. */ }
const inFolders = (path, folders) => folders.some(root => path === root || path.startsWith(root+'/'));
const statusLabels = { pending:'待识别', recognized:'待解读', needs_review:'待确认', answered:'已解答', error:'处理失败' };
const fmt = (value, short = false) => {
  if (!value) return '日期不确定';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return '日期不确定';
  return date.toLocaleString('zh-CN', short ? {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false} : {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
};
function toast(message, error = false) {
  const item = document.createElement('div'); item.className = 'toast'+(error?' error':''); item.textContent = message;
  $('#toast-stack').append(item); setTimeout(() => item.remove(), 6500);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result = await response.json();
  if (response.status === 401) { window.location.assign('/'); throw new Error('请重新登录'); }
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
async function job(path, body = {}) {
  const result = await api(path, body);
  toast(result.queued ? '已加入处理队列' : result.message);
  await refreshState();
}
function badge(note) {
  if (note.ignored) return '<span class="badge pending">已忽略</span>';
  if (note.status === 'answered') return '';
  const style = note.status === 'needs_review' ? 'review' : note.status === 'error' ? 'error' : 'pending';
  return `<span class="badge ${style}">${esc(statusLabels[note.status] || note.status)}</span>`;
}
function stat(number, label) { return `<div class="stat"><strong>${esc(number)}</strong><span>${esc(label)}</span></div>`; }
function renderStats(counts) {
  for (const [filter,key] of [['all','notes'],['unread','unread'],['pending','pending'],['needs_review','needs_review'],['ignored','ignored']]) {
    const value=Number(counts[key])||0, item=$('#count-'+filter);
    item.textContent=filter==='all' || value ? String(value) : '';
    item.classList.toggle('hot',filter==='unread' && value>0);
  }
  $('#unread-badge').hidden = !counts.unread; $('#unread-badge').textContent = counts.unread;
}
async function refreshState() {
  try {
    const app = await api('/api/state');
    const previous = state.app;
    state.app = app;
    const roots = app.settings.sync_folders || [];
    if (!state.folderDirty) state.folderDraft = [...roots];
    $('#pdf-upload').disabled = !roots.length;
    $$('[data-action="logout"]').forEach(button => button.hidden = !app.auth_required);
    $('#connection-error').hidden = true;
    const counts = app.stats;
    renderStats(counts);
    $('#last-sync').textContent = app.settings.last_sync ? '上次同步 '+fmt(app.settings.last_sync,true) : '尚未同步';
    $('#device-state').textContent = app.online === true ? '已连接 · '+app.settings.device_address : app.online === false ? '等待设备唤醒' : (app.settings.paired ? '已配对 · 待连接' : '尚未配对');
    $('#paired-label').textContent = app.settings.paired ? '已保存配对' : '尚未配对';
    $('#key-state').textContent = app.settings.has_api_key ? '已保存密钥' : '未保存密钥';
    const active = app.jobs.find(item => ['queued','running','waiting_pin'].includes(item.status));
    $('#job-strip').hidden = !active;
    if (active) $('#job-strip').innerHTML = `<span class="spinner"></span>${esc(active.message)}${active.total ? ` · ${active.progress}/${active.total}`:''}`;
    $('#pin-box').hidden = !app.jobs.some(item => item.status === 'waiting_pin');
    $$('[data-action="sync"]').forEach(button => button.disabled = !!app.jobs.find(item => item.kind === 'sync' && ['queued','running'].includes(item.status)));
    let inventoryChanged = false;
    for (const item of app.jobs) {
      if (['complete','error','interrupted'].includes(item.status) && !state.seenJobs.has(item.id) && state.initialized) {
        toast(item.message, item.status !== 'complete');
        if (['mkdir','move'].includes(item.kind) || (item.status === 'complete' && ['inventory','sync','upload'].includes(item.kind))) inventoryChanged = true;
        if (item.status === 'complete' && item.kind === 'move') state.fileSelection.clear();
        if (item.status === 'complete' && item.kind === 'mkdir' && !$('#move-target').value) {
          try { $('#move-target').value = JSON.parse(item.result).path || ''; } catch (_) { /* A completed job may have no path. */ }
        }
      }
      if (['complete','error','interrupted'].includes(item.status)) state.seenJobs.add(item.id);
    }
    $('#activity-list').innerHTML = app.events.length ? app.events.map(item => `<div class="activity-item"><span>${esc(item.message)}</span><time>${esc(fmt(item.created_at,true))}</time></div>`).join('') : '<p class="helper">完成同步和解读后，活动会显示在这里。</p>';
    if (!state.initialized) fillSettings(app.settings);
    state.initialized = true;
    if (state.view === 'notes') await loadNotes(false);
    else if (state.view === 'files' && (inventoryChanged || !previous || previous.settings.last_sync !== app.settings.last_sync || JSON.stringify(previous.settings.sync_folders)!==JSON.stringify(roots))) await loadFiles();
  } catch (error) { $('#connection-error').hidden = false; }
}
function fillSettings(settings) {
  const form = $('#settings-form');
  for (const key of ['device_address','upload_folder','model_base_url','model_name','sync_interval','sync_clock','auto_sync','auto_analyze']) {
    const field = form.elements.namedItem(key);
    if (field.type === 'checkbox') field.checked = settings[key]; else field.value = settings[key] ?? '';
  }
}
async function saveSettings() {
  const form = $('#settings-form');
  const values = {};
  for (const key of ['device_address','upload_folder','model_base_url','model_name','sync_interval']) values[key] = form.elements.namedItem(key).value.trim();
  for (const key of ['sync_clock','auto_sync','auto_analyze']) values[key] = form.elements.namedItem(key).checked;
  const key = form.elements.namedItem('model_api_key').value.trim();
  if (key || form.elements.namedItem('clear_api_key').checked) values.model_api_key = form.elements.namedItem('clear_api_key').checked ? '' : key;
  const settings = await api('/api/settings', values);
  form.elements.namedItem('model_api_key').value = '';
  form.elements.namedItem('clear_api_key').checked = false;
  $('#settings-feedback').textContent = '已保存';
  state.app.settings = settings; fillSettings(settings); await refreshState();
}
async function changeView(view) {
  state.view = view;
  $$('.view').forEach(item => item.hidden = item.id !== 'view-'+view);
  $$('[data-view]').forEach(item => item.classList.toggle('active',item.dataset.view === view));
  $('#page-label').textContent = {notes:'笔记与解答',files:'设备文件',report:'笔记回顾',settings:'连接与设置'}[view];
  if (view === 'notes') await loadNotes(false);
  if (view === 'files') await loadFiles();
  if (view === 'report') await loadReport();
  if (view === 'settings' && state.app) fillSettings(state.app.settings);
}
function updateNoteMode() {
  const grouped=state.noteMode==='documents';
  $('#notes-workspace').classList.toggle('document-mode',grouped);
  $$('[data-note-mode]').forEach(button=>{const active=button.dataset.noteMode===state.noteMode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
}
async function loadNotes(force=false) {
  updateNoteMode();
  if (state.noteMode==='documents') return await loadChannels(force);
  return await loadNoteList(force);
}
const clock = value => { const date=new Date(value); return Number.isNaN(date.valueOf()) ? '' : date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}); };
function dayLabel(note) {
  if (!note.time_trusted) return '历史导入';
  const date=new Date(note.device_modified);
  if (Number.isNaN(date.valueOf())) return '日期不确定';
  const today=new Date(), days=Math.round((new Date(today.getFullYear(),today.getMonth(),today.getDate())-new Date(date.getFullYear(),date.getMonth(),date.getDate()))/86400000);
  return days===0?'今天':days===1?'昨天':date.toLocaleDateString('zh-CN',{year:date.getFullYear()===today.getFullYear()?undefined:'numeric',month:'long',day:'numeric'});
}
function noteRowHTML(note,timeline) {
  const unread=note.unread && !note.ignored;
  const time=note.time_trusted?(timeline?clock(note.device_modified):fmt(note.device_modified,true)):(timeline?'':'历史导入');
  return `<button class="nav-note${state.selected===note.id?' selected':''}${unread?' unread':''}" data-note="${note.id}"><span class="nav-note-head"><span class="nav-note-title">${unread?'<span class="dot" aria-hidden="true"></span><span class="sr-only">新回复：</span>':''}${esc(note.title)}</span><time>${esc(time)}</time></span><span class="nav-note-excerpt">${esc(note.question || note.transcript || '尚未识别笔迹，可打开原图核对。')}</span><span class="nav-note-foot"><span class="nav-note-origin">P${Number(note.page)}${timeline?' · '+esc(note.document_title):''}</span>${badge(note)}</span></button>`;
}
function navHTML() {
  if (state.noteMode!=='documents') {
    let group=null;
    return state.notes.length?state.notes.map(note=>{const day=dayLabel(note),head=day!==group?`<div class="nav-day">${esc(day)}</div>`:'';group=day;return head+noteRowHTML(note,true);}).join(''):'<div class="empty">这里没有符合条件的笔记。</div>';
  }
  const channels=state.channel && !state.channels.some(item=>item.id===state.channel.id)?[state.channel,...state.channels]:state.channels;
  if (!channels.length) return `<div class="empty">${state.filter==='ignored'?'没有已忽略的笔记。':state.filter==='unread'?'暂时没有新回复。':'还没有匹配的 PDF 笔记。'}</div>`;
  return channels.map(channel=>{
    const selected=state.channel?.id===channel.id, open=selected && state.collapsedChannel!==channel.id;
    const count=Number(channel.notes ?? (selected?state.notes.length:0)), unread=Number(channel.unread)||0;
    const meta=[`${count} 条笔记`,state.filter==='ignored'?'已忽略':channel.needs_review?`${Number(channel.needs_review)} 条待确认`:'',channel.last_activity?fmt(channel.last_activity,true):''].filter(Boolean).join(' · ');
    const children=state.notesFor!==channel.id?'<div class="nav-hint">正在读取…</div>':state.notes.length?state.notes.map(note=>noteRowHTML(note,false)).join(''):'<div class="nav-hint">这里没有符合条件的笔记。</div>';
    return `<div class="nav-doc${open?' open':''}${selected?' selected':''}"><button class="nav-doc-row" data-channel="${esc(channel.id)}" title="${esc(channel.remote_path)}" aria-expanded="${open}"><span class="caret" aria-hidden="true">›</span><span class="nav-doc-text"><span class="nav-doc-title">${esc(channel.title)}</span><span class="nav-doc-meta">${esc(meta)}</span></span>${unread?`<span class="count-pill hot" aria-label="${unread} 条未读回复">${unread}</span>`:`<span class="count-pill" aria-label="${count} 条笔记">${count}</span>`}</button>${open?`<div class="nav-notes">${children}</div>`:''}</div>`;
  }).join('');
}
function renderNav() {
  const nav=$('#note-nav'), html=navHTML();
  if (html!==state.navHTML) { const top=nav.scrollTop; nav.innerHTML=html; state.navHTML=html; nav.scrollTop=top; }
  $('#nav-count').textContent=state.noteMode==='documents'?`${state.channels.length} 份 PDF · ${state.channels.reduce((sum,item)=>sum+Number(item.unread),0)} 条新回复`:`${state.notes.length} 条笔记`;
  updatePager();
}
function updatePager() {
  const index=state.notes.findIndex(note=>note.id===state.detail?.id), previous=$('#prev-note'), next=$('#next-note');
  if (previous) previous.disabled=index<=0;
  if (next) next.disabled=index<0 || index>=state.notes.length-1;
}
async function moveNote(step) {
  const target=state.notes[state.notes.findIndex(note=>note.id===state.selected)+step];
  if (!target || state.editing) return;
  await openNote(target.id);
  $(`#note-nav [data-note="${target.id}"]`)?.scrollIntoView?.({block:'nearest'});
}
async function loadChannels(force=false) {
  const request=++state.channelRequest;
  const channels=await api('/api/channels?status='+encodeURIComponent(state.filter)+'&q='+encodeURIComponent($('#note-search').value));
  if (request!==state.channelRequest || state.noteMode!=='documents') return;
  state.channels=channels;
  const previous=state.channel?.id;
  const current=channels.find(channel=>channel.id===previous);
  const keep=state.editing || (state.filter==='unread' && state.selected && state.detail?.document_id===previous);
  state.channel=current || (keep?state.channel:null) || channels[0] || null;
  if (previous!==state.channel?.id && !state.editing) {state.selected=null;state.detail=null;state.historyAnswer=null;}
  renderNav();
  if (state.channel) await loadNoteList(force);
  else if (!state.editing) {
    state.notes=[];state.notesFor=null;state.selected=null;state.detail=null;
    renderNav();
    $('#note-detail').innerHTML='<div class="empty"><strong>先选择一份 PDF</strong>左侧选择文档，再从笔记列表中打开一条笔记。</div>';
  }
}
function detailSignature(note) {
  if (!note) return '';
  return JSON.stringify(['id','document_id','document_title','title','transcript','question','anchor','status','ignored','content_hash','answer_id','unread','in_sync_scope','time_trusted','device_modified','confidence','manual_transcript','confirmation_stale','scope_conflicts','question_refs','tags','answer','answer_model','answer_provenance'].map(key=>note[key]));
}
async function loadNoteList(force=false) {
  const request=++state.searchRequest;
  const mode=state.noteMode;
  const documentId=mode==='documents'?state.channel?.id:null;
  let search=$('#note-search').value;
  if (documentId && search && (state.channel.title+' '+(state.channel.name || '')).toLowerCase().includes(search.toLowerCase())) search='';
  const path='/api/notes?status='+encodeURIComponent(state.filter)+'&q='+encodeURIComponent(search)+(documentId?'&document_id='+encodeURIComponent(documentId):'');
  const notes=await api(path);
  if (request!==state.searchRequest || state.noteMode!==mode || (documentId && state.channel?.id!==documentId)) return;
  state.notes=notes;state.notesFor=documentId;
  const current=notes.find(note=>note.id===state.selected);
  const keepRead=state.filter==='unread' && state.detail && state.detail.id===state.selected && (!documentId || state.detail.document_id===documentId);
  if (!current && !state.editing && !state.historyAnswer && !keepRead) {
    state.selected=notes[0]?.id || null;state.detail=null;
  }
  renderNav();
  if (state.selected && !state.editing && !state.historyAnswer && (force || !state.detail || (current && detailSignature(current)!==detailSignature(state.detail)))) await openNote(state.selected,false);
  else if (!state.selected && !state.editing) $('#note-detail').innerHTML='<div class="empty"><strong>选择一条笔记</strong>在笔记列表中选择，查看原始笔迹、正文与解答。</div>';
  await markCurrentNoteRead();
}
async function openChannel(identifier) {
  const channel=state.channels.find(item=>item.id===identifier);
  if (!channel) return;
  ++state.detailRequest;
  state.channel=channel;state.collapsedChannel=null;state.selected=null;state.detail=null;state.editing=false;state.historyAnswer=null;state.notes=[];state.notesFor=null;
  renderNav();
  $('#note-detail').innerHTML='<div class="empty">正在读取这份 PDF 的笔记…</div>';
  await loadNoteList(true);
}
async function openNote(identifier,markRead=true) {
  const request=++state.detailRequest;
  state.selected=Number(identifier);state.editing=false;state.historyAnswer=null;state.collapsedChannel=null;
  renderNav();
  const note=await api('/api/notes/'+state.selected);
  if (request!==state.detailRequest || note.id!==state.selected || state.editing) return;
  const differentDocument=state.noteMode==='documents' && state.channel?.id!==note.document_id;
  if (state.noteMode==='documents') state.channel=state.channels.find(channel=>channel.id===note.document_id) || {...(state.channel?.id===note.document_id?state.channel:{}),id:note.document_id,title:note.document_title,remote_path:note.remote_path,unread:0};
  state.detail=note;
  renderDetail(note);renderNav();
  if (markRead) await markCurrentNoteRead();
  if (differentDocument) await loadNoteList(false);
}
async function markCurrentNoteRead() {
  const note=state.detail;
  if (document.hidden || state.view!=='notes' || state.editing || state.historyAnswer || !note || note.id!==state.selected || !note.unread || !note.answer_id || note.ignored || state.reading.has(note.answer_id)) return;
  const answer=note.answer_id;
  state.reading.add(answer);
  try {
    const result=await api(`/api/notes/${note.id}/read`,{answer_id:answer});
    if (!result.marked.includes(answer)) return;
    if (state.detail?.answer_id===answer) state.detail.unread=0;
    for (const item of state.notes) if (item.answer_id===answer) item.unread=0;
    const channel=state.channels.find(item=>item.id===note.document_id);
    if (channel) channel.unread=Math.max(0,Number(channel.unread)-1);
    if (state.channel?.id===note.document_id && state.channel!==channel) state.channel.unread=Math.max(0,Number(state.channel.unread || 0)-1);
    if (state.app) {state.app.stats.unread=Math.max(0,state.app.stats.unread-1);renderStats(state.app.stats);}
    const markButton=$('#note-detail [data-action="mark-read"]');
    if (markButton && state.detail?.answer_id===answer) markButton.hidden=true;
    renderNav();
  } finally {state.reading.delete(answer);}
}
async function setNoteIgnored(identifier,ignored) {
  await api(`/api/notes/${identifier}/ignore`,{ignored});
  ++state.detailRequest;
  state.editing=false;state.selected=null;state.detail=null;state.historyAnswer=null;
  toast(ignored?'已忽略；可在“已忽略”中恢复':'笔记已恢复');
  await refreshState();
}
function renderDetail(note, historical = null) {
  state.historyAnswer=historical?.id || null;
  const answer = historical ? historical.payload : note.answer;
  const provenance = historical ? historical.provenance : note.answer_provenance;
  const source = historical ? historical.model : note.answer_model;
  const tags = (note.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('');
  const tools = answer && !note.ignored ? `<button class="button quiet small" data-action="analyze">重新解读</button>${note.unread?'<button class="button quiet small" data-action="mark-read">标为已读</button>':''}${note.history?.length>1?`<select id="answer-history" aria-label="解读历史"><option value="">当前解读</option>${note.history.map(item=>`<option value="${item.id}"${historical?.id===item.id?' selected':''}>${esc(fmt(item.created_at,true))} · ${esc(item.model)}</option>`).join('')}</select>`:''}` : '';
  $('#note-detail').innerHTML = `<div class="detail-bar"><button class="button quiet small back-button" data-action="back-to-list">← 返回列表</button><div class="detail-crumb" title="${esc(note.remote_path || note.document_title)}"><span class="crumb-doc">${esc(note.document_title)}</span><span class="crumb-rest"><span class="crumb-sep" aria-hidden="true">›</span>P${note.page} · ${note.time_trusted ? esc(fmt(note.device_modified,true)):'历史批注 · 日期不确定'}</span></div><div class="detail-actions"><button id="prev-note" class="icon-button" data-action="prev-note" aria-label="上一条笔记" title="上一条（k）">‹</button><button id="next-note" class="icon-button" data-action="next-note" aria-label="下一条笔记" title="下一条（j）">›</button><a class="button quiet small" href="${esc(note.pdf_url)}" target="_blank" rel="noopener"><span class="label-long">打开 PDF 第 ${note.page} 页</span><span class="label-short">PDF</span> ↗</a><button class="button quiet small" data-ignore-note="${note.id}" data-ignored="${note.ignored?'false':'true'}">${note.ignored?'恢复笔记':'忽略这条笔记'}</button></div></div>
    <div class="detail-body"><header class="detail-head"><h2>${esc(note.title)}</h2><div class="tag-row">${tags}${note.status==='needs_review' && !note.ignored?'<span class="badge review">待确认 · 请先核对转写</span>':badge(note)}</div>
    ${note.ignored?'<div class="notice">已忽略，暂停解读，可随时恢复。</div>':''}
    ${note.confirmation_stale?'<div class="notice">笔迹已变更，需要重新确认。先前确认的文字与解答已保留；请核对转写，拆分或合并的内容也需重新确认。</div>':''}
    ${note.error?`<div class="notice error-text">${esc(note.error)}</div>`:''}
    ${note.in_sync_scope === false?'<div class="notice">这份文档不在当前同步文件夹内，已保留归档内容。若需继续解读，请在设备文件中添加所属文件夹。</div>':''}
    ${!note.time_trusted?'<p class="helper">这条批注的设备时间异常，只作为历史笔记归档，不计入近期统计。</p>':''}</header>
    <div class="detail-panels"><section class="ink-panel" aria-label="原始笔迹"><div class="panel-title"><strong>原始笔迹</strong><span>${note.confirmation_stale?'笔迹已变更 · 待重新确认':note.manual_transcript?'转写已人工核对':note.confidence!=null?'识别置信度 '+Math.round(note.confidence*100)+'%':'等待识别'}</span></div>
    <div class="ink-frame">${note.preview_url?`<img src="${esc(note.preview_url)}?v=${esc(note.content_hash.slice(0,12))}" alt="第${note.page}页的原始笔迹和正文" loading="lazy" data-action="zoom-ink" title="点击放大">`:'<div class="empty">暂无笔迹截图</div>'}</div>
    <div id="transcript-area"><div class="transcript-card"><div class="transcript-head"><span class="transcript-label">手写转写 / 提出的问题</span><button class="button quiet small" data-action="edit-note" ${note.ignored?'disabled':''}>${note.ignored?'恢复后可校正':note.status==='needs_review'?'核对转写':'校正文字'}</button></div><p>${esc(note.transcript || '尚未转写，可以手动填写，也可以使用视觉模型识别。')}</p>${note.question && note.question !== note.transcript?`<p class="muted">${esc(note.question)}</p>`:''}${note.anchor?`<p class="muted">标记：${esc(note.anchor)}</p>`:''}</div></div>${revisionHTML(note)}</section>
    <section class="answer-panel" aria-label="解答">${answer ? answerHTML(answer, provenance, source, note.status !== 'answered' || !!historical, tools) : note.ignored?'<div class="empty">这条笔记已忽略，原始笔迹仍在归档中。</div>': `<div class="empty"><strong>${note.status==='needs_review'?'转写待确认':'尚未解读'}</strong>${note.status==='needs_review'?'点“核对转写”，确认文字后再解读。':'识别笔迹并结合原文生成解答。'}<br><button class="button primary" data-action="analyze">${note.status==='needs_review'?'重新识别':'识别并解读'}</button></div>`}</section></div></div>`;
  updatePager();
}
function revisionHTML(note) {
  const revisions = note.revisions || [], lineage = note.lineage || [];
  if (!revisions.length && !lineage.length) return '';
  const reasons = {handwriting_changed:'笔迹变更前',manual_edit:'人工校正前',archived:'归档前',historical_repair:'历史修复前'};
  return `<details class="transcript-card"><summary>笔记修订与关联记录</summary>${revisions.map(item=>`<div class="notice"><strong>${esc(reasons[item.reason] || item.reason)} · ${esc(fmt(item.created_at,true))}</strong><p>${esc(item.note.title)}</p><p>${esc(item.note.transcript || item.note.question || item.note.anchor || '无转写文字')}</p></div>`).join('')}${lineage.map(item=>`<details><summary>${item.relation==='split'?'拆分关联':'合并关联'}：#${Number(item.id)} ${esc(item.title)}${item.archived?'（已归档）':''}</summary><p>${esc(item.transcript || item.question || item.anchor || '无转写文字')}</p>${item.answer?`<p class="helper">关联笔记的先前解答</p><p>${esc(item.answer.summary)}</p><p>${esc(item.answer.explanation)}</p>${exerciseReviewsHTML(item.answer.exercise_reviews)}`:''}</details>`).join('')}</details>`;
}
function sourceHTML(value) {
  const text = String(value); const match = text.match(/https:\/\/[^\s<>]+/);
  if (!match) return esc(text);
  return esc(text.slice(0,match.index))+`<a href="${esc(match[0])}" target="_blank" rel="noopener noreferrer">查看资料 ↗</a>`+esc(text.slice(match.index+match[0].length));
}
function exerciseReviewsHTML(reviews) {
  if (!Array.isArray(reviews)) return '';
  const entries = reviews.filter(item=>item && typeof item==='object').slice(0,8);
  if (!entries.length) return '';
  return '<h3>作答核对与解析</h3>'+entries.map(item=>{
    const verdict = ['correct','incorrect'].includes(item.verdict) ? item.verdict : 'uncertain';
    const label = {correct:'模型判断：答对',incorrect:'模型判断：答错',uncertain:'暂不能判定'}[verdict];
    const basis = {document:'依据文中答案（模型核对）',reasoning:'模型推导的参考解答',insufficient:'需要补充题目或作答信息'}[item.basis] || '需要补充题目或作答信息';
    const points = Array.isArray(item.knowledge_points) ? item.knowledge_points.filter(point=>typeof point==='string') : [];
    return `<article class="exercise-review" data-verdict="${verdict}"><div class="row wrap"><strong>${esc(item.question_ref || '当前题目')}</strong><span class="badge ${verdict==='incorrect'?'error':verdict==='uncertain'?'review':''}">${label}</span></div>${item.question?`<p class="answer-body">${esc(item.question)}</p>`:''}<p>你的作答：${esc(item.user_answer || '待确认')}</p><p>建议答案：${esc(item.suggested_answer || '待补充')}</p><p class="helper">${basis}</p>${item.reference_quote?`<blockquote>${esc(item.reference_quote)}</blockquote>`:''}<div class="answer-body">${esc(item.explanation)}</div>${points.length?'<h4>需要掌握的知识点</h4><ul>'+points.map(point=>`<li>${esc(point)}</li>`).join('')+'</ul>':''}</article>`;
  }).join('');
}
function answerHTML(answer, provenance, source, previous, tools = '') {
  const examples = Array.isArray(answer.examples) ? answer.examples.filter(v=>v && typeof v==='object') : [];
  const sources = Array.isArray(answer.sources) ? answer.sources : [];
  const uncertainties = Array.isArray(answer.uncertainties) ? answer.uncertainties : [];
  return `<div class="answer-header"><h3>解答</h3><span class="muted answer-source">${esc(provenance==='verified_import'?'核对资料后导入':source || '模型解读')}</span>${tools?`<div class="answer-tools">${tools}</div>`:''}</div><div class="answer-content">${previous?'<div class="notice">正在查看先前的解答。笔记修改后，请重新解读以匹配最新内容。</div>':''}<p class="answer-summary">${esc(answer.summary)}</p><div class="answer-body">${esc(answer.explanation)}</div>
    ${exerciseReviewsHTML(answer.exercise_reviews)}
    ${examples.length?'<h3>补充示例</h3>'+examples.map(item=>`<div class="example"><p class="example-text">${esc(item.text ?? item.ja)}</p><p class="example-explanation">${esc(item.explanation ?? item.zh)}</p></div>`).join(''):''}
    <div class="answer-foot">${uncertainties.map(item=>`<p>${esc(item)}</p>`).join('')}${sources.length?'<p>参考与上下文</p>'+sources.map(item=>`<p>· ${sourceHTML(item)}</p>`).join(''):''}${provenance!=='verified_import'?'<p>模型解读依据提供的上下文生成，关键内容请结合原文核对。</p>':''}</div></div>`;
}
function editNote() {
  const note = state.detail; if (!note) return;
  state.editing = true;
  $('#transcript-area').innerHTML = `<form id="edit-note-form" class="edit-form"><label>笔记标题<input name="title" value="${esc(note.title)}"></label><label>手写原文<textarea name="transcript">${esc(note.transcript)}</textarea></label><label>标记的原文<input name="anchor" value="${esc(note.anchor)}"></label><label>希望解答的问题<textarea name="question">${esc(note.question)}</textarea></label>${note.kind==='answer'?`<label>当前笔迹对应的题号<input name="question_refs" value="${esc((note.question_refs || []).join(', '))}" placeholder="例如 8, 9；分节题号用 2:8"></label>`:''}<div class="row"><button class="button primary small" type="submit">确认并解读</button><button class="button quiet small" type="button" data-action="save-transcript">只保存文字</button><button class="text-button" type="button" data-action="cancel-edit" aria-label="取消编辑">×</button></div></form>`;
}
async function saveTranscript(analyze) {
  const form = $('#edit-note-form'); if (!form) return;
  const values = Object.fromEntries(new FormData(form));
  await api(`/api/notes/${state.selected}/edit`, values);
  state.editing = false;
  await openNote(state.selected, false);
  if (analyze) await job(`/api/notes/${state.selected}/analyze`); else toast('转写已保存');
}
async function loadFiles() {
  [state.files, state.folders] = await Promise.all([api('/api/documents'),api('/api/folders')]);
  const present = new Set(state.files.filter(file=>file.device_present!==0).map(file=>file.id));
  state.fileSelection = new Set([...state.fileSelection].filter(identifier=>present.has(identifier)));
  if (!state.folderDirty) state.folderDraft = [...(state.app?.settings.sync_folders || [])];
  renderFolders(); renderFiles();
}
function renderFolders() {
  const folders = new Map(state.folders.map(item=>[item.path,item]));
  for (const path of state.folderDraft) if (!folders.has(path)) folders.set(path,{path,documents:0});
  $('#folder-list').innerHTML = [...folders.values()].sort((a,b)=>a.path.localeCompare(b.path)).map(folder=>{
    const selected = state.folderDraft.includes(folder.path);
    const inherited = !selected && inFolders(folder.path,state.folderDraft);
    return `<label class="folder-row"><input type="checkbox" data-folder="${esc(folder.path)}" ${selected?'checked':''}><span>${esc(folder.path)}${folder.path==='Document'?'（设备全部文档）':''}</span><small>${inherited?'已包含在上级目录中':`${folder.documents} 份 PDF`}</small></label>`;
  }).join('') || '<p class="helper">先刷新设备目录，或在下面填写文件夹路径。</p>';
  $('#folder-options').innerHTML = state.folders.map(folder=>`<option value="${esc(folder.path)}"></option>`).join('');
  $('#folder-summary').textContent = `${state.folderDraft.length} 个已选目录 · 包含子文件夹${state.folderDirty?' · 尚未保存':''}`;
}
async function saveScope() {
  const settings = await api('/api/settings',{sync_folders:state.folderDraft});
  state.app.settings = settings; state.folderDraft = [...settings.sync_folders]; state.folderDirty = false;
  fillSettings(settings); await loadFiles(); await refreshState();
  toast(settings.sync_folders.length?'同步范围已保存，新文档也会自动加入':'已暂停同步，历史笔记仍保留');
}
function renderFiles() {
  const search = $('#file-search').value.toLowerCase();
  const files = state.files.filter(file => (!$('#tracked-only').checked || file.tracked) && (file.name+file.remote_path).toLowerCase().includes(search));
  $('#file-count').textContent = `${files.length} / ${state.files.length} 份 PDF`;
  $('#file-list').innerHTML = files.length ? files.map(file => `<tr><td><input type="checkbox" data-file-select="${file.id}" ${state.fileSelection.has(file.id)?'checked':''} ${file.device_present===0?'disabled':''} aria-label="选择 ${esc(file.name)}"></td><td><strong>${esc(file.name)}</strong><small>${esc(file.remote_path)}</small><small class="compact-meta">${file.pages || '—'} 页 · ${file.notes} 条笔记</small></td><td class="col-optional">${file.pages || '—'} 页 / ${file.notes} 条</td><td class="col-optional muted">${esc(fmt(file.device_modified,true))}</td><td><span class="badge ${file.tracked?'':'pending'}">${file.device_present===0?'设备中已移除':file.tracked?'已包含':'范围外'}</span></td><td><div class="row">${file.tracked?`<button class="button quiet small" data-pull="${file.id}">拉取</button>`:''}${file.snapshot_id || file.has_baseline?`<a class="button quiet small" href="/media/document/${file.id}" target="_blank" rel="noopener">打开</a>`:''}</div></td></tr>`).join('') : '<tr><td colspan="6"><div class="empty">没有匹配的文件。选择同步文件夹并刷新设备目录后，文件会显示在这里。</div></td></tr>';
  $('#bulk-bar').hidden = !state.fileSelection.size;
  $('#move-selection-count').textContent = `已选择 ${state.fileSelection.size} 份文档`;
  $('#move-files-button').disabled = !state.fileSelection.size;
  const movable = files.filter(file=>file.device_present!==0);
  $('#select-visible-files').checked = movable.length>0 && movable.every(file=>state.fileSelection.has(file.id));
  $('#select-visible-files').indeterminate = !$('#select-visible-files').checked && movable.some(file=>state.fileSelection.has(file.id));
}
async function loadReport() {
  const report = await api('/api/report?days='+$('#report-days').value);
  $('#report-content').innerHTML = `<div class="stats-row">${stat(report.recent_notes,'本期更新的笔记')}${stat(report.answered,'已整理出解答')}${stat(report.needs_review,'待确认笔迹')}${stat(report.uncertain_time,'日期不确定的旧笔记')}</div><div class="report-grid"><section class="surface report-card"><h2>最近记录的主题</h2>${report.topics.length?report.topics.map(([tag,count])=>`<div class="topic-row"><span>${esc(tag)}</span><span>${count} 条笔记</span></div>`).join(''):'<p class="helper">本期还没有已标注主题的笔记。同步并解读后会在这里归纳。</p>'}<p class="helper">本页只统计当前同步文件夹中的笔记，范围外的历史记录仍可在知识库查看。</p></section><section class="surface report-card"><h2>尚待处理</h2>${report.next_steps.length?report.next_steps.map((item,index)=>`<div class="next-step"><span>${String(index+1).padStart(2,'0')}</span><button data-goto-note="${item.id}">${esc(item.title)}<small>${esc(item.reason)}</small></button></div>`).join(''):'<p class="helper">当前同步范围内没有待处理的笔记。</p>'}</section><section class="surface report-card"><h2>这份回顾的依据</h2><p class="helper">${esc(report.limitation)}</p><p class="helper">按可信的设备批注修改时间统计，每处笔记只计一次。修改日期会随后续编辑更新。</p><p class="helper">生成时间：${esc(fmt(report.generated_at))}</p></section></div>`;
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-view],[data-filter],[data-note],[data-action],[data-pull],[data-goto-note],[data-note-mode],[data-channel],[data-open-note],[data-ignore-note]'); if (!button) return;
  try {
    if (button.dataset.view) return await changeView(button.dataset.view);
    if (button.dataset.noteMode) {
      state.noteMode=button.dataset.noteMode;state.editing=false;state.selected=null;state.detail=null;state.historyAnswer=null;++state.detailRequest;
      try {window.localStorage?.setItem('dpt-note-mode',state.noteMode);} catch (_) { /* Browser storage is optional. */ }
      $('#note-nav').scrollTop=0;
      return await loadNotes(true);
    }
    if (button.dataset.filter) { state.filter = button.dataset.filter;state.channel=null;state.editing=false;state.selected=null;state.detail=null;state.historyAnswer=null;++state.detailRequest; $$('.filter').forEach(item=>item.classList.toggle('active',item===button)); $('#note-nav').scrollTop=0; return await loadNotes(true); }
    if (button.dataset.channel) {
      const identifier=button.dataset.channel;
      if (state.channel?.id===identifier && state.notesFor===identifier) { state.collapsedChannel=state.collapsedChannel===identifier?null:identifier; return renderNav(); }
      return await openChannel(identifier);
    }
    if (button.dataset.openNote) return await openNote(button.dataset.openNote);
    if (button.dataset.ignoreNote) return await setNoteIgnored(Number(button.dataset.ignoreNote),button.dataset.ignored==='true');
    if (button.dataset.note) { $('#notes-workspace').classList.add('show-detail'); return await openNote(button.dataset.note); }
    if (button.dataset.gotoNote) {
      const note=await api('/api/notes/'+Number(button.dataset.gotoNote));
      state.filter=note.ignored?'ignored':'all';state.selected=note.id;state.detail=null;state.editing=false;state.historyAnswer=null;state.collapsedChannel=null;++state.detailRequest;
      state.channel=state.channels.find(channel=>channel.id===note.document_id) || {id:note.document_id,title:note.document_title,remote_path:note.remote_path,unread:0};
      $('#note-search').value='';
      $$('.filter').forEach(item=>item.classList.toggle('active',item.dataset.filter===state.filter));
      $('#notes-workspace').classList.add('show-detail');
      return await changeView('notes');
    }
    if (button.dataset.pull) return await job(`/api/documents/${button.dataset.pull}/pull`);
    switch (button.dataset.action) {
      case 'sync':
        if (!state.app?.settings.sync_folders?.length) { toast('请先选择同步文件夹'); return await changeView('files'); }
        return await job('/api/sync');
      case 'refresh-folders': return await job('/api/device/inventory');
      case 'create-folder': {
        const path=$('#new-folder-path').value.trim();
        if (!path) throw new Error('请填写要新建的设备文件夹路径');
        return await job('/api/folders/create',{path});
      }
      case 'move-files': {
        const folder=$('#move-target').value.trim();
        if (!folder) throw new Error('请填写移动到的文件夹路径');
        if (!state.fileSelection.size) throw new Error('请先勾选需要移动的文档');
        return await job('/api/documents/move',{ids:[...state.fileSelection],folder});
      }
      case 'clear-file-selection': state.fileSelection.clear();return renderFiles();
      case 'save-scope': return await saveScope();
      case 'clear-scope': state.folderDraft=[];state.folderDirty=true;return renderFolders();
      case 'add-folder': {
        const path=$('#folder-path').value.trim().replace(/\/+$/,'');
        if (!(path==='Document'||path.startsWith('Document/'))||path.split('/').some(part=>['','.','..'].includes(part))) throw new Error('请输入 Document 下的文件夹路径');
        if (!state.folderDraft.includes(path)) state.folderDraft.push(path);
        state.folderDirty=true;$('#folder-path').value='';return renderFolders();
      }
      case 'probe': return await job('/api/device/probe');
      case 'refresh': return await refreshState();
      case 'pair': await saveSettings(); return await job('/api/device/pair');
      case 'pin': await api('/api/device/pin',{pin:$('#pair-pin').value}); $('#pair-pin').value=''; return toast('配对码已提交');
      case 'model-test': await saveSettings(); return await job('/api/model/test');
      case 'edit-note': return editNote();
      case 'cancel-edit': return await openNote(state.selected,false);
      case 'save-transcript': return await saveTranscript(false);
      case 'analyze':
        if (state.detail?.in_sync_scope===false) { toast('请先将这份文档所在的文件夹加入同步范围'); return await changeView('files'); }
        if (!state.app?.settings.model_ready) { toast('请先填写模型服务地址和名称'); return await changeView('settings'); }
        return await job(`/api/notes/${state.selected}/analyze`);
      case 'mark-read': await api(`/api/notes/${state.selected}/read`,{answer_id:state.detail.answer_id}); toast('已标为已读'); return await refreshState();
      case 'back-to-list': $('#notes-workspace').classList.remove('show-detail'); return $(`#note-nav [data-note="${state.selected}"]`)?.scrollIntoView?.({block:'nearest'});
      case 'prev-note': return await moveNote(-1);
      case 'next-note': return await moveNote(1);
      case 'zoom-ink': $('#ink-zoom img').src=button.currentSrc || button.src; $('#ink-zoom').hidden=false; return;
      case 'close-zoom': $('#ink-zoom').hidden=true; return;
      case 'print': return window.print();
      case 'logout': await api('/api/logout',{}); return window.location.assign('/');
    }
  } catch (error) { toast(error.message,true); }
});
document.addEventListener('submit', async event => {
  if (!['settings-form','edit-note-form'].includes(event.target.id)) return;
  event.preventDefault();
  try { if (event.target.id==='settings-form') { await saveSettings(); toast('设置已保存'); } else await saveTranscript(true); }
  catch(error) { toast(error.message,true); }
});
document.addEventListener('change', async event => {
  try {
    if (event.target.dataset.fileSelect) {
      if (event.target.checked) state.fileSelection.add(event.target.dataset.fileSelect);
      else state.fileSelection.delete(event.target.dataset.fileSelect);
      return renderFiles();
    }
    if (event.target.id==='select-visible-files') {
      const search=$('#file-search').value.toLowerCase();
      const files=state.files.filter(file=>file.device_present!==0&&(!$('#tracked-only').checked||file.tracked)&&(file.name+file.remote_path).toLowerCase().includes(search));
      for (const file of files) if (event.target.checked) state.fileSelection.add(file.id);else state.fileSelection.delete(file.id);
      return renderFiles();
    }
    if (event.target.dataset.folder) {
      const path=event.target.dataset.folder;
      state.folderDraft=event.target.checked?[...new Set([...state.folderDraft,path])]:state.folderDraft.filter(item=>item!==path);
      state.folderDirty=true;return renderFolders();
    }
    if (event.target.id==='answer-history') { if (!event.target.value) return await openNote(state.selected); return renderDetail(state.detail, await api('/api/answers/'+event.target.value)); }
    if (event.target.id==='report-days') return await loadReport();
    if (event.target.id==='tracked-only') return renderFiles();
    if (event.target.id==='pdf-upload') {
      const file = event.target.files[0]; if (!file) return;
      if (file.size > 128*1024*1024) throw new Error('PDF 大于 128 MB，请拆分后发送');
      toast('正在向服务器传送 PDF…');
      const response = await fetch('/api/upload',{method:'POST',headers:{'Content-Type':'application/pdf','X-Filename':encodeURIComponent(file.name)},body:file});
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      toast('文件已加入设备发送队列'); event.target.value=''; await refreshState();
    }
  } catch(error) { toast(error.message,true); }
});
let searchTimer;
$('#note-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.channel=null;state.selected=null;state.detail=null;state.editing=false;state.historyAnswer=null;++state.detailRequest;loadNotes(true).catch(error=>toast(error.message,true));},220);});
document.addEventListener('keydown',event=>{
  if (!$('#ink-zoom').hidden) { if (event.key==='Escape') $('#ink-zoom').hidden=true; return; }
  const target=event.target;
  if (state.view!=='notes' || state.editing || event.metaKey || event.ctrlKey || event.altKey || ['INPUT','TEXTAREA','SELECT'].includes(target?.tagName) || target?.isContentEditable) return;
  if (event.key==='/') { event.preventDefault(); return $('#note-search').focus(); }
  const step={j:1,k:-1}[event.key];
  if (step) { event.preventDefault(); moveNote(step).catch(error=>toast(error.message,true)); }
});
document.addEventListener('visibilitychange',()=>markCurrentNoteRead().catch(error=>toast(error.message,true)));
$('#file-search').addEventListener('input',renderFiles);
(async function poll(){await refreshState();setTimeout(poll,document.hidden?20000:4000);})();
