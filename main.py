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

from flask import Flask, request, Response
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

# Besh Besh Bingo â€” virtual-credit bingo bot.
# Never put your Telegram bot token directly in source code.
TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET_PATH = os.environ.get("WEBHOOK_SECRET_PATH", "").strip().strip("/")
LIVE_CHAT_ID = os.environ.get("LIVE_CHAT_ID", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "795212335").split(",") if x.strip().isdigit()}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

MAX_PLAYERS = 500
CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
CALL_INTERVAL_SECONDS = 5

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("besh-besh-bingo")

if not TOKEN:
    raise RuntimeError(
        "Set the TOKEN environment variable to your Telegram bot token."
    )

if not PUBLIC_URL:
    logger.warning(
        "RENDER_EXTERNAL_URL is not set; Telegram webhook setup will be skipped."
    )

app = Flask(__name__)
loop = asyncio.new_event_loop()
application = Application.builder().token(TOKEN).build()

# Embedded Mini App/Admin Panel assets: this project runs from main.py alone.
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
    <header class="topbar"><div class="brand-mark">ðŸŽ±</div><div><div class="eyebrow">HABESHA GAME</div><h1>BESH BESH <span>BINGO</span></h1></div><div class="live-pill"><i></i> LIVE</div></header>
    <section class="wallet-panel"><div><div class="muted">á‹¨áŠ¥áˆ­áˆµá‹Ž á‰€áˆª áŠáŒ¥á‰¥</div><div class="balance"><span id="credits">â€”</span> <small>PTS</small></div></div><div class="round-box"><span class="muted">á‹™áˆ­</span><strong id="round">#1</strong></div></section>
    <section class="stats-row"><div class="stat"><span>ðŸ† á‹¨á‹™áˆ© áˆ½áˆáˆ›á‰µ</span><strong id="prize">0 pts</strong></div><div class="stat"><span>ðŸ‘¥ á‰°áŒ«á‹‹á‰¾á‰½</span><strong id="players">0</strong></div></section>
    <section class="draw-panel"><div><span class="muted">á‹¨áˆ˜áŒ¨áˆ¨áˆ»á‹ á‰áŒ¥áˆ­</span><div class="last-number" id="current">â€”</div></div><div class="draw-meta"><span class="muted">á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½</span><strong id="called-count">0 / 75</strong><div class="number-strip" id="recent"></div></div></section>
    <section class="game-panel"><div class="section-heading"><div><span class="eyebrow">YOUR GAME</span><h2>á‹¨á‰¢áŠ•áŒŽ áŠ«áˆ­á‹µ</h2></div><span class="card-count" id="card-count">0 / 4</span></div>
      <div id="card-tabs" class="card-tabs"></div>
      <div class="bingo-grid" id="bingo-grid"><div class="loading">áŒ¨á‹‹á‰³á‹ á‰ áˆ˜áŒ«áŠ• áˆ‹á‹­ áŠá‹â€¦</div></div>
      <div class="legend"><span><i class="dot called"></i> á‹¨á‰°áŒ áˆ«</span><span><i class="dot marked"></i> á‹¨áˆ˜áˆ¨áŒ¡á‰µ</span></div>
      <div class="actions"><button id="buy-card" class="secondary">ï¼‹ áŠ«áˆ­á‹µ áŒá‹› (10)</button><button id="claim" class="primary">ðŸ† BINGO! áŒ á‹­á‰…</button></div>
      <p class="hint" id="hint">á‹¨á‰°áŒ áˆ« á‰áŒ¥áˆ­ á‰ áŠ«áˆ­á‹µá‹Ž áˆ‹á‹­ áˆ²áŠ–áˆ­ á‹­áŠ•áŠ©á‰µá¢</p>
    </section>
    <section class="called-panel"><h3>á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½</h3><div id="all-called" class="called-list">â€”</div></section>
    <section class="game-panel deposit-panel">
      <div class="section-heading"><div><span class="eyebrow">WALLET</span><h2>ðŸ’³ áŠáŒ¥á‰¥ áŒ¨áˆáˆ­</h2></div></div>
      <p class="hint">áŠ­áá‹« áŠ¨áˆ˜áˆáŒ¸áˆá‹Ž á‰ áŠá‰µ á‹¨á‰°á‰€á‰£á‹­ áˆ˜áˆ¨áŒƒá‹áŠ• á‹«áˆ¨áŒ‹áŒáŒ¡á¢ áŠáŒ¥á‰¦á‰½ á‹¨áˆšáŒ¨áˆ˜áˆ©á‰µ áŠ áˆµá‰°á‹³á‹³áˆªá‹ áŠ­áá‹«á‹áŠ• áŠ«áˆ¨áŒ‹áŒˆáŒ  á‰ áŠ‹áˆ‹ á‰¥á‰» áŠá‹á¢</p>
      <div class="payment-info"><strong>Telebirr</strong><span>0918719332 Â· Enyew</span></div>
      <div class="payment-info"><strong>CBE Birr</strong><span>0918719332 Â· Enyew</span></div>
      <label class="form-label" for="deposit-method">á‹¨áŠ­áá‹« áŠ á‹­áŠá‰µ</label><select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
      <label class="form-label" for="deposit-amount">á‹¨áŠ­áá‹« áˆ˜áŒ áŠ• (á‰¥áˆ­)</label><input id="deposit-amount" type="number" min="1" max="1000000" inputmode="numeric" placeholder="áˆˆáˆáˆ³áˆŒ 100">
      <label class="form-label" for="deposit-reference">á‹¨áŒá‰¥á‹­á‰µ áˆ›áˆ¨áŒ‹áŒˆáŒ« / Reference</label><input id="deposit-reference" maxlength="100" placeholder="á‹¨áŠ­áá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ« á‰áŒ¥áˆ­">
      <button id="send-deposit" class="primary full-width">á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ áˆ‹áŠ­</button>
    </section>
    <footer>á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ á‰ áŠ áˆµá‰°á‹³á‹³áˆª á‹­áˆ˜áˆ¨áˆ˜áˆ«áˆ â€¢ á‹¨áˆ›áˆ¨áŒ‹áŒˆáŒ« á‰áŒ¥áˆ­á‹ŽáŠ• á‰¥á‰» á‹«áˆµáŒˆá‰¡</footer>
  </main>
  <div id="toast" role="status"></div><script src="/static/app.js"></script>
