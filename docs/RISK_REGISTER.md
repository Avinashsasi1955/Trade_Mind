# TradeMind — Risk Register (RISK_REGISTER.md)

**Date**: 2026-10-12  
**Status**: Comprehensive Risk Matrix  
**System**: `Trade_Mind` Private Autonomous Trading System

---

## 1. Risk Evaluation Matrix

Risks are scored based on **Likelihood** (Low, Medium, High) and **Impact** (Low, Medium, High, Critical), resulting in a composite **Severity** rating:

$$\text{Severity} \in \{\text{CRITICAL}, \text{HIGH}, \text{MEDIUM}, \text{LOW}\}$$

```
                ┌────────────────────────────────────────────────────────┐
                │                       IMPACT                           │
                │     LOW         MEDIUM         HIGH         CRITICAL   │
┌───────────────┼────────────────────────────────────────────────────────┤
│ HIGH          │   MEDIUM         HIGH        CRITICAL       CRITICAL   │
│ MEDIUM        │    LOW          MEDIUM         HIGH         CRITICAL   │
│ LOW           │    LOW           LOW          MEDIUM          HIGH     │
└───────────────┴────────────────────────────────────────────────────────┘
```

---

## 2. Comprehensive Risk Register

### 2.1 Financial & Quantitative Risks

| Risk ID | Title | Likelihood | Impact | Severity | Root Cause & Failure Mechanism | Mitigation Strategy | Monitoring & Recovery |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **FIN-01** | **Catastrophic Account Drawdown** | Low | Critical | **HIGH** | Sequential strategy losses or unexpected market crash causing portfolio losses exceeding risk tolerance. | 1. Hard daily loss limit of 1.0% (₹10,000) blocks new entries.<br>2. Drawdown warning at 3.0% (₹30,000) cuts position sizes in half.<br>3. Hard emergency shutdown at 5.0% (₹50,000) halts all trading.<br>4. -7.32% database trigger locks table. | Real-time equity calculation in `position_manager.py`. Webhook alert triggers immediately upon breach. |
| **FIN-02** | **Disproportionate Trade Sizing** | Medium | High | **HIGH** | Using fixed notional (e.g. ₹50,000) rather than budgeting risk against stop-loss distance. | Formula: $\text{Qty} = \min(\lfloor ₹2,500 / \|P_{entry} - P_{sl}\| \rfloor, \lfloor \text{Available Funds} \times 0.20 / P_{entry} \rfloor)$. Maximum risk strictly capped at 0.25% of capital (₹2,500). Max single position capped at 20% of cash. | Pre-trade validation rejects orders where $|P_{entry} - P_{sl}| = 0$ or risk $> ₹2,500$. |
| **FIN-03** | **Unhedged Derivative Exposure** | Low | Critical | **HIGH** | Naked short option positions or unhedged multi-leg legs causing unlimited tail-risk losses. | Strict structural veto: Naked short options (CE/PE) are unconditionally blocked. Only long options and defined-risk spreads allowed. | Derivatives risk validator parses contract type and payoff curve before order creation. |
| **FIN-04** | **Overnight Gap Risk (Cash & F&O)** | Medium | High | **HIGH** | Major adverse price gaps between market sessions on open intraday or swing positions. | 1. Mandatory intraday force-flat at 15:15 IST.<br>2. Overnight cash shorts prohibited (SEBI regulatory guard).<br>3. Swing positions require $2.0 \times \text{ATR}$ stop buffer and 20% maximum portfolio allocation. | `position_manager.py` verifies clock at 15:15 IST and closes all intraday trades at market. |
| **FIN-05** | **Zero-Trade Trap vs. Loose Gate** | Medium | Medium | **MEDIUM** | Serial boolean AND gates rejecting 100% of candidates, or loose gates accepting poor setups. | 10-Component weighted ensemble scoring with calibrated minimum score threshold (75/100). | Rejection funnel telemetry logged per session in `forward_shadow_sessions`. |

---

### 2.2 Execution & State Machine Risks

