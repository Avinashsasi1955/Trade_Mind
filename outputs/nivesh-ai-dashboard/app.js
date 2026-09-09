const trades = [
  { time:'11:42:08', symbol:'RELIANCE', name:'Reliance Industries', action:'BUY', qty:12, price:'2,948.20', value:'35,918.40', pnl:'+₹540.00', pct:'+1.53%', up:true, reason:'Volume was 2.4× the 20-day average with a clean breakout above ₹2,930. The agent sized this at 3.5% of capital because sector momentum remains constructive.' },
  { time:'10:18:34', symbol:'TATAMOTORS', name:'Tata Motors', action:'SELL', qty:25, price:'1,024.50', value:'25,612.50', pnl:'+₹862.50', pct:'+3.49%', up:true, reason:'The position reached its first target while RSI crossed 72. The agent booked the full position as auto-sector breadth began to weaken.' },
  { time:'09:36:51', symbol:'HDFCBANK', name:'HDFC Bank', action:'BUY', qty:18, price:'1,682.10', value:'30,108.60', pnl:'−₹169.20', pct:'−0.56%', up:false, reason:'A mean-reversion entry near the VWAP support with a favourable 2.1:1 reward-to-risk ratio. Stop is maintained below ₹1,654.' },
];

const hotStocks = [
  {symbol:'BEL', name:'Bharat Electronics', price:'₹314.80', change:'+3.84%', confidence:92, note:'Fresh order-book momentum with volume expansion above the 50-DMA.'},
  {symbol:'TRENT', name:'Trent Ltd', price:'₹5,462.15', change:'+2.18%', confidence:87, note:'Relative strength leader; price structure suggests continuation above ₹5,500.'},
  {symbol:'COALINDIA', name:'Coal India', price:'₹487.65', change:'+1.42%', confidence:83, note:'High-yield support and a clean breakout from a three-week base.'},
  {symbol:'ICICIBANK', name:'ICICI Bank', price:'₹1,228.40', change:'+0.96%', confidence:79, note:'Strong banking breadth and consistent institutional accumulation.'},
];

const holdings = [
  ['RELIANCE','Reliance Industries',12,'₹2,948.20','₹2,993.20','₹35,918.40','+₹540.00','+1.53%'],
  ['HDFCBANK','HDFC Bank',18,'₹1,682.10','₹1,672.70','₹30,108.60','−₹169.20','−0.56%'],
  ['INFY','Infosys',20,'₹1,487.65','₹1,526.30','₹30,526.00','+₹773.00','+2.60%'],
  ['LT','Larsen & Toubro',8,'₹3,584.50','₹3,612.10','₹28,896.80','+₹220.80','+0.77%'],
  ['SUNPHARMA','Sun Pharma',15,'₹1,496.40','₹1,518.75','₹22,781.25','+₹335.25','+1.49%'],
  ['MARUTI','Maruti Suzuki',2,'₹12,340.00','₹12,218.50','₹24,437.00','−₹243.00','−0.98%']
];

const history = [
  ...trades,
  {time:'26 Jun · 14:48',symbol:'SBIN',name:'State Bank of India',action:'SELL',qty:30,price:'842.30',value:'25,269.00',pnl:'+₹1,182.00',pct:'+4.91%',up:true},
  {time:'26 Jun · 12:21',symbol:'LT',name:'Larsen & Toubro',action:'BUY',qty:8,price:'3,584.50',value:'28,676.00',pnl:'+₹220.80',pct:'+0.77%',up:true},
  {time:'25 Jun · 10:09',symbol:'ZOMATO',name:'Eternal Ltd',action:'SELL',qty:100,price:'198.10',value:'19,810.00',pnl:'−₹470.00',pct:'−2.32%',up:false},
];

const strategies = [
  ['01','Momentum','trending-up','Follows sustained price strength confirmed by relative volume and market breadth.',['Trending markets','2–10 days'],'Works best when NIFTY breadth is above 60%. Reduce sizing when India VIX rises sharply.'],
  ['02','Mean Reversion','waves','Looks for stretched prices returning toward VWAP or a statistically significant moving average.',['Range-bound','Intraday'],'Avoid averaging into a falling stock. I require a reversal candle before entry.'],
  ['03','Breakout','move-up-right','Enters when price clears a well-tested level with decisive participation from volume.',['High volume','1–5 days'],'False breakouts are common before 09:30. I wait for the opening range to establish.'],
  ['04','RSI Divergence','git-compare-arrows','Detects disagreement between price direction and momentum to anticipate potential reversals.',['Reversal','Swing'],'Divergence is a clue, not a trigger. I pair it with structure and delivery volume.'],
  ['05','VWAP Pullback','chart-no-axes-combined','Buys orderly retracements into institutional average price during strong directional sessions.',['Intraday','Low slippage'],'Highest quality after a strong open, when sector peers also hold above VWAP.'],
  ['06','Volatility Squeeze','minimize-2','Finds compressed ranges likely to expand, then follows the break with controlled risk.',['Expansion','1–3 days'],'Position size stays smaller until direction confirms; stop sits inside the prior range.']
];

