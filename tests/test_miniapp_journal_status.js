// Mini App: 2 рынка (без «Крипта (Алго)»), статус сделок в журнале + «🔄 Обновить», режим MOEX из /api/me.
// Запуск: NODE_PATH=<jsdom> node tests/test_miniapp_journal_status.js
const fs=require('fs'), path=require('path');
let JSDOM; try{ ({JSDOM}=require('jsdom')); }catch(e){ ({JSDOM}=require('/workspace/scratch_hw/node_modules/jsdom')); }
const R=path.join(__dirname,'..','webapp');
const h=fs.readFileSync(path.join(R,'screener.html'),'utf8').replace('</body>',`<script>${fs.readFileSync(path.join(R,'lean.js'),'utf8')}</script></body>`);
const T=Date.parse('2026-10-16T12:00:40Z'), H=3600000, t0=T-10*H;
const row=(id,side,e,st,tk)=>({id,market:'crypto',ticker:'AAAUSDT',signal_ts:t0,sent_ts:t0+H,card:{ticker:'AAAUSDT',side,strategy:'fbo',last:e,stop:st,take:tk,level:e,signal_ts:t0,score:6},charts:{}});
const J={crypto:[row(1,'SHORT',100,102,94),row(2,'LONG',100,98,106),row(3,'SHORT',100,102,94)],ru:[],algo:[]};
const dom=new JSDOM(h,{runScripts:'dangerously',pretendToBeVisual:true,url:'https://x/app',
  beforeParse(w){ w.MessageChannel=class{constructor(){this.port1={};this.port2={postMessage(){}};}};
    w.fetch=async(u)=>{ u=String(u); const js=o=>({ok:true,status:200,json:async()=>o,text:async()=>JSON.stringify(o)});
      if(u.includes('/api/journal')) return js(J); if(u.includes('/api/me')) return js({user_id:1,ru_mode:'swing'}); throw new Error('x'); };
    w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:()=>()=>({})}); w.Date.now=()=>T; }});
const w=dom.window; let fails=0; const chk=(n,c,x)=>{ fails+=!c; console.log((c?'OK   ':'FAIL ')+n+(c?'':'  '+JSON.stringify(x))); };
setTimeout(async()=>{
  // свечи: цена 100 → 101 → 95 (шорт: тейк на 94? нет; лонг: −0.5R…) — простая лента
  const bars=[]; for(let i=-400;i<=9;i++){ const ts=t0+i*H; const c=i<=0?100:(i<5?99:93); bars.push({ts,o:c,h:c+0.5,l:c-0.5,c,vol_quote:1e6,confirmed:true}); }
  w.eval(`Bybit.kline=async()=>(${JSON.stringify(bars)}); EX=null;`);
  chk('market bar: 2 buttons', w.document.querySelectorAll('#hwMktBar button').length===2, w.document.querySelectorAll('#hwMktBar button').length);
  chk('no algo option', !w.document.querySelector('#mkt option[value="algo"]'));
  chk('RU_MODE from /api/me', w.eval('RU_MODE')==='swing', w.eval('RU_MODE'));
  const tab=w.document.getElementById('tabJournal'); if(tab) tab.click();
  await new Promise(r=>setTimeout(r,1500));
  const st=[...w.document.querySelectorAll('.hw-j-status')].map(e=>e.textContent);
  chk('statuses rendered', st.length===3, st);
  chk('short → take', /тейк/.test(st[0]), st[0]);
  chk('long → stop', /стоп/.test(st[1]), st[1]);
  chk('refresh button', !!w.document.querySelector('.hw-j-refresh'));
  w.eval(`Bybit.kline=async()=>(${JSON.stringify(bars.slice(0,402))});`);   // свежие свечи: только 1 час после сигнала → активна
  w.document.querySelector('.hw-j-refresh').click();
  await new Promise(r=>setTimeout(r,1500));
  const st2=[...w.document.querySelectorAll('.hw-j-status')].map(e=>e.textContent);
  chk('refresh recomputes (active)', /активна/.test(st2[0]), st2);
  console.log('FAILS',fails); process.exit(fails?1:0);
},2500);