| Risk ID | Title | Likelihood | Impact | Severity | Root Cause & Failure Mechanism | Mitigation Strategy | Monitoring & Recovery |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **EXE-01** | **Split-Brain State Divergence** | High | High | **HIGH** | Web UI reading SQLite while execution engine writes PostgreSQL, causing invisible positions. | Collapse SQLite runtime: Unify web backend, worker, and position manager on PostgreSQL as single source of truth. | Health check verifies single database connection string across all services. |
| **EXE-02** | **Duplicate Order Submission** | Medium | High | **HIGH** | Strategy re-evaluating signals and submitting multiple identical orders for the same symbol. | Client UUID (`client_order_id`) enforced with database unique constraint. Idempotent submission returns existing intent. | Database raises unique constraint violation; order engine returns existing record gracefully. |
| **EXE-03** | **Stale Market-Data Execution** | Medium | High | **HIGH** | Placing orders based on cached or lagging ticks during fast market moves. | Quote freshness gate: If market tick age $> 60\text{s}$ (intraday) or $> 900\text{s}$ (swing), order proposal is rejected. | Risk engine verifies `(now - quote_time) <= 60s`. Alerts operator if stream is lagging. |
| **EXE-04** | **Unconfirmed Order Timeout / Drift** | Low | High | **MEDIUM** | Simulator or broker connection drops mid-order; state is left as SUBMITTED without fill confirmation. | Explicit order state machine with state `RECONCILIATION_REQUIRED`. Bounded retry and status query. | Startup reconciliation scans all non-terminal orders against broker/simulator book. |
| **EXE-05** | **Partial Fill Imbalance** | Medium | Medium | **MEDIUM** | Multi-leg orders filling partially, leaving unhedged directional exposure. | Partial fill tracking: If unhedged leg remains unfilled for $> 15\text{s}$, remaining balance is cancelled and filled leg is neutralized. | Execution engine tracks filled quantity per leg. |
| **EXE-06** | **Unsafe Panic Liquidation on Outage** | Medium | High | **HIGH** | System blind-liquidating all positions with market orders during feed drops or API errors, incurring severe slippage. | Safe position policy: On feed drop or risk limit breach, block all NEW entries immediately, but continue managing existing positions via pre-set stop orders and local timers. Do not dump positions blind into dark markets. | Operator alert raised; manual override protocol provided if connectivity cannot be restored. |

---

### 2.3 Market Data & Provider Risks

| Risk ID | Title | Likelihood | Impact | Severity | Root Cause & Failure Mechanism | Mitigation Strategy | Monitoring & Recovery |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **DAT-01** | **Upstox Token Revocation / Expiry** | High | High | **CRITICAL** | Upstox market data token returns HTTP 401 (`UDAPI100050`), halting real-time streaming. | 1. Fail-closed stream gate: immediately halts ingestion and marks feed inactive.<br>2. Graceful error handling with operator alert.<br>3. Documented OAuth 2.0 interactive token refresh. | Stream service halts safely on HTTP 401 and emits `AUTH_EXPIRED` event to observability bus. |
| **DAT-02** | **Cloudflare Anti-Bot WAF Rejection** | High | High | **HIGH** | Cloudflare WAF rejecting default script HTTP clients with Error 1010 (`browser_signature_banned`). | Centralized HTTP request utility enforcing compliant, standard headers across all API calls. | Monitored by pre-flight check in `backend/preflight.py`. |
| **DAT-03** | **Synthetic Data Contamination** | Medium | High | **HIGH** | Silent substitution of simulated sine-wave quotes for live data, skewing backtests and paper trades. | Mandatory data provenance tag (`LIVE_STREAM`, `REST_BACKFILL`, `HISTORICAL_BHAVCOPY`, `SYNTHETIC_FIXTURE`). Red UI banner on synthetic data. | Ingestion engine tags each candle record; risk engine forbids live orders on synthetic data. |
| **DAT-04** | **Bad Tick Spikes / Flash Outliers** | Medium | Medium | **MEDIUM** | Erroneous exchange ticks triggering false breakouts or premature stop-losses. | Tick Sanitizer: Filters price spikes exceeding 3.0% (equities) / 25.0% (options) unless confirmed by 3 consecutive ticks. | `backend/candle_sanitizer.py` logs filtered spikes. |
| **DAT-05** | **Feed Disconnection / Silent Freeze** | Medium | High | **HIGH** | WebSocket drops connection or enters silent deadlock during volatile market hours. | Watchdog heartbeat: If no tick received for any subscribed priority instrument for $> 15\text{s}$ during market hours, feed enters `DEGRADED` mode; blocks new entries. | Auto-reconnect with exponential backoff (1s, 2s, 4s, up to 30s). Alerts operator after 3 failed retries. |

---

### 2.4 Security & Privacy Risks

| Risk ID | Title | Likelihood | Impact | Severity | Root Cause & Failure Mechanism | Mitigation Strategy | Monitoring & Recovery |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC-01** | **Plaintext Credential Leakage** | Medium | Critical | **HIGH** | Committing API tokens, database passwords, or JWT secrets to Git or printing them in logs. | 1. Strict `.gitignore` wall for `.env` and `.key` files.<br>2. Log sanitization filtering keys and passwords.<br>3. Continuous pre-commit secret scanning. | Git history audits verify zero committed credentials. |
| **SEC-02** | **Prompt Injection / LLM Manipulation** | Low | High | **MEDIUM** | External news or social sentiment text injecting prompts that manipulate agent decisions. | 1. AI Gateway treats all external text as untrusted context.<br>2. LLMs have zero order execution or risk override permissions.<br>3. Trade decisions remain 100% deterministic code. | Adversarial checks in `DeepThinker` require strict numerical metrics, ignoring narrative text. |
| **SEC-03** | **Unauthorized Remote API Access** | Low | High | **HIGH** | Public deployment exposing broker execution endpoints without authentication. | 1. Bearer JWT / Cookie session auth required.<br>2. Private personal deployment (no public signup).<br>3. HTTPS termination via reverse proxy/load balancer. | Security pre-flight fails closed in production if security configurations are default. |

