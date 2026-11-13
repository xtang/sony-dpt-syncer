// Render and injection regression checks, without a browser or network.
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
const element = selector => {
  if (!elements.has(selector)) {
    const classes=new Set();
    elements.set(selector,{innerHTML:'',value:'',hidden:false,scrollTop:0,scrollHeight:1000,clientHeight:500,dataset:{},
      classList:{add(...names){names.forEach(name=>classes.add(name));},remove(...names){names.forEach(name=>classes.delete(name));},toggle(name,force){if (force ?? !classes.has(name)) classes.add(name);else classes.delete(name);},contains(name){return classes.has(name);}},
      addEventListener(){},querySelectorAll(){return [];},querySelector(){return null;},setAttribute(){},removeAttribute(){},append(){},focus(){this.focused=true;}});
  }
  return elements.get(selector);
};
const listeners=new Map();
const context = vm.createContext({
  document:{title:'墨记 · 文档与笔记助手',querySelector:element,querySelectorAll(){return [];},addEventListener(name,handler){listeners.set(name,handler);},createElement(){return {remove(){}};},hidden:false},
  fetch:()=>new Promise(()=>{}), // Startup polling must never access any real service.
  setTimeout(){},clearTimeout(){},console,URL,FormData:class {},window:{print(){}}
});
vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
const run = code => vm.runInContext(code,context);
const html = fs.readFileSync('app/static/index.html','utf8');
const nav = () => element('#note-nav').innerHTML;
const detailHTML = () => element('#note-detail').innerHTML;

// Escaping and answer rendering.
assert.equal(run('esc("<script>bad()</script>")'),'&lt;script&gt;bad()&lt;/script&gt;');
const answer = {summary:'<img src=x onerror=alert(1)>',explanation:'接口约定\n具体的说明',examples:[{text:'采用指数退避重试',explanation:'避免请求集中发送'}],sources:['https://example.com/x?x=" onclick="bad()'],uncertainties:[]};
context.fixtureAnswer = answer;
const markup = run('answerHTML(fixtureAnswer,"model","vision",false)');
assert(!markup.includes('<img src=x'));
assert(markup.includes('&lt;img'));
assert(markup.includes('&quot;'));
assert(markup.includes('采用指数退避重试'));
assert(!run('sourceHTML("javascript:alert(1)")').includes('<a '));
assert(run('sourceHTML("见 https://example.com/a 页")').includes('href="https://example.com/a"'));

