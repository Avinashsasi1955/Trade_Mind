# TradeMind — Living Project Status (STATUS.md)

*Generated Automatically by `scripts/generate_status.py`*  
*Generation Timestamp: `2026-10-08 19:04:30 IST`*  
*Exact Git Commit Hash: `20f5f3eb5fa7449210cc59bf6f114bc9f94f4c39`*  
*Canonical Single Source of Truth for Architecture, Model Benchmarks & Operational Ground Truth*

---

## 1. System Health & Test Suite Status
* **Full Unit & Integration Suite**: **184 / 184 passing** (`Ran 184 tests in 10.055s — OK (skipped=1)`).
* **Audit Persistence Integrity**: Enforced via atomic database update (`WHERE id = :id AND net_pnl IS NULL`).
* **Exit Concurrency**: `PositionManager` verified as sole live exit writer. Live inference loops (`_manage_open_trade_risk` and `_close_due`) are confirmed disabled in code (`active_trade_manager_enabled=False`, `inference_close_due_enabled=False`).
* **MTF Engine Hardening**: Patched fail-open bug in `backend/mtf_confirmation.py`. Sparse candles and exceptions fail **closed** (`checks = False`, `verdict = "INSUFFICIENT_DATA"`). Replaced crude high/low structure check with full `_structure_analysis(bars)` BOS/CHoCH engine.
* **GEX Role Clarified**: Verified as **advisory / sizing modulation only** (`stop_multiplier`, `size_multiplier`, +70 quality floor in volatility defense). GEX is NOT a hard veto and does not block trades.
* **Candle Sanitizer**: Verified live threshold is **3.0% for equities** and **25.0% for options** with 3-consecutive-tick confirmation.

---

## 2. Machine Learning Ground Truth (Queried Directly from DB)

### A. Real Live Paper Execution Book (`shadow_execution_audits`)

| Model Version | Status / Role | Closed Trades | Wins / Losses | Win Rate | Profit Factor | Gross Profit | Gross Loss | Fees Drag | Net Realised P&L | Date Range (Sessions) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `direction-v2.5-20260703T210440115936Z` | Retired Baseline | 590 | 178W / 412L | **30.17%** | **0.34** | ₹5516.23 | ₹16421.59 | ₹3903.75 | **₹-10905.36** | 2026-07-20 to 2026-09-29 (39d) |
| `direction-v3.0-20260930T121513227395Z` | Active Paper Model | 19 | 11W / 8L | **57.89%** | **1.43** | ₹18434.52 | ₹12886.24 | ₹715.64 | **₹5548.28** | 2026-10-05 to 2026-10-08 (4d) |

> **Critical Reconciliation Finding**:
> The figures previously labeled as active performance (`56.83% Win Rate / PF 1.22 / +18.24% Net Return`) were produced on **2026-09-29T19:57:22** by `walk_forward_validate()` running a **historical 6-fold backtest over Aug 2023–Jun 2026** (SQLite `validation_runs` Run 13). They were NOT live paper trades.
> The actual live paper track record for `direction-v2.5` in PostgreSQL is **30.17% Win Rate, PF 0.34, -₹10,905.36 net drawdown across 39 sessions** (only 2 winning days). `direction-v3.0` since deployment has produced **64.71% Win Rate, PF 1.61, +₹7,008.66 across 17 trades**.

---

### B. Holdout Log-Loss Investigation (0.6937 vs. 0.3810 / 85.10% Accuracy)

* **True v2.5 Untouched Holdout (Postgres `model_versions` ID 11)**:
  * Evaluated on **116,358 samples** covering `2025-07-13` to `2026-06-24`.
  * Accuracy: **51.47%** | Log-Loss: **0.6937** | Recall: **26.99%** | Precision: **54.55%**.
  * Consistent across all 4 July reports (`ML_V2_5_REPORT.md`, `V3_2_FULL_REPORT.md`, etc.).
* **Source of the 85.10% / 0.3810 Claim**:
  * Traced directly to Model ID 21 (`direction-v3.0` trained on `intraday_micro_v1`).
  * In that dataset, **84–86% of all samples belong to class 0 (no-trade / flat)**.
  * The classifier collapsed into predicting class 0 exclusively (**Holdout Recall: 0.0%, Holdout Precision: 0.0%**).
  * Predicting class 0 for every bar achieved **84.02%–86.32% accuracy and 0.3902 log-loss purely as a zero-recall dummy classifier**.
  * This dummy metric was mistakenly copied into `STATUS.md`. It reflects zero predictive edge and is invalidated for live promotion decisions.

---

### C. The `v2.6` Lineage Clarification
* **July 4 Model (Postgres ID 12)**: `direction-v2.6-20260704T053159679116Z` (Single HistGradientBoosting, underperformed v2.5 with PF 1.10, explicitly **rejected**).
* **Sept 23 Model (Postgres ID 20)**: `direction-v2.6-20260923T184822215949Z` (3-model soft-voting ensemble of Logistic Regression + HGB + DNN MLP with asymmetric weights).
* To prevent version collisions across models trained 11 weeks apart, the Sept 23 model is cataloged as `v2.6-ensemble`.

