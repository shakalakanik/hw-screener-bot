// Юнит: формат символа под источник (lean.js hwSymFor) + обёртка OKX/Bybit.kline. Запуск: node tests/test_okx_sym.js
const path = require('path'), fs = require('fs'), vm = require('vm');
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x))); };
const src = fs.readFileSync(path.join(__dirname, '..', 'webapp', 'lean.js'), 'utf8');
const tail = src.slice(src.indexOf('/* 17.10: формат символа'));
const calls = [];
const ctx = { console, module: { exports: {} }, document: { readyState: 'complete' } };
vm.createContext(ctx);
vm.runInContext(`var OKX={name:'OKX',async kline(s){__c.push(['OKX',s]);return [];},async k30(s){__c.push(['OKX30',s]);return [];}};
var Bybit={name:'Bybit',async kline(s){__c.push(['Bybit',s]);return [];},async m5(s){__c.push(['Bybit5',s]);return [];}};`, Object.assign(ctx, { __c: calls }));
vm.runInContext(tail, ctx);
const f = ctx.module.exports.hwSymFor;
ok('OKX: BTCUSDT → BTC-USDT-SWAP', f('OKX', 'BTCUSDT') === 'BTC-USDT-SWAP');
ok('OKX: 1000PEPEUSDT', f('OKX', '1000PEPEUSDT') === '1000PEPE-USDT-SWAP');
ok('OKX: уже инстр. OKX не трогаем', f('OKX', 'ETH-USDT-SWAP') === 'ETH-USDT-SWAP');
ok('Bybit: BTC-USDT-SWAP → BTCUSDT', f('Bybit', 'BTC-USDT-SWAP') === 'BTCUSDT');
ok('Bybit: BTCUSDT без изменений', f('Bybit', 'BTCUSDT') === 'BTCUSDT');
ok('MOEX: SBER без изменений', f('MOEX', 'SBER') === 'SBER');
(async () => {
  await vm.runInContext(`(async()=>{ await OKX.kline('BTCUSDT','1h',400); await OKX.k30('ETHUSDT',0); await Bybit.kline('SOL-USDT-SWAP','1h',10); await Bybit.m5('XRPUSDT',48); })()`, ctx);
  ok('обёртка: OKX.kline/k30 и Bybit.kline/m5 получают свой формат', JSON.stringify(calls) === JSON.stringify([['OKX','BTC-USDT-SWAP'],['OKX30','ETH-USDT-SWAP'],['Bybit','SOLUSDT'],['Bybit5','XRPUSDT']]), calls);
  console.log('FAILS', fails); process.exit(fails ? 1 : 0);
})();
