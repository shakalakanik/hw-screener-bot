// «Отслеживаемые» → «Обновить всё»: оценка/признаки/флаг The best после пересчёта == сигнал скана.
// Запуск: node tests/test_watch_refresh.js  (нужен jsdom: NODE_PATH или локальный node_modules)
const fs=require('fs'), path=require('path');
let JSDOM; try{ ({JSDOM}=require('jsdom')); }catch(e){ ({JSDOM}=require('/workspace/scratch_hw/nodeh/node_modules/jsdom')); }
const R=path.join(__dirname,'..','webapp'), FX=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures','watch_candles.json')));
let h=fs.readFileSync(path.join(R,'screener.html'),'utf8').replace('</body>',`<script>${fs.readFileSync(path.join(R,'lean.js'),'utf8')}</script></body>`);
const T=Date.parse('2026-10-12T19:00:40Z');
const dom=new JSDOM(h,{runScripts:'dangerously',pretendToBeVisual:true,url:'https://x/app',
  beforeParse(w){ w.fetch=()=>Promise.reject(new Error('x')); w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{get:()=>()=>({})}); w.Date.now=()=>T; }});
const w=dom.window; let fails=0, n=0;
const chk=(name,c,x)=>{ fails+=!c; console.log((c?'OK   ':'FAIL ')+name+(c?'':'  '+JSON.stringify(x))); };
const cut=(rows,dur)=>rows.filter(r=>r[0]<=T).map(r=>({ts:r[0],o:r[1],h:r[2],l:r[3],c:r[4],vol_quote:r[5],confirmed:(r[0]+dur)<=T}));
setTimeout(async()=>{
  for(const [sym,v] of Object.entries(FX)){
    const base=sym.replace(/USDT$/,''); w.__d1=cut(v.d1,86400000).slice(-90); w.__h1=cut(v.h1,3600000).slice(-1500);
    const sigs=w.eval(`scanSymbol(${JSON.stringify(base)},${JSON.stringify(sym)},__d1,__h1,{noNight:false,scanBars:400})`).filter(s=>(s.type||'fbo')==='fbo');
    for(const s of sigs.slice(0,4)){
      n++; w.__s=s;
      const exp=w.eval(`({score:signalScore(__s), best:!!(isBestFBO(__s)&&!((bestPoke(recompute(__s,readFFromObj({}))||{})||0)>BEST_POKE))})`);
      // позиция «как после старого Обновить всё»: признаки v5/v6 потеряны, оценка — старая
      const t=w.eval(`(()=>{ const r=recompute(__s,readFFromObj({})); const t=hwCopyFeat({type:'fbo',base:__s.base,sym:__s.sym,t:__s.t,d:__s.d,side:__s.side,lv:__s.lv,k:__s.k,p:__s.p,e:__s.e,st:r.st,tk:r.tk,rp:r.rp,strength:__s.strength,crosses:__s.crosses,score:1.1},__s);
        delete t.approach_atr; delete t.d1_streak; delete t.vtr; return t; })()`);
      w.__t=t;
      await w.eval(`hwRefreshSignal(__t,__d1,__h1)`);
      w.eval(`if(hwX(__t)) __t.score=signalScore(__t)`);   // последняя строка refreshWatch(all)
      const got=w.eval(`({score:__t.score, best:__t.is_best, d1:__t.d1_streak, ap:__t.approach_atr, lvl:__t.lv})`);
      chk(`${base} ${new Date(s.t).toISOString().slice(5,13)} lv=${s.lv} score ${exp.score}`, Math.abs(got.score-exp.score)<1e-9, got);
      chk(`${base} ${s.lv} best flag`, got.best===exp.best, [got.best,exp.best]);
      chk(`${base} ${s.lv} features restored`, got.d1===s.d1_streak && got.ap===s.approach_atr, got);
    }
  }
  chk('checked signals > 0', n>0, n);
  console.log('FAILS',fails); process.exit(fails?1:0);
},2500);
