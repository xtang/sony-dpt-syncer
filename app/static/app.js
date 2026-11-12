'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = { view: 'notes', filter: 'all', notes: [], selected: null, detail: null, app: null, files: [], fileSelection: new Set(), folders: [], folderDraft: [], folderDirty: false, editing: false, seenJobs: new Set(), initialized: false, searchRequest: 0 };
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
  let style = note.status === 'needs_review' ? 'review' : note.status === 'error' ? 'error' : note.status === 'answered' ? '' : 'pending';
  return `<span class="badge ${style}">${esc(statusLabels[note.status] || note.status)}</span>${note.unread ? '<span class="badge unread">新回复</span>' : ''}`;
}
function stat(number, label) { return `<div class="stat"><strong>${esc(number)}</strong><span>${esc(label)}</span></div>`; }
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
    $('#stats').innerHTML = stat(counts.notes,'已归档笔记')+stat(counts.answered,'已有解答')+stat(counts.needs_review,'待确认笔迹')+stat(counts.unread,'条新回复');
    $('#unread-badge').hidden = !counts.unread; $('#unread-badge').textContent = counts.unread;
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
  $('#page-label').textContent = {notes:'笔记工作台',files:'设备文件',report:'笔记回顾',settings:'连接与设置'}[view];
  if (view === 'notes') await loadNotes(false);
  if (view === 'files') await loadFiles();
  if (view === 'report') await loadReport();
  if (view === 'settings' && state.app) fillSettings(state.app.settings);
}
async function loadNotes(force = false) {
  const request = ++state.searchRequest;
  const notes = await api('/api/notes?status='+encodeURIComponent(state.filter)+'&q='+encodeURIComponent($('#note-search').value));
  if (request !== state.searchRequest) return;
  state.notes = notes;
  if (!state.selected && notes.length) state.selected = notes[0].id;
  $('#note-list').innerHTML = notes.length ? notes.map(note => `<button class="note-item ${state.selected === note.id?'selected':''}" data-note="${note.id}"><div class="note-item-head"><span>${badge(note)}</span><time>${note.time_trusted ? esc(fmt(note.device_modified,true)):'历史导入'}</time></div><h3>${esc(note.title)}</h3><p class="note-excerpt">${esc(note.question || note.transcript || '尚未识别笔迹，可打开原图核对。')}</p><div class="note-origin">${esc(note.document_title)} · P${note.page}</div></button>`).join('') : '<div class="empty">这里暂时没有笔记。<br>同步设备，或换个筛选条件。</div>';
  $('#note-count').textContent = `${notes.length} 条笔记 · 原始笔迹保留`;
  const current = notes.find(note => note.id === state.selected);
  if (state.selected && !state.editing && (force || !state.detail || (current && (current.answer_id !== state.detail.answer_id || current.status !== state.detail.status || current.content_hash !== state.detail.content_hash || current.in_sync_scope !== state.detail.in_sync_scope)))) await openNote(state.selected, false);
  if (!state.selected) $('#note-detail').innerHTML = '<div class="empty"><strong>从一条笔记开始</strong>在 DPT 上圈出疑问，写下问题，再点“同步并解读”。<br>未配置模型时，也可以查看和手动转写批注。</div>';
}
async function openNote(identifier, markRead = true) {
  state.selected = Number(identifier); state.editing = false;
  const note = await api('/api/notes/'+state.selected);
  if (note.id !== state.selected) return;
  state.detail = note;
  renderDetail(note);
  $$('.note-item').forEach(item => item.classList.toggle('selected',Number(item.dataset.note) === note.id));
  if (markRead && note.unread) {
    await api(`/api/notes/${note.id}/read`, {});
    note.unread = 0;
    await refreshState();
  }
}
function renderDetail(note, historical = null) {
  const answer = historical ? historical.payload : note.answer;
  const provenance = historical ? historical.provenance : note.answer_provenance;
  const source = historical ? historical.model : note.answer_model;
  const tags = (note.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('');
  $('#note-detail').innerHTML = `<div class="detail-meta"><span>笔记 ${String(note.id).padStart(3,'0')} <span aria-hidden="true">/</span> ${esc(note.document_title)} · P${note.page}</span><span>${note.time_trusted ? esc(fmt(note.device_modified)):'历史批注 · 日期不确定'}</span></div><h2>${esc(note.title)}</h2><div class="tag-row">${tags}${note.status==='needs_review'?'<span class="badge review">请先核对转写</span>':''}</div>
    <div class="note-source"><strong>你的原始笔迹</strong><a href="${esc(note.pdf_url)}" target="_blank" rel="noopener">打开 PDF 第 ${note.page} 页 ↗</a></div>
    <div class="ink-frame">${note.preview_url?`<img src="${esc(note.preview_url)}?v=${esc(note.content_hash.slice(0,12))}" alt="第${note.page}页的原始笔迹和正文" loading="lazy">`:'<div class="empty">暂无笔迹截图</div>'}<div class="ink-caption"><span>保留正文上下文与手写标记</span><span>${note.manual_transcript?'转写已人工核对':note.confidence!=null?'识别置信度 '+Math.round(note.confidence*100)+'%':'等待识别'}</span></div></div>
    <div id="transcript-area"><div class="transcript-block"><div><span class="transcript-label">手写转写 / 提出的问题</span><p>${esc(note.transcript || '尚未转写，可以手动填写，也可以使用视觉模型识别。')}</p>${note.question && note.question !== note.transcript?`<p class="muted">${esc(note.question)}</p>`:''}${note.anchor?`<p class="muted">标记：${esc(note.anchor)}</p>`:''}</div><button class="button quiet small" data-action="edit-note">${note.status==='needs_review'?'核对转写':'校正文字'}</button></div></div>
    ${note.error?`<div class="notice error-text">${esc(note.error)}</div>`:''}
    ${note.in_sync_scope === false?'<div class="notice">这份文档不在当前同步文件夹内，已保留归档内容。若需继续解读，请在设备文件中添加所属文件夹。</div>':''}
    ${!note.time_trusted?'<p class="helper">这条批注的设备时间异常，只作为历史笔记归档，不计入近期统计。</p>':''}
    ${answer ? answerHTML(answer, provenance, source, note.status !== 'answered' || !!historical) : `<div class="empty"><strong>${note.status==='needs_review'?'先确认你写了什么':'这条疑问，还可以再往前一步'}</strong>${note.status==='needs_review'?'点“核对转写”，确认文字后再解读。':'配置好模型后，可以直接从笔迹得到解释和解答。'}<br><button class="button primary" data-action="analyze">${note.status==='needs_review'?'重新识别':'识别并解读'}</button></div>`}
    ${answer?`<div class="answer-actions"><button class="button quiet small" data-action="analyze">重新解读</button><button class="button quiet small" data-action="mark-read">标为已读</button>${note.history?.length>1?`<select id="answer-history" aria-label="解读历史"><option value="">当前解读</option>${note.history.map(item=>`<option value="${item.id}">${esc(fmt(item.created_at,true))} · ${esc(item.model)}</option>`).join('')}</select>`:''}</div>`:''}`;
}
function sourceHTML(value) {
  const text = String(value); const match = text.match(/https:\/\/[^\s<>]+/);
  if (!match) return esc(text);
  return esc(text.slice(0,match.index))+`<a href="${esc(match[0])}" target="_blank" rel="noopener noreferrer">查看资料 ↗</a>`+esc(text.slice(match.index+match[0].length));
}
function answerHTML(answer, provenance, source, previous) {
  const examples = Array.isArray(answer.examples) ? answer.examples.filter(v=>v && typeof v==='object') : [];
  const practice = Array.isArray(answer.practice) ? answer.practice.filter(v=>v && typeof v==='object') : [];
  const sources = Array.isArray(answer.sources) ? answer.sources : [];
  const uncertainties = Array.isArray(answer.uncertainties) ? answer.uncertainties : [];
  return `${previous?'<div class="notice">正在查看先前的解答。笔记修改后，请重新解读以匹配最新内容。</div>':''}<div class="answer-header"><span class="answer-mark">解</span><h3>把这一点弄明白</h3><span class="muted">${esc(provenance==='verified_import'?'核对资料后导入':source || '模型解读')}</span></div><p class="answer-summary">${esc(answer.summary)}</p><div class="answer-body">${esc(answer.explanation)}</div>
    ${examples.length?'<h3>补充示例</h3>'+examples.map(item=>`<div class="example"><p class="ja">${esc(item.text ?? item.ja)}</p><p class="zh">${esc(item.explanation ?? item.zh)}</p></div>`).join(''):''}
    ${practice.length?'<h3>试着判断一下</h3><p class="helper">先在心里作答，再点开查看。</p>'+practice.map((item,index)=>`<details class="practice"><summary>${String(index+1).padStart(2,'0')}　${esc(item.question)}</summary><div><p>${esc(item.answer)}</p><small>${esc(item.explanation)}</small></div></details>`).join(''):''}
    <div class="answer-foot">${uncertainties.map(item=>`<p>${esc(item)}</p>`).join('')}${sources.length?'<p>参考与上下文</p>'+sources.map(item=>`<p>· ${sourceHTML(item)}</p>`).join(''):''}${provenance!=='verified_import'?'<p>模型解读依据提供的上下文生成，关键内容请结合原文核对。</p>':''}</div>`;
}
function editNote() {
  const note = state.detail; if (!note) return;
  state.editing = true;
  $('#transcript-area').innerHTML = `<form id="edit-note-form" class="edit-form"><label>笔记标题<input name="title" value="${esc(note.title)}"></label><label>手写原文<textarea name="transcript">${esc(note.transcript)}</textarea></label><label>标记的原文<input name="anchor" value="${esc(note.anchor)}"></label><label>希望解答的问题<textarea name="question">${esc(note.question)}</textarea></label><div class="row"><button class="button primary small" type="submit">确认并解读</button><button class="button quiet small" type="button" data-action="save-transcript">只保存文字</button><button class="text-button" type="button" data-action="cancel-edit" aria-label="取消编辑">×</button></div></form>`;
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
  $('#file-list').innerHTML = files.length ? files.map(file => `<tr><td><input type="checkbox" data-file-select="${file.id}" ${state.fileSelection.has(file.id)?'checked':''} ${file.device_present===0?'disabled':''} aria-label="选择 ${esc(file.name)}"></td><td><strong>${esc(file.name)}</strong><small>${esc(file.remote_path)}</small></td><td>${file.pages || '—'} 页 / ${file.notes} 条</td><td class="muted">${esc(fmt(file.device_modified,true))}</td><td><span class="badge ${file.tracked?'':'pending'}">${file.device_present===0?'设备中已移除':file.tracked?'已包含':'范围外'}</span></td><td><div class="row">${file.tracked?`<button class="button quiet small" data-pull="${file.id}">拉取</button>`:''}${file.snapshot_id || file.has_baseline?`<a class="button quiet small" href="/media/document/${file.id}" target="_blank" rel="noopener">打开</a>`:''}</div></td></tr>`).join('') : '<tr><td colspan="6"><div class="empty">没有匹配的文件。选择同步文件夹并刷新设备目录后，文件会显示在这里。</div></td></tr>';
  $('#move-selection-count').textContent = `已选择 ${state.fileSelection.size} 份文档`;
  $('#move-files-button').disabled = !state.fileSelection.size;
  const movable = files.filter(file=>file.device_present!==0);
  $('#select-visible-files').checked = movable.length>0 && movable.every(file=>state.fileSelection.has(file.id));
  $('#select-visible-files').indeterminate = !$('#select-visible-files').checked && movable.some(file=>state.fileSelection.has(file.id));
}
async function loadReport() {
  const report = await api('/api/report?days='+$('#report-days').value);
  $('#report-content').innerHTML = `<div class="stats-row">${stat(report.recent_notes,'本期更新的笔记')}${stat(report.answered,'已整理出解答')}${stat(report.needs_review,'待确认笔迹')}${stat(report.uncertain_time,'日期不确定的旧笔记')}</div><div class="report-grid"><section class="surface report-card"><span class="section-number">TOPICS IN YOUR NOTES</span><h2>最近记录的主题</h2>${report.topics.length?report.topics.map(([tag,count])=>`<div class="topic-row"><span>${esc(tag)}</span><span>${count} 条笔记</span></div>`).join(''):'<p class="helper">本期还没有已标注主题的笔记。同步并解读后会在这里归纳。</p>'}<p class="helper">本页只统计当前同步文件夹中的笔记，范围外的历史记录仍可在知识库查看。</p></section><section class="surface report-card"><span class="section-number">OPEN QUESTIONS</span><h2>尚待处理</h2>${report.next_steps.length?report.next_steps.map((item,index)=>`<div class="next-step"><span>${String(index+1).padStart(2,'0')}</span><button data-goto-note="${item.id}">${esc(item.title)}<small>${esc(item.reason)}</small></button></div>`).join(''):'<p class="helper">当前同步范围内没有待处理的笔记。</p>'}</section><section class="surface report-card"><span class="section-number">ABOUT THIS REVIEW</span><h2>这份回顾的依据</h2><p class="helper">${esc(report.limitation)}</p><p class="helper">按可信的设备批注修改时间统计，每处笔记只计一次。修改日期会随后续编辑更新。</p><p class="helper">生成时间：${esc(fmt(report.generated_at))}</p></section></div>`;
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-view],[data-filter],[data-note],[data-action],[data-pull],[data-goto-note]'); if (!button) return;
  try {
    if (button.dataset.view) return await changeView(button.dataset.view);
    if (button.dataset.filter) { state.filter = button.dataset.filter; $$('.filter').forEach(item=>item.classList.toggle('active',item===button)); return await loadNotes(); }
    if (button.dataset.note) return await openNote(button.dataset.note);
    if (button.dataset.gotoNote) { await changeView('notes'); return await openNote(button.dataset.gotoNote); }
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
      case 'mark-read': await api(`/api/notes/${state.selected}/read`,{}); toast('已标为已读'); return await refreshState();
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
    if (event.target.id==='answer-history') { if (!event.target.value) return renderDetail(state.detail); return renderDetail(state.detail, await api('/api/answers/'+event.target.value)); }
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
$('#note-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadNotes().catch(error=>toast(error.message,true)),220);});
$('#file-search').addEventListener('input',renderFiles);
(async function poll(){await refreshState();setTimeout(poll,document.hidden?20000:4000);})();