const titles = {
  dashboard:['Paper portfolio','Good afternoon, Arjun.'], positions:['Live exposure','Open positions'], history:['Execution ledger','Trade history'], strategies:['Agent intelligence','Strategy library'], settings:['Workspace controls','Settings']
};

const content = document.getElementById('content');
const icon = (name) => `<i data-lucide="${name}"></i>`;
const logo = (s) => `<span class="stock-logo">${s.slice(0,2)}</span>`;

function metric(label,value,foot,iconName,primary='') { return `<article class="metric ${primary}"><div class="metric-label"><span>${label}</span>${icon(iconName)}</div><strong class="metric-value">${value}</strong><div class="metric-foot">${foot}</div></article>`; }
function tradeRows(list, withReason=true) {
  return list.map((t,i)=>`<tr><td class="mono">${t.time}</td><td><div class="stock-cell">${logo(t.symbol)}<div><strong>${t.symbol}</strong><small>NSE</small></div></div></td><td><span class="action-pill ${t.action.toLowerCase()}">${t.action}</span></td><td class="mono">${t.qty}</td><td class="mono">₹${t.price}</td><td class="mono">₹${t.value}</td><td class="mono ${t.up?'up':'down'}"><strong>${t.pnl}</strong><br><small>${t.pct}</small></td>${withReason?`<td><button class="reason-btn" data-reason="${i}">${icon('message-square-text')} View thesis</button></td>`:''}</tr>${withReason?`<tr class="reason-row" data-reason-row="${i}"><td colspan="8"><div class="reason-content"><b>AI trade thesis · </b>${t.reason}</div></td></tr>`:''}`).join('');
}
function dashboard() {
  return `<section class="metric-grid">
    ${metric('Total capital','₹10,00,000','Starting paper balance','landmark','primary')}
    ${metric('Current value','₹10,43,286','<b class="up">+4.33%</b> all time','wallet-cards')}
    ${metric('Total P&L','+₹43,286','<b class="up">+₹5,214</b> this week','chart-spline')}
    ${metric("Today's P&L",'+₹1,233','<b class="up">+0.12%</b> since open','sun')}
  </section>
  <section class="dashboard-grid">
    <article class="panel"><div class="panel-head"><div><h3>Portfolio performance</h3><p>Capital growth over the last 30 days</p></div><div class="range-tabs"><button>1D</button><button>1W</button><button class="active">1M</button><button>3M</button></div></div><div class="chart-wrap"><div class="chart-summary"><strong>₹10.43L</strong><span>+4.33%</span></div><svg class="chart" viewBox="0 0 700 150" preserveAspectRatio="none"><defs><linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#138b61" stop-opacity=".18"/><stop offset="1" stop-color="#138b61" stop-opacity="0"/></linearGradient></defs><path class="chart-grid" d="M0 25H700M0 75H700M0 125H700"/><path class="chart-area" d="M0 130 C40 123 55 117 90 121 S145 105 180 110 S225 91 260 94 S310 83 350 88 S410 64 445 69 S500 45 535 54 S590 36 620 41 S665 23 700 29 V150H0Z"/><path class="chart-line" d="M0 130 C40 123 55 117 90 121 S145 105 180 110 S225 91 260 94 S310 83 350 88 S410 64 445 69 S500 45 535 54 S590 36 620 41 S665 23 700 29"/><circle class="chart-dot" cx="700" cy="29" r="4"/></svg><div class="chart-labels"><span>29 MAY</span><span>05 JUN</span><span>12 JUN</span><span>19 JUN</span><span>27 JUN</span></div></div></article>
    <article class="panel agent-panel"><div class="agent-top"><div class="agent-icon">${icon('bot')}</div><span class="status-badge"><span></span>Ready</span></div><h3>Your AI desk is watching.</h3><p>Scanning 214 liquid NSE stocks across 6 strategies. One high-confidence setup is forming.</p><div class="agent-stats"><div><span>Last scan</span><strong id="lastRun">12:31:08 IST</strong></div><div><span>Next auto scan</span><strong>13:00 IST</strong></div></div><button class="primary-btn run-agent">${icon('sparkles')} Run AI Agent Now</button></article>
  </section>
  <div class="section-head"><div><h2>Today’s trades</h2><p>Every decision the agent made during this session</p></div><button class="text-btn" data-go="history">View trade history ${icon('arrow-up-right')}</button></div>
  <section class="panel table-panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Time (IST)</th><th>Stock</th><th>Action</th><th>Qty</th><th>Exec. price</th><th>Current value</th><th>P&amp;L</th><th>AI reasoning</th></tr></thead><tbody>${tradeRows(trades)}</tbody></table></div></section>
  <div class="section-head"><div><h2>AI’s daily hot stocks</h2><p>Ranked opportunities from today’s market scan</p></div><span class="eyebrow">Updated 6 min ago</span></div>
  <section class="hot-grid">${hotStocks.map(s=>`<article class="hot-card"><div class="hot-top"><div class="hot-stock">${logo(s.symbol)}<div><strong>${s.symbol}</strong><small>NSE · EQ</small></div></div><div class="confidence"><strong>${s.confidence}</strong><small>AI score</small></div></div><div class="confidence-bar"><span style="width:${s.confidence}%"></span></div><div class="hot-price"><strong>${s.price}</strong><span class="up">${s.change}</span></div><p>${s.note}</p></article>`).join('')}</section>`;
}

