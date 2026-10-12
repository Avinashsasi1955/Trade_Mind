# TradeMind — Project Audit Report (PROJECT_AUDIT.md)

**Date**: 2026-10-12  
**Auditor**: Senior Quantitative Systems Architect & Financial Risk Engineer  
**Scope**: Full repository audit of `Trade_Mind` (internal codebase: `NiveshAI`)  
**Audit Objective**: Complete Phase 1 baseline audit evaluating architecture, code paths, persistence, broker integrations, risk controls, and automated tests. Non-destructive; no application code or configurations modified.

---

## 1. System Overview & Architecture Summary

`Trade_Mind` is designed as an autonomous quantitative trading and paper-trading system for Indian financial markets (NSE/BSE). An inspection of the codebase reveals two coexisting sub-architectures:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   SYSTEM A: LEGACY DEMO & WEB UI                       │
│  - Storage: SQLite (`data/nivesh.db`, 13.2 MB)                         │
│  - Server: `backend/server.py` (BaseHTTPRequestHandler)                │
│  - Quotes: `backend/market.py` (Deterministic sine-wave mock quotes)   │
│  - Execution: `backend/broker.py` (`PaperBroker`, naive cash debit)    │
│  - Scope: Visual demo, basic manual order intents, user auth           │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │  (Architectural Disconnect)
┌──────────────────────────────────▼─────────────────────────────────────┐
│                 SYSTEM B: ADVANCED SHADOW TRADING ENGINE               │
│  - Storage: PostgreSQL 17 (17 migrations: `v3_0` through `v3_16`)      │
│  - Stream: `backend/live_stream_service.py` (Upstox Protobuf / Kite)   │
│  - Processing: `backend/market_ingestion_v3.py` (1s/1m/5m/1D candles)   │
│  - Inference: `backend/live_inference.py` (10-model quant ensemble)    │
│  - Position Management: `backend/position_manager.py` (1s tick loop)   │
│  - Multi-Agent Bus: `backend/brains/` (SentinelWatcher, DeepThinker)   │
│  - Infrastructure: Docker Compose v3 (`postgres`, `redis`, `celery`)   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Implemented and Verified Components

The following components were audited and verified through static inspection and automated test execution:

### 2.1 Automated Test Suite
- **Result**: **283 / 283 passed** in 11.83s (`DATABASE_URL="" PYTHONPATH=. ./.venv/bin/pytest tests/ --ignore=tests/test_postgres_runtime.py`).
- **Coverage**:
  - `test_risk_management_suite.py` (12/12 passed): Stop-loss calculation, order frequency limits, kill switch enforcement, maximum position size, quote freshness verification.
  - `test_swing_stops.py` (19/19 passed): ATR-based trailing stops, swing risk bounds, weekend gap risk buffers.
  - `test_profit_harvest.py` (25/25 passed): Dynamic profit harvesting, reversal scoring, lock fractions.
  - `test_candle_sanitizer.py` (4/4 passed): Spike filtering with 3-consecutive-tick confirmation.
  - `test_system.py` (48/48 passed in SQLite isolation): Multi-timeframe analysis, indicator math, security master lookups, AI gateway rate limiting and caching.
  - `test_adaptive_regime_gates.py`, `test_volume_profile.py`, `test_asi_indicator.py`, `test_pcr_engine.py`: Microstructure and quantitative indicators.

### 2.2 Statutory & Broker Transaction Costs (`backend/transaction_costs.py`)
- Verified exact implementation of April 2026 Indian market tariffs:
  - **Securities Transaction Tax (STT)**: 0.1% on delivery equity turnover, 0.025% on intraday sell turnover, 0.05% on futures sell turnover, 0.15% on options sell turnover.
  - **Exchange Turnover Charges**: NSE 0.00307% (Cash) / 0.00183% (Futures) / 0.03553% (Options premium); BSE 0.00375% (Cash) / 0.0325% (Options).
  - **SEBI & IPFT**: 0.0001% SEBI turnover charge, ₹1 per crore IPFT.
  - **GST**: 18% on (Brokerage + Exchange Charges + SEBI Charges + IPFT).
  - **Stamp Duty**: 0.015% on equity delivery buy, 0.003% on intraday buy, 0.002% on futures buy, 0.003% on options buy.
  - **Reconciliation**: `reconcile_contract_note(...)` verifies fee variance and slippage.

### 2.3 Trading Calendar & Market Session Validation (`backend/market_ingestion_v3.py`, `scripts/sync_trading_calendar.py`)
- Verified exchange holiday enforcement using `data/nse_holidays_2026.json`.
- `_is_continuous_market_bar(...)` strictly gates candle aggregation between 09:15 and 15:30 IST on non-holiday weekdays, rejecting off-market ticks.

### 2.4 Multi-Level Execution Safety Fuses (`backend/execution.py`, `backend/config.py`)
- Live trading is guarded by a triple-lock mechanism:
  1. `LIVE_TRADING_ENABLED=0` (environment flag).
  2. `LIVE_ELIGIBLE=FALSE` (model promotion flag).
  3. `system_promotion_ledger.live_eligible=FALSE` (database promotion singleton).
- Verified that `submit_intent(...)` fails closed and cannot place broker orders unless all three fuses are explicitly flipped.

### 2.5 Market Microstructure & Technical Core (`backend/strategies/chart_gate.py`)
- Pure Python deterministic implementation of:
  - Market structure: Break of Structure (BOS), Change of Character (CHoCH), Swing Highs/Lows.
  - Accumulative Swing Index (ASI) and NSE trap detection (`backend/asi_indicator.py`).
  - Volume Profile Value Area (VAH, VAL, POC) calculation (`backend/volume_profile.py`).
  - Put-Call Ratio (PCR) and Gamma Exposure (GEX) advisory filters (`backend/pcr_engine.py`, `backend/gex_engine.py`).

### 2.6 Real-Time Trade & Position Manager (`backend/position_manager.py`)
- Independent 1-second high-frequency monitoring loop for open trades (`WHERE net_pnl IS NULL`):
  - Fast adverse cut: -0.40R within 90 seconds of entry.
  - Stagnation tighten: Stops tightened to -0.35R at 300 seconds if gain < +0.20R.
  - Stagnation scratch: 900 seconds (15 minutes) flat-trade exit.
  - Breakeven trigger at +1.10R; profit lock trigger at +1.60R (locks +1.00R).
  - Trailing giveback: 0.40R protected trailing stop.
  - Forced end-of-day liquidation at 15:15 IST.

### 2.7 AI Research & Supervisory Boundaries (`backend/ai_gateway.py`, `backend/brains/`)
- LLM interactions are strictly restricted to narrative generation and chat assistance.
- Quantitative analysis and pre-trade deliberation (`backend/brains/deep_thinker.py`) are 100% deterministic code.
- All LLM calls pass through `AIGateway` with per-user rate limiting (10 req/min), caching (600s), and a circuit breaker that fails back to deterministic local rule text upon any exception.

---

## 3. Simulated, Incomplete, or Disconnected Components

### 3.1 Upstox Market-Data Authentication Failure
- **Finding**: The Upstox token configured in `.env` (`UPSTOX_ANALYTICS_TOKEN`) returns `HTTP 401 Unauthorized` across all endpoints:
  - Token claims: Issued `2026-07-13T18:05:47Z`, Expires `2027-07-13T22:00:00Z`, Subject `2YCFQG`.
  - Upstox API response: `{"status":"error","errors":[{"errorCode":"UDAPI100050","message":"Invalid token used to access API"}]}`.
  - The token was revoked or invalidated on the Upstox gateway. Live streaming and option chain ingestion fail on startup.

### 3.2 Cloudflare Anti-Bot Blocking (`api.upstox.com`)
- **Finding**: Upstox REST endpoints fronted by Cloudflare reject standard Python client headers:
  - Error: `403 Forbidden` with `Cloudflare Error 1010: Access denied (browser_signature_banned)`.
  - Calls succeed only when explicitly configured with a non-standard User-Agent header (e.g., `TradeMind/1.0`).

### 3.3 The "Split-Brain" Database & Portfolio Ledger
- **Finding**:
  - The Web Dashboard (`frontend/app.js` and `backend/server.py`) reads user portfolios from SQLite `data/nivesh.db`.
  - The Shadow Trading execution pipeline (`backend/live_inference.py`, `backend/position_manager.py`) writes trades to PostgreSQL `shadow_execution_audits`.
  - The PostgreSQL shadow database has **no persistent cash ledger**. Fills record realized P&L, but do not deduct cash, track margin, or calculate total portfolio equity starting from ₹10,00,000.
  - Trades executed by the background shadow manager never appear on the web dashboard trade book.

