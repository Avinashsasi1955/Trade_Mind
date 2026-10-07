/**
 * Live Metrics, Notification Center & Market Telemetry.
 * Native ES Module (Zero-dependency Vanilla JS).
 */

import { escapeHtml, formatPnlEl, icon } from './utils.js';

export const systemNotifications = [];

export function addSystemNotification(type, title, desc) {
  const timeStr = new Date().toLocaleTimeString('en-IN', {
    hour12: false,
    timeZone: 'Asia/Kolkata',
  });
  systemNotifications.unshift({
    type,
    title,
    desc,
    time: timeStr,
    id: Date.now(),
  });
  if (systemNotifications.length > 50) systemNotifications.pop();
  const badge = document.getElementById('notifBadge');
  if (badge) badge.classList.add('active');
  renderNotificationList();
}

export function renderNotificationList() {
  const list = document.getElementById('notificationList');
  if (!list) return;
  if (!systemNotifications.length) {
    list.innerHTML =
      '<div class="empty-state">No alerts recorded yet. Real-time trade executions and risk warnings appear here.</div>';
    return;
  }
  list.innerHTML = systemNotifications
    .map(
      (n) => `
    <div class="notif-item ${n.type}">
      <div class="notif-top">
        <span class="${
          n.type.includes('profit') ? 'up' : n.type.includes('stop') ? 'down' : ''
        }">${n.type.replace('-', ' ').toUpperCase()}</span>
        <small>${n.time} IST</small>
      </div>
      <div class="notif-title">${escapeHtml(n.title)}</div>
      <p class="notif-desc">${escapeHtml(n.desc)}</p>
    </div>
  `
    )
    .join('');
}

export function handleMetricsTick(tick) {
  if (!tick) return;
  if (tick.latest_thought) {
    const ticker = document.getElementById('reasoningTickerText');
    if (ticker) ticker.textContent = tick.latest_thought;
  }
  formatPnlEl('[data-live-marked-pnl]', tick.net_marked_pnl ?? tick.total_pnl, true);
  formatPnlEl('[data-live-realised-pnl]', tick.realised_pnl, false);
  formatPnlEl('[data-live-unrealised-pnl]', tick.unrealised_pnl, false);

  const openCntEl = document.querySelector('[data-live-open-trades]');
  if (openCntEl && tick.open_trades !== undefined) openCntEl.textContent = tick.open_trades;

  const closedCntEl = document.querySelector('[data-live-closed-trades]');
  if (closedCntEl && tick.closed_trades !== undefined)
    closedCntEl.textContent = tick.closed_trades;
}

export function applyMarketUpdate(data) {
  if (!data || !data.indices) return;
  const nodes = document.querySelectorAll('.ticker-item');
  data.indices.slice(0, nodes.length).forEach((item, i) => {
    const n = nodes[i];
    const strong = n.querySelector('strong');
    if (strong) {
      strong.textContent = Number(item.price).toLocaleString('en-IN', {
        minimumFractionDigits: 2,
      });
    }
    const em = n.querySelector('em');
    if (em) {
      em.className = item.change_pct >= 0 ? 'up' : 'down';
      em.innerHTML = `${icon(
        item.change_pct >= 0 ? 'trending-up' : 'trending-down'
      )}${Math.abs(item.change_pct).toFixed(2)}%`;
    }
  });
  const delay = document.querySelector('.data-delay');
  if (delay) {
    const mode =
      data.data_mode && data.data_mode.startsWith('provider')
        ? 'Live provider'
        : 'Simulated';
    delay.textContent = `${mode} · updated ${new Date(
      data.updated_at
    ).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;
  }
  if (window.lucide) window.lucide.createIcons();
}
