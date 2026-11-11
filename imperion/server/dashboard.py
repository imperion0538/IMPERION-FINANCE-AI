"""Operator dashboard: one static page (no data, no secrets). The browser asks for the ADMIN_TOKEN, keeps it in
sessionStorage only, and polls the token-protected /status endpoint. Every value is written with textContent."""

DASHBOARD_HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMPERION Panel</title>
<style>
:root{--bg:#0f141b;--card:#18202b;--line:#263243;--fg:#e6edf5;--mut:#8b9bb0;--ok:#3fb984;--bad:#e5645f;--warn:#e0a84a}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f9;--card:#fff;--line:#dde3ec;--fg:#18212d;--mut:#5c6b7f;--ok:#1b8f5d;--bad:#c53d38;--warn:#a8741a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;letter-spacing:.04em}h2{font-size:12px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
main{padding:16px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}.wide{grid-column:1/-1}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px}.kv span:nth-child(odd){color:var(--mut)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mut);font-weight:500}
.pill{padding:2px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
button,input{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:7px;padding:6px 10px}
button{cursor:pointer}#msg{color:var(--mut)}ul{margin:0;padding-left:18px}li{margin:2px 0;overflow-wrap:anywhere}
</style></head><body>
<header><h1>IMPERION FINANCE</h1><span id="ver" class="pill">-</span><span id="mode" class="pill">-</span>
<span id="trading" class="pill">-</span><span style="flex:1"></span>
<input id="tok" type="password" placeholder="ADMIN_TOKEN" autocomplete="off"><button id="go">Bağlan</button><span id="msg"></span></header>
<main>
<section class="card"><h2>Hesap ve risk</h2><div class="kv" id="risk"></div></section>
<section class="card"><h2>Yapay zekâ</h2><div class="kv" id="ai"></div></section>
<section class="card"><h2>Veri akışı</h2><div class="kv" id="streams"></div></section>
<section class="card wide"><h2>Performans sınıfları (ayrı ölçülür)</h2><div class="scroll"><table id="perf"></table></div></section>
<section class="card"><h2>Canlı kapısı ve hız sınırı</h2><div class="kv" id="gate"></div></section>
<section class="card wide"><h2>Açık pozisyonlar</h2><div class="scroll"><table id="trades"></table></div></section>
<section class="card wide"><h2>Son kararlar</h2><div class="scroll"><table id="decisions"></table></div></section>
<section class="card wide"><h2>Son uyarılar</h2><ul id="events"></ul></section>
</main>
<script>
const $=id=>document.getElementById(id);
const fmt=v=>v===null||v===undefined?'-':(typeof v==='number'?(Math.abs(v)>=100?v.toFixed(2):+v.toPrecision(5)):String(v));
function kv(el,rows){el.replaceChildren();for(const [k,v,c] of rows){const a=document.createElement('span');a.textContent=k;
 const b=document.createElement('span');b.textContent=fmt(v);if(c)b.className=c;el.append(a,b);}}
function table(el,head,rows){el.replaceChildren();const tr=document.createElement('tr');
 for(const h of head){const th=document.createElement('th');th.textContent=h;tr.append(th);}el.append(tr);
 if(!rows.length){const r=document.createElement('tr'),d=document.createElement('td');d.colSpan=head.length;d.textContent='kayıt yok';r.append(d);el.append(r);}
 for(const row of rows){const r=document.createElement('tr');for(const v of row){const d=document.createElement('td');d.textContent=fmt(v);r.append(d);}el.append(r);}}
function pill(el,text,cls){el.textContent=text;el.className='pill '+(cls||'');}
async function load(){const t=sessionStorage.getItem('imperion_token');if(!t){$('msg').textContent='Token girin';return;}
 try{const r=await fetch('/status',{headers:{Authorization:'Bearer '+t},cache:'no-store'});
  if(!r.ok){$('msg').textContent=r.status===401?'Token hatalı':'Hata '+r.status;return;}
  const s=await r.json();$('msg').textContent='Güncellendi '+new Date().toLocaleTimeString();
  pill($('ver'),'v'+s.version+' · '+s.core);pill($('mode'),s.mode);
  pill($('trading'),s.trading_enabled?'İşlem etkin':'İşlem kapalı: '+(s.why_not||''),s.trading_enabled?'ok':'warn');
  const e=s.equity||{},k=s.risk||{};
  kv($('risk'),[['Equity',e.equity],['Kullanılabilir',e.available],['Gerçekleşmemiş',e.unrealized],
   ['Durdurma',k.halted||'yok',k.halted?'bad':'ok'],['Günlük yeni risk %',k.daily_new_risk_pct],['Ardışık kayıp',k.consecutive_losses],['Tepe equity',k.peak_equity]]);
  const a=s.ai||{},m=a.win_model;
  kv($('ai'),[['Ağırlık sürümü','v'+(a.active_version||0)],['Çözülmüş gölge',a.resolved_shadows],['Açık gölge',a.open_shadows],
   ['Gölge beklenti (R)',a.shadow_expectancy_r],['Gölge kazanma',a.shadow_win_rate],['Bekleyen öneri',(a.pending_proposals||[]).length],
   ['Olasılık modeli',m?(m.validated?'doğrulandı':'danışma'):'yok',m&&m.validated?'ok':''],['Model AUC',m&&m.metrics?m.metrics.auc:null],
   ['Bağlam filtresi',a.context_filter?'açık':'kapalı'],['Veto',a.veto?'açık':'kapalı']]);
  const pf=(s.ai||{}).performance||{};
  table($('perf'),['Sınıf','İşlem','Kazanma','Beklenti (R)','Toplam (R)','PnL USDT'],
   [['Gölge','shadow'],['Dry','dry'],['Testnet','testnet'],['Canlı','live']].map(([l,k])=>{const x=pf[k]||{};return [l,x.samples,x.win_rate,x.expectancy_r,x.total_r,x.pnl_usdt];}));
  const g=s.live_gate||{},rl=s.rate_limit||{};
  kv($('gate'),[['Canlı kapısı',g.applies?(g.ok?'açık':'kapalı'):'uygulanmıyor',g.applies?(g.ok?'ok':'bad'):''],['Kapı nedeni',(g.reasons||[]).join('; ')||'-'],
   ['Kullanılan ağırlık/dk',rl.used_weight],['En yüksek ağırlık',rl.max_weight],['429 sayısı',rl.count_429],['418 sayısı',rl.count_418,rl.count_418?'bad':'']]);
  const w=s.streams||{};
  kv($('streams'),[['Market akışı',w.market_connected?'bağlı':'kopuk',w.market_connected?'ok':'bad'],['Public akışı',w.public_connected?'bağlı':'kopuk',w.public_connected?'ok':'bad'],
   ['Market yaşı (sn)',w.market_age_s],['Public yaşı (sn)',w.public_age_s],['Yeniden bağlanma',w.reconnects],['Son hata',w.last_error||'-']]);
  table($('trades'),['Sembol','Yön','Stil','Giriş','Stop','TP1','TP2','Risk USDT','Durum'],
   (s.open_trades||[]).map(t=>[t.symbol,t.side,t.style,t.entry,t.stop,t.tp1,t.tp2,t.risk_usdt,t.status||(t.meta&&t.meta.foreign?'yabancı':'-')]));
  table($('decisions'),['Sembol','Karar','Stil','Derece','Kurulum','Kalite','Rejim','Seans','Zaman'],
   Object.entries(s.last_decisions||{}).map(([sym,d])=>[sym,d.direction,d.style,d.grade||'-',d.playbook||'-',d.quality,d.regime,d.session,new Date(d.ts).toLocaleTimeString()]));
  const ul=$('events');ul.replaceChildren();
  for(const ev of (s.recent_events||[]).slice().reverse()){const li=document.createElement('li');
   li.textContent='['+ev.level+'] '+new Date(ev.ts).toLocaleString()+' — '+ev.message;li.className=ev.level==='CRITICAL'?'bad':ev.level==='ERROR'?'warn':'';ul.append(li);}
 }catch(err){$('msg').textContent='Bağlantı hatası';}}
$('go').onclick=()=>{const v=$('tok').value.trim();if(v){sessionStorage.setItem('imperion_token',v);$('tok').value='';}load();};
load();setInterval(load,15000);
</script></body></html>
"""
