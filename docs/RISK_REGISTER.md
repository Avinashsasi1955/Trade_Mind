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
| **FIN-01** | **Catastrophic Account Drawdown** | Low | Critical | **HIGH** | Sequential strategy losses or unexpected market crash causing portfolio losses exceeding risk tolerance. | 1. Hard daily loss limit of 1.0% (₹10,000) blocks new entries.<br>2. Drawdown warning at 3.0% (₹30,000).<br>3. Hard emergency shutdown at 5.0% (₹50,000).<br>4. -7.32% database trigger locks table. | Real-time equity calculation in `position_manager.py`. Webhook alert triggers immediately upon breach. |
| **FIN-02** | **Disproportionate Trade Sizing** | Medium | High | **HIGH** | Using fixed notional (e.g. ₹50,000) rather than budgeting risk against stop-loss distance. | Formula: $\text{Qty} = \lfloor ₹2,500 / \|P_{entry} - P_{sl}\| \rfloor$. Maximum risk strictly capped at 0.25% of capital (₹2,500). Max single position capped at 20% of cash. | Pre-trade validation rejects orders where $|P_{entry} - P_{sl}| = 0$ or risk $> ₹2,500$. |
| **FIN-03** | **Unhedged Derivative Exposure** | Low | Critical | **HIGH** | Naked short option positions or unhedged multi-leg legs causing unlimited tail-risk losses. | Strict structural veto: Naked short options (CE/PE) are unconditionally blocked. Only long options and defined-risk spreads allowed. | Derivatives risk validator parses contract type and payoff curve before order creation. |
| **FIN-04** | **Overnight Gap Risk (Cash & F&O)** | Medium | High | **HIGH** | Major adverse price gaps between market sessions on open intraday or swing positions. | 1. Mandatory intraday force-flat at 15:15 IST.<br>2. Overnight cash shorts prohibited (SEBI regulatory guard).<br>3. Swing positions require $2.0 \times \text{ATR}$ stop buffer. | `position_manager.py` verifies clock at 15:15 IST and closes all intraday trades at market. |
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

---

### 2.3 Market Data & Provider Risks

| Risk ID | Title | Likelihood | Impact | Severity | Root Cause & Failure Mechanism | Mitigation Strategy | Monitoring & Recovery |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **DAT-01** | **Upstox Token Revocation / Expiry** | High | High | **CRITICAL** | Upstox Analytics Token returning HTTP 401 (`UDAPI100050`), halting real-time streaming. | 1. Graceful error handling with operator alert.<br>2. Token health probe on startup.<br>3. Structured instructions for interactive token refresh. | Stream service halts safely on HTTP 401 and emits `AUTH_EXPIRED` event to observability bus. |
| **DAT-02** | **Cloudflare Anti-Bot Header Block** | High | High | **HIGH** | Cloudflare WAF rejecting default Python HTTP clients with Error 1010 (`browser_signature_banned`). | Centralized HTTP request utility enforcing legitimate `User-Agent: TradeMind/1.0` header across all API calls. | Monitored by pre-flight check in `backend/preflight.py`. |
| **DAT-03** | **Synthetic Data Contamination** | Medium | High | **HIGH** | Silent substitution of simulated sine-wave quotes for live data, skewing backtests and paper trades. | Mandatory data provenance tag (`LIVE_STREAM`, `REST_BACKFILL`, `HISTORICAL_BHAVCOPY`, `SYNTHETIC_FIXTURE`). Red UI banner on synthetic data. | Ingestion engine tags each candle record; risk engine forbids live orders on synthetic data. |
| **DAT-04** | **Bad Tick Spikes / Flash Outliers** | Medium | Medium | **MEDIUM** | Erroneous exchange ticks triggering false breakouts or premature stop-losses. | Tick Sanitizer: Filters price spikes exceeding 3.0% (equities) / 25.0% (options) unless confirmed by 3 consecutive ticks. | `backend/candle_sanitizer.py` logs filtered spikes. |

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
| **OPS-01** | **Container Crash Mid-Session** | Medium | High | **HIGH** | Worker or position manager killed unexpectedly while trades are open. | 1. Docker Compose restart policy (`unless-stopped`).<br>2. All open positions persisted in PostgreSQL.<br>3. Startup state reconciliation recovers open trades within 5s. | Healthcheck alerts operator on container restart. |
| **OPS-02** | **Database Failure / Disk Exhaustion** | Low | High | **MEDIUM** | PostgreSQL container runs out of disk space from 1-second candle storage. | 1. Retention policy: 1-second candles pruned after 30 days.<br>2. Automated daily WAL database backups.<br>3. Health probes monitor disk capacity. | Automated cron job executes `VACUUM` and archives old bars. |
| **OPS-03** | **Accidental Live Broker Execution** | Low | Critical | **CRITICAL** | Code bug routing paper orders to live broker API. | Triple execution fuse: `LIVE_TRADING_ENABLED=0`, `LIVE_ELIGIBLE=FALSE`, `ledger.live_eligible=FALSE`. Broker adapter order methods raise `NotImplementedError` in paper mode. | Unit test `test_triple_fuse_lock.py` runs on every CI build. |

---

## 3. Risk Governance & Escalation Protocol

```
Risk Event Detected (Feed loss, Drawdown breach, or Auth failure)
                      │
                      ▼
        [1. AUTOMATIC SAFE-STOP]
        - Block all new trade entries immediately
        - Preserve all existing stop-losses and targets
        - Maintain autonomous position management loop
                      │
                      ▼
        [2. OPERATOR NOTIFICATION]
        - Dispatch high-priority webhook alert with error stack
        - Record event in audit log table
                      │
                      ▼
        [3. RESUMPTION REQUIREMENTS]
        - Operator inspects issue and applies remediation
        - System health check (/api/health) returns 200 OK
        - Operator manually confirms resumption via authenticated API
```
