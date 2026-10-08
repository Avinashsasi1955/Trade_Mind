# TradeMind — Living Project Status (STATUS.md)

*Last Updated: 2026-10-08 12:30 IST*  
*Living Single Source of Truth for Architecture, Model Benchmarks & Operational Ground Truth*

---

## 1. System Health & Test Suite Status
* **Full Unit & Integration Suite**: **148 / 148 tests passing** (`Ran 148 tests in 15.928s — OK (skipped=3)`).
* **Audit Persistence Integrity**: Verified with atomic concurrency lock on exits (`WHERE net_pnl IS NULL` atomic guard in `record_shadow_exit`).
* **Exit Concurrency**: PositionManager is established as sole exit writer; competing exit loops in `live_inference.py` deactivated.
* **BLAS & Apple Silicon Runtime**: Spurious Accelerate/OpenBLAS runtime matmul warnings eliminated via `_predict_batch`.
* **MTF Triple Confirmation Engine**: **Fixed (Oct 8, 2026)**. Neutral fail-open vulnerability patched in `backend/mtf_confirmation.py`. Missing or sparse candle history now strictly fails closed (`checks = False`, `verdict = "INSUFFICIENT_DATA"`), preventing unearned 100% scores on illiquid symbols.
* **Phase B Step 2 (Index F&O Mapping)**: ✅ Complete. NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY mapped with Black-Scholes Greeks, SEBI margin benefits, 2026 NSE holiday calendar.
* **Phase C.1 (3-Regime Router)**: ✅ Complete in `backend/regime_router.py`.
* **Phase C.2 (Autonomous Spider Bot Engine)**: ✅ Complete in `backend/spider_bot.py`.
* **Phase C.3 (Dual-Tier Intelligence Memo Package)**: ✅ Complete in `backend/intelligence_memory.py`.
* **Phase C.4 (20-Validation RL Action Controller)**: ✅ Complete in `backend/ml/rl_policy_agent.py` (Mode `RL_ACTIVE`).

---

## 2. Machine Learning Ground Truth & Direct Reconciliation

### A. Performance Reconciliation: Historical Backtest vs. Real Live Paper Execution

| Metric | `direction-v2.5` Historical Backtest (6-Fold Walk-Forward) | `direction-v2.5` Real Live Paper Shadow Trading (`shadow_execution_audits`) | `direction-v3.0` Real Live Paper Shadow Trading (`shadow_execution_audits`) |
| :--- | :--- | :--- | :--- |
| **Model Version** | `direction-v2.5-20260701T073458745271Z` | `direction-v2.5-20260703T210440115936Z` | `direction-v3.0-20260930T121513227395Z` |
| **Data Source / Query** | `walk_forward_validate()` (SQLite `validation_runs` Run 13) | Direct SQL on `shadow_execution_audits` | Direct SQL on `shadow_execution_audits` |
| **Evaluation Type** | Historical Simulation (Aug 2023 – Jun 2026) | **Real-Time Paper Execution** (Jul 20 – Sep 29, 2026) | **Real-Time Paper Execution** (Oct 05 – Oct 07, 2026) |
| **Total Closed Trades** | 542 | **590** (178 wins / 412 losses) | **17** (11 wins / 6 losses) |
| **Win Rate** | **56.83%** | **30.17%** | **64.71%** |
| **Profit Factor** | **1.22** | **0.34** | **1.61** |
| **Gross Profit** | Simulated | ₹5,516.23 | ₹18,434.52 |
| **Gross Loss** | Simulated | ₹16,421.59 | ₹11,425.86 |
| **Total Fees Incurred** | Idealized cost model | **₹3,903.75** (Zerodha statutory drag) | **₹557.63** |
| **Net Realised P&L** | **+18.24%** (capital model) | **-₹10,905.36** | **+₹7,008.66** |
| **Profitable Days / Sessions**| N/A | **2 profitable sessions out of 39** (5.1%) | **2 profitable sessions out of 3** (66.7%) |
| **Status / Role** | Archived Historical Walk-Forward Baseline | Deactivated Baseline (Heavy fee drag & probe churn) | **Active Paper Model** |

