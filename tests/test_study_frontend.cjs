// Render and injection regression checks, without a browser or network.
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
const element = selector => {
  if (!elements.has(selector)) elements.set(selector, {innerHTML:'',value:'',hidden:false,addEventListener(){},querySelectorAll(){return [];}});
  return elements.get(selector);
};
const context = vm.createContext({
  document:{querySelector:element,querySelectorAll(){return [];},addEventListener(){},hidden:false},
  fetch:()=>new Promise(()=>{}), // Startup polling must never access any real service.
  setTimeout(){},clearTimeout(){},console,URL,FormData:class {},window:{print(){}}
});
vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
const run = code => vm.runInContext(code,context);
assert.equal(run('esc("<script>bad()</script>")'),'&lt;script&gt;bad()&lt;/script&gt;');
const answer = {summary:'<img src=x onerror=alert(1)>',explanation:'ルール\n具体的な説明',examples:[{ja:'はずがない。',zh:'不可能。'}],practice:[{question:'区别？',answer:'答案',explanation:'原因'}],sources:['https://example.com/x?x=" onclick="bad()'],uncertainties:[]};
context.fixtureAnswer = answer;
const markup = run('answerHTML(fixtureAnswer,"model","vision",false)');
assert(!markup.includes('<img src=x'));
assert(markup.includes('&lt;img'));
assert(markup.includes('&quot;'));
assert(markup.includes('<details class="practice">'));
assert(!run('sourceHTML("javascript:alert(1)")').includes('<a '));
context.fixtureNote = {id:8,page:11,title:'两种系统设计的区别',document_title:'系统设计文档',time_trusted:1,device_modified:'2026-11-12T01:49:04Z',tags:['架构'],status:'answered',pdf_url:'/media/document/id#page=11',preview_url:'/media/preview/8',content_hash:'abc123',manual_transcript:1,transcript:'区别是什么',question:'两者有什么区别？',anchor:'接口与实现',answer,answer_provenance:'verified_import',answer_model:'人工核对',history:[]};
run('renderDetail(fixtureNote)');
const detail = elements.get('#note-detail').innerHTML;
assert(detail.includes('区别是什么'));
assert(detail.includes('核对资料后导入'));
assert(detail.includes('/media/preview/8?v=abc123'));
assert(!detail.includes('<img src=x'));
context.genericAnswer={summary:'说明',explanation:'正文',examples:[{text:'采用指数退避重试',explanation:'避免请求集中发送'}]};
assert(run('answerHTML(genericAnswer,"model","vision",false)').includes('采用指数退避重试'));
run('state.folderDraft=["Document/工作"];state.folders=[{path:"Document/工作",documents:2},{path:"Document/工作/项目",documents:1},{path:"Document/工作备份",documents:3}];renderFolders()');
const folderMarkup=elements.get('#folder-list').innerHTML;
assert(folderMarkup.includes('已包含在上级目录中'));
assert(folderMarkup.includes('data-folder="Document/工作" checked'));
assert(!folderMarkup.includes('data-folder="Document/工作备份" checked'));
assert(fs.readFileSync('app/static/index.html','utf8').includes('id="folder-list"'));
assert(!fs.readFileSync('app/static/index.html','utf8').includes('N2'));
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
console.log('Frontend rendering and injection checks passed.');