function positionsPage() { return `<div class="page-intro"><div><h2>Your open positions</h2><p>Marked to the latest available NSE price · paper trades only</p></div><div class="summary-pills"><span class="summary-pill">Invested <b>₹1.73L</b></span><span class="summary-pill">Unrealised <b class="up">+₹1,456</b></span></div></div><section class="panel table-panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Stock</th><th>Qty</th><th>Avg. price</th><th>LTP</th><th>Current value</th><th>Unrealised P&amp;L</th><th>Weight</th></tr></thead><tbody>${holdings.map((h,i)=>`<tr><td><div class="stock-cell">${logo(h[0])}<div><strong>${h[0]}</strong><small>${h[1]}</small></div></div></td><td class="mono">${h[2]}</td><td class="mono">${h[3]}</td><td class="mono">${h[4]}</td><td class="mono">${h[5]}</td><td class="mono ${h[6].includes('+')?'up':'down'}"><strong>${h[6]}</strong><br><small>${h[7]}</small></td><td class="mono">${[20.8,17.4,17.7,16.7,13.2,14.2][i]}%</td></tr>`).join('')}</tbody></table></div></section>`; }

function historyPage() { return `<div class="page-intro"><div><h2>Trade history</h2><p>A complete, auditable record of your AI agent’s actions</p></div><button class="secondary-btn">${icon('download')} Export CSV</button></div><div class="filters"><label class="field">${icon('search')}<input id="tradeSearch" placeholder="Search stock symbol…"></label><label class="field"><select id="actionFilter"><option value="ALL">All actions</option><option>BUY</option><option>SELL</option></select></label><label class="field"><select><option>Last 30 days</option><option>This week</option><option>Today</option></select></label></div><section class="panel table-panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>Date &amp; time</th><th>Stock</th><th>Action</th><th>Qty</th><th>Exec. price</th><th>Value</th><th>Realised P&amp;L</th></tr></thead><tbody id="historyBody">${tradeRows(history,false)}</tbody></table></div></section>`; }

function strategiesPage() { return `<div class="page-intro"><div><h2>Strategy library</h2><p>The playbooks your agent evaluates before every paper trade</p></div><span class="summary-pill">Active strategies <b>6 / 6</b></span></div><section class="strategy-grid">${strategies.map(s=>`<article class="strategy-card"><span class="strategy-number">PLAYBOOK ${s[0]}</span>${icon(s[2])}<h3>${s[1]}</h3><p>${s[3]}</p><div class="strategy-tags">${s[4].map(t=>`<span>${t}</span>`).join('')}</div><div class="ai-note">${icon('sparkles')}<span><b>AI note:</b> ${s[5]}</span></div></article>`).join('')}</section>`; }

function settingsPage() { return `<div class="page-intro"><div><h2>Workspace settings</h2><p>Manage agent risk, capital, and paper-trading preferences</p></div></div><section class="settings-grid"><article class="panel settings-card"><h3>Risk profile</h3><p>Controls position sizing, maximum drawdown, and stop-loss behaviour.</p><div class="risk-options"><div class="risk-option"><span class="radio"></span><div><strong>Conservative</strong><small>Prioritise capital protection</small></div><em>1% / trade</em></div><div class="risk-option active"><span class="radio"></span><div><strong>Balanced</strong><small>Measured growth and risk</small></div><em>2% / trade</em></div><div class="risk-option"><span class="radio"></span><div><strong>Aggressive</strong><small>Higher volatility tolerance</small></div><em>3.5% / trade</em></div></div></article><article class="panel settings-card danger-zone"><h3>Paper capital</h3><p>Reset your simulation and begin again with a clean portfolio.</p><div class="capital-box"><span>Current starting capital</span><strong>₹10,00,000</strong></div><button class="danger-btn" id="resetCapital">${icon('rotate-ccw')} Reset paper portfolio</button></article></section>`; }

