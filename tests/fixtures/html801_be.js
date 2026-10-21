// Выдержка из HTML v8.01 (reference): расчёт и формат уровня безубытка — без изменений
const BE_TRIGGER_R=1.5;
const RU_SW={stopH:50, stopMaxPct:0.02, atrMin:0.025, d1Str:4, holdH:168, beH:0, holdD:96, beD:1.5, ix20:0.04};
function swStored(t){ if(!t) return 0; if(t.sw===2) return 2; return t.swm===1?1:0; }
function beROf(t){ const w=swStored(t); return w===1?RU_SW.beH:w===2?RU_SW.beD:BE_TRIGGER_R; }
function fmt(v){
  if(v==null) return '—';
  const a=Math.abs(v);
  if(a>=1000) return v.toFixed(1);
  if(a>=1) return v.toFixed(3);
  if(a>=0.01) return v.toFixed(5);
  return v.toPrecision(4);
}
function fmtBE(t){
  if(t&&t.mkt==='us') return '—';   // у США безубытка нет
  // Цена, на которой сработает безубыток (+1.5R от входа в сторону тейка) — удобно под уведомление.
  const e=t.e, st=t.st;
  if(e==null||st==null||isNaN(e)||isNaN(st)) return '—';
  const risk=Math.abs(e-st), beR_=beROf(t);
  if(!(beR_>0)) return '—';   // 7.37: свинг по часовому пробою — без безубытка
  const lvl=t.d?e+beR_*risk:e-beR_*risk;
  return fmt(lvl);
}
module.exports={fmtBE,fmt,beROf};
