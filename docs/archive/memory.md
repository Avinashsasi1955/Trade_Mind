# Nivesh AI — Persistent Architectural & Agent Memory (memory.md)

> **Document Code:** MEMORY-NIVESH-01  
> **Status:** Active Reference & Knowledge Base  
> **Last Synchronized:** 2026-10-08 11:51 IST  

---

## 1. Key Architectural Decisions (Why Things Are Built This Way)

### 1.1 Why `shadow_execution_audits` is the Single Source of Truth
- **Decision:** Never read paper trades from the legacy demo portfolio tables (`user_holdings`, `demo_trades`).
- **Rationale:** Legacy demo tables contain unverified synthetic holdings from early UI testing. Reading from them corrupts forward model validation evidence. All live paper evidence must come strictly from `shadow_execution_audits` joined with `instrument_master`.

### 1.2 Why the Retroactive Profit Retrieval Bug Occurred & How It Was Killed
- **Decision:** Bar replay in [`backend/position_manager.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/position_manager.py) must strictly break immediately upon touching Take Profit, Profit Spike Capture (+2.5R), or Trailed Stop Loss.
- **Rationale:** Previously, code was checking `if not is_multi_day:` before breaking, and later scanning the full day's history to pick a historical peak profit candidate. In reality, a trade cannot "retroactively" exit at the high of the day hours after it happened; it must exit in chronological real time on the bar that hits the target.

### 1.3 Why Stop Loss Must Be Persisted to PostgreSQL Column `stop_loss_price`
- **Decision:** Whenever `PositionManager` trails a stop loss, it executes:
  ```sql
  UPDATE shadow_execution_audits 
  SET stop_loss_price = :sl, improvement_note = :note 
  WHERE id = :id AND net_pnl IS NULL;
  ```
- **Rationale:** Previously, only `improvement_note` JSON was being updated. SQL queries in [`backend/service.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/service.py) select `a.stop_loss_price`. If the column is not updated, the database and UI table continue showing the unadjusted initial stop loss forever.

### 1.4 Why `today_items` Must Include `item["is_open"]`
- **Decision:** In [`backend/service.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/service.py):
  ```python
  today_items = [
      item for item in items
      if item["is_open"]
      or (item.get("signal_at") and item["signal_at"].astimezone(IST).date() == market_date)
      or (item.get("exit_at") and item["exit_at"].astimezone(IST).date() == market_date)
  ]
  ```
- **Rationale:** If `today_items` only filters by `item["signal_at"].date() == market_date`, any open swing position opened on a prior day (e.g., INFY entered yesterday) is completely excluded from today's summary. This causes today's open count and unrealized P&L to miss prior-day floating swing trades.

### 1.5 Why `holding_days` is Calculated Dynamically
- **Decision:** In [`backend/service.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/service.py) and PostgreSQL:
  ```python
  holding_days = max(0, (market_date - sig_date).days)
  ```
  ```sql
  UPDATE shadow_execution_audits 
  SET holding_days = GREATEST(0, ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - (signal_at AT TIME ZONE 'Asia/Kolkata')::date))
  WHERE audit_status = 'RECONCILED' AND net_pnl IS NULL;
  ```
- **Rationale:** The database default for `holding_days` was `0`. The UI renders `Day ${days + 1}/5`. Since `0 + 1 = 1`, multi-day trades like INFY entered on Oct 07 showed `Day 1` on Oct 08. Calculating elapsed market days gives `1`, which renders correctly as **Day 2/5**.

### 1.6 Why Native ES Modules and No Heavy Frameworks
- **Decision:** Frontend is composed of native ES2022 JavaScript modules (`utils.js`, `metrics.js`, `sse.js`, `trade_book.js`) with vanilla CSS.
- **Rationale:** In high-frequency trading dashboards with continuous Server-Sent Events updates, heavy virtual DOM reconciliation libraries (React/Vue) cause frame drops and garbage collection pauses. Native DOM mutation runs in $<16\text{ ms}$, ensuring 60 fps responsiveness.

---

## 2. Infrastructure & Environment Facts

- **Web Container:** `design-a-modern-distinctive-ui-ux-web-1`
- **Frontend Port:** `4173` (`http://127.0.0.1:4173/`)
- **PostgreSQL Port:** `5432` (`postgresql://postgres:postgres@db:5432/tradeready`)
- **Redis Port:** `6379` (`redis://redis:6379/0`)
- **Python Virtual Environment:** `.venv/bin/python` (Python 3.9/3.12 compatible)
- **Local Timezone:** Indian Standard Time (IST, UTC+05:30)
- **Primary Market Data Provider:** Upstox Market Data Feed V3 (with Zerodha Kite fallback)
- **Test Command:** `.venv/bin/python -m unittest discover -s tests -p "test_*.py"` (184 tests, 100% passing)