---

### 2.5 Operational & Cloud Infrastructure Risks

| Risk ID | Title | Likelihood | Impact | Severity | Root Cause & Failure Mechanism | Mitigation Strategy | Monitoring & Recovery |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **OPS-01** | **Database Outage / Connection Refusal** | Medium | High | **HIGH** | PostgreSQL container stops, connection pool exhausts, or database encounters lockup mid-session. | 1. Fail-closed: risk engine and execution stop immediately if database write fails.<br>2. Threaded connection pool with min=1, max=12 and `pool_pre_ping=True`.<br>3. Docker healthcheck restarts container automatically. | Application logs `OperationalError`, pages operator via webhook, and pauses order creation. |
| **OPS-02** | **Position Manager Process Failure** | Medium | High | **HIGH** | High-frequency 1s position manager crashes due to unhandled exception, leaving open trades unmonitored. | 1. Process isolated as dedicated Docker container with `restart: unless-stopped`.<br>2. Top-level exception loop prevents worker thread exit.<br>3. Redis watchdog heartbeat: `/api/health` reports status `UNHEALTHY` if position manager loop exceeds 5s silence. | Container supervisor auto-restarts within 3s; startup reconciliation re-attaches to open positions. |
| **OPS-03** | **Process Crash Mid-Trade & Restart Drift** | Medium | High | **HIGH** | Machine reboots while positions are open; trailing stops, breakeven levels, or profit locks lost from memory. | State persistence: All active trade parameters (entry, SL, current trailing stop, peak R) are persisted in PostgreSQL `shadow_execution_audits`. On restart, `PositionManager` reloads all `WHERE net_pnl IS NULL` records immediately. | Reconciler asserts memory state equals database state before accepting new signals. |
| **OPS-04** | **Accidental Live Broker Execution** | Low | Critical | **CRITICAL** | Code bug routing paper orders to live broker API. | Triple execution fuse: `LIVE_TRADING_ENABLED=0`, `LIVE_ELIGIBLE=FALSE`, `ledger.live_eligible=FALSE`. Broker adapter order methods raise `NotImplementedError` in paper mode. | Unit test `test_triple_fuse_lock.py` runs on every CI build. |
| **OPS-05** | **Disk Exhaustion from High-Volume Bars** | Low | High | **MEDIUM** | PostgreSQL container runs out of disk space from 1-second candle storage. | 1. Retention policy: 1-second candles pruned after 30 days.<br>2. Automated daily WAL database backups.<br>3. Health probes monitor disk capacity. | Automated cron job executes `VACUUM` and archives old bars. |

---

## 3. Safe Handling of Open Paper Positions During Outages

When an operational failure occurs (feed drop, database outage, or risk breach):

```
FAILURE DETECTED (Feed Disconnected, DB Unreachable, or Limit Breached)
                                │
                                ▼
              [RULE 1: IMMEDIATE INGRESS GATE SHUTDOWN]
              - Reject 100% of new trade proposals immediately.
              - Set system status to HALTED_PROTECTIVE.
                                │
                                ▼
              [RULE 2: EVALUATE POSITION SAFETY STATE]
              - Assess open positions: Are stops already registered in DB?
              - DO NOT blindly fire market liquidation orders into dark/stale feeds.
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼                                               ▼
[FEED DISCONNECTED]                             [DATABASE RECOVERABLE]
- Keep stop-losses in place.                    - Attempt auto-reconnect (up to 30s).
- Run local timer towards 15:15 IST.            - Once restored, run Startup Reconciliation.
- If feed offline at 15:15 IST,                  - Recalculate trailing stops against latest
  tag positions as RECONCILIATION_REQUIRED.       confirmed bar.
```

---

## 4. Risk Governance & Escalation Protocol

1. **Automatic Safe-Stop**: Any critical error halts all new order generation. Open trades are preserved under deterministic local management.
2. **Immediate Alerting**: High-priority webhook dispatch to the operator containing component failure details and open position counts.
3. **Resumption Checklist**: Re-enabling trading requires:
   - Component root cause resolved (e.g. database back up, fresh market data token verified).
   - `/api/health` probes return `200 OK` across all services.
   - Reconciliation audit passes with zero open position mismatch.
   - Operator manual confirmation.
