# TradeMind — Implementation Roadmap (IMPLEMENTATION_ROADMAP.md)

**Date**: 2026-10-12  
**Status**: Multi-Phase Implementation Plan  
**System**: `Trade_Mind` Private Autonomous Trading System

---

## 1. Roadmap Overview & Sequencing Logic

The roadmap is strictly ordered by **safety and dependency constraints**:
1. Foundation & security first (no exposed secrets, clean test harness).
2. Data ingestion before strategies (cannot trade without verified prices).
3. Canonical accounting and risk before execution (cannot submit orders without risk limits and cash tracking).
4. Cash equities first, derivatives later (derivatives require verified cash and execution infrastructure).
5. Cloud deployment and monitoring before live readiness.

```
┌────────────────────────────────────────────────────────────────────────┐
│ PHASE 1: Baseline Audit & Planning Documentation (COMPLETED)           │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 2: Security Hardening, Environmental Isolation & Test Harness   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 3: Unified Market-Data Ingestion & Upstox Provider Verification  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 4: Canonical ₹10,00,000 Paper Portfolio Ledger & Accounting      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 5: Deterministic Risk Engine & Proportional Position Sizing      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 6: Quantitative Strategy Engine & Cash-Equity Paper Trading      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 7: Derivatives Engine (Futures & Options Risk & Sizing)          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 8: Cloud Deployment, Automated Recovery & Observability          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ PHASE 9: Zerodha Live Adapter Boundary & Readiness Certification       │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Phase-by-Phase Implementation Specifications

### PHASE 1: Baseline Audit & Planning Documentation (Status: COMPLETED)
- **Objective**: Conduct comprehensive code audit, verify working features, catalog defects, and establish target architecture and risk register.
- **Deliverables**:
  - `docs/PROJECT_AUDIT.md`
  - `docs/TARGET_ARCHITECTURE.md`
  - `docs/IMPLEMENTATION_ROADMAP.md`
  - `docs/RISK_REGISTER.md`
- **Acceptance Criteria**:
  - Full codebase inspected; 283 tests verified.
  - Defects categorized with file and line evidence.
  - Zero code modifications during audit.

---

### PHASE 2: Security Hardening, Environmental Isolation & Test Harness
- **Objective**: Isolate secrets, fix Cloudflare User-Agent blocks, and eliminate PostgreSQL test brittleness.
- **Tasks**:
  1. Audit `.env` usage; ensure `.env.example` has complete placeholders with zero real credentials.
  2. Implement a centralized HTTP request utility with enforced User-Agent (`TradeMind/1.0`) across all Upstox calls to prevent Cloudflare Error 1010.
  3. Refactor test suite fixtures: mock PostgreSQL connections gracefully so unit tests run cleanly in any local environment.
  4. Ensure all logs and error reporters sanitize tokens, passwords, and API keys.
- **Acceptance Criteria**:
  - `pytest tests/` passes 100% without requiring external database services.
  - All Upstox HTTP requests pass Cloudflare without 403 blocks.
  - Zero secrets committed to git history or printed in logs.
- **Verification Tests**:
  - `tests/test_clean_clone_fallback.py`
  - `tests/test_security_headers.py`

---

### PHASE 3: Unified Market-Data Ingestion & Upstox Provider Verification
- **Objective**: Establish the broker-independent `MarketDataFeed` abstraction, resolve Upstox authentication, and enforce data provenance.
- **Tasks**:
  1. Define `MarketDataFeed` abstract interface in `backend/market_data/`.
  2. Implement `UpstoxV3MarketDataFeed` implementing `MarketDataFeed` with Protobuf WebSocket decoder.
  3. Validate fresh Upstox Analytics Token credentials against `/v3/feed/market-data-feed/authorize`.
  4. Implement data provenance tagging (`LIVE_STREAM`, `REST_BACKFILL`, `HISTORICAL_BHAVCOPY`, `SYNTHETIC_FIXTURE`).
  5. Enforce 3-consecutive-tick spike sanitization and market session filtering (09:15 - 15:30 IST).
  6. Eliminate silent fallback to synthetic mathematical curves; display prominent UI tags when live feed is inactive.
- **Acceptance Criteria**:
  - Upstox WebSocket authenticates successfully and streams ticks.
  - Stale ticks ($> 60\text{s}$) trigger an immediate data staleness warning.
  - Provenance label is transmitted with every candle payload.
- **Verification Tests**:
  - `tests/test_upstox_stream.py`
  - `tests/test_candle_sanitizer.py`
  - `tests/test_market_data_provenance.py`

---

### PHASE 4: Canonical ₹10,00,000 Paper Portfolio Ledger & Accounting
- **Objective**: Eliminate the SQLite/PostgreSQL split-brain by establishing a single PostgreSQL double-entry paper ledger initialized with ₹10,00,000.
- **Tasks**:
  1. Create `paper_portfolio_ledger` and `paper_transaction_journal` in PostgreSQL.
  2. Initialize virtual trading capital with exact ₹10,00,000.00 cash balance.
  3. Implement double-entry transaction posting on every fill:
     - Cash debit/credit.
     - Margin blocked/released.
     - Exact transaction fees (STT, exchange turnover, SEBI, GST, stamp duty) via `backend/transaction_costs.py`.
  4. Integrate portfolio state reconciliation on process startup (rebuilding cash and position ledger).
  5. Unify web server endpoints (`/api/dashboard`, `/api/portfolio`) to query the PostgreSQL ledger directly.
- **Acceptance Criteria**:
  - Paper portfolio starts with ₹10,00,000.00.
  - Restarting the service restores the exact cash balance, margin, and positions without resetting.
  - Every trade incurs verified Indian statutory transaction costs.
- **Verification Tests**:
  - `tests/test_portfolio_ledger_persistence.py`
  - `tests/test_transaction_costs_reconciliation.py`

---

### PHASE 5: Deterministic Risk Engine & Proportional Position Sizing
- **Objective**: Build an independent pre-trade risk engine and enforce proportional sizing based on stop-loss distance.
- **Tasks**:
  1. Implement proportional position sizing formula:
     $$\text{Qty} = \min\left( \left\lfloor \frac{₹2,500}{|\text{Entry} - \text{SL}|} \right\rfloor, \left\lfloor \frac{\text{Cash} \times 0.20}{\text{Entry}} \right\rfloor \right)$$
  2. Implement idempotent order creation via client UUID (`client_order_id`).
  3. Enforce session circuit breakers:
     - Daily loss shutdown: 1.0% (₹10,000) blocks new entries.
     - Drawdown warning: 3.0% (₹30,000) halves risk budget.
     - Emergency shutdown: 5.0% (₹50,000) halts all trading.
  4. Implement order state machine: `PROPOSED -> APPROVED -> SUBMITTED -> OPEN -> FILLED -> CLOSED`.
- **Acceptance Criteria**:
  - No trade risk exceeds 0.25% of capital (₹2,500).
  - Breaching ₹10,000 daily loss immediately rejects subsequent entry proposals.
  - Submitting duplicate order intents returns existing intent without extra exposure.
- **Verification Tests**:
  - `tests/test_risk_management_suite.py`
  - `tests/test_order_state_machine.py`
  - `tests/test_proportional_position_sizing.py`

---

### PHASE 6: Quantitative Strategy Engine & Cash-Equity Paper Trading
- **Objective**: Validate cash-equity algorithmic trading end-to-end using the strategy catalogue and high-frequency position manager.
- **Tasks**:
  1. Standardize strategy signals (`SignalCandidate`) across Trend Following, Breakouts, and Pullbacks.
  2. Integrate microstructure filters: BOS/CHoCH, ASI traps, and Volume Profile.
  3. Connect `DeepThinker` agent for adversarial justification without granting execution authority.
  4. Connect `backend/position_manager.py` 1-second loop for open trade management:
     - Adverse cut (-0.40R in 90s).
     - Stagnation scratch (15m flat cut).
     - Breakeven (+1.10R) and Profit Lock (+1.60R).
     - Mandatory 15:15 IST intraday force-flat.
  5. Run 5 consecutive paper-trading sessions on NSE cash equities.
- **Acceptance Criteria**:
  - Simulated orders execute against verified live/historical market ticks.
  - Zero intraday cash positions remain open past 15:15 IST.
  - Full audit trail recorded in database explaining why trades were taken or rejected.
- **Verification Tests**:
  - `tests/test_live_paper_pipeline.py`
  - `tests/test_position_manager_exits.py`
  - `tests/test_gate_check_regression.py`

---

### PHASE 7: Derivatives Engine (Futures & Options Risk & Sizing)
- **Objective**: Extend the platform to NSE derivatives with dedicated contract specifications, margin calculations, and options risk gates.
- **Tasks**:
  1. Build derivatives contract resolver: Lot sizes, strike intervals, expiries from exchange masters.
  2. Ingest option chain OI, Put-Call Ratio, and Greeks via `backend/upstox_option_chain.py`.
  3. Implement futures margin and gap-risk calculation.
  4. Implement options risk gate:
     - Restrict to defined-risk structures (naked short options strictly prohibited).
     - Bid-ask spread $\le 2.5\%$ filter; open interest $\ge 500$ contracts.
     - Theta decay modeling and DTE (Days to Expiry) guards.
- **Acceptance Criteria**:
  - Lot size constraints strictly enforced (orders in exact lot multiples).
  - Margin requirements calculated and blocked from virtual cash before fill.
  - Illiquid options with wide spreads are rejected by the risk engine.
- **Verification Tests**:
  - `tests/test_derivatives.py`
  - `tests/test_pcr_engine.py`
  - `tests/test_options_risk_gate.py`

---

### PHASE 8: Cloud Deployment, Automated Recovery & Observability
- **Objective**: Package the entire system for autonomous, cloud-hosted 24/7 operation with automated recovery and alerting.
- **Tasks**:
  1. Consolidate `docker-compose.v3.yml` services: `web`, `stream`, `position_manager`, `worker`, `postgres`, `redis`.
  2. Configure structured JSON logging with request tracing.
  3. Set up automated webhook alerts for feed disconnections, risk breaches, and unhandled exceptions.
  4. Implement automated database backup to secure storage.
  5. Validate crash recovery: kill processes mid-trade and confirm state restores safely.
- **Acceptance Criteria**:
  - System launches cleanly with a single command: `docker compose up -d`.
  - Service recovers gracefully after container restart, reconciling open positions.
  - Health check endpoint `/api/health` accurately reports sub-service statuses.
- **Verification Tests**:
  - `tests/test_recovery_reconciliation.py`
  - `tests/test_docker_health_probes.py`

---

### PHASE 9: Zerodha Live Adapter Boundary & Readiness Certification
- **Objective**: Implement the `ExecutionBroker` interface for Zerodha Kite while keeping live execution disabled by default.
- **Tasks**:
  1. Build `ZerodhaExecutionBroker` implementing `ExecutionBroker`.
  2. Maintain the triple safety fuses (`LIVE_TRADING_ENABLED=0`, `LIVE_ELIGIBLE=FALSE`, `ledger.live_eligible=FALSE`).
  3. Create an interactive pre-flight authorization tool requiring explicit operator confirmation before live deployment.
  4. Document the Zerodha migration and regulatory checklist.
- **Acceptance Criteria**:
  - Zerodha adapter compiles and passes interface compliance tests.
  - Live execution remains 100% disabled by default.
  - Comprehensive operational runbook published in `docs/ZERODHA_MIGRATION.md`.
- **Verification Tests**:
  - `tests/test_broker_interface_compliance.py`
  - `tests/test_triple_fuse_lock.py`
