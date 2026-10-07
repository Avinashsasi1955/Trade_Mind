/**
 * Server-Sent Events (SSE) Transport & Real-time Stream Client.
 * Native ES Module (Zero-dependency Vanilla JS).
 */

import { addSystemNotification, handleMetricsTick } from './metrics.js';

let sseConnection = null;
let sseRetryTimeout = null;
let sseActive = false;
let lastAlertTs = 0;

export function getSSEStatus() {
  return { active: sseActive, connected: Boolean(sseConnection) };
}

export function closeSSETransport() {
  if (sseConnection) {
    try {
      sseConnection.close();
    } catch (_) {}
    sseConnection = null;
  }
  if (sseRetryTimeout) {
    clearTimeout(sseRetryTimeout);
    sseRetryTimeout = null;
  }
  sseActive = false;
}

export function initSSETransport(callbacks = {}) {
  const {
    isAuthenticated = () => true,
    onTradeOpened = () => {},
    onTradeClosed = () => {},
    onPositionAlert = () => {},
    showToast = () => {},
  } = callbacks;

  if (!isAuthenticated()) return;
  closeSSETransport();

  const badge = document.getElementById('sseBadge');
  const label = document.getElementById('sseLabel');
  const setBadgeState = (state, text) => {
    if (!badge || !label) return;
    badge.className = `sse-indicator sse-${state}`;
    label.textContent = text;
  };

  setBadgeState('connecting', 'Connecting…');

  try {
    sseConnection = new EventSource('/api/stream/events');

    sseConnection.onopen = () => {
      sseActive = true;
      setBadgeState('live', 'STREAM LIVE ⚡');
      console.log('[SSE] Real-time push stream connected');
    };

    sseConnection.addEventListener('connected', () => {
      sseActive = true;
      setBadgeState('live', 'STREAM LIVE ⚡');
    });

    sseConnection.addEventListener('trade_opened', (e) => {
      try {
        const trade = JSON.parse(e.data);
        const modeTag = trade.trade_mode === 'SWING' ? '🌊 SWING' : '⚡ INTRADAY';
        const price = trade.fill_price || trade.entry_price || 0;
        showToast(
          `Order Opened · ${modeTag}`,
          `${trade.side || 'BUY'} ${trade.symbol} @ ₹${Number(price).toLocaleString('en-IN')}`
        );
        addSystemNotification(
          'order-opened',
          `Order Opened · ${trade.symbol}`,
          `${trade.side || 'BUY'} @ ₹${Number(price).toLocaleString('en-IN')} (${modeTag})`
        );
        onTradeOpened(trade);
      } catch (err) {
        console.error('[SSE] trade_opened error:', err);
      }
    });

    sseConnection.addEventListener('trade_closed', (e) => {
      try {
        const trade = JSON.parse(e.data);
        const pnl = Number(trade.net_pnl || 0);
        const sign = pnl >= 0 ? '+' : '';
        const outcome = pnl >= 0 ? '🏆 Profit Captured' : '🛑 Stop Hit';
        showToast(
          outcome,
          `${trade.symbol}: ${sign}₹${pnl.toLocaleString('en-IN', {
            minimumFractionDigits: 2,
          })} (${trade.exit_reason || 'closed'})`
        );
        addSystemNotification(
          pnl >= 0 ? 'profit-captured' : 'stop-hit',
          `${outcome} · ${trade.symbol}`,
          `${sign}₹${pnl.toLocaleString('en-IN', {
            minimumFractionDigits: 2,
          })} · Reason: ${trade.exit_reason || 'closed'}`
        );
        onTradeClosed(trade);
      } catch (err) {
        console.error('[SSE] trade_closed error:', err);
      }
    });

    sseConnection.addEventListener('agent_thought', (e) => {
      try {
        const data = JSON.parse(e.data);
        const ticker = document.getElementById('reasoningTickerText');
        if (ticker && data.thought) ticker.textContent = data.thought;
      } catch (_) {}
    });

    sseConnection.addEventListener('position_alert', (e) => {
      try {
        const alert = JSON.parse(e.data);
        showToast(
          alert.alert_type || 'Risk Alert',
          `${alert.symbol}: ₹${alert.current_price} (P&L ₹${alert.pnl})`
        );
        addSystemNotification(
          'risk-alert',
          `${alert.alert_type || 'Risk Alert'} · ${alert.symbol}`,
          `Current Price ₹${alert.current_price} · Marked P&L: ₹${alert.pnl}`
        );
        onPositionAlert(alert);
      } catch (_) {}
    });

    sseConnection.addEventListener('metrics_tick', (e) => {
      try {
        const tick = JSON.parse(e.data);
        handleMetricsTick(tick);
      } catch (_) {}
    });

    sseConnection.onerror = () => {
      sseActive = false;
      setBadgeState('fallback', 'POLLING (FALLBACK)');
      if (sseConnection) {
        try {
          sseConnection.close();
        } catch (_) {}
        sseConnection = null;
      }
      if (sseRetryTimeout) clearTimeout(sseRetryTimeout);
      sseRetryTimeout = setTimeout(() => {
        if (isAuthenticated()) initSSETransport(callbacks);
      }, 6000);
    };
  } catch (initErr) {
    sseActive = false;
    setBadgeState('fallback', 'POLLING (FALLBACK)');
    console.warn('[SSE] EventSource init failed; falling back to polling:', initErr);
  }
}

export async function pollAlerts(apiClient, isAuthenticated, showToast) {
  if (!isAuthenticated() || sseActive) return;
  try {
    const alerts = await apiClient('/api/brain/alerts');
    if (!Array.isArray(alerts)) return;
    for (const a of alerts) {
      const ts = new Date(a.ts).getTime();
      if (ts > lastAlertTs) {
        showToast(
          a.alert_type,
          `${a.symbol} ${a.alert_type}: ${a.current_price} (P&L: ${a.pnl})`
        );
        lastAlertTs = ts;
      }
    }
  } catch (_) {}
}
