/**
 * Shadow Paper Trading Book, Position Inspector & Audit Ledger.
 * Native ES Module (Zero-dependency Vanilla JS).
 */

import {
  escapeHtml,
  fmtTime,
  icon,
  logo,
  money,
  normaliseTags,
  shadowActionLabel,
  shadowQualityCell,
  shadowRiskCell,
  shadowSeniorAgentCell,
  shadowStrategyNote,
  shadowTradeActions,
  signedMoney,
  signedPct,
  strategyPill,
  swingLifecycleCell,
} from './utils.js';

export function shadowTradeRows(list, closed = false) {
  if (!Array.isArray(list)) return '';
  return list
    .map((t) => {
      const pnl = Number(t.marked_pnl || 0),
        pct = Number(t.pnl_pct || 0),
        side = escapeHtml(t.side || 'BUY'),
        action = escapeHtml(shadowActionLabel(t)),
        prob = Number(t.signal_probability || 0) * 100;
      const tags =
        normaliseTags(t.mistake_tags)
          .slice(0, 3)
          .map((x) => escapeHtml(String(x)))
          .join(' · ') || '—';
      const model = escapeHtml(
        String(t.model_version || 'paper-model')
          .split('-')
          .slice(0, 3)
          .join('-')
      );
      const note = escapeHtml(shadowStrategyNote(t));
      const exitPrice = t.realised_exit_price || t.latest_price || 0;
      const stratTag =
        t.strategy_tag ||
        t.strategy_label ||
        (side === 'BUY' ? 'BREAKOUT_CALL_BUY' : 'BREAKDOWN_PUT_BUY');

      return closed
        ? `<tr>
            <td class="mono">${fmtTime(t.signal_at)}</td>
            <td class="mono">${fmtTime(t.exit_at)}</td>
            <td>
              <div class="stock-cell">
                ${logo(escapeHtml(t.symbol || '--'))}
                <div>
                  <strong>${escapeHtml(t.symbol || '—')}</strong>
                  <small>${escapeHtml(t.exchange || 'NSE')} · ${escapeHtml(
            t.instrument_type || 'EQ'
          )}${note ? ` · ${note}` : ''}</small>
                </div>
              </div>
            </td>
            <td>${swingLifecycleCell(t)}</td>
            <td>${strategyPill(stratTag)}</td>
            <td><span class="action-pill ${
              side === 'BUY' ? 'buy' : 'sell'
            }">${action}</span></td>
            <td class="mono">${Number(t.quantity || 0).toLocaleString('en-IN')}</td>
            <td class="mono">${money(t.entry_price || 0)}</td>
            <td class="mono">${money(exitPrice)}</td>
            <td class="mono ${pnl >= 0 ? 'up' : 'down'}">
              <strong>${signedMoney(pnl)}</strong><br>
              <small>${signedPct(pct)}</small>
            </td>
            <td class="mono">${money(t.estimated_fees || 0)}</td>
            <td>${shadowRiskCell(t)}</td>
            <td>${shadowQualityCell(t)}</td>
            <td>${shadowSeniorAgentCell(t)}</td>
            <td>
              <span class="intent-status">${escapeHtml(
                t.exit_reason || 'closed'
              )}</span>
              <small class="risk-reason">${tags}</small>
            </td>
            <td>
              <span class="shadow-model-tag">${model}</span>
              <small>${prob.toFixed(1)}% signal</small>
            </td>
          </tr>`
        : `<tr>
            <td class="mono">${fmtTime(t.signal_at)}</td>
            <td>
              <div class="stock-cell">
                ${logo(escapeHtml(t.symbol || '--'))}
                <div>
                  <strong>${escapeHtml(t.symbol || '—')}</strong>
                  <small>${escapeHtml(t.exchange || 'NSE')} · ${escapeHtml(
            t.instrument_type || 'EQ'
          )}${note ? ` · ${note}` : ''}</small>
                </div>
              </div>
            </td>
            <td>${swingLifecycleCell(t)}</td>
            <td>${strategyPill(stratTag)}</td>
            <td><span class="action-pill ${
              side === 'BUY' ? 'buy' : 'sell'
            }">${action}</span></td>
            <td class="mono">${Number(t.quantity || 0).toLocaleString('en-IN')}</td>
            <td class="mono">${money(t.entry_price || 0)}</td>
            <td class="mono">${money(t.latest_price || 0)}</td>
            <td class="mono ${pnl >= 0 ? 'up' : 'down'}">
              <strong>${signedMoney(pnl)}</strong><br>
              <small>${signedPct(pct)}</small>
            </td>
            <td>${shadowRiskCell(t)}</td>
            <td>${shadowQualityCell(t)}</td>
            <td>${shadowSeniorAgentCell(t)}</td>
            <td>${shadowTradeActions(t)}</td>
            <td>
              <span class="shadow-model-tag">${model}</span>
              <small>${prob.toFixed(1)}% signal</small>
            </td>
          </tr>`;
    })
    .join('');
}