---

## 3. Verified Runtime Parameters vs. Documentation Stale Matrix

| Parameter Name | Live Runtime Value (`.env` & Code) | Source Code Origin | Conflicting Documented Values | Stale Documents | Status / Rationale |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`NIVESH_SHADOW_MIN_PROFESSIONAL_RR`** | **1.90** (tiered: best=1.50, neutral=1.80, worst=2.00) | `.env:159`, `live_inference.py:264` | "Hard 2.00 R:R mandatory non-negotiable" | `rules.md` | **1.90 active** (tiered 1.50–2.00 to avoid Zero-Trade Trap). |
| **`breakeven_trigger_r`** | **+1.10R** | `position_manager.py:57`, `.env:161` | +0.70R or +0.75R | `MASTER_COMPREHENSIVE_PLAN.md`, `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **+1.10R active** to avoid cutting winners prematurely. |
| **`profit_lock_trigger_r`** | **+1.60R** | `position_manager.py:58`, `.env:162` | +1.50R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4`, `rules.md` | **+1.60R active**. |
| **`profit_lock_guaranteed_r`** | **+1.00R** | `position_manager.py:59`, `.env:163` | +0.75R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **+1.00R locked** once +1.60R is reached. |
| **`trailing_trigger_r`** | **+1.50R** | `position_manager.py:60`, `.env:164` | +2.00R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **+1.50R active**. |
| **`trailing_giveback_r`** | **0.40R** | `position_manager.py:61`, `.env:165` | 0.50R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **0.40R active** (protects 60% of peak excursion beyond trigger). |
| **`stagnation_scratch_seconds`** | **900.0s (15 min)** | `position_manager.py:68` | 20 minutes | `MASTER_COMPREHENSIVE_PLAN.md` | **15 minutes active** (tightens flat trades at 0.25R). |
| **`adverse_cut_threshold_r`** | **0.40R in 90s** | `position_manager.py:64` | 0.35R | Historical architectural notes | **0.40R active** (fast cut on immediate counter-trend). |
| **`daily_circuit_breaker`** | **₹2000 max daily loss / 2 daily losses / 3 consecutive losses** | `.env:186,190`, `live_inference.py:219,229` | Disagreed across 3 docs (₹1,500 vs ₹2,000 vs 3-4 losses) | Legacy specs | **₹2,000 / 2 session losses halts first in practice**. |
| **`account_drawdown_trigger`** | **-7.32% hard DB stop** | `v3_0_production.sql:81`, `validation_engine.py:331` | -8% in test reports | `INFRASTRUCTURE_STATUS.md` | **-7.32% catastrophic SQL trigger**. |

---

## 4. Rejection Funnel & Zero-Trade Trap Diagnostic

### A. Last 10 Trading Days Acceptance Rates (Direct DB Query)

| Trade Date | Total Candidates Evaluated | Accepted Candidates | Acceptance Rate (%) | Zero-Trade Trap Flag |
| :--- | :--- | :--- | :--- | :--- |
| 2026-10-08 | 330 | 4 | 1.21% | Normal Filtered |
| 2026-10-07 | 396 | 5 | 1.26% | Normal Filtered |
| 2026-10-06 | 525 | 8 | 1.52% | Normal Filtered |
| 2026-10-05 | 648 | 6 | 0.93% | Normal Filtered |
| 2026-09-10 | 283 | 27 | 9.54% | Normal Filtered |
| 2026-09-09 | 114 | 8 | 7.02% | Normal Filtered |
| 2026-09-08 | 434 | 4 | 0.92% | Normal Filtered |
| 2026-09-03 | 637 | 1 | 0.16% | Normal Filtered |
| 2026-09-02 | 163 | 15 | 9.2% | Normal Filtered |
| 2026-09-01 | 426 | 0 | 0.0% | ⚠️ **ZERO ACCEPTED** |

### B. Gate Architecture: Scored Confluence Ensemble vs. Serial Boolean AND

To resolve the **Zero Trade Trap** identified in `MASTER_COMPREHENSIVE_PLAN.md §6`, the entry decision pipeline in `backend/live_inference.py` supports a **10-Component Weighted Ensemble Scorer** controlled via feature flag.

#### 1. Runtime Feature Flags
* **`NIVESH_SHADOW_USE_WEIGHTED_ENSEMBLE`**: Default `0` (Off, runs legacy sequential AND cascade). Set to `1` to activate scored confluence.
* **`NIVESH_SHADOW_WEIGHTED_ENSEMBLE_MIN_SCORE`**: Default `75.0` (out of 100.0 points required for trade permission).

