/**
 * Shared formatting and UI utility functions.
 * Native ES Module (Zero-dependency Vanilla JS).
 */

export const icon = (name) => `<i data-lucide="${name}"></i>`;

export const logo = (s) => `<span class="stock-logo">${String(s || '').slice(0, 2)}</span>`;

export const money = (value) =>
  `₹${Math.abs(Number(value || 0)).toLocaleString('en-IN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;

export const signedMoney = (value) =>
  `${Number(value) >= 0 ? '+' : '−'}${money(value)}`;

export const signedPct = (value) =>
  `${Number(value) >= 0 ? '+' : '−'}${Math.abs(Number(value || 0)).toFixed(2)}%`;

export const escapeHtml = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (char) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      }[char])
  );

export const fmtTime = (value) =>
  value
    ? new Date(value).toLocaleString('en-IN', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—';

export function normaliseTags(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      return value
        ? value
            .split(',')
            .map((x) => x.trim())
            .filter(Boolean)
        : [];
    }
  }
  return [];
}

export function formatPnlEl(selector, val, isMetric) {
  const el = document.querySelector(selector);
  if (!el) return;
  const num = Number(val || 0);
  el.textContent = signedMoney(num);
  el.className = isMetric
    ? `metric-value ${num >= 0 ? 'up' : 'down'}`
    : num >= 0
    ? 'up'
    : 'down';
}

export function strategyPill(strategy) {
  let s = String(strategy || 'BREAKOUT_CALL_BUY').trim().toUpperCase();
  if (
    !s ||
    s === 'UNKNOWN' ||
    s === 'UNKNOWN_STRATEGY' ||
    s === 'UNKNOWN STRATEGY' ||
    s === 'NO_TRADE' ||
    s === 'NO_STRATEGY' ||
    s === 'NONE'
  ) {
    s = 'BREAKOUT_CALL_BUY';
  }
  let cls = 'breakout';
  let iconName = 'zap';
  if (s.includes('GOLDEN') || s.includes('FAST') || s.includes('VECTOR')) {
    cls = 'fastpath';
    iconName = 'sparkles';
  } else if (
    s.includes('QUANT') ||
    s.includes('STAT_ARB') ||
    s.includes('FACTOR') ||
    s.includes('VECM') ||
    s.includes('LEAD_LAG')
  ) {
    cls = 'quant';
    iconName = 'cpu';
  } else if (
    s.includes('SPREAD') ||
    s.includes('STRADDLE') ||
    s.includes('CONDOR')
  ) {
    cls = 'spread';
    iconName = 'layers';
  } else if (
    s.includes('INDEX') ||
    s.includes('NIFTY') ||
    s.includes('BANKNIFTY') ||
    s.includes('SENSEX')
  ) {
    cls = 'index';
    iconName = 'bar-chart-2';
  } else if (s.includes('PULLBACK')) {
    cls = 'pullback';
    iconName = 'trending-down';
  } else if (s.includes('MOMENTUM')) {
    cls = 'momentum';
    iconName = 'activity';
  } else if (s.includes('REVERSION') || s.includes('RANGE')) {
    cls = 'reversion';
    iconName = 'waves';
  }

  const clean = s
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace('call buy', 'Call (CE)')
    .replace('put sell', 'Put Write (PE)')
    .replace('put buy', 'Put (PE)')
    .replace('call sell', 'Call Write (CE)')
    .replace(/\b\w/g, (c) => c.toUpperCase());

  return `<span class="strategy-pill ${cls}">${icon(iconName)} ${escapeHtml(
    clean
  )}</span>`;
}

export function shadowActionLabel(t) {
  const type = String(t.instrument_type || 'EQ').toUpperCase(),
    side = String(t.side || 'BUY').toUpperCase();
  if (type === 'CE') return `${side} CALL`;
  if (type === 'PE') return `${side} PUT`;
  if (type === 'FUT') return `${side} FUT`;
  return side;
}

export function shadowStrategyNote(t) {
  const raw = t.improvement_note || '';
  if (!raw) return '';
  try {
    const parsed = JSON.parse(raw);
    const hedge = parsed.paired_leg ? `🛡️ [HEDGE: ${parsed.paired_leg}]` : '';
    return [
      hedge,
      parsed.strategy,
      parsed.route,
      parsed.reason,
      parsed.rr ? `R:R ${parsed.rr}` : '',
    ]
      .filter(Boolean)
      .join(' · ');
  } catch (_) {
    return raw;
  }
}

export function shadowRiskManagerNote(t) {
  const raw = t.improvement_note || '';
  if (!raw) return '';
  try {
    const parsed = JSON.parse(raw),
      rm = parsed.risk_manager;
    if (!rm) return '';
    const reasons = (rm.reasons || [])
      .map((x) => String(x).replaceAll('_', ' '))
      .join(', ');
    const slChanged =
      rm.old_sl && rm.new_sl && Number(rm.old_sl) !== Number(rm.new_sl);
    const tpChanged =
      rm.old_tp && rm.new_tp && Number(rm.old_tp) !== Number(rm.new_tp);
    const changes = [
      slChanged ? `SL ${money(rm.old_sl)} → ${money(rm.new_sl)}` : '',
      tpChanged ? `TP ${money(rm.old_tp)} → ${money(rm.new_tp)}` : '',
    ]
      .filter(Boolean)
      .join(' · ');
    return [
      changes || 'No active SL/TP change',
      reasons ? `Reason: ${reasons}` : '',
      rm.latest ? `Latest ${money(rm.latest)}` : '',
      rm.atr_5m ? `ATR ${money(rm.atr_5m)}` : '',
    ]
      .filter(Boolean)
      .join(' · ');
  } catch (_) {
    return '';
  }
}

export function shadowRiskCell(t) {
  const sl = money(t.stop_loss_price || 0),
    tp = money(t.take_profit_price || 0),
    rm = escapeHtml(shadowRiskManagerNote(t));
  return `<div class="risk-cell"><strong>${sl} / ${tp}</strong>${
    rm
      ? `<small class="risk-manager-note">${icon('shield-check')} ${rm}</small>`
      : `<small>Initial SL / TP</small>`
  }</div>`;
}

export function shadowQualityCell(t) {
  const q = t.quality || {},
    score = Number(q.score || 0),
    grade = escapeHtml(q.grade || '—');
  const tone = score >= 68 ? 'good' : score >= 52 ? 'warn' : 'bad';
  const summary = escapeHtml(q.summary || 'Awaiting enough evidence');
  return `<div class="quality-cell ${tone}"><strong>${
    score ? score.toFixed(1) : '—'
  }</strong><span>${grade}</span><small>${summary}</small></div>`;
}

export function shadowSeniorAgentCell(t) {
  const a = t.senior_agent || {},
    severity = escapeHtml(a.severity || 'warn'),
    action = escapeHtml(a.action || 'WATCH');
  const reasons =
    (a.reasons || []).slice(0, 2).map((x) => escapeHtml(String(x))).join(' · ') ||
    'Awaiting senior review';
  const rr = Number(a.rr || 0);
  const grade =
    a.option_grade?.grade && a.option_grade.grade !== 'N/A'
      ? ` · Option ${escapeHtml(a.option_grade.grade)}`
      : '';
  const decision = a.decision_report?.action
    ? ` · ${escapeHtml(String(a.decision_report.action).replaceAll('_', ' '))}`
    : '';
  const thoughtBtn = `<button type="button" class="thought-btn" data-trade-thought="${Number(
    t.id || 0
  )}">${icon('brain')} Thoughts</button>`;
  return `<div class="quality-cell ${severity}"><strong>${action}</strong><span>${escapeHtml(
    a.experience_label || 'Senior layer'
  )}${grade}</span><small>${reasons}${
    rr ? ` · R:R ${rr.toFixed(2)}` : ''
  }${decision}</small><div style="margin-top:4px">${thoughtBtn}</div></div>`;
}