// Detail: two panels, sticky action bar, transcript, answer tools.
context.fixtureNote = {document_id:'a'.repeat(24),first_seen:'2026-11-12T09:00:00Z',answer_id:7,unread:0,id:8,page:11,title:'两种系统设计的区别',document_title:'系统设计文档',time_trusted:1,device_modified:'2026-11-12T01:49:04Z',tags:['架构'],status:'answered',pdf_url:'/media/document/id#page=11',preview_url:'/media/preview/8',content_hash:'abc123',manual_transcript:1,transcript:'区别是什么',question:'两者有什么区别？',anchor:'接口与实现',answer,answer_provenance:'verified_import',answer_model:'人工核对',history:[]};
run('renderDetail(fixtureNote)');
const detail = detailHTML();
assert(detail.includes('区别是什么'));
assert(detail.includes('核对资料后导入'));
assert(detail.includes('/media/preview/8?v=abc123'));
assert(!detail.includes('<img src=x'));
for (const text of ['detail-bar','ink-panel','answer-panel','ink-frame','id="transcript-area"','data-action="edit-note"','打开 PDF 第 11 页','data-ignore-note="8" data-ignored="true"','忽略这条笔记','系统设计文档','P11','data-action="prev-note"','data-action="next-note"','data-action="back-to-list"','data-action="zoom-ink"','转写已人工核对','data-action="analyze"']) assert(detail.includes(text),text);
assert(!detail.includes('data-action="mark-read"'),'read notes do not offer mark-read');
assert(!detail.includes('class="badge'),'answered notes carry no status badge');
context.changedNote={...context.fixtureNote,confirmation_stale:1,status:'needs_review',revisions:[{id:1,reason:'handwriting_changed',created_at:'2026-11-13T01:00:00Z',note:{title:'旧标题',transcript:'<img src=x onerror=bad()>旧文字'}}],lineage:[{id:9,title:'旧笔记',archived:1,relation:'merge',transcript:'合并前的文字',answer:{summary:'旧解答摘要',explanation:'旧解释'}}]};
run('renderDetail(changedNote)');
assert(detailHTML().includes('笔迹已变更，需要重新确认'));
assert(!detailHTML().includes('转写已人工核对'));
assert(detailHTML().includes('笔记修订与关联记录'));
assert(detailHTML().includes('合并前的文字'));
assert(detailHTML().includes('旧解答摘要'));
assert(detailHTML().includes('&lt;img src=x onerror=bad()&gt;旧文字'));
assert(!detailHTML().includes('<img src=x'));
context.genericAnswer={summary:'说明',explanation:'正文',examples:[{text:'采用指数退避重试',explanation:'避免请求集中发送'}]};
assert(run('answerHTML(genericAnswer,"model","vision",false)').includes('采用指数退避重试'));
context.exerciseAnswer={summary:'需要订正',explanation:'见解析',exercise_reviews:[
  {question_ref:'第1题',question:'2 + 3 = ?',user_answer:'A. 4',suggested_answer:'B. 5',verdict:'incorrect',basis:'reasoning',explanation:'所选结果少算了 1。',knowledge_points:['2 + 3 = 5','<img src=x onerror=alert(1)>']},
  {question_ref:'第2题',user_answer:'B',suggested_answer:'B',verdict:'correct',basis:'document',reference_quote:'第2题：B'},
  {question_ref:'第3题',verdict:'uncertain',basis:'insufficient',explanation:'缺少听力文字稿'}
]};
const reviewMarkup=run('answerHTML(exerciseAnswer,"model","vision",false)');
for (const text of ['模型判断：答错','模型判断：答对','暂不能判定','模型推导的参考解答','依据文中答案','你的作答：A. 4','建议答案：B. 5','所选结果少算了 1','需要掌握的知识点','缺少听力文字稿']) assert(reviewMarkup.includes(text));
assert(!reviewMarkup.includes('<img src=x'));
assert(reviewMarkup.includes('&lt;img'));
assert(!markup.includes('作答核对与解析'));
context.unreadHistoryNote={...context.fixtureNote,unread:1,history:[{id:7,model:'vision <b>',created_at:'2026-11-12T02:00:00Z'},{id:6,model:'older',created_at:'2026-11-11T02:00:00Z'}]};
run('renderDetail(unreadHistoryNote)');
assert(detailHTML().includes('data-action="mark-read"'),'unread notes offer mark-read');
assert(detailHTML().includes('id="answer-history"'));
assert(detailHTML().includes('vision &lt;b&gt;'));
assert(detailHTML().indexOf('answer-tools')<detailHTML().indexOf('answer-summary'),'answer tools sit in the answer header');
context.untrustedNote={...context.fixtureNote,time_trusted:0,status:'needs_review',answer:null,answer_id:null,manual_transcript:0,confidence:.42,in_sync_scope:false,error:'<b>失败</b>'};
run('renderDetail(untrustedNote)');
for (const text of ['历史批注 · 日期不确定','待确认 · 请先核对转写','识别置信度 42%','核对转写','重新识别','不在当前同步文件夹内','设备时间异常','&lt;b&gt;失败']) assert(detailHTML().includes(text),text);

// Filter tab counts and sidebar badge.
run('renderStats({notes:128,unread:3,pending:0,needs_review:2,ignored:4,answered:100})');
assert.equal(element('#count-all').textContent,'128');
assert.equal(element('#count-unread').textContent,'3');
assert(element('#count-unread').classList.contains('hot'));
assert.equal(element('#count-pending').textContent,'','zero counts are hidden');
assert.equal(element('#count-needs_review').textContent,'2');
assert.equal(element('#count-ignored').textContent,'4');
assert.equal(element('#unread-badge').hidden,false);
assert.equal(element('#unread-badge').textContent,3);
assert.equal(context.document.title,'(3) 墨记 · 文档与笔记助手');
assert.equal(element('#tab-icon').href,'/favicon-unread.svg');
context.document.hidden=true;
run('renderStats({notes:128,unread:105})');
assert.equal(context.document.title,'(105) 墨记 · 文档与笔记助手','background tabs show the global unread count');
assert.equal(element('#tab-icon').href,'/favicon-unread.svg');
context.document.hidden=false;
run('renderStats({notes:0,unread:0,pending:0,needs_review:0,ignored:0})');
assert.equal(element('#count-all').textContent,'0','the “all” count is always shown');
assert.equal(element('#count-unread').textContent,'');
assert(!element('#count-unread').classList.contains('hot'));
assert.equal(element('#unread-badge').hidden,true);
assert.equal(context.document.title,'墨记 · 文档与笔记助手');
assert.equal(element('#tab-icon').href,'/favicon.svg');