> **Critical Clarification**: The figures `Win Rate 56.83% / Profit Factor 1.22 / Net Return +18.24%` previously shown in STATUS.md were generated on 2026-09-29T19:57:22 by `walk_forward_validate()` running across 6 expanding walk-forward folds over 2023–2026 data. They were **not** live paper performance. The actual live paper book for v2.5 experienced a 30.17% win rate, 0.34 profit factor, and -₹10,905.36 net drawdown due to legacy probe trades (-25 bps), excessive churn, and tight intraday stops.

---

### B. Holdout Log-Loss Investigation (0.6937 vs. 0.3810 / 85.10% Accuracy)

| Investigation Item | Finding & Evidence |
| :--- | :--- |
| **True v2.5 Untouched Holdout** | **51.47% Accuracy / 0.6937 Log-Loss**.<br>Confirmed directly in Postgres `model_versions` (ID 11) and July reports (`ML_V2_5_REPORT.md`).<br>Evaluated on **116,358 samples** covering `2025-07-13` to `2026-06-24`. Recall: **26.99%**, Precision: **54.55%**.<br>Documented across all July archives as barely distinguishing from random binary noise (baseline 0.6931). |
| **Origin of 85.10% / 0.3810** | Traced directly to Postgres `model_versions` ID 21 (`direction-v3.0` on `intraday_micro_v1`):<br>• Calibration Split (42,660 samples): **86.32% accuracy**, **0.3902 log-loss**<br>• Final Holdout Split (78,600 samples, Sep 16–30, 2026): **84.02% accuracy**, **0.4395 log-loss** |
| **Data Leakage & Class Imbalance Artifact** | In the `intraday_micro_v1` dataset, negative samples (class 0 = no trade) constitute **~84–86%** of all rows.<br>The candidate classifier suffered from a severe class-collapse bug:<br>• **Holdout Recall: 0.0%**<br>• **Holdout Precision: 0.0%**<br>• **Calibration Recall: 0.02%** (predicted exactly 1 positive trade in 42,660 samples)<br>The model achieved 84–86% accuracy and 0.38–0.44 log-loss purely by functioning as a **dummy classifier** that always predicts class 0. |
| **Governance Verdict** | **The 85.10% / 0.3810 metric is invalid for model quality assessment and must not be used for live promotion.** It reflects class imbalance collapse (zero recall), not predictive skill. Genuine model edge is measured by the balanced class-weighted retrain and live paper trade metrics. |

---

## 3. Verified Runtime Parameters vs. Documentation Discrepancies

The following table documents the actual running runtime values (read from `.env` and `os.getenv` in code) against contradicting values in historical documentation:

| Parameter Name | Live Runtime Value (`.env` / Code Default) | Source Code Location | Conflicting Documented Values | Document Files with Discrepancy | Operational Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`NIVESH_SHADOW_MIN_PROFESSIONAL_RR`** | **1.90** (tiered: best=1.50, neutral=1.80, worst=2.00) | `.env:159`, `live_inference.py:264` | "Hard 2.00 R:R mandatory & non-negotiable" | `rules.md`, `doc required/rules.md` | **Softened intentionally** to 1.90/1.50 to avoid the Zero-Trade Trap while demanding edge. |
| **`breakeven_trigger_r`** | **+1.10R** | `position_manager.py:57`, `.env:161` | +0.70R or +0.75R | `MASTER_COMPREHENSIVE_PLAN.md`, `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **+1.10R active** to allow intraday trades room to breathe before moving stop to scratch. |
| **`profit_lock_trigger_r`** | **+1.60R** | `position_manager.py:58`, `.env:162` | +1.50R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4`, `rules.md` | **+1.60R active**. |
| **`profit_lock_guaranteed_r`**| **+1.00R** | `position_manager.py:59`, `.env:163` | +0.75R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **+1.00R locked** once +1.60R is reached. |
| **`trailing_trigger_r`** | **+1.50R** | `position_manager.py:60`, `.env:164` | +2.00R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **+1.50R active**. |
| **`trailing_giveback_r`** | **0.40R** | `position_manager.py:61`, `.env:165` | 0.50R | `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md §10.4` | **0.40R active** (protects 60% of peak excursion beyond trigger). |
| **`stagnation_scratch_seconds`**| **900.0s (15.0 min)** | `position_manager.py:68` | 20 minutes | `MASTER_COMPREHENSIVE_PLAN.md` | **15 minutes active**; tightens stop to 0.25R if trade is flat for 15 min. |
| **`adverse_cut_threshold_r`** | **0.40R in 90 seconds** | `position_manager.py:64` | 0.35R | Historical architectural notes | **0.40R active** (fast bail on immediate counter-momentum). |
| **`daily_loss_limit`** | **₹2,000 / 2 consecutive losses** | `.env:128`, `position_manager.py` | 3 different variations (₹1,500 vs ₹2,000 vs 3 losses) | Legacy specs | **₹2,000 max daily loss / 2 consecutive losses strictly enforced**. |

---

## 4. Multi-Timeframe (MTF) & Gate Stacking ("Zero-Trade Trap") Resolution

### A. The MTF Confirmation Fail-Open Bug (FIXED)
* **Vulnerability**: In `backend/mtf_confirmation.py`, fallback conditions for sparse candle history (`< 10` daily candles, `< 5` 15m candles, `< 3` 5m candles) and generic exception handlers returned `True`. This allowed symbols with zero history or API connection drops to receive `score = 100%`, `verdict = "TRIPLE_CONFIRMED"`, and a +5.0 ranking bonus in `screener_brain.py`.
* **Fix Applied**: Lines 45, 59, 74, 79 updated to set `checks[...] = False`, `verdict = "INSUFFICIENT_DATA"`, failing strictly **closed**. Illiquid symbols lacking minimum history are rejected.

### B. The Gate-Stacking Dilemma & Confluence Transition
* When 12+ filters (ADX, RVOL, VWAP, Nifty 5m alignment, GEX strike walls, VP shapes, PCR, ASI, MTF, regime router, minimum edge bps, cycle throttle) are evaluated as a serial logical `AND`, the probability of any trade passing approaches zero (the "Zero Trade Trap").
* **Current Operational Stance**:
  1. **Hard Invariants (Binary Gates)**:
     - Nifty trend contradiction veto (never trade long against strong market trend).
     - Strict capital / daily circuit breaker (max 2 consecutive losses or ₹2,000 drawdown).
     - R:R $\ge 1.50$ (tiered by setup quality).
  2. **Soft Multi-Factor Confluence (Weighted Scoring)**:
     - MTF Alignment (33% macro + 33% structure + 33% micro trigger).
     - Volume Profile shape + PCR wall proximity.
     - Expected net edge hurdle ($\ge +15$ to $+35$ bps depending on market regime).

---

## 5. Documentation Consolidation State

To prevent recurrence of contradicting parameters and multi-document drift:
* **The 3 Canonical Root Living Documents**:
  1. [STATUS.md](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/STATUS.md): Current architecture, verified database numbers, runtime parameter truth table.
  2. [MASTER_COMPREHENSIVE_PLAN.md](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/MASTER_COMPREHENSIVE_PLAN.md): Engineering roadmap, phase milestones, and architectural patterns.
  3. [README.md](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/README.md): Project overview, developer quickstart, system commands.
* **Archived Specs**: `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md`, `rules.md`, `task.md`, and `memory.md` are archived in [docs/archive/](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/docs/archive) and synchronized in `doc required/` as reference-only material. All operational decisions defer to `STATUS.md` and live runtime configuration.