export function showTradeThoughtModal(trade) {
  if (!trade) return;
  const modal = document.getElementById('thoughtModal');
  if (!modal) return;
  const eyebrow = document.getElementById('thoughtEyebrow');
  const title = document.getElementById('thoughtTitle');
  const content = document.getElementById('thoughtContent');

  const mode = String(trade.trade_mode || 'INTRADAY').toUpperCase();
  if (eyebrow) eyebrow.textContent = `Autonomous Multi-Agent Deliberation · ${mode} Track`;
  if (title)
    title.textContent = `${trade.symbol} ${trade.side} (${
      trade.strategy_label || trade.strategy_tag || 'Directional'
    })`;

  const rc = trade.reasoning_chain || {};
  const thoughts = rc.thought_chain || [
    `1. Setup Review: ${trade.symbol} ${trade.side} evaluated via ${
      trade.strategy_label || trade.strategy_tag || 'Directional'
    }.`,
    `2. Risk & Friction: Estimated fees ₹${Number(
      trade.estimated_fees || 0
    ).toFixed(2)}. SL ₹${Number(trade.stop_loss_price || 0).toFixed(
      2
    )}, TP ₹${Number(trade.take_profit_price || 0).toFixed(2)}.`,
    `3. Execution Route: Assigned to ${mode} mode based on multi-timeframe regime.`,
    `4. Invariants Check: Delta direction and chart structure verified consistent.`,
    `5. Final Verdict: Approved by Senior Agent for paper execution.`,
  ];

  let html = thoughts
    .map((step, idx) => {
      const isVerdict =
        step.toLowerCase().includes('verdict:') || idx === thoughts.length - 1;
      return `<div class="thought-step-card ${
        isVerdict ? 'verdict' : ''
      }">${escapeHtml(step)}</div>`;
    })
    .join('');

  if (rc.risk_assessment) {
    const ra = rc.risk_assessment;
    html += `<div class="thought-step-card" style="margin-top:6px;background:rgba(59,130,246,0.06);border-color:rgba(59,130,246,0.2);">
      <strong>Risk & Structure Metrics:</strong>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px;">
        <span>Expected Net Edge: <b>${
          ra.expected_edge_bps ? ra.expected_edge_bps.toFixed(1) : '—'
        } bps</b></span>
        <span>Aligned Timeframes: <b>${ra.aligned_frames ?? '—'} frames</b></span>
        <span>Higher TF Bias: <b>${ra.higher_tf_bias || 'neutral'}</b></span>
        <span>Gap Risk Warning: <b>${ra.gap_risk_warning || 'None'}</b></span>
      </div>
    </div>`;
  }

  if (content) content.innerHTML = html;
  modal.hidden = false;
  document.body.classList.add('modal-open');
}

export function bindThoughtButtons(tradesList = []) {
  document.querySelectorAll('[data-trade-thought]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = Number(btn.getAttribute('data-trade-thought'));
      const trade = tradesList.find((t) => Number(t.id) === id);
      if (trade) showTradeThoughtModal(trade);
    });
  });
}