// Saved daily summaries render untrusted model text safely and retain download/history controls.
for (const name of ['daily_summary_enabled','daily_summary_time','daily_summary_timezone']) assert(html.includes(`name="${name}"`));
context.summaryFixture={id:7,study_date:'2020-04-01',timezone:'Asia/Shanghai',note_count:1,status:'ready',
  markdown:'# Summary\n\n<script>bad()</script>\n\n- a\n- b',sources:[{id:1,document:'<img src=x>',page:2,title:'note'}]};
run('renderDailySummary(summaryFixture)');
assert(element('#daily-content').innerHTML.includes('/api/daily-summaries/7/download'));
assert(element('#daily-content').innerHTML.includes('&lt;script&gt;'));
assert(!element('#daily-content').innerHTML.includes('<script>'));
assert(!element('#daily-content').innerHTML.includes('<img src=x>'));
assert(element('#generate-daily').disabled);
run('renderDailySummary({...summaryFixture,status:"error",error:"<img src=x>"})');
assert(!element('#generate-daily').disabled);
assert(element('#daily-content').innerHTML.includes('&lt;img'));
assert.equal(element('#generate-daily').textContent,'重试这一天的总结');
run('renderDailySummary(null,true)');
assert(element('#generate-daily').disabled);
assert(element('#daily-content').innerHTML.includes('正在排队或生成'));
run('renderDailySummary(null)');
assert(!element('#generate-daily').disabled);

// PDF tree: document rows, unread/count pills, nested notes, loading state.
context.sampleChannels=[{id:'a'.repeat(24),title:'系统设计 <img src=x>',remote_path:'Document/design.pdf',notes:3,unread:2,needs_review:1,last_activity:'2026-11-12T10:00:00Z',last_snippet:'接口与实现片段'},
  {id:'b'.repeat(24),title:'阅读材料',remote_path:'Document/book.pdf',notes:1,unread:0,needs_review:0,last_activity:'2026-11-11T10:00:00Z',last_snippet:'标记内容'}];
run('state.channels=sampleChannels;state.channel=sampleChannels[0];state.notes=[];state.notesFor=null;renderNav()');
assert(nav().includes('2 条未读回复'));
assert(nav().includes('3 条笔记'));
assert(nav().includes('1 条待确认'));
assert(nav().includes('&lt;img'));
assert(!nav().includes('<img src=x>'));
assert(!nav().includes('接口与实现片段'),'last_snippet is no longer shown');
assert(nav().includes('aria-label="1 条笔记">1<'),'documents without unread show their note count');
assert(nav().includes('title="Document/design.pdf"'));
assert(nav().includes('正在读取…'),'switching documents shows a loading row');
assert.equal(element('#nav-count').textContent,'2 份 PDF · 2 条新回复');
context.sampleNote={...context.fixtureNote,unread:1,ignored:0};
context.reviewNote={...context.fixtureNote,id:12,status:'needs_review',title:'需要核对'};
run('state.notes=[sampleNote,reviewNote];state.notesFor=sampleChannels[0].id;state.selected=sampleNote.id;state.detail=null;renderNav()');
const tree=nav();
assert(tree.includes('data-note="8"'));
assert(tree.includes('data-channel="'+'a'.repeat(24)+'"'));
assert(tree.indexOf('data-channel="'+'a'.repeat(24))<tree.indexOf('data-note="8"') && tree.indexOf('data-note="8"')<tree.indexOf('data-channel="'+'b'.repeat(24)),'notes are nested under their open PDF');
assert(tree.includes('nav-note selected unread'));
assert(tree.includes('class="dot"'));
assert(!tree.includes('系统设计文档'),'document mode does not repeat the PDF name on notes');
assert(!tree.includes('正在读取…'));
const firstRow=tree.slice(tree.indexOf('data-note="8"'),tree.indexOf('data-note="12"'));
assert(!firstRow.includes('class="badge'),'answered notes carry no badge');
assert(tree.slice(tree.indexOf('data-note="12"')).includes('<span class="badge review">待确认</span>'));
// Polling re-renders only write the tree when the markup changes, and keep the scroll position.
element('#note-nav').innerHTML='unchanged sentinel';
run('renderNav()');
assert.equal(nav(),'unchanged sentinel');
element('#note-nav').scrollTop=120;
run('state.notes[1].title="已改标题";renderNav()');
assert(nav().includes('已改标题'));
assert.equal(element('#note-nav').scrollTop,120);
// Prev/next follow the position inside state.notes.
run('state.detail=sampleNote;updatePager()');
assert.equal(element('#prev-note').disabled,true);
assert.equal(element('#next-note').disabled,false);
run('state.detail=reviewNote;updatePager()');
assert.equal(element('#prev-note').disabled,false);
assert.equal(element('#next-note').disabled,true);