export function shadowTradeActions(t) {
  return `<div class="shadow-actions"><button type="button" data-shadow-exit="${Number(
    t.id || 0
  )}">${icon('log-out')} Exit</button><button type="button" data-shadow-risk="${Number(
    t.id || 0
  )}" data-current-sl="${escapeHtml(
    t.stop_loss_price || ''
  )}" data-current-tp="${escapeHtml(
    t.take_profit_price || ''
  )}">${icon('sliders-horizontal')} SL/TP</button></div>`;
}

export function swingLifecycleCell(t) {
  const isSwing = String(t.trade_mode || 'INTRADAY').toUpperCase() === 'SWING';
  if (!isSwing) {
    return `<div style="min-width:85px;"><span class="mode-pill intraday">INTRADAY</span><small style="display:block;font-size:9px;color:var(--muted);margin-top:2px;">75m · 15:20 flat</small></div>`;
  }
  let days = Number(t.holding_days ?? -1);
  if (days < 0 || (days === 0 && t.signal_at)) {
    try {
      const sigDate = new Date(t.signal_at).toISOString().split('T')[0];
      const todayDate = new Date().toISOString().split('T')[0];
      const diffMs = new Date(todayDate) - new Date(sigDate);
      const computed = Math.max(0, Math.floor(diffMs / (1000 * 60 * 60 * 24)));
      if (computed > 0) days = computed;
    } catch (_) {}
  }
  if (days < 0) days = 0;
  const maxDays = Number(t.max_holding_days || 5);
  const pct = Math.min(100, Math.round(((days + 1) / maxDays) * 100));
  return `<div class="swing-lifecycle-wrap">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:4px;">
      <span class="mode-pill swing">SWING</span>
      <b style="font-size:10px;color:#c084fc;">Day ${days + 1}/${maxDays}</b>
    </div>
    <div class="swing-progress-bar"><div class="swing-progress-fill" style="width:${Math.max(
      20,
      pct
    )}%"></div></div>
    <small style="font-size:9px;color:var(--muted);">Multi-day · 2.0x ATR</small>
  </div>`;
}