</body></html>
""",
    'admin.html': r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"><meta name="theme-color" content="#10152b"><title>Besh Besh Bingo Admin</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>
.admin-wrap{max-width:760px;margin:auto;padding:18px}.admin-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.admin-title h1{font-size:21px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:18px 0}.metric{background:#1b2340;border:1px solid #303b5d;border-radius:14px;padding:13px}.metric span{display:block;color:#aab5d3;font-size:12px}.metric strong{display:block;font-size:22px;margin-top:7px}.request{background:#171f38;border:1px solid #303b5d;border-radius:14px;padding:14px;margin:12px 0;overflow-wrap:anywhere}.request-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.request small{color:#aab5d3}.request-actions{display:flex;gap:8px;margin-top:12px}.request-actions button{flex:1;border:0;border-radius:10px;padding:11px;font-weight:700}.approve{background:#35c98b;color:#082719}.reject{background:#ff6575;color:#330a10}.status{font-weight:700}.empty{color:#aab5d3;text-align:center;padding:24px}.refresh{border:1px solid #3c496d;background:#202a49;color:white;border-radius:10px;padding:10px 12px}.warning{background:#392d1c;color:#ffdc9a;border-radius:12px;padding:12px;font-size:13px;line-height:1.5}@media(max-width:420px){.metrics{grid-template-columns:1fr}.metric{display:flex;justify-content:space-between;align-items:center}.metric strong{margin:0}}
</style></head><body><main class="admin-wrap"><header class="admin-title"><div><div class="eyebrow">BESH BESH BINGO</div><h1>ðŸ›¡ á‹¨áŠ áˆµá‰°á‹³á‹³áˆª á“áŠáˆ</h1></div><button id="refresh" class="refresh">âŸ³ áŠ á‹µáˆµ</button></header><div class="warning">áŠ­áá‹«áŠ• áŠ¨áˆ˜á‰€á‰ áˆá‹Ž á‰ áŠá‰µ á‰ Telebirr/CBE Birr áˆ˜áˆˆá‹«á‹Ž á‹¨áŒá‰¥á‹­á‰µ áˆ˜áˆ¨áŒƒá‹áŠ• á‰ á‰€áŒ¥á‰³ á‹«áˆ¨áŒ‹áŒáŒ¡á¢ á‰ á‰°áŒ«á‹‹á‰¹ á‹¨áŒ»áˆá‹ á‹¨áˆ›áˆ¨áŒ‹áŒˆáŒ« á‰áŒ¥áˆ­ á‰¥á‰» á‰ á‰‚ áˆ›áˆµáˆ¨áŒƒ áŠ á‹­á‹°áˆˆáˆá¢</div><section class="metrics"><div class="metric"><span>á‰°áŒ«á‹‹á‰¾á‰½</span><strong id="users">â€”</strong></div><div class="metric"><span>áŒ á‰…áˆ‹áˆ‹ áŠáŒ¥á‰¦á‰½</span><strong id="credits">â€”</strong></div><div class="metric"><span>á‹¨áˆšáŒ á‰¥á‰ áŠ­áá‹«á‹Žá‰½</span><strong id="pending">â€”</strong></div></section><h2>ðŸ’³ á‹¨áŠ­áá‹« áŒ¥á‹«á‰„á‹Žá‰½</h2><div id="requests"><div class="empty">á‰ áˆ˜áŒ«áŠ• áˆ‹á‹­â€¦</div></div><div id="toast" role="status"></div></main><script src="/static/admin.js"></script></body></html>
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
    if(!res.ok) throw new Error(data.error || 'áŒ¥á‹«á‰„á‹ áŠ áˆá‰°áˆ³áŠ«áˆ'); return data;
  }
  function numberLabel(n) { if(n==='FREE') return 'FREE'; if(n<=15)return 'B'+n; if(n<=30)return 'I'+n; if(n<=45)return 'N'+n; if(n<=60)return 'G'+n; return 'O'+n; }
  function render() {
    if(!model)return;
    $('credits').textContent=model.credits; $('round').textContent='#'+model.round; $('prize').textContent=model.prize+' pts'; $('players').textContent=model.player_count;
    $('current').textContent=model.current_number ?? 'â€”'; $('called-count').textContent=model.called_numbers.length+' / 75'; $('card-count').textContent=model.cards.length+' / 4';
    $('recent').innerHTML=model.called_numbers.slice(-6).reverse().map(n=>`<span class="number-chip">${numberLabel(n)}</span>`).join('') || '<span class="muted">áŒˆáŠ“ á‹¨áˆˆáˆ</span>';
    $('all-called').innerHTML=model.called_numbers.length ? model.called_numbers.map(n=>`<span>${numberLabel(n)}</span>`).join('') : 'â€”';
    $('card-tabs').innerHTML=model.cards.map((_,i)=>`<button class="card-tab ${i===activeCard?'active':''}" data-card="${i}">áŠ«áˆ­á‹µ ${i+1}</button>`).join('');
    $('card-tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{activeCard=Number(b.dataset.card);render();});
    const grid=$('bingo-grid'); grid.innerHTML=''; ['B','I','N','G','O'].forEach(x=>{const e=document.createElement('div');e.className='cell head';e.textContent=x;grid.appendChild(e);});
    const card=model.cards[activeCard]; const marked=new Set(model.marked[activeCard]||[]); const called=new Set(model.called_numbers);
    if(!card){grid.innerHTML='<div class="loading">áŠ«áˆ­á‹µ á‹­áŒá‹™ áŠ¥áŠ“ áŒ¨á‹‹á‰³á‹áŠ• á‹­áŒ€áˆáˆ©á¢</div>';}
    else card.forEach((row,r)=>row.forEach((n,c)=>{const e=document.createElement('button');e.className='cell';if(n==='FREE'){e.classList.add('free');e.textContent='â˜…';e.title='FREE';}else{e.textContent=n;if(called.has(n))e.classList.add('called');if(marked.has(n))e.classList.add('marked');e.onclick=()=>mark(n);e.disabled=!called.has(n)||marked.has(n)||busy;}grid.appendChild(e);}));
    $('buy-card').disabled=busy||model.cards.length>=4||model.credits<10;
    $('claim').disabled=busy||!model.cards.length;
  }
  async function refresh(silent=true){try{model=await api('/api/miniapp/game');activeCard=Math.min(activeCard,Math.max(0,model.cards.length-1));render();}catch(e){if(!silent)toast(e.message);$('bingo-grid').innerHTML='<div class="loading">'+escapeHtml(e.message)+'</div>';}}
  function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  async function action(path,body,message){if(busy)return;busy=true;render();try{const d=await api(path,'POST',body||{});if(message)toast(message);else if(d.message)toast(d.message);await refresh();}catch(e){toast(e.message);}finally{busy=false;render();}}
  function mark(number){action('/api/miniapp/mark',{card_index:activeCard,number},'á‰áŒ¥áˆ© á‰°áˆ˜áˆ­áŒ§áˆá¢');}
  $('buy-card').onclick=()=>action('/api/miniapp/buy-card',{},'áŠ«áˆ­á‹µ á‰°áŒˆá‹á‰·áˆá¢');
  $('claim').onclick=()=>action('/api/miniapp/claim',{},null);
  $('send-deposit').onclick=async()=>{
    const amount=Number($('deposit-amount').value); const method=$('deposit-method').value; const reference=$('deposit-reference').value.trim();
    if(!Number.isInteger(amount)||amount<1){toast('á‰µáŠ­áŠ­áˆˆáŠ› á‹¨áŒˆáŠ•á‹˜á‰¥ áˆ˜áŒ áŠ• á‹«áˆµáŒˆá‰¡á¢');return;}
    if(reference.length<3){toast('á‹¨áŒá‰¥á‹­á‰µ áˆ›áˆ¨áŒ‹áŒˆáŒ« á‹«áˆµáŒˆá‰¡á¢');return;}
    await action('/api/miniapp/deposit-request',{amount,method,reference},null);
    $('deposit-reference').value='';
  };
  refresh(false); setInterval(()=>refresh(true),4000);
})();
""",
    'admin.js': r"""(() => {
  const tg=window.Telegram?.WebApp; if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#10152b');tg.setBackgroundColor('#0b1020')}catch(_){}}
  const initData=tg?.initData||''; const $=id=>document.getElementById(id);
  function toast(s){const el=$('toast');el.textContent=s;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),3000)}
  async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({error:'Server response error'}));if(!r.ok)throw new Error(d.error||'áŒ¥á‹«á‰„á‹ áŠ áˆá‰°áˆ³áŠ«áˆ');return d}
  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  function render(d){$('users').textContent=d.users_count;$('credits').textContent=d.total_credits;$('pending').textContent=d.pending_count;const root=$('requests');if(!d.requests.length){root.innerHTML='<div class="empty">áŒˆáŠ“ á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ á‹¨áˆˆáˆá¢</div>';return}root.innerHTML=d.requests.map(r=>`<article class="request"><div class="request-top"><strong>#${r.id} Â· ${esc(r.method)} Â· ${Number(r.amount).toLocaleString()} á‰¥áˆ­</strong><span class="status">${r.status==='pending'?'â³ á‰ áˆ˜áŒ á‰£á‰ á‰… áˆ‹á‹­':r.status==='approved'?'âœ… á‰°á‰€á‰£':'âŒ á‰°áŠ¨áˆáŠ­áˆáˆ'}</span></div><p>á‰°áŒ«á‹‹á‰½ ID: <strong>${r.user_id}</strong></p><p>Reference: <strong>${esc(r.reference)}</strong></p><small>${esc(r.created_at)}</small>${r.status==='pending'?`<div class="request-actions"><button class="approve" data-id="${r.id}" data-decision="approve">âœ“ áŠ áˆ¨áŒ‹áŒáŒ¥ áŠ¥áŠ“ áŠáŒ¥á‰¥ áŒ¨áˆáˆ­</button><button class="reject" data-id="${r.id}" data-decision="reject">âœ• áŠ¨áˆáŠ­áˆ</button></div>`:''}</article>`).join('');root.querySelectorAll('button[data-id]').forEach(b=>b.onclick=()=>review(Number(b.dataset.id),b.dataset.decision,b))}
  async function load(){ $('requests').innerHTML='<div class="empty">á‰ áˆ˜áŒ«áŠ• áˆ‹á‹­â€¦</div>';try{render(await api('/api/admin/overview'))}catch(e){$('requests').innerHTML=`<div class="empty">${esc(e.message)}<br><br>á‰ Telegram á‰¦á‰µ á‹áˆµáŒ¥ /admin á‰ áˆ›áˆˆá‰µ á“áŠáˆ‰áŠ• á‹­áŠ­áˆá‰±á¢</div>`}}
  async function review(id,decision,button){const message=decision==='approve'?'áŠ­áá‹«á‹áŠ• á‰ á‰€áŒ¥á‰³ áŠ áˆ¨áŒ‹áŒáŒ á‹‹áˆ? áŠ¨á‰°á‰€á‰ áˆ‰ áŠáŒ¥á‰¦á‰¹ á‹ˆá‹²á‹«á‹áŠ‘ á‹­áŒ¨áˆ˜áˆ«áˆ‰á¢':'á‹­áˆ…áŠ• á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ áˆ˜áŠ¨áˆáŠ¨áˆ á‹­áˆáˆáŒ‹áˆ‰?';if(!confirm(message))return;button.disabled=true;try{const d=await api(`/api/admin/deposit/${id}/review`,'POST',{decision});toast(d.message||'á‰°áˆ³áŠ­á‰·áˆ');await load()}catch(e){toast(e.message);button.disabled=false}}
  $('refresh').onclick=load;load();setInterval(load,15000);
})();
""",
    'style.css': r""":root{color-scheme:dark;--bg:#0b1020;--panel:#151d34;--panel2:#1b2642;--line:#2a3656;--text:#f4f6ff;--muted:#9aa8c8;--gold:#ffc857;--teal:#37d6b2;--purple:#8174ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#1b2b4b 0,#0b1020 55%);font-family:system-ui,-apple-system,"Noto Sans Ethiopic",sans-serif;color:var(--text);min-height:100vh}.app-shell{max-width:520px;margin:auto;padding:18px 14px 28px}.topbar{display:flex;align-items:center;gap:12px;margin:4px 2px 18px}.brand-mark{display:grid;place-items:center;width:48px;height:48px;border-radius:16px;background:linear-gradient(135deg,#ffc857,#ff8b5e);font-size:27px;box-shadow:0 7px 24px #ffb44435}.eyebrow{font-size:10px;letter-spacing:2px;color:var(--teal);font-weight:800}.topbar h1{font-size:17px;letter-spacing:.6px;margin:2px 0 0}.topbar h1 span{color:var(--gold)}.live-pill{margin-left:auto;font-size:10px;color:#ff8585;background:#ff646414;border:1px solid #ff64644a;border-radius:99px;padding:6px 9px}.live-pill i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#ff6c7c;margin-right:5px}.wallet-panel,.draw-panel,.game-panel,.called-panel{background:linear-gradient(145deg,#192440,#121a30);border:1px solid var(--line);border-radius:20px;padding:17px;margin-bottom:12px;box-shadow:0 12px 32px #0002}.wallet-panel{display:flex;align-items:center;justify-content:space-between}.muted{font-size:12px;color:var(--muted)}.balance{font-size:29px;font-weight:850;margin-top:4px;letter-spacing:.4px}.balance small{font-size:10px;color:var(--gold);letter-spacing:1px}.round-box{border-left:1px solid var(--line);padding-left:22px;min-width:70px}.round-box strong{display:block;font-size:21px;color:var(--gold);margin-top:3px}.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}.stat{padding:13px 14px;background:#151d34;border:1px solid var(--line);border-radius:15px}.stat span{font-size:11px;color:var(--muted);display:block}.stat strong{display:block;font-size:18px;margin-top:5px}.draw-panel{display:flex;align-items:center;gap:18px}.last-number{display:grid;place-items:center;width:72px;height:72px;border-radius:22px;background:linear-gradient(145deg,#ffda77,#ff9f43);color:#20233a;font-size:30px;font-weight:900;margin-top:8px;box-shadow:0 7px 22px #ffb23e24}.draw-meta{flex:1;min-width:0}.draw-meta strong{display:block;font-size:15px;margin:4px 0 8px}.number-strip{display:flex;gap:5px;flex-wrap:wrap}.number-chip{font-size:10px;padding:4px 6px;border-radius:7px;background:#263452;color:#dce4ff}.section-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.section-heading h2{margin:4px 0 0;font-size:20px}.card-count{font-size:12px;color:var(--gold);background:#ffc85716;border:1px solid #ffc85744;border-radius:9px;padding:7px 9px}.card-tabs{display:flex;gap:7px;overflow-x:auto;margin-bottom:12px}.card-tab{border:1px solid var(--line);background:#10182c;color:var(--muted);border-radius:9px;padding:8px 13px;font-size:12px;white-space:nowrap}.card-tab.active{background:#ffc8571c;border-color:var(--gold);color:var(--gold)}.bingo-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}.cell{aspect-ratio:1;display:grid;place-items:center;border:1px solid #2d3b5d;border-radius:10px;background:#202c49;color:#e8edff;font-weight:750;font-size:clamp(13px,4vw,18px);padding:0;min-width:0}.cell.head{aspect-ratio:auto;height:34px;background:#2a3556;color:var(--gold);font-size:13px;border:0}.cell.called{border-color:#37d6b2;background:#163c40;color:#69f2d1}.cell.marked{background:linear-gradient(135deg,#8174ff,#6256d9);border-color:#a69dff;color:white;box-shadow:0 0 0 1px #a69dff44}.cell.free{font-size:11px;background:#45371e;color:var(--gold);border-color:#6c552a}.legend{display:flex;gap:15px;color:var(--muted);font-size:11px;margin:12px 2px 16px}.dot{display:inline-block;width:8px;height:8px;border-radius:3px;margin-right:5px;background:#163c40;border:1px solid var(--teal)}.dot.marked{background:#8174ff;border-color:#a69dff}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions button{border:0;border-radius:12px;min-height:46px;font-weight:800;font-size:12px;padding:10px 6px}.primary{background:linear-gradient(135deg,#ffd36b,#ffa84b);color:#24243a}.secondary{background:#283554;color:#f4f6ff;border:1px solid #3a496d!important}.hint{font-size:11px;line-height:1.5;color:var(--muted);text-align:center;margin:12px 4px 0}.called-panel h3{font-size:14px;margin:0 0 10px}.called-list{display:flex;flex-wrap:wrap;gap:5px}.called-list span{padding:5px 7px;border-radius:7px;background:#222e4b;border:1px solid #334364;font-size:11px}footer{text-align:center;color:#7180a1;font-size:10px;padding:8px}.loading{grid-column:1/-1;text-align:center;padding:30px 0;color:var(--muted)}#toast{position:fixed;bottom:20px;left:50%;transform:translate(-50%,20px);background:#f2f4ff;color:#172039;border-radius:12px;padding:11px 16px;font-size:13px;font-weight:700;box-shadow:0 8px 28px #0005;opacity:0;pointer-events:none;transition:.2s;max-width:90%;text-align:center;z-index:9}#toast.show{opacity:1;transform:translate(-50%,0)}button:disabled{opacity:.45}
/* Deposit form */
.deposit-panel .form-label{display:block;margin:12px 0 6px;color:#aab5d3;font-size:13px}
.deposit-panel input,.deposit-panel select{display:block;width:100%;box-sizing:border-box;background:#0e152b;border:1px solid #354263;border-radius:10px;padding:13px;color:#f5f7ff;font:inherit;outline:none}
.deposit-panel input:focus,.deposit-panel select:focus{border-color:#7b8cff}
.payment-info{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:12px;margin:8px 0;background:#111a31;border:1px solid #2c395b;border-radius:10px;flex-wrap:wrap}
.payment-info strong{color:#dce3ff}.payment-info span{font-size:13px;color:#b9c4e3}
.full-width{width:100%;margin-top:14px}
""",
}


