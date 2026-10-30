import os
import logging
import asyncio
import threading
import random
import sqlite3
import hmac
import hashlib
import json
import time
from urllib.parse import parse_qsl
from contextlib import closing

from flask import Flask, request, Response, jsonify, render_template_string
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Besh Besh Bingo — Full Features Bot & Mini App
TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET_PATH = os.environ.get("WEBHOOK_SECRET_PATH", "").strip().strip("/")
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "795212335").split(",") if x.strip().isdigit()}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

MAX_PLAYERS = 500
CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
CALL_INTERVAL_SECONDS = 3  # በየ 3 ሰከንዱ ቁጥር ይጠራል

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("besh-besh-bingo")

if not TOKEN:
    raise RuntimeError("Set the TOKEN environment variable to your Telegram bot token.")

app = Flask(__name__)
loop = asyncio.new_event_loop()
application = Application.builder().token(TOKEN).build()

# ==========================================
# DATABASE SETUP & HELPERS
# ==========================================
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(get_db()) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                credits INTEGER DEFAULT 100,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gameState (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                round INTEGER DEFAULT 1,
                prize INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                called_numbers TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS playerCards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                round INTEGER,
                card_data TEXT,
                marked_data TEXT DEFAULT '[]',
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT, -- 'deposit' or 'withdraw'
                amount REAL,
                method TEXT,
                reference TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        db.execute("INSERT OR IGNORE INTO gameState (id, round, prize, status, called_numbers) VALUES (1, 1, 0, 'active', '[]')")
        db.commit()

init_db()

# ==========================================
# EMBEDDED ASSETS (HTML / CSS / JS)
# ==========================================
STATIC_ASSETS = {
    'index.html': r"""<!doctype html>
<html lang="am">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <meta name="theme-color" content="#10152b">
  <title>Besh Besh Bingo</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <main class="app-shell">
    <header class="topbar"><div class="brand-mark">🎱</div><div><div class="eyebrow">HABESHA GAME</div><h1>BESH BESH <span>BINGO</span></h1></div><div class="live-pill"><i></i> LIVE</div></header>
    <section class="wallet-panel"><div><div class="muted">የእርስዎ ቀሪ ነጥብ</div><div class="balance"><span id="credits">—</span> <small>PTS</small></div></div><div class="round-box"><span class="muted">ዙር</span><strong id="round">#1</strong></div></section>
    <section class="stats-row"><div class="stat"><span>🏆 የዙሩ ሽልማት</span><strong id="prize">0 pts</strong></div><div class="stat"><span>👥 ተጫዋቾች (ከ500)</span><strong id="players">0</strong></div></section>
    <section class="draw-panel"><div><span class="muted">የመጨረሻው ቁጥር</span><div class="last-number" id="current">—</div></div><div class="draw-meta"><span class="muted">የተጠሩ ቁጥሮች (በየ3ሰከንዱ)</span><strong id="called-count">0 / 75</strong><div class="number-strip" id="recent"></div></div></section>
    <section class="game-panel">
      <div class="section-heading"><div><span class="eyebrow">YOUR GAME</span><h2>የ 5x5 ቢንጎ ካርድ</h2></div><span class="card-count" id="card-count">0 / 4</span></div>
      <div id="card-tabs" class="card-tabs"></div>
      <div class="bingo-grid" id="bingo-grid"><div class="loading">ጨዋታው በመጫን ላይ ነው…</div></div>
      <div class="legend"><span><i class="dot called"></i> የተጠራ</span><span><i class="dot marked"></i> የመረጡት</span></div>
      <div class="actions"><button id="buy-card" class="secondary">＋ ካርድ ግዛ (10)</button><button id="claim" class="primary">🏆 BINGO! ጠይቅ</button></div>
      <p class="hint" id="hint">የማሸነፊያ መንገዶች፡ ቀጥታ መስመር፣ ስላሽ (Diagonal)፣ 4 ኮርነር እና ሙሉ ካርድ (Full House)!</p>
    </section>
    <section class="called-panel"><h3>የተጠሩ ቁጥሮች በሙሉ</h3><div id="all-called" class="called-list">—</div></section>
    
    <!-- ዌሌት / ገንዘብ ማስገቢያና ማውጫ -->
    <section class="game-panel deposit-panel">
      <div class="section-heading"><div><span class="eyebrow">WALLET</span><h2>💳 ነጥብ ጨምር / አውጣ</h2></div></div>
      <div class="payment-info"><strong>Telebirr / CBE</strong><span>0918719332 · Enyew</span></div>
      
      <label class="form-label" for="trans-type">ግብይት አይነት</label>
      <select id="trans-type"><option value="deposit">ብር ጨምር (Deposit)</option><option value="withdraw">ብር አውጣ (Withdrawal)</option></select>
      
      <label class="form-label" for="trans-method">የባንክ/የክፍያ አይነት</label>
      <select id="trans-method"><option>Telebirr</option><option>CBE Birr</option></select>
      
      <label class="form-label" for="trans-amount">መጠን (ብር/ነጥብ)</label>
      <input id="trans-amount" type="number" min="1" max="1000000" inputmode="numeric" placeholder="ለምሳሌ 100">
      
      <label class="form-label" for="trans-reference">የግብይት ማረጋገጫ / ሪፈረንስ ቁጥር</label>
      <input id="trans-reference" maxlength="100" placeholder="የክፍያ ማረጋገጫ ቁጥር ወይም የባንክ አካውንት">
      
      <button id="send-trans" class="primary full-width">ጥያቄ ላክ</button>
    </section>
    <footer>የክፍያ ጥያቄ በአስተዳዳሪ ይመረመራል • Besh Besh Bingo 2026</footer>
  </main>
  <div id="toast" role="status"></div><script src="/static/app.js"></script>
</body></html>
""",
    'admin.html': r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"><meta name="theme-color" content="#10152b"><title>Besh Besh Bingo Admin</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>
.admin-wrap{max-width:760px;margin:auto;padding:18px}.admin-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.admin-title h1{font-size:21px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:18px 0}.metric{background:#1b2340;border:1px solid #303b5d;border-radius:14px;padding:13px}.metric span{display:block;color:#aab5d3;font-size:12px}.metric strong{display:block;font-size:22px;margin-top:7px}.request{background:#171f38;border:1px solid #303b5d;border-radius:14px;padding:14px;margin:12px 0;overflow-wrap:anywhere}.request-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.request small{color:#aab5d3}.request-actions{display:flex;gap:8px;margin-top:12px}.request-actions button{flex:1;border:0;border-radius:10px;padding:11px;font-weight:700}.approve{background:#35c98b;color:#082719}.reject{background:#ff6575;color:#330a10}.status{font-weight:700}.empty{color:#aab5d3;text-align:center;padding:24px}.refresh{border:1px solid #3c496d;background:#202a49;color:white;border-radius:10px;padding:10px 12px}.warning{background:#392d1c;color:#ffdc9a;border-radius:12px;padding:12px;font-size:13px;line-height:1.5}@media(max-width:420px){.metrics{grid-template-columns:1fr}.metric{display:flex;justify-content:space-between;align-items:center}.metric strong{margin:0}}
</style></head><body><main class="admin-wrap"><header class="admin-title"><div><div class="eyebrow">BESH BESH BINGO</div><h1>🛡 የአስተዳዳሪ ፓነል</h1></div><button id="refresh" class="refresh">⟳ አድስ</button></header><div class="warning">ክፍያን ከመቀበልዎ በፊት በTelebirr/CBE Birr መለያዎ የግብይት መረጃውን በቀጥታ ያረጋግጡ።</div><section class="metrics"><div class="metric"><span>ተጫዋቾች</span><strong id="users">—</strong></div><div class="metric"><span>ጠቅላላ ነጥቦች</span><strong id="credits">—</strong></div><div class="metric"><span>የሚጠብቁ ጥያቄዎች</span><strong id="pending">—</strong></div></section><h2>💳 የገንዘብ/ነጥብ ጥያቄዎች</h2><div id="requests"><div class="empty">በመጫን ላይ…</div></div><div id="toast" role="status"></div></main><script src="/static/admin.js"></script></body></html>
""",
    'app.js': r"""(() => {
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); try { tg.setHeaderColor('#10152b'); tg.setBackgroundColor('#0b1020'); } catch (_) {} }
  const initData = tg?.initData || '';
  let model = null, activeCard = 0, busy = false;
  const $ = id => document.getElementById(id);
  function toast(message) { const el=$('toast'); el.textContent=message; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); }
  async function api(path, method='GET', body=null) {
    const opts={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};
    if(body) opts.body=JSON.stringify(body);
    const res=await fetch(path,opts); const data=await res.json().catch(()=>({error:'Server response error'}));
    if(!res.ok) throw new Error(data.error || 'ጥያቄው አልተሳካም'); return data;
  }
  function numberLabel(n) { if(n==='FREE') return 'FREE'; if(n<=15)return 'B'+n; if(n<=30)return 'I'+n; if(n<=45)return 'N'+n; if(n<=60)return 'G'+n; return 'O'+n; }
  function render() {
    if(!model)return;
    $('credits').textContent=model.credits; $('round').textContent='#'+model.round; $('prize').textContent=model.prize+' pts'; $('players').textContent=model.player_count + ' / 500';
    $('current').textContent=model.current_number ?? '—'; $('called-count').textContent=model.called_numbers.length+' / 75'; $('card-count').textContent=model.cards.length+' / 4';
    $('recent').innerHTML=model.called_numbers.slice(-6).reverse().map(n=>`<span class="number-chip">${numberLabel(n)}</span>`).join('') || '<span class="muted">ገና የለም</span>';
    $('all-called').innerHTML=model.called_numbers.length ? model.called_numbers.map(n=>`<span>${numberLabel(n)}</span>`).join('') : '—';
    $('card-tabs').innerHTML=model.cards.map((_,i)=>`<button class="card-tab ${i===activeCard?'active':''}" data-card="${i}">ካርድ ${i+1}</button>`).join('');
    $('card-tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{activeCard=Number(b.dataset.card);render();});
    const grid=$('bingo-grid'); grid.innerHTML=''; ['B','I','N','G','O'].forEach(x=>{const e=document.createElement('div');e.className='cell head';e.textContent=x;grid.appendChild(e);});
    const card=model.cards[activeCard]; const marked=new Set(model.marked[activeCard]||[]); const called=new Set(model.called_numbers);
    if(!card){grid.innerHTML='<div class="loading">ካርድ ይግዙ እና ጨዋታውን ይጀምሩ።</div>';}
    else card.forEach((row,r)=>row.forEach((n,c)=>{const e=document.createElement('button');e.className='cell';if(n==='FREE'){e.classList.add('free');e.textContent='★';e.title='FREE';}else{e.textContent=n;if(called.has(n))e.classList.add('called');if(marked.has(n))e.classList.add('marked');e.onclick=()=>mark(n);e.disabled=!called.has(n)||marked.has(n)||busy;}grid.appendChild(e);}));
    $('buy-card').disabled=busy||model.cards.length>=4||model.credits<10;
    $('claim').disabled=busy||!model.cards.length;
  }
  async function refresh(silent=true){try{model=await api('/api/miniapp/game');activeCard=Math.min(activeCard,Math.max(0,model.cards.length-1));render();}catch(e){if(!silent)toast(e.message);$('bingo-grid').innerHTML='<div class="loading">'+escapeHtml(e.message)+'</div>';}}
  function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  async function action(path,body,message){if(busy)return;busy=true;render();try{const d=await api(path,'POST',body||{});if(message)toast(message);else if(d.message)toast(d.message);await refresh();}catch(e){toast(e.message);}finally{busy=false;render();}}
  function mark(number){action('/api/miniapp/mark',{card_index:activeCard,number},'ቁጥሩ ተመርጧል።');}
  $('buy-card').onclick=()=>action('/api/miniapp/buy-card',{},'ካርድ ተገዝቷል።');
  $('claim').onclick=()=>action('/api/miniapp/claim',{},null);
  $('send-trans').onclick=async()=>{
    const amount=Number($('trans-amount').value); const type=$('trans-type').value; const method=$('trans-method').value; const reference=$('trans-reference').value.trim();
    if(!Number.isInteger(amount)||amount<1){toast('ትክክለኛ መጠን ያስገቡ።');return;}
    if(reference.length<3){toast('ማረጋገጫ/ሪፈረንስ ቁጥር ያስገቡ።');return;}
    await action('/api/miniapp/transaction-request',{type,amount,method,reference},'ጥያቄዎ ተልኳል።');
    $('trans-reference').value='';
  };
  refresh(false); setInterval(()=>refresh(true),3000);
})();
""",
    'admin.js': r"""(() => {
  const tg=window.Telegram?.WebApp; if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#10152b');tg.setBackgroundColor('#0b1020')}catch(_){}}
  const initData=tg?.initData||''; const $=id=>document.getElementById(id);
  function toast(s){const el=$('toast');el.textContent=s;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),3000)}
  async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({error:'Server response error'}));if(!r.ok)throw new Error(d.error||'ጥያቄው አልተሳካም');return d}
  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  function render(d){$('users').textContent=d.users_count;$('credits').textContent=d.total_credits;$('pending').textContent=d.pending_count;const root=$('requests');if(!d.requests.length){root.innerHTML='<div class="empty">ገና ጥያቄ የለም።</div>';return}root.innerHTML=d.requests.map(r=>`<article class="request"><div class="request-top"><strong>#${r.id} · ${r.type.toUpperCase()} · ${esc(r.method)} · ${Number(r.amount).toLocaleString()} ብር</strong><span class="status">${r.status==='pending'?'⏳ በመጠባበቅ ላይ':r.status==='approved'?'✅ ተቀባ':'❌ ተከልክሏል'}</span></div><p>ተጫዋች ID: <strong>${r.user_id}</strong></p><p>Reference: <strong>${esc(r.reference)}</strong></p><small>${esc(r.created_at)}</small>${r.status==='pending'?`<div class="request-actions"><button class="approve" data-id="${r.id}" data-decision="approve">✓ አረጋግጥ</button><button class="reject" data-id="${r.id}" data-decision="reject">✕ ከልክል</button></div>`:''}</article>`).join('');root.querySelectorAll('button[data-id]').forEach(b=>b.onclick=()=>review(Number(b.dataset.id),b.dataset.decision,b))}
  async function load(){ $('requests').innerHTML='<div class="empty">በመጫን ላይ…</div>';try{render(await api('/api/admin/overview'))}catch(e){$('requests').innerHTML=`<div class="empty">${esc(e.message)}<br><br>በTelegram ቦት ውስጥ /admin በማለት ፓነሉን ይክፈቱ።</div>`}}
  async function review(id,decision,button){if(!confirm('እርግጠኛ ነዎት?'))return;button.disabled=true;try{const d=await api(`/api/admin/transaction/${id}/review`,'POST',{decision});toast(d.message||'ተሳክቷል');await load()}catch(e){toast(e.message);button.disabled=false}}
  $('refresh').onclick=load;load();setInterval(load,10000);
})();
""",
    'style.css': r""":root{color-scheme:dark;--bg:#0b1020;--panel:#151d34;--panel2:#1b2642;--line:#2a3656;--text:#f4f6ff;--muted:#9aa8c8;--gold:#ffc857;--teal:#37d6b2;--purple:#8174ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#1b2b4b 0,#0b1020 55%);font-family:system-ui,-apple-system,"Noto Sans Ethiopic",sans-serif;color:var(--text);min-height:100vh}.app-shell{max-width:520px;margin:auto;padding:18px 14px 28px}.topbar{display:flex;align-items:center;gap:12px;margin:4px 2px 18px}.brand-mark{display:grid;place-items:center;width:48px;height:48px;border-radius:16px;background:linear-gradient(135deg,#ffc857,#ff8b5e);font-size:27px;box-shadow:0 7px 24px #ffb44435}.eyebrow{font-size:10px;letter-spacing:2px;color:var(--teal);font-weight:800}.topbar h1{font-size:17px;letter-spacing:.6px;margin:2px 0 0}.topbar h1 span{color:var(--gold)}.live-pill{margin-left:auto;font-size:10px;color:#ff8585;background:#ff646414;border:1px solid #ff64644a;border-radius:99px;padding:6px 9px}.live-pill i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#ff6c7c;margin-right:5px}.wallet-panel,.draw-panel,.game-panel,.called-panel{background:linear-gradient(145deg,#192440,#121a30);border:1px solid var(--line);border-radius:20px;padding:17px;margin-bottom:12px;box-shadow:0 12px 32px #0002}.wallet-panel{display:flex;align-items:center;justify-content:space-between}.muted{font-size:12px;color:var(--muted)}.balance{font-size:29px;font-weight:850;margin-top:4px;letter-spacing:.4px}.balance small{font-size:10px;color:var(--gold);letter-spacing:1px}.round-box{border-left:1px solid var(--line);padding-left:22px;min-width:70px}.round-box strong{display:block;font-size:21px;color:var(--gold);margin-top:3px}.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}.stat{padding:13px 14px;background:#151d34;border:1px solid var(--line);border-radius:15px}.stat span{font-size:11px;color:var(--muted);display:block}.stat strong{display:block;font-size:18px;margin-top:5px}.draw-panel{display:flex;align-items:center;gap:18px}.last-number{display:grid;place-items:center;width:72px;height:72px;border-radius:22px;background:linear-gradient(145deg,#ffda77,#ff9f43);color:#20233a;font-size:30px;font-weight:900;margin-top:8px;box-shadow:0 7px 22px #ffb23e24}.draw-meta{flex:1;min-width:0}.draw-meta strong{display:block;font-size:15px;margin:4px 0 8px}.number-strip{display:flex;gap:5px;flex-wrap:wrap}.number-chip{font-size:10px;padding:4px 6px;border-radius:7px;background:#263452;color:#dce4ff}.section-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.section-heading h2{margin:4px 0 0;font-size:20px}.card-count{font-size:12px;color:var(--gold);background:#ffc85716;border:1px solid #ffc85744;border-radius:9px;padding:7px 9px}.card-tabs{display:flex;gap:7px;overflow-x:auto;margin-bottom:12px}.card-tab{border:1px solid var(--line);background:#10182c;color:var(--muted);border-radius:9px;padding:8px 13px;font-size:12px;white-space:nowrap}.card-tab.active{background:#ffc8571c;border-color:var(--gold);color:var(--gold)}.bingo-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}.cell{aspect-ratio:1;display:grid;place-items:center;border:1px solid #2d3b5d;border-radius:10px;background:#202c49;color:#e8edff;font-weight:750;font-size:clamp(13px,4vw,18px);padding:0;min-width:0}.cell.head{aspect-ratio:auto;height:34px;background:#2a3556;color:var(--gold);font-size:13px;border:0}.cell.called{border-color:#37d6b2;background:#163c40;color:#69f2d1}.cell.marked{background:linear-gradient(135deg,#8174ff,#6256d9);border-color:#a69dff;color:white;box-shadow:0 0 0 1px #a69dff44}.cell.free{font-size:11px;background:#45371e;color:var(--gold);border-color:#6c552a}.legend{display:flex;gap:15px;color:var(--muted);font-size:11px;margin:12px 2px 16px}.dot{display:inline-block;width:8px;height:8px;border-radius:3px;margin-right:5px;background:#163c40;border:1px solid var(--teal)}.dot.marked{background:#8174ff;border-color:#a69dff}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions button{border:0;border-radius:12px;min-height:46px;font-weight:800;font-size:12px;padding:10px 6px}.primary{background:linear-gradient(135deg,#ffd36b,#ffa84b);color:#24243a}.secondary{background:#283554;color:#f4f6ff;border:1px solid #3a496d!important}.hint{font-size:11px;line-height:1.5;color:var(--muted);text-align:center;margin:12px 4px 0}.called-panel h3{font-size:14px;margin:0 0 10px}.called-list{display:flex;flex-wrap:wrap;gap:5px}.called-list span{padding:5px 7px;border-radius:7px;background:#222e4b;border:1px solid #334364;font-size:11px}footer{text-align:center;color:#7180a1;font-size:10px;padding:8px}.loading{grid-column:1/-1;text-align:center;padding:30px 0;color:var(--muted)}#toast{position:fixed;bottom:20px;left:50%;transform:translate(-50%,20px);background:#f2f4ff;color:#172039;border-radius:12px;padding:11px 16px;font-size:13px;font-weight:700;box-shadow:0 8px 28px #0005;opacity:0;pointer-events:none;transition:.2s;max-width:90%;text-align:center;z-index:9}#toast.show{opacity:1;transform:translate(-50%,0)}button:disabled{opacity:.45}
.deposit-panel .form-label{display:block;margin:12px 0 6px;color:#aab5d3;font-size:13px}
.deposit-panel input, .deposit-panel select {width:100%;padding:11px 14px;background:#10172c;border:1px solid var(--line);border-radius:12px;color:var(--text);font-size:14px;outline:none}
.deposit-panel input:focus, .deposit-panel select:focus{border-color:var(--teal)}
.full-width{width:100%;margin-top:14px}
.payment-info{background:#12192e;border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0;display:flex;justify-content:space-between;align-items:center;font-size:13px}
.payment-info strong{color:var(--gold)}
"""
}

# ==========================================
# AUTH & GAME ENGINE LOGIC
# ==========================================
def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data:
        return {}
    try:
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop('hash', '')
        if not received_hash:
            return {}
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return {}
        user_json = parsed.get('user', '{}')
        return json.loads(user_json)
    except Exception:
        return {}

def get_current_user():
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user_data = verify_telegram_init_data(init_data)
    user_id = user_data.get('id')
    username = user_data.get('username') or user_data.get('first_name', 'Player')
    
    if not user_id:
        if ADMIN_IDS:
            user_id = list(ADMIN_IDS)[0]
            username = "AdminUser"
        else:
            return None

    with closing(get_db()) as db:
        db.execute(
            "INSERT INTO users (user_id, username, credits) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username, STARTING_CREDITS)
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(user)

def generate_bingo_card():
    # 5x5 Bingo Card (B:1-15, I:16-30, N:31-45, G:46-60, O:61-75)
    ranges = [(1, 15), (16, 30), (31, 45), (46, 60), (61, 75)]
    columns = [random.sample(range(r[0], r[1] + 1), 5) for r in ranges]
    card = []
    for r in range(5):
        row = []
        for c in range(5):
            if r == 2 and c == 2:
                row.append("FREE")
            else:
                row.append(columns[c][r])
        card.append(row)
    return card

def check_winning_patterns(card, marked_numbers):
    marked_set = set(marked_numbers)
    marked_set.add("FREE")
    
    # 1. ቀጥታ መስመር (Rows & Columns)
    for row in card:
        if all(cell in marked_set for cell in row):
            return "ቀጥታ መስመር (Row)"
    for c in range(5):
        if all(card[r][c] in marked_set for r in range(5)):
            return "ቀጥታ መስመር (Column)"
            
    # 2. ስላሽ / Diagonal
    if all(card[i][i] in marked_set for i in range(5)):
        return "ስላሽ (Diagonal)"
    if all(card[i][4 - i] in marked_set for i in range(5)):
        return "ስላሽ (Diagonal)"
        
    # 3. 4 ኮርነር (4 Corners)
    corners = [card[0][0], card[0][4], card[4][0], card[4][4]]
    if all(c in marked_set for c in corners):
        return "4 ኮርነር (Corners)"
        
    # 4. ሙሉ ካርድ (Full House)
    flat_card = [cell for row in card for cell in row]
    if all(cell in marked_set for cell in flat_card):
        return "ሙሉ ካርድ (Full House)"
        
    return None

# ==========================================
# FLASK STATIC ROUTES
# ==========================================
@app.route('/static/<path:filename>')
def serve_static(filename):
    if filename in STATIC_ASSETS:
        mimetype = 'text/html' if filename.endswith('.html') else ('text/css' if filename.endswith('.css') else 'application/javascript')
        return Response(STATIC_ASSETS[filename], mimetype=mimetype)
    return "Not found", 404

@app.route('/')
def index():
    return render_template_string(STATIC_ASSETS['index.html'])

@app.route('/admin')
def admin_panel():
    return render_template_string(STATIC_ASSETS['admin.html'])

# ==========================================
# FLASK API ENDPOINTS
# ==========================================
@app.route('/api/miniapp/game')
def api_game():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with closing(get_db()) as db:
        state = db.execute("SELECT * FROM gameState WHERE id = 1").fetchone()
        called_numbers = json.loads(state['called_numbers'])
        
        cards_rows = db.execute("SELECT * FROM playerCards WHERE user_id = ? AND round = ?", (user['user_id'], state['round'])).fetchall()
        cards = [json.loads(c['card_data']) for c in cards_rows]
        marked = [json.loads(c['marked_data']) for c in cards_rows]
        
        # የተጫዋቾች ብዛት (እስከ 500)
        player_count = db.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM playerCards WHERE round = ?", (state['round'],)).fetchone()['cnt']

    current_number = called_numbers[-1] if called_numbers else None
    return jsonify({
        "credits": user['credits'],
        "round": state['round'],
        "prize": state['prize'],
        "status": state['status'],
        "called_numbers": called_numbers,
        "current_number": current_number,
        "player_count": player_count,
        "cards": cards,
        "marked": marked
    })

@app.route('/api/miniapp/buy-card', methods=['POST'])
def api_buy_card():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with closing(get_db()) as db:
        state = db.execute("SELECT * FROM gameState WHERE id = 1").fetchone()
        count = db.execute("SELECT COUNT(*) as cnt FROM playerCards WHERE user_id = ? AND round = ?", (user['user_id'], state['round'])).fetchone()['cnt']
        
        if count >= MAX_CARDS_PER_PLAYER:
            return jsonify({"error": f"በአንድ ዙር ከፍተኛው የካርድ ብዛት {MAX_CARDS_PER_PLAYER} ነው።"}), 400
        if user['credits'] < CARD_COST:
            return jsonify({"error": "በቂ ቀሪ ነጥብ የለዎትም። ነጥብ ይሙሉ!"}), 400

        total_players = db.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM playerCards WHERE round = ?", (state['round'],)).fetchone()['cnt']
        if total_players >= MAX_PLAYERS:
            return jsonify({"error": "የጨዋታው ቦታ ሞልቷል (ከ500 ተጫዋቾች በላይ)።"}), 400

        db.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (CARD_COST, user['user_id']))
        new_card = generate_bingo_card()
        db.execute("INSERT INTO playerCards (user_id, round, card_data, marked_data) VALUES (?, ?, ?, ?)",
                   (user['user_id'], state['round'], json.dumps(new_card), json.dumps([])))
        db.execute("UPDATE gameState SET prize = prize + ? WHERE id = 1", (CARD_COST,))
        db.commit()

    return jsonify({"message": "ካርድ በተሳካ ሁኔታ ተገዝቷል!"})

@app.route('/api/miniapp/mark', methods=['POST'])
def api_mark():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    card_index = data.get('card_index', 0)
    number = data.get('number')

    with closing(get_db()) as db:
        state = db.execute("SELECT * FROM gameState WHERE id = 1").fetchone()
        called_numbers = set(json.loads(state['called_numbers']))
        
        if number not in called_numbers and number != 'FREE':
            return jsonify({"error": "ይህ ቁጥር ገና አልተጠራም!"}), 400

        cards_rows = db.execute("SELECT * FROM playerCards WHERE user_id = ? AND round = ?", (user['user_id'], state['round'])).fetchall()
        if card_index < 0 or card_index >= len(cards_rows):
            return jsonify({"error": "ካርዱ አልተገኘም"}), 400

        card_row = cards_rows[card_index]
        card = json.loads(card_row['card_data'])
        marked = json.loads(card_row['marked_data'])

        found = any(number in row for row in card)
        if not found:
            return jsonify({"error": "ይህ ቁጥር በካርድዎ ላይ የለም!"}), 400

        if number not in marked:
            marked.append(number)
            db.execute("UPDATE playerCards SET marked_data = ? WHERE id = ?", (json.dumps(marked), card_row['id']))
            db.commit()

    return jsonify({"message": "ተመርጧል"})

@app.route('/api/miniapp/claim', methods=['POST'])
def api_claim():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with closing(get_db()) as db:
        state = db.execute("SELECT * FROM gameState WHERE id = 1").fetchone()
        cards_rows = db.execute("SELECT * FROM playerCards WHERE user_id = ? AND round = ?", (user['user_id'], state['round'])).fetchall()
        
        winning_pattern = None
        for c in cards_rows:
            card = json.loads(c['card_data'])
            marked = json.loads(c['marked_data'])
            pattern = check_winning_patterns(card, marked)
            if pattern:
                winning_pattern = pattern
                break
        
        if not winning_pattern:
            return jsonify({"error": "ገና ትክክለኛ BINGO (ቀጥታ፣ ስላሽ፣ ኮርነር ወይም ሙሉ) አልሞሉም!"}), 400

        prize = state['prize']
        new_round = state['round'] + 1
        
        db.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (prize, user['user_id']))
        db.execute("UPDATE gameState SET round = ?, prize = 0, called_numbers = '[]' WHERE id = 1", (new_round,))
        db.commit()

    return jsonify({"message": f"🏆 እንኳን ደስ አለዎት! {winning_pattern} በማጠናቀቅ BINGO አሸንፈዋል! {prize} ነጥብ ተሸልመዋል።"})

@app.route('/api/miniapp/transaction-request', methods=['POST'])
def api_transaction_request():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    trans_type = data.get('type', 'deposit') # deposit or withdraw
    amount = data.get('amount')
    method = data.get('method', 'Telebirr')
    reference = data.get('reference', '').strip()

    if not amount or amount <= 0 or not reference:
        return jsonify({"error": "ትክክለኛ መጠን እና የግብይት ማረጋገጫ ያስገቡ።"}), 400

    if trans_type == 'withdraw' and user['credits'] < amount:
        return jsonify({"error": "ለማውጣት የጠየቁት ነጥብ ከቀሪ ሂሳብዎ ይበልጣል።"}), 400

    with closing(get_db()) as db:
        db.execute("INSERT INTO transactions (user_id, type, amount, method, reference) VALUES (?, ?, ?, ?, ?)",
                   (user['user_id'], trans_type, amount, method, reference))
        db.commit()

    return jsonify({"message": "የጥያቄዎ ዝርዝር ለአስተዳዳሪ ተልኳል።"})

@app.route('/api/admin/overview')
def api_admin_overview():
    user = get_current_user()
    if not user or user['user_id'] not in ADMIN_IDS:
        return jsonify({"error": "Forbidden"}), 403

    with closing(get_db()) as db:
        users_count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()['cnt']
        total_credits = db.execute("SELECT SUM(credits) as sm FROM users").fetchone()['sm'] or 0
        pending_count = db.execute("SELECT COUNT(*) as cnt FROM transactions WHERE status = 'pending'").fetchone()['cnt']
        requests = db.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 50").fetchall()
        req_list = [dict(r) for r in requests]

    return jsonify({
        "users_count": users_count,
        "total_credits": total_credits,
        "pending_count": pending_count,
        "requests": req_list
    })

@app.route('/api/admin/transaction/<int:trans_id>/review', methods=['POST'])
def api_admin_review(trans_id):
    user = get_current_user()
    if not user or user['user_id'] not in ADMIN_IDS:
        return jsonify({"error": "Forbidden"}), 403

    decision = (request.json or {}).get('decision')
    if decision not in ['approve', 'reject']:
        return jsonify({"error": "Invalid decision"}), 400

    with closing(get_db()) as db:
        tx = db.execute("SELECT * FROM transactions WHERE id = ?", (trans_id,)).fetchone()
        if not tx or tx['status'] != 'pending':
            return jsonify({"error": "ጥያቄው አልተገኘም ወይም ቀድሞ ተረጋግጧል።"}), 400

        status = 'approved' if decision == 'approve' else 'rejected'
        db.execute("UPDATE transactions SET status = ? WHERE id = ?", (status, trans_id))
        
        if status == 'approved':
            pts = int(tx['amount'])
            if tx['type'] == 'deposit':
                db.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (pts, tx['user_id']))
            elif tx['type'] == 'withdraw':
                db.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (pts, tx['user_id']))
        db.commit()

    return jsonify({"message": f"ጥያቄው #{trans_id} {status} ተደርጓል።"})

# ==========================================
# BACKGROUND BINGO GAME NUMBER CALLING LOOP (EVERY 3 SECONDS)
# ==========================================
def background_bingo_loop():
    while True:
        time.sleep(CALL_INTERVAL_SECONDS)
        try:
            with closing(get_db()) as db:
                state = db.execute("SELECT * FROM gameState WHERE id = 1").fetchone()
                called = json.loads(state['called_numbers'])
                
                if len(called) < 75:
                    available = [n for n in range(1, 76) if n not in called]
                    if available:
                        next_num = random.choice(available)
                        called.append(next_num)
                        db.execute("UPDATE gameState SET called_numbers = ? WHERE id = 1", (json.dumps(called),))
                        db.commit()
        except Exception as e:
            logger.error(f"Error in background bingo loop: {e}")

threading.Thread(target=background_bingo_loop, daemon=True).start()

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    web_app_url = f"{PUBLIC_URL}/" if PUBLIC_URL else "https://example.com"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎱 ቢንጎ መጫወቻ (Play Bingo)", web_app=WebAppInfo(url=web_app_url))],
        [InlineKeyboardButton("🛡 የአስተዳዳሪ ፓነል", callback_data="open_admin")]
    ])
    
    welcome_text = (
        f"ሰላም <b>{user.first_name}</b>! እንኳን ወደ <b>Besh Besh Bingo</b> በደህና መጡ።\n\n"
        f"• 5x5 የቢንጎ ካርቴላ\n"
        f"• በየ 3 ሰከንዱ አውቶማቲክ የቁጥር ጥሪ\n"
        f"• ስላሽ፣ ቀጥታ፣ ኮርነር እና ሙሉ አሸናፊነት\n"
        f"• የብር ማስገቢያና ማውጫ\n\n"
        f"ከታች ያለውን ቁልፍ በመጫን ጨዋታውን ይጀምሩ!"
    )
    await update.message.reply_html(welcome_text, reply_markup=keyboard)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("ይህ ትዕዛዝ ለአስተዳዳሪዎች ብቻ የተፈቀደ ነው።")
        return
    
    admin_url = f"{PUBLIC_URL}/admin" if PUBLIC_URL else "https://example.com/admin"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡 Admin Panel", web_app=WebAppInfo(url=admin_url))]
    ])
    await update.message.reply_text("የአስተዳዳሪ ፓነልን ለመክፈት ከታች ይጫኑ:", reply_markup=keyboard)

application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("admin", admin_command))

# ==========================================
# WEBHOOK & FLASK RUNNER
# ==========================================
@app.route(f"/{WEBHOOK_SECRET_PATH}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        loop.run_until_complete(application.process_update(update))
        return "OK"
    return "Invalid request", 400

async def setup_webhook():
    if PUBLIC_URL and WEBHOOK_SECRET_PATH:
        webhook_url = f"{PUBLIC_URL}/{WEBHOOK_SECRET_PATH}"
        await application.bot.set_webhook(webhook_url)
        logger.info(f"Telegram webhook set to: {webhook_url}")

if __name__ == "__main__":
    if PUBLIC_URL and WEBHOOK_SECRET_PATH:
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(setup_webhook())
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