#### 2. Gate Separation Principle
* **Hard Risk / Safety Vetoes (Mandatory AND)**:
  * **Morning Cooloff**: Block trades before 09:30 IST (`morning_cooloff_ist`).
  * **Execution Route**: Reject if instrument is non-executable or missing strike mapping (`not target`).
  * **Portfolio Underlyings**: Reject if cooldown active, max session losses reached, or max active trades per underlying reached.
  * **Directional Concentration Cap**: Max directional imbalance enforced across session.
  * **Sector Allocation & Cooldowns**: Max concurrent sector exposure and intraday sector cooldowns.
  * **Option Risk Loss Ceiling**: Hard monetary loss cap (`max_option_loss_rupees`) scaled stop loss.
* **Signal Quality Ensemble (Scored 0–100 Points, Min 75 Required)**:
  * Non-binary point assignment: borderlines contribute partial positive points (never negative).

| Signal Quality Component | Max Weight | Evaluation Logic & Point Allocation | Rationale |
| :--- | :--- | :--- | :--- |
| **`chart_gate_quality`** | **15 pts** | Full pass = 15.0 pts; Partial quality > 0 = `quality * 0.15` (up to 15); Sparse candle/data failure = 0 pts. | Core chart pattern formation is the primary setup anchor. |
| **`risk_reward_margin`** | **15 pts** | $R:R \ge \text{target}$ = 15.0 pts; $1.20 \le R:R < \text{target}$ = proportional 0–15 pts; $<1.20$ = 0 pts. | Assures mathematical expectancy without discarding 1.85 R:R trades on a 1.90 cutoff. |
| **`net_edge_margin`** | **15 pts** | Net edge $\ge \text{target}$ = 15.0 pts; $0 < \text{edge} < \text{target}$ = proportional 0–15 pts; $\le 0$ = 0 pts. | Protects positive post-slippage edge while tolerating slight margin variance. |
| **`mtf_alignment`** | **15 pts** | 3+ aligned timeframes = 15.0 pts; 2 aligned = 10.0 pts; 1 aligned = 5.0 pts; Strong counter-trend = 0 pts. | Higher timeframe flow confirmation avoids counter-trend whipsaws. |
| **`nifty_index_trend`** | **10 pts** | Market beta aligned = 10.0 pts; Neutral trend = 7.0 pts; Stock alpha override = 5.0 pts; Counter-trend = 0 pts. | Reduces systemic beta drag on equity/derivative setups. |
| **`vwap_overextension`** | **10 pts** | Price within normal volatility bands = 10.0 pts; Extreme overextension = 0 pts. | Prevents buying at intraday liquidity tops or shorting at cycle exhaustion. |
| **`asi_divergence`** | **5 pts** | No Accumulative Swing Index trap = 5.0 pts; ASI liquidity sweep detected = 0 pts. | Filters false breakouts engineered by institutional sweep orders. |
| **`wyckoff_rvol`** | **5 pts** | Relative volume confirms price expansion = 5.0 pts; Volume divergence = 0 pts. | Confirms institutional participation in breakout direction. |
| **`volume_profile_value_area`** | **5 pts** | Entry favorably placed relative to VAH/VAL = 5.0 pts; Value exhaustion = 0 pts. | Ensures favorable trade location within the intraday value distribution. |
| **`adx_chop`** | **5 pts** | Non-choppy regime ($ADX > 20$ / clear trend) = 5.0 pts; Range compression = 0 pts. | Penalizes momentum strategies inside tight chop zones. |
| **TOTAL** | **100 pts** | **Acceptance Threshold: $\ge 75.0$ points** | **Enables strong 8/10 setups to clear without single-gate trap.** |

#### 3. Backtest Verification (Last 10 Trading Sessions: 3,956 Candidates)
* **Baseline Serial AND Acceptance**: **78 / 3,956 accepted (2.0%)** (zero-trade days on 2026-09-01 and near-zero on 2026-09-03).
* **Weighted Ensemble ($\ge 75$) Acceptance**: **1,105 / 3,956 accepted (27.9%)**.
* **Zero-Trade Day Rescue**:
  * `2026-09-01`: Baseline accepted **0 trades (0.0%)** $\rightarrow$ Weighted Ensemble accepted **77 trades (18.1%)** while safely vetoing 35 candidates on capital/portfolio limits.
  * `2026-09-03`: Baseline accepted **1 trade (0.2%)** $\rightarrow$ Weighted Ensemble accepted **177 trades (27.8%)** while safely vetoing 76 on safety constraints.
* **Safety Invariant**: All 506 candidates that breached hard safety limits (portfolio caps, sector concentration, execution route, option risk ceiling) remained strictly rejected.


---

## 5. Documentation Consolidation Compliance

* **Root Directory Boundary**: Maintained strictly at **3 files**:
  1. `STATUS.md` (this living script output)
  2. `MASTER_COMPREHENSIVE_PLAN.md`
  3. `README.md`
* **Archived Sprawl**: All 26 legacy reports and duplicate specifications are consolidated in `docs/archive/` with full indexing in `docs/archive/INDEX.md`.
