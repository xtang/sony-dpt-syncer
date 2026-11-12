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
      addEventListener(){},querySelectorAll(){return [];},querySelector(){return null;},setAttribute(){},removeAttribute(){},append(){}});
  }
  return elements.get(selector);
};
const listeners=new Map();
const context = vm.createContext({
  document:{querySelector:element,querySelectorAll(){return [];},addEventListener(name,handler){listeners.set(name,handler);},createElement(){return {remove(){}};},hidden:false},
  fetch:()=>new Promise(()=>{}), // Startup polling must never access any real service.
  setTimeout(){},clearTimeout(){},console,URL,FormData:class {},window:{print(){}}
});
vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
const run = code => vm.runInContext(code,context);
assert.equal(run('esc("<script>bad()</script>")'),'&lt;script&gt;bad()&lt;/script&gt;');
const answer = {summary:'<img src=x onerror=alert(1)>',explanation:'接口约定\n具体的说明',examples:[{text:'采用指数退避重试',explanation:'避免请求集中发送'}],sources:['https://example.com/x?x=" onclick="bad()'],uncertainties:[]};
context.fixtureAnswer = answer;
const markup = run('answerHTML(fixtureAnswer,"model","vision",false)');
assert(!markup.includes('<img src=x'));
assert(markup.includes('&lt;img'));
assert(markup.includes('&quot;'));
assert(markup.includes('采用指数退避重试'));
assert(!run('sourceHTML("javascript:alert(1)")').includes('<a '));
context.fixtureNote = {document_id:'a'.repeat(24),first_seen:'2026-11-12T09:00:00Z',answer_id:7,unread:0,id:8,page:11,title:'两种系统设计的区别',document_title:'系统设计文档',time_trusted:1,device_modified:'2026-11-12T01:49:04Z',tags:['架构'],status:'answered',pdf_url:'/media/document/id#page=11',preview_url:'/media/preview/8',content_hash:'abc123',manual_transcript:1,transcript:'区别是什么',question:'两者有什么区别？',anchor:'接口与实现',answer,answer_provenance:'verified_import',answer_model:'人工核对',history:[]};
run('renderDetail(fixtureNote)');
const detail = elements.get('#note-detail').innerHTML;
assert(detail.includes('区别是什么'));
assert(detail.includes('核对资料后导入'));
assert(detail.includes('/media/preview/8?v=abc123'));
assert(!detail.includes('<img src=x'));
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
context.sampleChannels=[{id:'a'.repeat(24),title:'系统设计 <img src=x>',remote_path:'Document/design.pdf',notes:3,unread:2,needs_review:1,last_activity:'2026-11-12T10:00:00Z',last_snippet:'接口与实现'},
  {id:'b'.repeat(24),title:'阅读材料',remote_path:'Document/book.pdf',notes:1,unread:0,needs_review:0,last_activity:'2026-11-11T10:00:00Z',last_snippet:'标记内容'}];