// Timeline: flat rows grouped by day, in server order, with document names.
const now=new Date();
const yesterday=new Date(now.getTime()-86400000);
context.timelineNotes=[{...context.sampleNote,id:21,title:'今天的笔记',device_modified:now.toISOString()},{...context.sampleNote,id:22,title:'昨天的笔记',device_modified:yesterday.toISOString()},{...context.sampleNote,id:23,title:'旧笔记',time_trusted:0,device_modified:null}];
run('state.noteMode="timeline";state.notes=timelineNotes;state.selected=21;renderNav()');
for (const text of ['<div class="nav-day">今天</div>','<div class="nav-day">昨天</div>','<div class="nav-day">历史导入</div>','系统设计文档','data-note="21"','data-note="23"']) assert(nav().includes(text),text);
assert(!nav().includes('data-channel'));
assert.equal(element('#nav-count').textContent,'3 条笔记');
run('state.noteMode="documents"');

// Folder scope.
run('state.folderDraft=["Document/工作"];state.folders=[{path:"Document/工作",documents:2},{path:"Document/工作/项目",documents:1},{path:"Document/工作备份",documents:3}];renderFolders()');
const folderMarkup=elements.get('#folder-list').innerHTML;
assert(folderMarkup.includes('已包含在上级目录中'));
assert(folderMarkup.includes('data-folder="Document/工作" checked'));
assert(!folderMarkup.includes('data-folder="Document/工作备份" checked'));

// Device files and the bulk action bar.
for (const id of ['folder-list','folder-path','folder-options','folder-summary','new-folder-path','move-target','move-files-button','move-selection-count','select-visible-files','file-search','tracked-only','file-count','file-list','pdf-upload','bulk-bar','note-nav','note-search','nav-count','count-all','count-unread','count-pending','count-needs_review','count-ignored','ink-zoom','page-label','unread-badge']) assert(html.includes(`id="${id}"`),id);
for (const action of ['create-folder','move-files','clear-file-selection','save-scope','clear-scope','add-folder','refresh-folders']) assert(html.includes(`data-action="${action}"`),action);
for (const filter of ['all','unread','pending','needs_review','ignored']) assert(html.includes(`data-filter="${filter}"`),filter);
assert(!html.includes('id="stats"'));
assert(!html.includes('id="document-list"') && !html.includes('id="note-list"'));
context.sampleFiles=[{id:'a'.repeat(24),name:'guide.pdf',remote_path:'Document/guide.pdf',device_present:1,tracked:0,pages:10,notes:2},{id:'b'.repeat(24),name:'archived.pdf',remote_path:'Document/archived.pdf',device_present:0,tracked:0,pages:1,notes:0}];
run('state.files=sampleFiles;state.fileSelection=new Set([sampleFiles[0].id]);');
element('#tracked-only').checked=false;
run('renderFiles()');
assert(elements.get('#file-list').innerHTML.includes('data-file-select="'+'a'.repeat(24)+'" checked'));
assert(elements.get('#file-list').innerHTML.includes('data-file-select="'+'b'.repeat(24)+'"  disabled'));
assert.equal(elements.get('#move-files-button').disabled,false);
assert.equal(elements.get('#move-selection-count').textContent,'已选择 1 份文档');
assert.equal(elements.get('#bulk-bar').hidden,false,'bulk bar appears with a selection');
run('state.fileSelection.clear();renderFiles()');
assert.equal(elements.get('#bulk-bar').hidden,true,'bulk bar hides without a selection');
assert.equal(elements.get('#move-files-button').disabled,true);

