const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost:3200/dashboard/new-request?draft=test'});
for (const name of ['window','document','HTMLElement','HTMLInputElement','HTMLDialogElement','Event','KeyboardEvent','MouseEvent','File','Blob']) global[name]=dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
window.scrollTo=()=>{};
HTMLDialogElement.prototype.showModal=function(){this.open=true;};
HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=require('react');
const {act}=React;
const {createRoot}=require('react-dom/client');
const {QueryClient,QueryClientProvider}=require('@tanstack/react-query');
const rootPath=path.resolve(__dirname,'../src');
// Compile the real TS/TSX modules. Only network, routing and the PDF renderer
// are substituted; component events and React state run against a DOM.
function loader(overrides={}) {
 const cache=new Map();
 function load(name){
  if(Object.hasOwn(overrides,name))return overrides[name];
  if(name==='next/link')return {__esModule:true,default:({children,...props})=>React.createElement('a',props,children)};
  if(!name.startsWith('@/'))return require(name);
  const base=path.join(rootPath,name.slice(2));
  const file=['.ts','.tsx','/index.ts','/index.tsx'].map(ext=>base+ext).find(fs.existsSync);
  if(!file)throw new Error('Missing '+name);
  if(cache.has(file))return cache.get(file).exports;
  const module={exports:{}};cache.set(file,module);
  const out=ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true,target:ts.ScriptTarget.ES2020}}).outputText;
  new Function('require','module','exports',out)((requested)=>load(requested.startsWith('.') ? '@/'+path.relative(rootPath,path.resolve(path.dirname(file),requested)) : requested),module,module.exports);
  return module.exports;
 }
 return load;
}
const logic=loader()('@/lib/orderLines');
const line=(key,extra={})=>({key,kind:'item',parentKey:null,positionDescription:'Item '+key,amount:1,unit:'piece',unitPrice:100,totalPrice:100,commodityGroupId:1,...extra});
test('removing an item never promotes its charges into saved totals',()=>{
 const lines=[line('A',{removed:true}),line('B',{kind:'alternative',parentKey:'A'}),line('C',{kind:'fee',parentKey:'B'}),line('D',{kind:'shipping',parentKey:'A'}),line('E')];
 const saved=logic.linesForSave(lines);
 assert.deepEqual(saved.map(l=>l.key),['E']);
 assert.deepEqual(logic.computeTotals(saved,0),logic.computeTotals(lines,0));
 const restored=logic.setRemoved(lines,'A',false);
 assert.equal(logic.linesForSave(restored).length,5);
});
test('positive discount inputs and kind changes subtract before save',()=>{
 const discount=logic.recalcLine(line('A',{kind:'discount',amount:2,unitPrice:10}));
 assert.equal(discount.totalPrice,-20);
 assert.equal(discount.unitPrice,-10);
 assert.equal(logic.setKind([line('A')],'A','discount')[0].totalPrice,-100);
});
test('decimal comma and grouped USD inputs preserve the intended amount',()=>{
 for(const [input,want] of [['1,5',1.5],['-20,5',-20.5],['$1,234.50',1234.5],['1,234',1.234],['$1,234',1234],['1,234,567',1234567],['1.234,50',1234.5],['0.',0],['.5',.5],['-',null],['Infinity',null],['0x10',null]])assert.equal(logic.parseDecimal(input),want,input);
});
async function mount(component){
 const host=document.createElement('div');document.body.appendChild(host);
 const root=createRoot(host),client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 await act(async()=>root.render(React.createElement(QueryClientProvider,{client},component)));
 return {host,async render(component){await act(async()=>root.render(React.createElement(QueryClientProvider,{client},component)));},async close(){await act(async()=>root.unmount());host.remove();client.clear();}};
}
const article={id:'a1',article_number:'CAT-1',supplier_name:'Catalog supplier',supplier_id:'s1',description:'Catalog panel',unit:'piece',unit_price:'25',currency:'USD',quantity:'1'};
const api={articles:{get:async()=>article,list:async()=>({items:[article],total:1,offset:0,limit:20})},suppliers:{list:async()=>[]}};
test('catalog inputs consume Enter; replacement changes only the chosen line',async()=>{
 const Catalog=loader({'@/lib/api':api})('@/components/CatalogSuggestion').default;
 let submitted=0,applied=[];
 const view=await mount(React.createElement('form',{onSubmit:e=>{e.preventDefault();submitted++;}},React.createElement(Catalog,{line:line('A'),editable:true,onApply:(...args)=>applied.push(args),onRestore:()=>{}})));
 try{
  await act(async()=>[...view.host.querySelectorAll('button')].find(b=>b.textContent==='Replace from catalog').click());
  const search=view.host.querySelector('input[aria-label="Search catalog"]');
  const enter=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true});
  await act(async()=>search.dispatchEvent(enter));
  assert.equal(enter.defaultPrevented,true);
  assert.equal(submitted,0);
  await act(async()=>new Promise(r=>setTimeout(r,300)));
  await act(async()=>[...view.host.querySelectorAll('button')].find(b=>b.textContent.includes('Catalog panel')).click());
  const qty=view.host.querySelector('input[type=number]');
  await act(async()=>qty.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true})));
  assert.equal(applied.length,1);
  assert.equal(applied[0][0].articleId,'a1');
  assert.equal(applied[0][1],1);
  assert.equal(submitted,0);
 }finally{await view.close();}
});
test('saved links use the catalog article number, not the quoted vendor number',async()=>{
 const Catalog=loader({'@/lib/api':api})('@/components/CatalogSuggestion').default;
 const view=await mount(React.createElement(Catalog,{line:line('A',{articleId:'a1',vendorItemNumber:'QUOTED-OLD'}),editable:false,onApply:()=>{},onRestore:()=>{}}));
 try{
  await act(async()=>new Promise(r=>setTimeout(r,20)));
  assert.equal(view.host.querySelector('a').textContent,'CAT-1 ↗');
  assert.equal(view.host.querySelector('a').getAttribute('href'),'/dashboard/articles/a1');
  assert.equal(view.host.textContent.includes('QUOTED-OLD'),false);
  assert.equal([...view.host.querySelectorAll('button')].some(b=>b.textContent==='Replace from catalog'),false);
 }finally{await view.close();}
});
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function pageApi(){
 const draft={requestorName:'Rita',title:'Quote',vendorName:'Vendor',department:'IT',orderLines:[line('A')],extracted:true};
 return {drafts:{get:async()=>({data:draft,hasDocument:false,updatedAt:new Date().toISOString()}),update:async()=>({updatedAt:new Date().toISOString()})},articles:api.articles,suppliers:api.suppliers,requests:{create:async()=>({id:'request1'})}};
}
function pageLoader(service){return loader({'@/lib/api':service,'next/navigation':{useRouter:()=>({push:()=>{},refresh:()=>{},back:()=>{}})},'@/lib/hooks/useCommodityGroups':{useCommodityGroups:()=>({data:[{id:1,name:'Test',category:'Test'}]})},'@/lib/useCatalogSuggestions':{useCatalogSuggestions:()=>({byKey:{},loading:false})},'@/components/DocumentPanel':{__esModule:true,default:()=>null}})('@/app/dashboard/new-request/page').default;}
test('double submission creates exactly one request while its first call is pending',async()=>{
 window.history.replaceState(null,'','/dashboard/new-request?draft=test');
 const service=pageApi(),pending=deferred();let calls=0;
 service.requests.create=()=>{calls++;return pending.promise;};
 const Page=pageLoader(service);const view=await mount(React.createElement(Page));
 try{
  const form=view.host.querySelector('form');
  await act(async()=>{form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));});
  assert.equal(calls,1);
  assert.equal([...view.host.querySelectorAll('button')].find(b=>b.type==='submit').disabled,true);
  await act(async()=>pending.resolve({id:'request1'}));
 }finally{await view.close();}
});
test('draft updates stay ordered when the older request is slow',async()=>{
 window.history.replaceState(null,'','/dashboard/new-request?draft=test');
 const service=pageApi(),first=deferred(),titles=[];
 service.drafts.update=(id,data)=>{titles.push(data.title);return titles.length===1?first.promise:Promise.resolve({updatedAt:new Date().toISOString()});};
 const Page=pageLoader(service);const view=await mount(React.createElement(Page));
 try{
  await act(async()=>new Promise(r=>setTimeout(r,1100)));
  assert.deepEqual(titles,['Quote']);
  const input=view.host.querySelector('[aria-label="Title / Short Description"]');
  await act(async()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'Newer quote');input.dispatchEvent(new Event('input',{bubbles:true}));});
  await act(async()=>new Promise(r=>setTimeout(r,1100)));
  assert.equal(titles.length,1,'new write waits for the old one');
  await act(async()=>first.resolve({updatedAt:new Date().toISOString()}));
  assert.deepEqual(titles,['Quote','Newer quote']);
 }finally{await view.close();}
});
test('request submission waits for pending draft writes before consuming the draft',async()=>{
 window.history.replaceState(null,'','/dashboard/new-request?draft=test');
 const service=pageApi(),write=deferred();let saves=0,creates=0;
 service.drafts.update=()=>{saves++;return write.promise;};
 service.requests.create=async()=>{creates++;return {id:'request1'};};
 const Page=pageLoader(service);const view=await mount(React.createElement(Page));
 try{
  await act(async()=>new Promise(r=>setTimeout(r,1100)));
  assert.equal(saves,1);
  await act(async()=>view.host.querySelector('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(creates,0);
  await act(async()=>write.resolve({updatedAt:new Date().toISOString()}));
  assert.equal(creates,1);
 }finally{await view.close();}
});
test('a failed draft PDF download keeps its identity and stored fields',async()=>{
 window.history.replaceState(null,'','/dashboard/new-request?draft=original');
 const service=pageApi(),baseGet=service.drafts.get;
 service.drafts.get=async()=>({...await baseGet(),hasDocument:true});
 service.drafts.documentUrl=async()=>{throw new Error('Temporary network failure');};
 const Page=pageLoader(service);const view=await mount(React.createElement(Page));
 try{
  assert.match(window.location.search,/draft=original/);
  assert.equal(view.host.querySelector('[aria-label="Title / Short Description"]').value,'Quote');
  assert.match(view.host.textContent,/draft fields are preserved/);
 }finally{await view.close();}
});
test('a fresh dashboard session clears the prior account cache',async()=>{
 const client=new QueryClient();client.setQueryData(['commodity-groups'],[{id:9,name:'Prior account'}]);
 const router={push:()=>{}};
 const Layout=loader({'@/lib/api':{auth:{me:async()=>({user:{id:'new',email:'new@example.com'}})},organizations:{getMine:async()=>({role:'admin'})}},'next/navigation':{useRouter:()=>router},'@/app/dashboard/components/DashboardNav':{__esModule:true,default:()=>null}})('@/app/dashboard/layout').default;
 const host=document.createElement('div');document.body.appendChild(host);const root=createRoot(host);
 try{
  await act(async()=>root.render(React.createElement(QueryClientProvider,{client},React.createElement(Layout,null,React.createElement('p',null,'Ready')))));
  assert.equal(client.getQueryData(['commodity-groups']),undefined);
  assert.match(host.textContent,/Ready/);
 }finally{await act(async()=>root.unmount());host.remove();client.clear();}
});
function upload(view,file){const input=view.host.querySelector('input[type=file]');Object.defineProperty(input,'files',{value:[file],configurable:true});input.dispatchEvent(new Event('change',{bubbles:true}));}
URL.createObjectURL=()=> 'blob:mock-document';
URL.revokeObjectURL=()=>{};
test('a failed replacement extraction cannot retain the previous quote lines',async()=>{
 window.history.replaceState(null,'','/dashboard/new-request?draft=test');
 const service=pageApi();
 service.extraction={extractPdf:async()=>({success:false,error:'Unreadable quote'})};
 service.drafts.uploadDocument=async()=>{};
 const Page=pageLoader(service);const view=await mount(React.createElement(Page));
 try{
  await act(async()=>upload(view,new File(['%PDF-test'],'new-quote.pdf',{type:'application/pdf'})));
  assert.equal(view.host.querySelector('[aria-label="Title / Short Description"]').value,'');
  assert.equal(view.host.querySelectorAll('[role=button]').length,0);
  assert.match(view.host.textContent,/Unreadable quote/);
 }finally{await view.close();}
});
test('submitting a replacement quote waits for the new PDF upload',async()=>{
 window.history.replaceState(null,'','/dashboard/new-request?draft=test');
 const service=pageApi(),uploading=deferred();let uploaded=0,created=0;
 service.extraction={extractPdf:async()=>({success:true,data:{title:'New PDF',vendorName:'New Vendor',orderLines:[line('A')],taxTotal:0}})};
 service.drafts.uploadDocument=()=>{uploaded++;return uploading.promise;};
 service.requests.create=async data=>{created++;assert.equal(data.titleShortDescription,'New PDF');assert.equal(data.draftId,'test');return {id:'request1'};};
 const Page=pageLoader(service);const view=await mount(React.createElement(Page));
 try{
  await act(async()=>upload(view,new File(['%PDF-test'],'new-quote.pdf',{type:'application/pdf'})));
  assert.equal(uploaded,1);
  await act(async()=>view.host.querySelector('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(created,0);
  await act(async()=>uploading.resolve());
  assert.equal(created,1);
  assert.equal(uploaded,1);
 }finally{await view.close();}
});
test('unsaved line edits disable actions that would replace them with stored lines',async()=>{
 const request={id:'r1',requestor_name:'Rita',title:'Request',vendor_name:'Vendor',department:'IT',commodity_group_id:1,total_cost:100,status:'open',approval_status:'pending',order_lines:[{...line('A'),position_description:'Item A',unit_price:100,total_price:100}],taxTotal:0};
 const router={push:()=>{}};
 const service={...api,requests:{get:async()=>request,getActivity:async()=>[],documentUrl:async()=>null},organizations:{getMine:async()=>({role:'admin'})}};
 const Page=loader({'@/lib/api':service,'next/navigation':{useRouter:()=>router,useParams:()=>({id:'r1'})},'@/lib/hooks/useCommodityGroups':{useCommodityGroups:()=>({data:[{id:1,name:'Test',category:'Test'}]})},'@/lib/useCatalogSuggestions':{useCatalogSuggestions:()=>({byKey:{},loading:false})},'@/components/DocumentPanel':{__esModule:true,default:()=>null}})('@/app/dashboard/requests/[id]/page').default;
 const view=await mount(React.createElement(Page));
 try{
  await act(async()=>view.host.querySelector('[role=button]').click());
  const input=view.host.querySelector('input[id=description]');
  await act(async()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'Unsaved new description');input.dispatchEvent(new Event('input',{bubbles:true}));});
  for(const label of ['Edit','Approve','Reject','Closed','In Progress']){
   const button=[...view.host.querySelectorAll('button')].find(b=>b.textContent===label);
   assert.ok(button,label);assert.equal(button.disabled,true,label);
  }
  assert.equal(input.value,'Unsaved new description');
 }finally{await view.close();}
});
test('a delayed previous request cannot overwrite the current request details',async()=>{
 let currentId='old';const old=deferred(),router={push:()=>{}};
 const request=id=>({id,requestor_name:'Rita',title:'Request '+id,vendor_name:'Vendor',department:'IT',commodity_group_id:1,total_cost:100,status:'open',approval_status:'pending',order_lines:[{...line('A'),position_description:'Item A',unit_price:100,total_price:100}],taxTotal:0});
 const service={...api,requests:{get:async id=>id==='old'?old.promise:request(id),getActivity:async()=>[],documentUrl:async()=>null},organizations:{getMine:async()=>({role:'admin'})}};
 const Page=loader({'@/lib/api':service,'next/navigation':{useRouter:()=>router,useParams:()=>({id:currentId})},'@/lib/hooks/useCommodityGroups':{useCommodityGroups:()=>({data:[]})},'@/lib/useCatalogSuggestions':{useCatalogSuggestions:()=>({byKey:{},loading:false})},'@/components/DocumentPanel':{__esModule:true,default:()=>null}})('@/app/dashboard/requests/[id]/page').default;
 const view=await mount(React.createElement(Page));
 try{
  currentId='new';await view.render(React.createElement(Page));
  assert.equal(view.host.querySelector('h1').textContent,'Request new');
  await act(async()=>old.resolve(request('old')));
  assert.equal(view.host.querySelector('h1').textContent,'Request new');
 }finally{await view.close();}
});
test('changing a position to optional keeps its charges inactive and attached',()=>{
 const lines=[line('A'),line('F',{kind:'fee',parentKey:'A',unitPrice:10,totalPrice:10})];
 const changed=logic.setKind(lines,'A','optional');
 assert.equal(changed[1].parentKey,'A');
 assert.equal(logic.computeTotals(changed,0).grand,0);
 const restored=logic.setKind(changed,'A','item');
 assert.equal(logic.computeTotals(restored,0).grand,110);
});
test('changing a charge to an alternative clears an incompatible optional parent',()=>{
 const lines=[line('A',{kind:'optional'}),line('F',{kind:'fee',parentKey:'A'})];
 assert.equal(logic.setKind(lines,'F','alternative')[1].parentKey,null);
});