# In-memory round state. User credits are stored in SQLite.
state_lock = threading.RLock()
game_state = {
    "round": 1,
    "prize": 0,
    "is_active": False,
    "called_numbers": [],
    "current_number": None,
    "players": set(),
    "cards": {},       # user_id -> list of 5x5 card matrices
    "marked": {},      # user_id -> list of sets
    "winners": set(),  # one claim per user per round
}

active_cards = {}
live_message_id = None
private_board_messages = {}
private_update_lock = None


def db_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits INTEGER NOT NULL DEFAULT 100
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            method TEXT NOT NULL CHECK(method IN ('Telebirr', 'CBE Birr')),
            amount INTEGER NOT NULL CHECK(amount > 0),
            reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by INTEGER
        )"""
    )
    return conn


def ensure_user(user_id):
    with closing(db_connection()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, ?)",
            (user_id, STARTING_CREDITS),
        )
        conn.commit()


def get_credits(user_id):
    ensure_user(user_id)
    with closing(db_connection()) as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0


def change_credits(user_id, delta):
    ensure_user(user_id)
    with closing(db_connection()) as conn:
        conn.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (int(delta), user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0


def spend_credits(user_id, amount):
    ensure_user(user_id)
    with closing(db_connection()) as conn:
        cur = conn.execute(
            "UPDATE users SET credits = credits - ? "
            "WHERE user_id = ? AND credits >= ?",
            (int(amount), user_id, int(amount)),
        )
        conn.commit()
        return cur.rowcount == 1


def generate_bingo_card():
    columns = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]
    card = [[columns[c][r] for c in range(5)] for r in range(5)]
    card[2][2] = "FREE"
    return card


def check_win(card, marked):
    size = 5

    for r in range(size):
        if all(
            card[r][c] == "FREE" or card[r][c] in marked
            for c in range(size)
        ):
            return "áŠ áŒá‹µáˆ"

    for c in range(size):
        if all(
            card[r][c] == "FREE" or card[r][c] in marked
            for r in range(size)
        ):
            return "á‰áˆ"

    if all(
        card[i][i] == "FREE" or card[i][i] in marked
        for i in range(size)
    ):
        return "áˆ°á‹«á"

    if all(
        card[i][size - 1 - i] == "FREE"
        or card[i][size - 1 - i] in marked
        for i in range(size)
    ):
        return "áˆ°á‹«á"

    corners = [(0, 0), (0, 4), (4, 0), (4, 4)]
    if all(
        card[r][c] == "FREE" or card[r][c] in marked
        for r, c in corners
    ):
        return "áŠ áˆ«á‰± áˆ›á‹•á‹˜áŠ–á‰½"

    numbers = [
        card[r][c]
        for r in range(5)
        for c in range(5)
        if card[r][c] != "FREE"
    ]
    if all(number in marked for number in numbers):
        return "áˆ™áˆ‰ áŠ«áˆ­á‹µ"

    return None


def main_keyboard():
    if PUBLIC_URL:
        play_button = InlineKeyboardButton(
            "ðŸŽ« á‰¢áŠ•áŒŽ áŒ¨á‹‹á‰³ áŠ­áˆá‰µ",
            web_app=WebAppInfo(url=f"{PUBLIC_URL}/miniapp"),
        )
    else:
        play_button = InlineKeyboardButton("ðŸŽ« á‰¢áŠ•áŒŽ áŒ¨á‹‹á‰³", callback_data="play")
    return InlineKeyboardMarkup([
        [play_button],
        [InlineKeyboardButton("ðŸ’³ á‹¨áŠáŒ¥á‰¥ á‰€áˆª áˆ‚áˆ³á‰¥", callback_data="balance")],
        [InlineKeyboardButton("â„¹ï¸ á‹¨áŒ¨á‹‹á‰³ áˆ˜áˆ˜áˆªá‹«", callback_data="rules")],
    ])


def main_text(user_id):
    return (
        "ðŸŽ¯ *BESH BESH BINGO*\n\n"
        f"ðŸ’³ á‹¨áŠáŒ¥á‰¥ á‰€áˆª áˆ‚áˆ³á‰¥: *{get_credits(user_id)}*\n"
        f"ðŸ”„ á‹™áˆ­: *#{game_state['round']}*\n"
        f"ðŸ† á‹¨á‹™áˆ© áˆ½áˆáˆ›á‰µ: *{game_state['prize']} áŠáŒ¥á‰¥*\n\n"
        "áŠ¨á‰³á‰½ á‹«áˆ‰á‰µáŠ• áŠ áˆ›áˆ«áŒ®á‰½ á‹­áˆáˆ¨áŒ¡á¢\n"
        "_á‹­áˆ… á‰¦á‰µ áˆáŠ“á‰£á‹Š áŠáŒ¥á‰¦á‰½áŠ• á‰¥á‰» á‹­áŒ á‰€áˆ›áˆá¢_"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    await update.effective_message.reply_text(
        main_text(user_id),
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text("â›” á‹­áˆ… á‰µáŠ¥á‹›á‹ áˆˆáŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰» áŠá‹á¢")
        return
    if not PUBLIC_URL:
        await update.effective_message.reply_text("RENDER_EXTERNAL_URL áŠ áˆá‰°á‹˜áŒ‹áŒ€áˆá¢")
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        "ðŸ›  á‹¨áŠ áˆµá‰°á‹³á‹³áˆª á“áŠáˆ áŠ­áˆá‰µ",
        web_app=WebAppInfo(url=f"{PUBLIC_URL}/admin"),
    )]])
    await update.effective_message.reply_text(
        "ðŸ›¡ Besh Besh Bingo á‹¨áŠ áˆµá‰°á‹³á‹³áˆª á“áŠáˆá¢",
        reply_markup=keyboard,
    )


async def show_menu(query, user_id):
    await query.edit_message_text(
        main_text(user_id),
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


def contextless_active_card(user_id):
    return active_cards.get(user_id, 0)


def build_board_payload(user_id):
    with state_lock:
        cards = game_state["cards"].get(user_id, [])
        marks = game_state["marked"].get(user_id, [])
        if not cards:
            return (
                "áŠ«áˆ­á‹µ á‹¨áˆˆá‹Žá‰µáˆá¢ á‰ áˆ˜áŒ€áˆ˜áˆªá‹« áŒ¨á‹‹á‰³á‹áŠ• á‹­áŒ€áˆáˆ©á¢",
                main_keyboard(),
                False,
            )

        idx = min(max(0, contextless_active_card(user_id)), len(cards) - 1)
        card = cards[idx]
        marked = marks[idx]
        called_count = len(game_state["called_numbers"])
        current = game_state["current_number"]
        player_count = len(game_state["players"])
        round_num = game_state["round"]
        prize = game_state["prize"]
        called = list(game_state["called_numbers"])

    keyboard = [[
        InlineKeyboardButton(
            ("ðŸŸ¢ " if i == idx else "âšª ") + f"áŠ«áˆ­á‹µ {i + 1}",
            callback_data=f"switch:{i}",
        )
        for i in range(len(cards))
    ]]
    keyboard.append([
        InlineKeyboardButton(letter, callback_data="noop")
        for letter in ["B", "I", "N", "G", "O"]
    ])

    for r, row in enumerate(card):
        row_buttons = []
        for c, value in enumerate(row):
            if value == "FREE":
                label = "â­ FREE"
            else:
                label = ("âœ…" if value in marked else "â¬œ") + str(value)
            row_buttons.append(
                InlineKeyboardButton(
                    label, callback_data=f"cell:{idx}:{value}"
                )
            )
        keyboard.append(row_buttons)

    extras = []
    if len(cards) < MAX_CARDS_PER_PLAYER:
        extras.append(
            InlineKeyboardButton(
                f"âž• áŠ«áˆ­á‹µ (+{CARD_COST} áŠáŒ¥á‰¥)",
                callback_data="add",
            )
        )
    extras.append(InlineKeyboardButton("ðŸ”„ áŠ á‹µáˆµ", callback_data="refresh"))
    keyboard.append(extras)
    keyboard.append([
        InlineKeyboardButton("ðŸ“¢ BINGO!", callback_data="claim")
    ])
    keyboard.append([
        InlineKeyboardButton("ðŸ  á‹‹áŠ“ áˆ›á‹áŒ«", callback_data="menu")
    ])

    text = (
        "ðŸŽ¯ *BESH BESH BINGO*\n\n"
        f"ðŸ”„ á‹™áˆ­: *#{round_num}* | ðŸŽ« áŠ«áˆ­á‹µ: *{idx + 1}/{len(cards)}*\n"
        f"ðŸ† á‹¨á‹™áˆ© áˆ½áˆáˆ›á‰µ: *{prize} áŠáŒ¥á‰¥*\n"
        f"ðŸ”Š á‹¨áŠ áˆáŠ‘ á‰áŒ¥áˆ­: *{current if current is not None else 'áŒˆáŠ“ áŠ áˆá‰°áŒ áˆ«áˆ'}*\n"
        f"âšª á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½: *{called_count}/75*\n"
        f"ðŸ‘¥ á‰°áŒ«á‹‹á‰¾á‰½: *{player_count}/{MAX_PLAYERS}*\n\n"
        "á‰áŒ¥áˆ­ áˆ²áŒ áˆ« á‰ áŠ«áˆ­á‹µá‹Ž áˆ‹á‹­ á‹«áˆˆá‹áŠ• á‰áŒ¥áˆ­ á‹­áŒ«áŠ‘á¢\n"
        f"ðŸ“‹ á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½ (á‹¨áˆ˜áŒ¨áˆ¨áˆ» 15): "
        f"{', '.join(map(str, called[-15:])) or 'â€”'}"
    )
    return text, InlineKeyboardMarkup(keyboard), True


async def render_board(query, user_id):
    text, markup, has_card = build_board_payload(user_id)
    await query.edit_message_text(
        text, reply_markup=markup, parse_mode="Markdown"
    )
    if has_card and query.message:
        private_board_messages[user_id] = (
            query.message.chat_id,
            query.message.message_id,
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    ensure_user(user_id)
    data = query.data or ""

    if data == "noop":
        await query.answer()
        return

    if data in ("menu", "balance", "rules"):
        await query.answer()
        if data == "balance":
            await query.edit_message_text(
                f"ðŸ’³ *á‹¨áŠáŒ¥á‰¥ á‰€áˆª áˆ‚áˆ³á‰¥á‹Ž: {get_credits(user_id)}*\n\n"
                "áŠáŒ¥á‰¦á‰¹ áˆˆá‹šáˆ… áŒ¨á‹‹á‰³ á‰¥á‰» á‹¨áˆšá‹áˆ‰ áŠ“á‰¸á‹á¢",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "ðŸ”™ á‹‹áŠ“ áˆ›á‹áŒ«", callback_data="menu"
                    )
                ]]),
                parse_mode="Markdown",
            )
        elif data == "rules":
            await query.edit_message_text(
                "ðŸ“œ *á‹¨áŒ¨á‹‹á‰³ áˆ˜áˆ˜áˆªá‹«*\n\n"
                f"â€¢ áŠ«áˆ­á‹µ á‹‹áŒ‹: {CARD_COST} áŠáŒ¥á‰¥\n"
                "â€¢ á‰áŒ¥áˆ®á‰½ á‰ á‹¨5 áˆ°áŠ¨áŠ•á‹± á‹­áŒ áˆ«áˆ‰á¢\n"
                "â€¢ á‹¨á‰°áŒ áˆ« á‰áŒ¥áˆ­ á‰ áŠ«áˆ­á‹µá‹Ž áˆ‹á‹­ á‹­áŒ«áŠ‘á¢\n"
                "â€¢ áŠ áŒá‹µáˆá£ á‰áˆá£ áˆ°á‹«áá£ áŠ áˆ«á‰µ áˆ›á‹•á‹˜áŠ• á‹ˆá‹­áˆ "
                "áˆ™áˆ‰ áŠ«áˆ­á‹µ áˆ²áˆžáˆ‹ BINGO á‹­áŒ«áŠ‘á¢\n"
                "â€¢ áŠ¥áˆµáŠ¨ 4 áŠ«áˆ­á‹¶á‰½ áˆ˜áŒ«á‹ˆá‰µ á‹­á‰»áˆ‹áˆá¢\n"
                "â€¢ áŠ¥á‹áŠá‰°áŠ› áŒˆáŠ•á‹˜á‰¥ á‹ˆá‹­áˆ áˆ½áˆáˆ›á‰µ áŠ á‹«áŠ«á‰µá‰µáˆá¢",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "ðŸ”™ á‹‹áŠ“ áˆ›á‹áŒ«", callback_data="menu"
                    )
                ]]),
                parse_mode="Markdown",
            )
        else:
            await show_menu(query, user_id)
        return

    if data == "play":
        with state_lock:
            if (
                user_id not in game_state["players"]
                and len(game_state["players"]) >= MAX_PLAYERS
            ):
                await query.answer(
                    "á‹¨á‰¢áŠ•áŒŽá‹ á‰¦á‰³ áˆžáˆá‰·áˆá¢", show_alert=True
                )
                return

            has_card = bool(game_state["cards"].get(user_id))
            if not has_card:
                if not spend_credits(user_id, CARD_COST):
                    await query.answer(
                        "á‰ á‰‚ áŠáŒ¥á‰¥ á‹¨áˆˆá‹Žá‰µáˆá¢", show_alert=True
                    )
                    return
                game_state["cards"][user_id] = [generate_bingo_card()]
                game_state["marked"][user_id] = [set()]
                game_state["players"].add(user_id)
                active_cards[user_id] = 0
                game_state["prize"] += CARD_COST
                game_state["is_active"] = True

        await query.answer()
        await publish_live_board()
        await render_board(query, user_id)
        return

    if data == "add":
        with state_lock:
            cards = game_state["cards"].get(user_id, [])
            if not cards:
                await query.answer(
                    "áˆ˜áŒ€áˆ˜áˆªá‹« áŒ¨á‹‹á‰³á‹áŠ• á‹­áŒ€áˆáˆ©á¢", show_alert=True
                )
                return
            if len(cards) >= MAX_CARDS_PER_PLAYER:
                await query.answer(
                    "áŠ¥áˆµáŠ¨ 4 áŠ«áˆ­á‹¶á‰½ á‰¥á‰» á‹­áˆá‰€á‹³áˆ‰á¢", show_alert=True
                )
                return
            if not spend_credits(user_id, CARD_COST):
                await query.answer(
                    "á‰ á‰‚ áŠáŒ¥á‰¥ á‹¨áˆˆá‹Žá‰µáˆá¢", show_alert=True
                )
                return
            cards.append(generate_bingo_card())
            game_state["marked"][user_id].append(set())
            active_cards[user_id] = len(cards) - 1
            game_state["prize"] += CARD_COST

        await query.answer("áŠ«áˆ­á‹µ á‰°áŒ¨áˆáˆ¯áˆá¢")
        await publish_live_board()
        await render_board(query, user_id)
        return

    if data.startswith("switch:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("á‹¨áŠ«áˆ­á‹µ áˆáˆ­áŒ«á‹ áˆµáˆ…á‰°á‰µ áŠá‹á¢", show_alert=True)
            return

        with state_lock:
            cards = game_state["cards"].get(user_id, [])
            if not 0 <= idx < len(cards):
                await query.answer("á‹­áˆ… áŠ«áˆ­á‹µ áŠ á‹­áŒˆáŠáˆá¢", show_alert=True)
                return
            active_cards[user_id] = idx

        await query.answer()
        await render_board(query, user_id)
        return

    if data == "refresh":
        await query.answer()
        await render_board(query, user_id)
        return

    if data.startswith("cell:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer()
            return

        try:
            idx = int(parts[1])
        except ValueError:
            await query.answer()
            return

        if parts[2] == "FREE":
            await query.answer("FREE á‰¦á‰³á‹ á‰ áˆ«áˆ± á‹­á‰†áŒ áˆ«áˆá¢")
            return

        try:
            number = int(parts[2])
        except ValueError:
            await query.answer()
            return

        with state_lock:
            cards = game_state["cards"].get(user_id, [])
            marks = game_state["marked"].get(user_id, [])
            if idx < 0 or idx >= len(cards) or idx >= len(marks):
                await query.answer("á‹­áˆ… áŠ«áˆ­á‹µ áŠ á‹­áŒˆáŠáˆá¢", show_alert=True)
                return
            if number not in game_state["called_numbers"]:
                await query.answer(
                    "á‹­áˆ… á‰áŒ¥áˆ­ áŒˆáŠ“ áŠ áˆá‰°áŒ áˆ«áˆá¢", show_alert=True
                )
                return
            if number not in cards[idx] and not any(
                number in row for row in cards[idx]
            ):
                await query.answer("á‹­áˆ… á‰áŒ¥áˆ­ á‰ áŠ«áˆ­á‹± áˆ‹á‹­ á‹¨áˆˆáˆá¢", show_alert=True)
                return
            marks[idx].add(number)
            active_cards[user_id] = idx

        await query.answer(f"á‰áŒ¥áˆ­ {number} á‰°áˆ˜áˆ­áŒ§áˆá¢")
        await render_board(query, user_id)
        return

    if data == "claim":
        with state_lock:
            cards = game_state["cards"].get(user_id, [])
            marks = game_state["marked"].get(user_id, [])
            idx = active_cards.get(user_id, 0)

            if not cards or idx >= len(cards) or idx >= len(marks):
                await query.answer(
                    "áˆ˜áŒ€áˆ˜áˆªá‹« áŠ«áˆ­á‹µ á‹­á‹áˆ°á‹±á¢", show_alert=True
                )
                return

            if user_id in game_state["winners"]:
                await query.answer(
                    "á‰ á‹šáˆ… á‹™áˆ­ áŠ áˆµá‰€á‹µáˆ˜á‹ áŠ áˆ¸áŠ•áˆá‹‹áˆá¢",
                    show_alert=True,
                )
                return

            win_type = check_win(cards[idx], marks[idx])
            if not win_type:
                await query.answer(
                    "á‹¨BINGO áˆ˜áˆµáˆáˆ­á‰± áŒˆáŠ“ áŠ áˆá‰°áˆŸáˆ‹áˆá¢",
                    show_alert=True,
                )
                return

            reward = max(0, game_state["prize"])
            change_credits(user_id, reward)

            game_state["round"] += 1
            game_state["prize"] = 0
            game_state["is_active"] = False
            game_state["called_numbers"] = []
            game_state["current_number"] = None
            game_state["players"] = set()
            game_state["cards"] = {}
            game_state["marked"] = {}
            game_state["winners"] = set()
            active_cards.clear()
            private_board_messages.pop(user_id, None)

        await query.answer("BINGO! áŠ áˆ¸áŠ•áˆá‹‹áˆ!")
        await publish_live_board()
        await query.edit_message_text(
            "ðŸŽ‰ *BINGO! áŠ áˆ¸áŠ•áˆá‹‹áˆ!*\n\n"
            f"ðŸ… á‹¨áŠ áˆ¸áŠ“áŠáŠá‰µ áŠ á‹­áŠá‰µ: *{win_type}*\n"
            f"ðŸŽ á‹¨á‰°áŒ¨áˆ˜áˆ¨á‹ áˆ½áˆáˆ›á‰µ: *{reward} áŠáŒ¥á‰¥*\n"
            f"ðŸ’³ áŠ á‹²áˆµ á‰€áˆª áŠáŒ¥á‰¥: *{get_credits(user_id)}*",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("ðŸ  á‹‹áŠ“ áˆ›á‹áŒ«", callback_data="menu")
            ]]),
            parse_mode="Markdown",
        )
        await publish_private_boards()
        return

    await query.answer("á‹­áˆ… á‰µáŠ¥á‹›á‹ áŠ á‹­á‰³á‹ˆá‰…áˆá¢")


async def publish_live_board():
    global live_message_id

    if not LIVE_CHAT_ID:
        return

    with state_lock:
        round_num = game_state["round"]
        current = game_state["current_number"]
        called = list(game_state["called_numbers"])
        players = len(game_state["players"])
        prize = game_state["prize"]
        active = game_state["is_active"]

    text = (
        "ðŸŽ¯ *BESH BESH BINGO â€” LIVE*\n\n"
        f"ðŸ”„ á‹™áˆ­: *#{round_num}*\n"
        f"ðŸ”Š á‹¨áŠ áˆáŠ‘ á‰áŒ¥áˆ­: "
        f"*{current if current is not None else 'áŒˆáŠ“ áŠ áˆá‰°áŒ áˆ«áˆ'}*\n"
        f"ðŸ“‹ á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½ ({len(called)}/75): "
        f"{', '.join(map(str, called)) or 'â€”'}\n"
        f"ðŸ‘¥ á‰°áŒ«á‹‹á‰¾á‰½: *{players}/{MAX_PLAYERS}*\n"
        f"ðŸ† á‹¨á‹™áˆ© áˆ½áˆáˆ›á‰µ: *{prize} áŠáŒ¥á‰¥*\n"
        f"ðŸ“¡ áˆáŠ”á‰³: *{'LIVE' if active else 'á‰°áŒ á‰£á‰£á‰‚'}*\n\n"
        "á‰ áŒáˆ á‰¦á‰µ á‹áˆµáŒ¥ áŠ«áˆ­á‹µá‹ŽáŠ• á‹­áŠ¨á‰³á‰°áˆ‰á¢"
    )

    try:
        chat_id = int(LIVE_CHAT_ID)
        if live_message_id is None:
            message = await application.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown"
            )
            live_message_id = message.message_id
        else:
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=live_message_id,
                    text=text,
                    parse_mode="Markdown",
                )
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                logger.warning(
                    "Could not edit live board; creating a new one: %s", exc
                )
                message = await application.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                live_message_id = message.message_id
    except Exception:
        logger.exception(
            "Failed to publish live board. Check LIVE_CHAT_ID and bot permissions."
        )


async def publish_private_boards():
    global private_update_lock

    if private_update_lock is None:
        private_update_lock = asyncio.Lock()

    async with private_update_lock:
        with state_lock:
            recipients = list(private_board_messages.items())

        for user_id, (chat_id, message_id) in recipients:
            text, markup, has_card = build_board_payload(user_id)

            if not has_card:
                try:
                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=(
                            "ðŸŽ¯ *BESH BESH BINGO*\n\n"
                            "á‹™áˆ© á‰°áŒ áŠ“á‰‹áˆá¢ áŠ¥áŠ•á‹°áŒˆáŠ“ áˆˆáˆ˜áŒ«á‹ˆá‰µ "
                            "á‹‹áŠ“ áˆ›á‹áŒ«áŠ• á‹­áŒ«áŠ‘á¢"
                        ),
                        reply_markup=main_keyboard(),
                        parse_mode="Markdown",
                    )
                except Exception as exc:
                    logger.debug(
                        "Could not clear ended private board for user %s: %s",
                        user_id, exc,
                    )
                private_board_messages.pop(user_id, None)
                await asyncio.sleep(0.05)
                continue

            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode="Markdown",
                )
            except Exception as exc:
                message = str(exc).lower()
                if "message is not modified" in message:
                    pass
                elif (
                    "message to edit not found" in message
                    or "chat not found" in message
                ):
                    private_board_messages.pop(user_id, None)
                    logger.info(
                        "Removed unavailable private board for user %s", user_id
                    )
                else:
                    logger.warning(
                        "Could not refresh private board for user %s: %s",
                        user_id, exc,
                    )
            await asyncio.sleep(0.05)


async def number_caller():
    while True:
        await asyncio.sleep(CALL_INTERVAL_SECONDS)

        with state_lock:
            if not game_state["players"] or not game_state["is_active"]:
                continue

            available = [
                n for n in range(1, 76)
                if n not in game_state["called_numbers"]
            ]

            if available:
                number = random.choice(available)
                game_state["called_numbers"].append(number)
                game_state["current_number"] = number
                logger.info(
                    "Round %s called number %s",
                    game_state["round"], number,
                )
            else:
                # No numbers remain: close the round and reset the table.
                game_state["round"] += 1
                game_state["called_numbers"] = []
                game_state["current_number"] = None
                game_state["winners"] = set()
                game_state["cards"] = {}
                game_state["marked"] = {}
                game_state["players"] = set()
                active_cards.clear()
                game_state["prize"] = 0
                game_state["is_active"] = False

        await publish_live_board()
        await publish_private_boards()


async def initialize_bot():
    # Register handlers before starting the Telegram application.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    await application.initialize()
    await application.start()

    if PUBLIC_URL and WEBHOOK_SECRET_PATH:
        webhook_url = (
            f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET_PATH}"
        )
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Telegram webhook configured.")
    else:
        logger.warning(
            "Webhook not configured. Set RENDER_EXTERNAL_URL "
            "and WEBHOOK_SECRET_PATH."
        )

    loop.create_task(number_caller())


def verify_telegram_init_data(init_data):
    """Verify Telegram Mini App initData and return the authenticated user ID."""
    if not init_data or not TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(pairs.items())
        )
        secret_key = hmac.new(
            b"WebAppData", TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
            return None
        user_data = json.loads(pairs.get("user", "{}"))
        user_id = int(user_data["id"])
        return user_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def miniapp_user_id():
    user_id = verify_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id is None:
        return None
    ensure_user(user_id)
    return user_id


@app.get("/miniapp")
def miniapp_page():
    return Response(STATIC_ASSETS["index.html"], mimetype="text/html; charset=utf-8")


@app.get("/static/<path:filename>")
def miniapp_static(filename):
    content = STATIC_ASSETS.get(filename)
    if content is None:
        return {"error": "Not found"}, 404
    mime = "text/css; charset=utf-8" if filename.endswith(".css") else "application/javascript; charset=utf-8"
    return Response(content, mimetype=mime)


@app.get("/api/miniapp/game")
def miniapp_game():
    user_id = miniapp_user_id()
    if user_id is None:
        return {"error": "Telegram áˆ˜áˆˆá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ¥ áŠ áˆá‰°áˆ³áŠ«áˆá¢ áŠ¥á‰£áŠ­á‹Ž áŒ¨á‹‹á‰³á‹áŠ• áŠ¨Telegram á‰¦á‰µ á‹­áŠ­áˆá‰±á¢"}, 401
    with state_lock:
        cards = game_state["cards"].get(user_id, [])
        marked = game_state["marked"].get(user_id, [])
        payload = {
            "credits": get_credits(user_id),
            "round": game_state["round"],
            "prize": game_state["prize"],
            "is_active": game_state["is_active"],
            "current_number": game_state["current_number"],
            "called_numbers": list(game_state["called_numbers"]),
            "player_count": len(game_state["players"]),
            "cards": cards,
            "marked": [sorted(list(values)) for values in marked],
            "card_cost": CARD_COST,
            "max_cards": MAX_CARDS_PER_PLAYER,
        }
    return payload, 200


@app.post("/api/miniapp/buy-card")
def miniapp_buy_card():
    user_id = miniapp_user_id()
    if user_id is None:
        return {"error": "Telegram áˆ˜áˆˆá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ¥ áŠ áˆá‰°áˆ³áŠ«áˆá¢"}, 401
    with state_lock:
        cards = game_state["cards"].get(user_id, [])
        if len(cards) >= MAX_CARDS_PER_PLAYER:
            return {"error": "áŠ¥áˆµáŠ¨ 4 áŠ«áˆ­á‹¶á‰½ á‰¥á‰» á‹­áˆá‰€á‹³áˆ‰á¢"}, 400
        if user_id not in game_state["players"] and len(game_state["players"]) >= MAX_PLAYERS:
            return {"error": "á‹¨áŒ¨á‹‹á‰³á‹ á‰¦á‰³ áˆžáˆá‰·áˆá¢"}, 400
        if not spend_credits(user_id, CARD_COST):
            return {"error": "á‰ á‰‚ áŠáŒ¥á‰¥ á‹¨áˆˆá‹Žá‰µáˆá¢"}, 400
        game_state["cards"].setdefault(user_id, []).append(generate_bingo_card())
        game_state["marked"].setdefault(user_id, []).append(set())
        active_cards[user_id] = len(game_state["cards"][user_id]) - 1
        game_state["players"].add(user_id)
        game_state["prize"] += CARD_COST
        game_state["is_active"] = True
    return {"ok": True, "message": "áŠ«áˆ­á‹µ á‰°áŒˆá‹á‰·áˆá¢"}, 200


@app.post("/api/miniapp/mark")
def miniapp_mark_number():
    user_id = miniapp_user_id()
    if user_id is None:
        return {"error": "Telegram áˆ˜áˆˆá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ¥ áŠ áˆá‰°áˆ³áŠ«áˆá¢"}, 401
    body = request.get_json(silent=True) or {}
    try:
        card_index = int(body.get("card_index", 0))
        number = int(body.get("number"))
    except (ValueError, TypeError):
        return {"error": "á‹¨á‰áŒ¥áˆ­ áˆáˆ­áŒ«á‹ áˆµáˆ…á‰°á‰µ áŠá‹á¢"}, 400
    with state_lock:
        cards = game_state["cards"].get(user_id, [])
        marks = game_state["marked"].get(user_id, [])
        if card_index < 0 or card_index >= len(cards):
            return {"error": "áŠ«áˆ­á‹± áŠ áˆá‰°áŒˆáŠ˜áˆá¢"}, 400
        if number not in game_state["called_numbers"]:
            return {"error": "á‹­áˆ… á‰áŒ¥áˆ­ áŒˆáŠ“ áŠ áˆá‰°áŒ áˆ«áˆá¢"}, 400
        if not any(number in row for row in cards[card_index]):
            return {"error": "á‹­áˆ… á‰áŒ¥áˆ­ á‰ áŠ«áˆ­á‹± áˆ‹á‹­ á‹¨áˆˆáˆá¢"}, 400
        marks[card_index].add(number)
        active_cards[user_id] = card_index
    return {"ok": True}, 200


@app.post("/api/miniapp/claim")
def miniapp_claim_bingo():
    user_id = miniapp_user_id()
    if user_id is None:
        return {"error": "Telegram áˆ˜áˆˆá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ¥ áŠ áˆá‰°áˆ³áŠ«áˆá¢"}, 401
    with state_lock:
        cards = game_state["cards"].get(user_id, [])
        marks = game_state["marked"].get(user_id, [])
        idx = active_cards.get(user_id, 0)
        if not cards or idx >= len(cards) or idx >= len(marks):
            return {"error": "áˆ˜áŒ€áˆ˜áˆªá‹« áŠ«áˆ­á‹µ á‹­áŒá‹™á¢"}, 400
        if user_id in game_state["winners"]:
            return {"error": "á‰ á‹šáˆ… á‹™áˆ­ áŠ áˆµá‰€á‹µáˆ˜á‹ áŠ áˆ¸áŠ•áˆá‹‹áˆá¢"}, 400
        win_type = check_win(cards[idx], marks[idx])
        if not win_type:
            return {"error": "BINGO áˆˆáˆ›áˆ¸áŠá á‹¨áˆšá‹«áˆµáˆáˆáŒˆá‹ á‰…áˆ­áŒ½ áŒˆáŠ“ áŠ áˆá‰°áˆŸáˆ‹áˆá¢"}, 400
        reward = max(0, game_state["prize"])
        change_credits(user_id, reward)
        game_state["round"] += 1
        game_state["prize"] = 0
        game_state["is_active"] = False
        game_state["called_numbers"] = []
        game_state["current_number"] = None
        game_state["players"] = set()
        game_state["cards"] = {}
        game_state["marked"] = {}
        game_state["winners"] = set()
        active_cards.clear()
    return {"ok": True, "message": f"BINGO! {win_type} â€” {reward} áŠáŒ¥á‰¥ áŠ áˆ¸áŠ•áˆá‹‹áˆ!"}, 200


def admin_user_id():
    user_id = miniapp_user_id()
    if user_id is None or user_id not in ADMIN_IDS:
        return None
    return user_id


@app.get("/admin")
def admin_page():
    return Response(STATIC_ASSETS["admin.html"], mimetype="text/html; charset=utf-8")


@app.get("/api/admin/overview")
def admin_overview():
    admin_id = admin_user_id()
    if admin_id is None:
        return {"error": "á‹¨áŠ áˆµá‰°á‹³á‹³áˆª áˆá‰ƒá‹µ á‹¨áˆˆá‹Žá‰µáˆá¢ á‰ Telegram á‹áˆµáŒ¥ /admin á‹­áŒ á‰€áˆ™á¢"}, 403
    with closing(db_connection()) as conn:
        users = conn.execute("SELECT COUNT(*), COALESCE(SUM(credits),0) FROM users").fetchone()
        pending = conn.execute("SELECT COUNT(*) FROM deposit_requests WHERE status='pending'").fetchone()[0]
        requests = conn.execute(
            "SELECT id,user_id,method,amount,reference,status,created_at FROM deposit_requests ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return {
        "users_count": int(users[0]), "total_credits": int(users[1]),
        "pending_count": int(pending),
        "requests": [dict(id=r[0], user_id=r[1], method=r[2], amount=r[3], reference=r[4], status=r[5], created_at=r[6]) for r in requests],
    }, 200


@app.post("/api/miniapp/deposit-request")
def create_deposit_request():
    user_id = miniapp_user_id()
    if user_id is None:
        return {"error": "Telegram áˆ˜áˆˆá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ¥ áŠ áˆá‰°áˆ³áŠ«áˆá¢"}, 401
    body = request.get_json(silent=True) or {}
    method = str(body.get("method", "")).strip()
    reference = str(body.get("reference", "")).strip()[:100]
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if method not in ("Telebirr", "CBE Birr"):
        return {"error": "Telebirr á‹ˆá‹­áˆ CBE Birr á‹­áˆáˆ¨áŒ¡á¢"}, 400
    if amount < 1 or amount > 1000000:
        return {"error": "á‹¨áŒˆáŠ•á‹˜á‰¥ áˆ˜áŒ áŠ‘ á‰µáŠ­áŠ­áˆ áŠ á‹­á‹°áˆˆáˆá¢"}, 400
    if len(reference) < 3:
        return {"error": "á‹¨áŒá‰¥á‹­á‰µ áˆ›áˆ¨áŒ‹áŒˆáŒ«/áˆªáˆáˆ¨áŠ•áˆµ á‹«áˆµáŒˆá‰¡á¢"}, 400
    with closing(db_connection()) as conn:
        cur = conn.execute(
            "INSERT INTO deposit_requests(user_id,method,amount,reference) VALUES(?,?,?,?)",
            (user_id, method, amount, reference),
        )
        conn.commit()
        request_id = cur.lastrowid
    return {"ok": True, "request_id": request_id, "message": "á‹¨áŠ­áá‹« áŒ¥á‹«á‰„á‹Ž á‰°áˆáŠ³áˆá¢ áŠ áˆµá‰°á‹³á‹³áˆªá‹ áŠ¥áˆµáŠªá‹«áˆ¨áŒ‹áŒáŒ¥ á‹­áŒ á‰¥á‰á¢"}, 200


@app.post("/api/admin/deposit/<int:request_id>/review")
def review_deposit(request_id):
    admin_id = admin_user_id()
    if admin_id is None:
        return {"error": "á‹¨áŠ áˆµá‰°á‹³á‹³áˆª áˆá‰ƒá‹µ á‹¨áˆˆá‹Žá‰µáˆá¢"}, 403
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return {"error": "á‹áˆ³áŠ”á‹ á‰µáŠ­áŠ­áˆ áŠ á‹­á‹°áˆˆáˆá¢"}, 400
    with closing(db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id,amount,status FROM deposit_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return {"error": "á‹¨áŠ­áá‹« áŒ¥á‹«á‰„á‹ áŠ áˆá‰°áŒˆáŠ˜áˆá¢"}, 404
        user_id, amount, status = row
        if status != "pending":
            conn.rollback()
            return {"error": "á‹­áˆ… áŒ¥á‹«á‰„ áŠ áˆµá‰€á‹µáˆž á‰°áˆ˜áˆ­áˆáˆ¯áˆá¢"}, 409
        new_status = "approved" if decision == "approve" else "rejected"
        conn.execute(
            "UPDATE deposit_requests SET status=?, reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE id=? AND status='pending'",
            (new_status, admin_id, request_id),
        )
        if decision == "approve":
            conn.execute("INSERT OR IGNORE INTO users(user_id,credits) VALUES(?,?)", (user_id, STARTING_CREDITS))
            conn.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (amount, user_id))
        conn.commit()
    return {"ok": True, "message": "áŒ¥á‹«á‰„á‹ á‰°áˆá‰µá‰·áˆá¢"}, 200


@app.get("/")
def home():
    return "Besh Besh Bingo bot is running.", 200


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Besh Besh Bingo",
    }, 200


@app.post("/webhook/<secret>")
def telegram_webhook(secret):
    if (
        not WEBHOOK_SECRET_PATH
        or not hmac.compare_digest(secret, WEBHOOK_SECRET_PATH)
    ):
        return {"error": "Unauthorized"}, 403

    if not request.is_json:
        return {"error": "Expected JSON"}, 400

    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return {"error": "Invalid update"}, 400

        update = Update.de_json(data, application.bot)
        future = asyncio.run_coroutine_threadsafe(
            application.update_queue.put(update),
            loop,
        )
        future.result(timeout=10)
        return {"ok": True}, 200

    except Exception:
        logger.exception("Failed to enqueue Telegram webhook update")
        return {"error": "Webhook processing failed"}, 500


def run_bot_loop():
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(initialize_bot())
        loop.run_forever()
    except Exception:
        logger.exception("Telegram bot failed to start")
        raise


if __name__ == "__main__":
    bot_thread = threading.Thread(
        target=run_bot_loop,
        name="telegram-bot-loop",
        daemon=True,
    )
    bot_thread.start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )
