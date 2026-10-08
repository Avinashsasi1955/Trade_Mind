# Nivesh AI — System Rules & Operational Invariants (rules.md)

> **Document Code:** RULES-NIVESH-01  
> **Status:** Active & Mandatory  
> **Classification:** Development Constraints, Architectural Invariants & Agent Guidelines  

---

## 1. Absolute Operational Rules (Non-Negotiable)

### 1.1 Git Execution Constraint
- **NEVER execute automated Git commands** (`git add`, `git commit`, `git push`, `git checkout`, etc.).
- All code and document modifications must be written directly to the filesystem. The user manages git versioning and commits manually.

### 1.2 Broker Execution & Safety Fence
- **Zero Real Broker Order Placement:** The trading execution pipe is hard-locked in simulated paper-trading mode (`LIVE_ORDERS_ALLOWED = False`). Under no circumstances should real orders be routed to live broker API endpoints (Upstox / Zerodha).
- All simulated executions must be recorded as immutable rows in `shadow_execution_audits`.

### 1.3 Fee Friction & Reality Check
- Every simulated trade must deduct realistic transaction fees:
  $$\text{Fees} = \text{STT} + \text{Exchange Turnover} + \text{SEBI Charges} + \text{Stamp Duty} + \text{GST (18%)}$$
- Phantom zero-commission paper profits are strictly prohibited.

---

## 2. Trading Strategy & Risk Management Rules

### 2.1 The Mandatory 2.0 R:R Rule
- Every candidate trade must satisfy:
  $$\frac{|\text{Take Profit} - \text{Entry}|}{|\text{Entry} - \text{Stop Loss}|} \ge 2.00$$
- Low reward-to-risk scalps ($R:R < 2.0$) must be automatically rejected by the Risk Brain to prevent fee drag and adverse churn.

### 2.2 Anti-Churn & Daily Frequency Limit
- A hard maximum ceiling of **40 trades per day** is enforced per trading session.
- Once the 40-trade limit is reached, candidate evaluation terminates immediately for the remainder of the session, regardless of model confidence.

### 2.3 Strict Chronological Exit Rule (No Retroactive Retrieval)
- Bar replay and live monitoring must evaluate exits in strict forward chronological order.
- When Take Profit (+2.0R), Profit Spike Capture (+2.5R), or Trailed Stop Loss is hit on any bar, the trade must exit **immediately on that bar**.
- Retroactive searching or picking the highest peak price of the session is strictly forbidden.

### 2.4 Multi-Leg Atomic Spread Rule
- Multi-leg options spread baskets (`spread_basket_id` and `paired_primary_audit_id`) must be executed and exited atomically.
- If one leg of a spread hits its exit condition, all sibling legs belonging to that basket must close immediately in the same transaction. Naked options exposure is an invariant violation.

### 2.5 Dual-Mode Holding Rules
- **Intraday (MIS):** Maximum holding time of 75 minutes. All intraday positions must be squared off automatically at **15:20 IST** (`SESSION_FORCE_FLAT`).
- **Swing (CNC / Defined-Risk Spreads):** Multi-day holding capacity up to 5 market days. Holding days must be dynamically tracked ($D_1 \to D_5$) based on IST market dates, with 2.0x ATR dynamic trailing stops.

---

## 3. Engineering & Code Architecture Rules

### 3.1 Backend Rules (Python / ASGI)
- **Runtime:** Python 3.12 running under Uvicorn ASGI.
- **Asynchronous Concurrency:** Database queries and CPU-heavy ML inferences must run in thread pools or non-blocking coroutines so WebSocket tick ingestion is never delayed.
- **Database Persistence:** When stop losses trail up, `stop_loss_price` must be updated directly in the PostgreSQL column alongside `improvement_note`.
- **Active Book P&L:** P&L aggregation for the current active book must include all currently open positions (`is_open = True`), ensuring prior-day floating swing trades are always counted.

### 3.2 Frontend Rules (JavaScript / CSS)
- **Zero Framework Bloat:** Do not introduce heavy frontend frameworks (React, Vue, Angular) or utility toolchains (TailwindCSS) unless explicitly mandated.
- **Native ES Modules:** Frontend code is organized into native ES modules (`utils.js`, `metrics.js`, `sse.js`, `trade_book.js`).
- **Institutional Aesthetics:** Maintain high-contrast institutional dark theme (`#090d16` background, `#10b981` positive, `#f43f5e` negative, `#06b6d4` quant, `#c084fc` swing).
- **60 FPS Performance:** UI updates via Server-Sent Events must run under 16 ms per frame without DOM freezing.