### 3.4 Sizing Decoupled from Portfolio Equity
- **Finding**: In `backend/live_inference.py`, position quantity is derived from a static environment variable:
  `self.paper_notional = Decimal(os.getenv("NIVESH_PAPER_NOTIONAL", "50000"))`.
  - Quantity is calculated as `int(50000 / price)` rather than budgeting 0.25% of ₹10,00,000 (₹2,500) divided by the stop-loss distance.
  - Violates the requirement for dynamic risk-proportional position sizing based on portfolio equity.

### 3.5 Synthetic Sine-Wave Fallback in Web Server
- **Finding**: `backend/market.py` generates synthetic mock prices using `math.sin((phase + idx * 3) * 0.37) * 0.0015` when live data is unavailable.
  - This data is rendered in `/api/market/update` without explicit UI labeling as "SYNTHETIC / SIMULATED", risking confusion with real market prices.

### 3.6 Zerodha Broker Adapter Incomplete
- **Finding**: `backend/zerodha_adapter.py` is read-only. Methods for placing, modifying, or cancelling orders raise `NotImplementedError`. No abstract adapter interface links `UpstoxAdapter` and `ZerodhaAdapter`.

### 3.7 Test Suite Environment Dependency
- **Finding**: 7 tests fail if `DATABASE_URL` is set to `localhost:5433` in `.env` while PostgreSQL is not running. Tests lack automated mock fixtures to fallback gracefully to isolated in-memory test databases.

---

## 4. Defect Severity Breakdown

| ID | Severity | Category | Description | Source File & Line |
| :--- | :--- | :--- | :--- | :--- |
| **DEF-01** | **CRITICAL** | Data Ingestion | Upstox Analytics Token revoked (`UDAPI100050`); market feeds fail on startup. | `backend/config.py:83`, `.env:73` |
| **DEF-02** | **CRITICAL** | Financial Integrity | Missing canonical ₹10,00,000 paper cash/margin ledger in shadow execution engine. | `backend/live_inference.py:199`, `backend/position_manager.py` |
| **DEF-03** | **HIGH** | Architecture | Split-brain database divergence (SQLite UI vs PostgreSQL shadow workers). | `backend/server.py:16`, `backend/live_inference.py:31` |
| **DEF-04** | **HIGH** | Financial Risk | Fixed ₹50,000 notional sizing instead of 0.25% equity-at-risk per trade. | `backend/live_inference.py:199` |
| **DEF-05** | **HIGH** | Operational | Cloudflare WAF bans default Python User-Agent headers (Error 1010). | `backend/upstox_adapter.py:38`, `backend/upstox_backfill.py` |
| **DEF-06** | **HIGH** | Data Integrity | Synthetic sine-wave prices served without prominent live vs. simulated tagging. | `backend/market.py:32-48` |
| **DEF-07** | **MEDIUM** | Code Quality | No common abstract base class for broker and market data adapters. | `backend/upstox_adapter.py`, `backend/zerodha_adapter.py` |
| **DEF-08** | **MEDIUM** | Testing | Test suite fails on unmocked PostgreSQL connection when Docker is offline. | `tests/test_postgres_runtime.py:5`, `tests/test_system.py:355` |
| **DEF-09** | **LOW** | Operations | Hardcoded local ports (5433 for Postgres, 4173 for web) in default configs. | `docker-compose.v3.yml:33`, `backend/config.py:48` |

---

## 5. Audit Conclusion

The algorithmic, mathematical, and risk-management cores of `Trade_Mind` (`position_manager.py`, `transaction_costs.py`, `chart_gate.py`, `deep_thinker.py`) are sophisticated, rigorous, and verified. 

However, the application cannot operate as a reliable personal trading system until:
1. The Upstox authentication layer is repaired with a fresh token and Cloudflare-compliant headers.
2. The split-brain architecture is collapsed into a single PostgreSQL persistence layer.
3. A true double-entry ₹10,00,000 paper portfolio ledger with dynamic 0.25% risk-proportional sizing is established.
4. Clean abstract broker and market-data interfaces decouple strategy logic from broker specifics.
