// Render and paging checks without running platform actions or a live server.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('templates/scrm/comments.html', 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nload\(\);\s*$/, '');
const nodes = new Map();
const S = {
  $(id) { if (!nodes.has(id)) nodes.set(id, {value:'',innerHTML:'',addEventListener(){}}); return nodes.get(id); },
  $$() { return []; },
  esc(s) { return String(s ?? '').replace(/[<>&"']/g, ''); },
  platformTag(s) { return s || ''; }, badge(s) { return s; }, fmtTs(s) { return s; },
  toast() {}, pager() {}, api: async () => ({rows:[],offset:50,has_more:false})
};
const context = vm.createContext({S, URLSearchParams, location:{search:'',pathname:'/scrm/comments'},
  history:{replaceState(){}}, document:{}, console, setTimeout,clearTimeout});
vm.runInContext(source, context); // Also checks JavaScript syntax.
vm.runInContext(`
  rows = [{src_pk:1,src_table:'test',thread_root:1,content:'root',matches_filter:false},
          {src_pk:2,src_table:'test',thread_root:1,content:'reply',depth:1},
          {src_pk:9,src_table:'test',thread_root:9,content:'other root'}];
  threadPages.set('test/1',{src_table:'test',root_pk:1,offset:0,has_more:true,batch_size:50});
  render();
`, context);
assert(nodes.get('tbody').innerHTML.includes('下一页回复'));
assert(nodes.get('tbody').innerHTML.includes('关联上下文'));
S.api = async () => ({rows:[{src_pk:51,src_table:'test',thread_root:1,depth:2}],offset:50,has_more:false});
(async () => {
  await vm.runInContext("loadThread({dataset:{thread:'test/1',offset:'50'}})", context);
  assert.deepStrictEqual(JSON.parse(vm.runInContext('JSON.stringify(rows.map(r=>r.src_pk))',context)),[1,51,9]);
  assert(nodes.get('tbody').innerHTML.includes('上一页回复'));
  assert(!nodes.get('tbody').innerHTML.includes('下一页回复'));
  let resolve;
  S.api = () => new Promise(r => { resolve = r; });
  const pending = vm.runInContext("loadThread({dataset:{thread:'test/1',offset:'0'}})",context);
  vm.runInContext('loadVersion++',context);
  resolve({rows:[],offset:0,has_more:false});
  await pending;
  assert.deepStrictEqual(JSON.parse(vm.runInContext('JSON.stringify(rows.map(r=>r.src_pk))',context)),[1,51,9]);
  console.log('PASS: render, bounded reply replacement, root preservation, navigation, stale response');
})().catch(e => {console.error(e);process.exitCode=1;});