(async()=>{
  let yesterday='2020-04-01';
  context.dailyApi=async()=>({items:[],selected:null,yesterday,timezone:'Asia/Shanghai'});
  run('api=dailyApi;state.view="report";state.app={settings:{daily_summary_enabled:false},jobs:[]}');
  await run('loadDailySummaries()');
  assert.equal(element('#daily-date').value,'2020-04-01');
  yesterday='2020-04-02';
  await run('loadDailySummaries()');
  assert.equal(element('#daily-date').value,'2020-04-02','default review follows yesterday across midnight');
  element('#daily-date').value='2020-03-30';
  await listeners.get('change')({target:{id:'daily-date'}});
  yesterday='2020-04-03';
  await run('loadDailySummaries()');
  assert.equal(element('#daily-date').value,'2020-03-30','explicit historical date stays selected');
  run('state.view="notes";dailyDateChosen=false');
  const requests=[];
  const entries=[{...context.sampleNote,title:'First document note'},
    {...context.sampleNote,id:10,answer_id:10,title:'Another unread note'},
    {...context.sampleNote,id:11,answer_id:null,answer:null,unread:0,status:'needs_review',title:'Unclear writing'},
    {...context.sampleNote,id:9,answer_id:9,document_id:'b'.repeat(24),document_title:'阅读材料',title:'Second document note'}];
  const copy=value=>JSON.parse(JSON.stringify(value));
  context.integrationApi=async(path,body)=>{
    requests.push({path,body});
    if (/\/view$/.test(path)) return {ok:true};
    if (/\/ignore$/.test(path)) {
      const id=Number(path.split('/')[3]);const note=entries.find(item=>item.id===id);note.ignored=Number(body.ignored);note.unread=0;return copy(note);
    }
    if (/\/read$/.test(path)) {
      const note=entries.find(item=>item.id===Number(path.split('/')[3]));
      const marked=note && !note.ignored && note.unread && note.answer_id===body.answer_id?[note.answer_id]:[];
      if (marked.length) note.unread=0;
      return {ok:true,marked};
    }
    const url=new URL(path,'http://localhost');
    const status=url.searchParams.get('status') || 'all';
    const visible=entries.filter(note=>Boolean(note.ignored)===(status==='ignored'));
    if (url.pathname==='/api/channels') return context.sampleChannels.filter(channel=>visible.some(note=>note.document_id===channel.id)).map(channel=>({...channel,notes:visible.filter(note=>note.document_id===channel.id).length,unread:visible.filter(note=>note.document_id===channel.id&&note.unread).length}));
    if (url.pathname==='/api/notes') return copy(visible.filter(note=>(!url.searchParams.has('document_id') || note.document_id===url.searchParams.get('document_id'))&&(status!=='unread'||note.unread)));
    return copy(entries.find(note=>note.id===Number(path.split('/').at(-1))));
  };
  run('api=integrationApi;refreshState=async()=>loadNotes();state.channel=null;state.noteMode="documents";state.filter="all";state.selected=null;state.detail=null;state.app=null;');
  const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset})}});
  const key=(name,tagName='BODY')=>listeners.get('keydown')({key:name,target:{tagName},preventDefault(){}});
  const workspace=element('#notes-workspace');
  await click({noteMode:'documents'});
  assert(workspace.classList.contains('document-mode'));
  assert(nav().includes('First document note'));
  assert(nav().includes('Another unread note'));
  assert(!nav().includes('Second document note'),'only the open PDF shows its notes');
  assert(detailHTML().includes('First document note'));
  assert(!detailHTML().includes('Another unread note'));
  assert(!workspace.classList.contains('show-detail'),'auto-selecting the first note keeps the list on narrow screens');
  assert.equal(entries[0].unread,0);
  assert.equal(entries[1].unread,1);
  assert.equal(entries[3].unread,1);
  assert.equal(run('state.channel.unread'),1);
  await click({channel:'b'.repeat(24)});
  assert(nav().includes('Second document note'));
  assert(!nav().includes('First document note'));
  assert(detailHTML().includes('Second document note'));
  assert(!detailHTML().includes('First document note'));
  assert.equal(entries[1].unread,1);
  // Clicking the open PDF again collapses it, a third click expands it.
  await click({channel:'b'.repeat(24)});
  assert(!nav().includes('data-note='),'collapsed PDF hides its notes');
  assert(nav().includes('aria-expanded="false"'));
  await click({channel:'b'.repeat(24)});
  assert(nav().includes('Second document note'));
  await click({channel:'a'.repeat(24)});
  // Keyboard: j moves down, input focus is ignored, / focuses the search.
  element('#ink-zoom').hidden=true;
  assert.equal(run('state.selected'),8);
  await key('j');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run('state.selected'),10);
  await key('k','INPUT');
  assert.equal(run('state.selected'),10);
  await key('/');
  assert(element('#note-search').focused);
  // A user click opens the detail pane on narrow screens; “back” returns to the list.
  await click({note:'11'});
  assert(workspace.classList.contains('show-detail'));
  assert(detailHTML().includes('Unclear writing'));
  await click({action:'back-to-list'});
  assert(!workspace.classList.contains('show-detail'));
  await click({ignoreNote:'11',ignored:'true'});
  assert.equal(entries[2].ignored,1);
  assert(!nav().includes('Unclear writing'));
  await click({filter:'ignored'});
  assert(nav().includes('Unclear writing'));
  assert(detailHTML().includes('恢复笔记'));
  assert(!detailHTML().includes('data-action="analyze"'));
  await click({ignoreNote:'11',ignored:'false'});
  assert.equal(entries[2].ignored,0);
  await click({filter:'all'});
  await click({noteMode:'timeline'});
  assert(!workspace.classList.contains('document-mode'));
  assert(!nav().includes('data-channel'));
  assert(nav().includes('First document note'));
  assert(nav().includes('Second document note'));
  assert(nav().includes('nav-day'));
  entries[3].unread=1;entries[3].answer_id=99;
  context.document.hidden=true;
  await click({noteMode:'documents'});
  await click({channel:'b'.repeat(24)});
  assert.equal(entries[3].unread,1,'hidden pages send no read receipt');
  run('state.app={stats:{unread:1}};renderStats(state.app.stats)');
  assert.equal(element('#tab-icon').href,'/favicon-unread.svg');
  context.document.hidden=false;
  await listeners.get('visibilitychange')();
  assert.equal(entries[3].unread,0);
  assert.equal(context.document.title,'墨记 · 文档与笔记助手','reading the last reply clears the tab count immediately');
  assert.equal(element('#tab-icon').href,'/favicon.svg');
  run('state.app=null');
  assert.equal(requests.at(-1).body.answer_id,99);
  await click({filter:'ignored'});
  workspace.classList.remove('show-detail');
  await click({gotoNote:'9'});
  assert.equal(run('state.channel.id'),'b'.repeat(24));
  assert(detailHTML().includes('Second document note'));
  assert(workspace.classList.contains('show-detail'),'jumping from the review opens the detail pane');
  let deliverDetail;
  let reportRequest;
  const detailRequested=new Promise(resolve=>{reportRequest=resolve;});
  context.draftApi=async path=>path==='/api/notes/9'?new Promise(resolve=>{deliverDetail=resolve;reportRequest();}):context.integrationApi(path);
  run('api=draftApi;state.editing=false;state.detail=null;');
  const pendingRefresh=run('loadNoteList()');
  await detailRequested;
  run('state.editing=true');
  element('#note-detail').innerHTML='unsaved draft';
  deliverDetail(copy(entries[3]));
  await pendingRefresh;
  assert.equal(element('#note-detail').innerHTML,'unsaved draft');
  await key('j');
  assert.equal(run('state.selected'),9,'keyboard navigation is off while editing');
  console.log('Frontend PDF tree, detail, filters, files, keyboard, ignore, read receipts and injection checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