run('state.channels=sampleChannels;state.channel=sampleChannels[0];renderChannels()');
const channelMarkup=element('#document-list').innerHTML;
assert(channelMarkup.includes('2 条未读回复'));
assert(channelMarkup.includes('3 条笔记'));
assert(channelMarkup.includes('&lt;img'));
assert(!channelMarkup.includes('<img src=x>'));
context.sampleNote={...context.fixtureNote,unread:1,ignored:0};
run('state.notes=[sampleNote];state.selected=sampleNote.id;renderNoteList()');
assert(element('#note-list').innerHTML.includes('data-note="8"'));
assert(element('#document-list').innerHTML.includes('data-channel="'+'a'.repeat(24)+'"'));
assert(!element('#note-list').innerHTML.includes('data-channel'));
assert(!element('#document-list').innerHTML.includes('data-note="8"'));
context.ignoredNote={...context.sampleNote,ignored:1};
run('renderDetail(ignoredNote)');
assert(element('#note-detail').innerHTML.includes('恢复笔记'));
assert(!element('#note-detail').innerHTML.includes('data-action="analyze"'));
run('renderDetail(sampleNote)');
assert(element('#note-detail').innerHTML.includes('ink-frame'));
assert(element('#note-detail').innerHTML.includes('transcript-area'));
assert(!element('#note-detail').innerHTML.includes('conversation-stream'));
assert(!element('#note-detail').innerHTML.includes('thread-assistant'));
run('state.folderDraft=["Document/工作"];state.folders=[{path:"Document/工作",documents:2},{path:"Document/工作/项目",documents:1},{path:"Document/工作备份",documents:3}];renderFolders()');
const folderMarkup=elements.get('#folder-list').innerHTML;
assert(folderMarkup.includes('已包含在上级目录中'));
assert(folderMarkup.includes('data-folder="Document/工作" checked'));
assert(!folderMarkup.includes('data-folder="Document/工作备份" checked'));
assert(fs.readFileSync('app/static/index.html','utf8').includes('id="folder-list"'));
context.sampleFiles=[{id:'a'.repeat(24),name:'guide.pdf',remote_path:'Document/guide.pdf',device_present:1,tracked:0,pages:10,notes:2},{id:'b'.repeat(24),name:'archived.pdf',remote_path:'Document/archived.pdf',device_present:0,tracked:0,pages:1,notes:0}];
run('state.files=sampleFiles;state.fileSelection=new Set([sampleFiles[0].id]);');
element('#tracked-only').checked=false;
run('renderFiles()');
assert(elements.get('#file-list').innerHTML.includes('data-file-select="'+'a'.repeat(24)+'" checked'));
assert(elements.get('#file-list').innerHTML.includes('data-file-select="'+'b'.repeat(24)+'"  disabled'));
assert.equal(elements.get('#move-files-button').disabled,false);
assert.equal(elements.get('#move-selection-count').textContent,'已选择 1 份文档');
assert(fs.readFileSync('app/static/index.html','utf8').includes('data-action="create-folder"'));
assert(fs.readFileSync('app/static/index.html','utf8').includes('data-action="move-files"'));
(async()=>{
  const requests=[];
  const entries=[{...context.sampleNote,title:'First document note'},
    {...context.sampleNote,id:10,answer_id:10,title:'Another unread note'},
    {...context.sampleNote,id:11,answer_id:null,answer:null,unread:0,status:'needs_review',title:'Unclear writing'},
    {...context.sampleNote,id:9,answer_id:9,document_id:'b'.repeat(24),document_title:'阅读材料',title:'Second document note'}];
  const copy=value=>JSON.parse(JSON.stringify(value));
  context.integrationApi=async(path,body)=>{
    requests.push({path,body});
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
  await click({noteMode:'documents'});
  assert(element('#notes-workspace').classList.contains('document-mode'));
  assert.equal(element('#document-browser').hidden,false);
  assert(element('#note-list').innerHTML.includes('First document note'));
  assert(element('#note-list').innerHTML.includes('Another unread note'));
  assert(!element('#note-list').innerHTML.includes('Second document note'));
  assert(element('#note-detail').innerHTML.includes('First document note'));
  assert(!element('#note-detail').innerHTML.includes('Another unread note'));
  assert.equal(entries[0].unread,0);
  assert.equal(entries[1].unread,1);
  assert.equal(entries[3].unread,1);
  assert.equal(run('state.channel.unread'),1);
  await click({channel:'b'.repeat(24)});
  assert(element('#note-list').innerHTML.includes('Second document note'));
  assert(!element('#note-list').innerHTML.includes('First document note'));
  assert(element('#note-detail').innerHTML.includes('Second document note'));
  assert(!element('#note-detail').innerHTML.includes('First document note'));
  assert.equal(entries[1].unread,1);
  await click({channel:'a'.repeat(24)});
  await click({note:'11'});
  assert(element('#note-detail').innerHTML.includes('Unclear writing'));
  await click({ignoreNote:'11',ignored:'true'});
  assert.equal(entries[2].ignored,1);
  assert(!element('#note-list').innerHTML.includes('Unclear writing'));
  await click({filter:'ignored'});
  assert(element('#note-list').innerHTML.includes('Unclear writing'));
  assert(element('#note-detail').innerHTML.includes('恢复笔记'));
  await click({ignoreNote:'11',ignored:'false'});
  assert.equal(entries[2].ignored,0);
  await click({filter:'all'});
  await click({noteMode:'timeline'});
  assert(!element('#notes-workspace').classList.contains('document-mode'));
  assert.equal(element('#document-browser').hidden,true);
  assert(element('#note-list').innerHTML.includes('First document note'));
  assert(element('#note-list').innerHTML.includes('Second document note'));
  entries[3].unread=1;entries[3].answer_id=99;
  context.document.hidden=true;
  await click({noteMode:'documents'});
  await click({channel:'b'.repeat(24)});
  assert.equal(entries[3].unread,1);
  context.document.hidden=false;
  await listeners.get('visibilitychange')();
  assert.equal(entries[3].unread,0);
  assert.equal(requests.at(-1).body.answer_id,99);
  await click({filter:'ignored'});
  await click({gotoNote:'9'});
  assert.equal(run('state.channel.id'),'b'.repeat(24));
  assert(element('#note-detail').innerHTML.includes('Second document note'));
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
  console.log('Frontend PDF/note/detail navigation, ignore, read receipts and injection checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