function render(view='dashboard') {
  const pages = {dashboard,positions:positionsPage,history:historyPage,strategies:strategiesPage,settings:settingsPage};
  content.innerHTML = pages[view]();
  document.getElementById('pageEyebrow').textContent = titles[view][0];
  document.getElementById('pageTitle').textContent = titles[view][1];
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  if(window.lucide) lucide.createIcons();
  wirePage(view);
  window.scrollTo({top:0,behavior:'smooth'});
}

function wirePage(view) {
  document.querySelectorAll('[data-reason]').forEach(btn=>btn.addEventListener('click',()=>document.querySelector(`[data-reason-row="${btn.dataset.reason}"]`).classList.toggle('open')));
  document.querySelectorAll('.run-agent').forEach(b=>b.addEventListener('click',runAgent));
  document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>render(b.dataset.go)));
  if(view==='history') {
    const update=()=>{ const q=document.getElementById('tradeSearch').value.toUpperCase(); const a=document.getElementById('actionFilter').value; document.getElementById('historyBody').innerHTML=tradeRows(history.filter(t=>t.symbol.includes(q)&&(a==='ALL'||t.action===a)),false); };
    document.getElementById('tradeSearch').addEventListener('input',update); document.getElementById('actionFilter').addEventListener('change',update);
  }
  if(view==='settings') {
    document.querySelectorAll('.risk-option').forEach(o=>o.addEventListener('click',()=>{document.querySelectorAll('.risk-option').forEach(x=>x.classList.remove('active'));o.classList.add('active')}));
    document.getElementById('resetCapital').addEventListener('click',()=>toggleModal(true));
  }
}

function runAgent(e) {
  const btn=e.currentTarget; const old=btn.innerHTML; btn.disabled=true; btn.innerHTML=`${icon('loader-circle')} Scanning NSE…`; if(window.lucide) lucide.createIcons(); btn.querySelector('svg').style.animation='spin 1s linear infinite';
  setTimeout(()=>{ btn.disabled=false; btn.innerHTML=old; if(window.lucide) lucide.createIcons(); const last=document.getElementById('lastRun'); if(last) last.textContent=new Date().toLocaleTimeString('en-IN',{hour12:false})+' IST'; const toast=document.getElementById('toast'); toast.classList.add('show'); setTimeout(()=>toast.classList.remove('show'),3500); },1500);
}
function toggleModal(open) { const m=document.getElementById('resetModal'); m.classList.toggle('open',open); m.setAttribute('aria-hidden',String(!open)); }
function toggleAuth(open) { const a=document.getElementById('authScreen'); a.classList.toggle('open',open); a.setAttribute('aria-hidden',String(!open)); document.body.style.overflow=open?'hidden':''; }

document.querySelectorAll('.nav-item').forEach(b=>b.addEventListener('click',()=>{render(b.dataset.view);document.getElementById('sidebar').classList.remove('open')}));
document.getElementById('menuBtn').addEventListener('click',()=>document.getElementById('sidebar').classList.toggle('open'));
document.getElementById('runAgentTop').addEventListener('click',runAgent);
document.getElementById('signOutBtn').addEventListener('click',()=>toggleAuth(true));
document.getElementById('authClose').addEventListener('click',()=>toggleAuth(false));
document.getElementById('authForm').addEventListener('submit',e=>{e.preventDefault();toggleAuth(false)});
let creating=false; document.getElementById('authToggle').addEventListener('click',e=>{creating=!creating;document.getElementById('authEyebrow').textContent=creating?'Start paper trading':'Welcome back';document.getElementById('authTitle').textContent=creating?'Create your account':'Sign in to your desk';document.getElementById('authDesc').textContent=creating?'Build conviction without risking capital.':'Continue your paper-trading session.';e.currentTarget.textContent=creating?'Sign in instead':'Create an account';});
document.querySelectorAll('[data-close-modal]').forEach(b=>b.addEventListener('click',()=>toggleModal(false)));
document.getElementById('confirmReset').addEventListener('click',()=>{toggleModal(false); const t=document.getElementById('toast'); t.querySelector('strong').textContent='Paper portfolio reset';t.querySelector('span').textContent='Starting balance restored to ₹10,00,000';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),3500)});
document.getElementById('resetModal').addEventListener('click',e=>{if(e.target===e.currentTarget)toggleModal(false)});
setInterval(()=>document.getElementById('clock').textContent=new Date().toLocaleTimeString('en-IN',{hour12:false,timeZone:'Asia/Kolkata'}),1000);
const style=document.createElement('style');style.textContent='@keyframes spin{to{transform:rotate(360deg)}}';document.head.appendChild(style);
render('dashboard');
