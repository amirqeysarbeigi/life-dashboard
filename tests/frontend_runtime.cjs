const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync('index.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const {execFileSync}=require('node:child_process');
const payloads=JSON.parse(execFileSync(process.env.PYTHON || 'python3',['tests/frontend_fixture.py'],{encoding:'utf8'}));
const themeTokens=Object.fromEntries([...fs.readFileSync('theme.css','utf8').matchAll(/--([\w-]+):\s*(#[0-9a-f]{6});/g)].map(m=>['--'+m[1],m[2]]));
const elements=new Map();
function make(){const classes=new Set();return {innerHTML:'',textContent:'',style:{},dataset:{},hidden:false,disabled:false,listeners:{},appendChild(){},addEventListener(type,fn){this.listeners[type]=fn},setAttribute(key,value){this[key]=value},classList:{toggle(key,force){if(force??!classes.has(key))classes.add(key);else classes.delete(key)},add(key){classes.add(key)},remove(key){classes.delete(key)},contains(key){return classes.has(key)}},getContext(){return new Proxy({}, {get(){return()=>{}}})},focus(){this.focused=true}}}
function el(id){assert.ok(html.includes('id="'+id+'"'),'Unknown DOM id '+id);if(!elements.has(id))elements.set(id,make());return elements.get(id)}
const tabs=['summary','education','growth','operations','fun','fucked','things','work','insights'].map(id=>({...make(),dataset:{tab:id}}));
const panels=tabs.map(tab=>Object.assign(el('panel-'+tab.dataset.tab),{id:'panel-'+tab.dataset.tab}));
let fail=new Set(),delay=null,requestCount=0;
const context=vm.createContext({getComputedStyle(){return {getPropertyValue(name){return themeTokens[name]||''}}},document:{getElementById:el,createElement(){const e=make();Object.defineProperty(e,'textContent',{set(v){e.innerHTML=String(v).replaceAll('&','&amp;').replaceAll('<','&lt;')},get(){return e.innerHTML}});return e},querySelectorAll(selector){if(selector==='.tab-btn'||selector==='#tab-nav .tab-btn')return tabs;if(selector==='.tab-panel')return panels;return[]}},localStorage:{getItem(){throw Error('Storage blocked')},setItem(){throw Error('Storage blocked')}},Chart:class{constructor(el,config){this.config=config}destroy(){}},Date,Intl,console,AbortController,setTimeout,clearTimeout,requestAnimationFrame(){},window:{innerWidth:1200,innerHeight:800,matchMedia(){return {matches:true}}},fetch:async(url)=>{requestCount++;if(delay)await delay;const name=url.split('/').pop().split('?')[0];if(fail.has(name))throw Error('Offline');return {ok:true,headers:{get(){return'application/json'}},json:async()=>payloads[name]}}});
vm.runInContext(fs.readFileSync('insights.js','utf8'),context);
vm.runInContext(source.replace(/    switchTab\(activeTab\);\n    loadStats\(\);/,''),context);
(async()=>{
 await vm.runInContext('loadStats()',context);
 assert.ok(el('status').textContent.startsWith('Updated'),el('load-feedback').textContent);
 assert.ok(el('content').classList.contains('loaded'));
 for(const tab of tabs)vm.runInContext(`switchTab('${tab.dataset.tab}')`,context);
 assert.equal(tabs[8]['aria-selected'],'true');assert.equal(tabs[8].tabIndex,0);
 el('tab-nav').listeners.keydown({key:'Home',target:tabs[8],preventDefault(){}});
 assert.equal(tabs[0].focused,true);assert.equal(tabs[0]['aria-selected'],'true');
 const sleepCheck=vm.runInContext(`sleepTrendData([
   {start:'2026-10-10T04:00:00+03:30',end:'2026-10-10T12:00:00+03:30',sleep_date:'2026-10-10',hours:8},
   {start:'2026-10-10T23:30:00+03:30',end:'2026-10-11T07:30:00+03:30',sleep_date:'2026-10-11',hours:8}
 ],'7','daily',new Date('2026-10-11T12:00:00Z'))`,context);
 assert.equal(sleepCheck.records.get('2026-10-10').hours,8);
 assert.equal(sleepCheck.records.get('2026-10-11').hours,8);
 assert.equal(sleepCheck.main.filter(Boolean).length,2);
 assert.equal(vm.runInContext("summaryDomains().find(d=>d.id==='growth').data.total_hours",context),payloads.growth.total_hours*2);
 assert.equal(vm.runInContext("insightsAreas().find(d=>d.id==='growth').data.total_hours",context),payloads.growth.total_hours);
 assert.ok(html.includes('data-allocation-category="work"'));
 assert.ok(html.includes('data-allocation-category="transport"'));
 assert.ok(html.includes('data-trend-category="transport"'));
 assert.ok(vm.runInContext("selectedAllocationCategories.has('transport') && selectedTrendCategories.has('transport')",context));
 assert.equal(vm.runInContext("summaryHours(summaryDomains().find(d => d.id === 'transport'), '30')",context),1);
 assert.ok(el('ops-transport-avg').textContent !== '—');
 assert.ok(el('ops-food-avg').textContent !== '—');
 assert.equal(vm.runInContext("summaryHours(summaryDomains().find(d => d.id === 'food'), '30')",context),.5);
 assert.ok(html.includes('data-allocation-category="food"'));
 assert.ok(html.includes('data-trend-category="food"'));
 assert.ok(vm.runInContext("charts['ops-timeframe'].config.data.datasets.some(d=>d.label==='Transport')",context));

 assert.ok(html.includes('data-trend-category="work"'));
 assert.ok(el('work-tasks').innerHTML.includes('&lt;b>Client work&lt;/b>'));
 // Reproduce the early-October month/weekly mismatch and exercise the actual controls.
 vm.runInContext("analyticsData.meta.fetched_at='2026-10-04T12:00:00Z'; analyticsData.allocation_history['2026-09-01']=1; switchTab('summary')",context);
 function chooseTrendWindow(window) {
   const button={dataset:{window}};
   el('category-trend-window').listeners.click({target:{closest:()=>button}});
 }
 function trendConfig(){return vm.runInContext("charts['category-trend'].config",context)}
 chooseTrendWindow('30');
 assert.equal(trendConfig().type,'line');
 assert.deepEqual(Array.from(trendConfig().data.labels),['Oct 1','Oct 2','Oct 3','Oct 4']);
 assert.ok(el('category-trend-note').textContent.includes('daily (auto)'));
 el('category-trend-granularity').listeners.change({target:{value:'weekly'}});
 assert.equal(trendConfig().type,'bar');
 assert.deepEqual(Array.from(trendConfig().data.labels),['Oct 1–4']);
 assert.equal(trendConfig().options.plugins.tooltip.callbacks.title([{dataIndex:0}]),'2026-10-01 – 2026-10-04');
 el('category-trend-granularity').listeners.change({target:{value:'auto'}});
 chooseTrendWindow('365');
 assert.ok(el('category-trend-note').textContent.includes('monthly (auto)'));
 chooseTrendWindow('1');
 assert.equal(trendConfig().type,'bar');
 assert.deepEqual(Array.from(trendConfig().data.labels),['Oct 4']);
 chooseTrendWindow('30');
 el('trend-previous').listeners.click();
 assert.equal(trendConfig().data.labels.length,30);
 assert.equal(trendConfig().data.labels[0],'Sep 1');
 el('trend-current').listeners.click();
 assert.equal(trendConfig().data.labels.length,4);
 // Partial bucket averages include exactly the days in range, with zeros preserved.
 const partial=vm.runInContext("categoryTrendSeries([{data:{allocation_history:{'2026-09-28':99,'2026-10-01':8,'2026-10-04':4}}}], '30', 'weekly', new Date('2026-10-04T12:00:00Z'), 0)",context);
 assert.equal(partial.buckets[0].totals[0],12);
 assert.equal(partial.buckets[0].days,4);
 await vm.runInContext('loadStats()',context);

 assert.ok(vm.runInContext("selectedAllocationCategories.has('work') && selectedTrendCategories.has('work')",context));
 assert.equal(vm.runInContext("summaryHours(summaryDomains().find(d => d.id === 'work'), '30')",context),3);
 vm.runInContext("switchTab('insights')",context);
 assert.ok(el('insights-status').textContent.includes('8 of 8'));
 assert.ok(vm.runInContext("insightsAnalysis().results.every(r=>r.a!=='work' && r.b!=='work')",context));
 assert.ok(!el('insights-coverage').innerHTML.includes('Work'));
 assert.ok(!el('insights-coverage').innerHTML.includes('Things'));
 assert.ok(vm.runInContext("summaryDomains().some(d=>d.id==='work')",context));
 assert.ok(el('insights-findings').innerHTML.includes('No relationship passes'));
 const beforeInsights=requestCount;
 vm.runInContext("insightsWindow=365; renderInsights(); insightsPair={a:'growth',b:'education',lag:1}; renderInsightsPair()",context);
 assert.equal(requestCount,beforeInsights);
 assert.ok(el('insights-pair-note').textContent.includes('next-day'));
 vm.runInContext("thingsData={available:true,completed_daily:{'2026-09-01':4},meta:{fetched_at:new Date().toISOString()}};renderInsights()",context);
 assert.ok(el('insights-status').textContent.includes('9 of 9'));
 assert.ok(el('insights-coverage').innerHTML.includes('Things'));
 assert.ok(vm.runInContext("insightsAnalysis().domains.some(d=>d.id==='things')",context));
 vm.runInContext("thingsData=null;analyticsData=null;workData=null;growthData=null;operationsData=null;funData=null;fuckedData=null;renderInsights()",context);
 assert.ok(el('insights-status').textContent.includes('0 of 8'));
 assert.ok(el('insights-pair-note').textContent.includes('Choose two'));
 await vm.runInContext('loadStats()',context);
 fail.add('work');await vm.runInContext('loadStats(true)',context);
 assert.ok(el('panel-work').classList.contains('domain-unavailable'));
 assert.equal(vm.runInContext('workData',context),null);
 fail.clear();
 fail.add('operations');await vm.runInContext('loadStats(true)',context);
 assert.ok(el('panel-operations').classList.contains('domain-unavailable'));
 assert.equal(vm.runInContext('operationsData',context),null);
 assert.ok(el('load-feedback').textContent.includes('Operations'));
 fail.clear();await vm.runInContext('loadStats(true)',context);
 assert.ok(!el('panel-operations').classList.contains('domain-unavailable'));
 fail.add('analytics');await vm.runInContext('loadStats(true)',context);
 assert.ok(el('content').classList.contains('loaded'));
 assert.ok(el('panel-education').classList.contains('domain-unavailable'));
 assert.ok(el('load-feedback').textContent.includes('AI Education'));
 assert.ok(vm.runInContext('workData.total_hours > 0',context));
 vm.runInContext("switchTab('summary')",context);
 assert.ok(el('summary-stats').innerHTML.includes('Work'));
 assert.ok(vm.runInContext("Object.keys(charts).some(key => key.includes('trend'))",context));
 assert.equal(el('refresh-btn').disabled,false);
 fail=new Set(['analytics','growth','operations','fun','fucked','things','work']);
 await vm.runInContext('loadStats(true)',context);
 assert.equal(el('refresh-btn').textContent,'↻ Retry');
 assert.ok(el('content').classList.contains('loaded'));
 fail.clear();let release;delay=new Promise(resolve=>release=resolve);const before=requestCount;
 const one=vm.runInContext('loadStats(true)',context);await vm.runInContext('loadStats(true)',context);
 assert.equal(requestCount-before,7);assert.ok(el('content').classList.contains('loaded'));assert.equal(el('refresh-btn').disabled,true);
 release();await one;delay=null;assert.equal(el('refresh-btn').disabled,false);assert.equal(el('content')['aria-busy'],'false');
 console.log('PASS: all tabs and charts, blocked storage, keyboard navigation, partial failure/recovery, failed-refresh view preservation and single-flight refresh.');
})().catch(error=>{console.error(error);process.exitCode=1});
