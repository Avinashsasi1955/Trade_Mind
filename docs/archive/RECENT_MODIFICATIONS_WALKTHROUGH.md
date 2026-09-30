# Walkthrough: Risk Management Suite & Deployment

## Session Summary (Sep 22, 2026)

### 1. Bug Fix: ADX Chop Filter Leak (Test 5)

**Root Cause**: The `_chart_strategy_gate` in [`live_inference.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/live_inference.py#L386-L393) had a critical logic hole. When the ADX chop filter correctly blocked `breakout_up` during choppy markets (ADX < 18), trades leaked through alternative strategy paths that lacked the `not is_choppy` guard:

| Strategy Path | Before Fix | After Fix |
|:---|:---|:---|
| `breakout_up/down` | ✅ Had `not is_choppy` | ✅ No change |
| `momentum_up/down` | ✅ Had `not is_choppy` | ✅ No change |
| **`pullback_buy/sell`** | ❌ **No chop guard** | ✅ **Added `and not is_choppy`** |
| **`range_support_bounce/reject`** | ❌ **No chop guard** | ✅ **Added `and not is_choppy`** |

**Rationale**: A "trend pullback" by definition requires a trend (ADX ≥ 18). Similarly, "range reversion" requires defined support/resistance levels that don't exist in dead-flat consolidation. Both strategies produce noise signals in choppy markets.

**Files Changed**:
- [`backend/live_inference.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/live_inference.py#L388-L393) — Lines 388-393: Added `and not is_choppy` to 4 strategy conditions

### 2. Test Suite Fix (Test 5 Data)

**Problem**: The original test data for `test_5_adx_chop_filter_rejection` generated ADX = 61.3 (not choppy) because the breakout bar had a massive directional move that dominated the 7-bar DX average.

**Fix**: Regenerated test data with:
- 25 bars with **identical** high/low (no directional movement → DX = 0 for all)
- Final bar barely crosses `prior_high` by 0.02 (minimal directional contribution)
- RVOL = 2.0x to pass Wyckoff gate (isolating ADX test)

**Result**: ADX = 1.30 (correctly choppy), all strategy paths blocked → `accepted: False` ✅

**File Changed**:
- [`tests/test_risk_management_suite.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/tests/test_risk_management_suite.py#L136-L169) — Lines 136-169: Rebuilt test_5 data

### 3. Test Results: 8/8 PASS ✅

```
tests/test_risk_management_suite.py::test_1_fee_padded_breakeven_guarantees_profit    PASSED
tests/test_risk_management_suite.py::test_2_immediate_adverse_excursion_cut_under_90s PASSED
tests/test_risk_management_suite.py::test_3_stagnation_guard_5m_tighten_and_15m_exit  PASSED
tests/test_risk_management_suite.py::test_4_vwap_anti_chase_rejection                 PASSED
tests/test_risk_management_suite.py::test_5_adx_chop_filter_rejection                 PASSED
tests/test_risk_management_suite.py::test_6_zero_lag_microstructure_entry_gate         PASSED
tests/test_risk_management_suite.py::test_7_volume_authenticity_true_vs_fake           PASSED
tests/test_risk_management_suite.py::test_8_dual_track_swing_routing_and_exemption     PASSED
```

### 4. Multi-Container Deployment

Updated files deployed to **all 4 app containers**:

| Container | Files Deployed | Restarted |
|:---|:---|:---|
| `web-1` | live_inference.py, position_manager.py, service.py, trade_memory.py, ml_pipeline.py, test_risk_management_suite.py | ✅ Yes |
| `worker-1` | live_inference.py, position_manager.py, service.py, trade_memory.py, ml_pipeline.py | ✅ Yes |
| `beat-1` | live_inference.py, position_manager.py, service.py, trade_memory.py, ml_pipeline.py | ✅ Yes |
| `stream-1` | live_inference.py, position_manager.py, service.py, trade_memory.py, ml_pipeline.py | (no restart needed — reads files on demand) |

### 5. Tonight's Work: Immediate Loss Defense & Phase A Deployment (Sep 23, 2026)

**Objective**: Completely eliminate the leaks that allowed today's 20% win-rate drop (-₹573 loss) and tune Phase A ML parameters for high conviction.

#### Implemented & Deployed Fixes:
1. **Plugged `_learning_strategy_gate` Leak**: Added the strict ADX chop filter (`adx < 18`), Wyckoff Effort-vs-Result RVOL (`< 0.90x`), and VWAP overextension (`±0.8%`) directly into `_learning_strategy_gate`. This permanently blocks low-quality fallback paper trades.
2. **Nifty 50 Macro Directional Veto**: Automatically queries the latest 5-minute candles of Nifty 50 and calculates EMA9 vs EMA21. All stock `BUY` trades are vetoed if Nifty is in a downtrend (EMA9 < EMA21); all stock `SELL` trades are vetoed if Nifty is in an uptrend.
3. **₹300 Minimum Stock Price Floor**: Cash equity intraday momentum setups now enforce `close >= ₹300`, preventing large share quantity tick-spread slippage traps (e.g. ONGC and TATASTEEL).
4. **Cycle Entry Batch Cap**: Decreased `max_new_trades_per_cycle` from 6 to **1**, preventing 4 simultaneous entries from hitting stop losses on a single market dip.
5. **Fee & Slippage-Padded Breakeven**: Updated `breakeven_price` to `entry ± (fees * 2.5 + 2 * tick_size * quantity) / quantity` across both `live_inference.py` and `position_manager.py`. Guarantees positive net profit.
6. **Phase A ML Conviction**: Raised default `decision_threshold` from 0.55 to **0.70** (filtering out the 52% coin-flip noise) and introduced **2.5x asymmetric sample loss weights** in `ml_pipeline.py`.

#### Automated Unit Testing: 10/10 PASS ✅
```
test_1_fee_padded_breakeven_guarantees_profit                          PASSED
test_2_immediate_adverse_excursion_cut_under_90s                       PASSED
test_3_stagnation_guard_5m_tighten_and_15m_exit                        PASSED
test_4_vwap_anti_chase_rejection                                       PASSED
test_5_adx_chop_filter_rejection                                       PASSED
test_6_zero_lag_microstructure_entry_gate                               PASSED
test_7_volume_authenticity_true_vs_fake                                 PASSED
test_8_dual_track_swing_routing_and_exemption                           PASSED
test_9_learning_strategy_gate_plugs_chop_leak                          PASSED
test_10_fee_padded_breakeven_includes_slippage_and_guarantees_green    PASSED
```
*Ran 10 tests in 0.005s — OK*

#### Deployment Status:
- Files copied and active across all 4 containers (`web-1`, `worker-1`, `beat-1`, `stream-1`).
- `worker-1`, `beat-1`, and `web-1` restarted and verified running.
### 5. Remaining Work

| Item | Priority | Status |
|:---|:---|:---|
| Execute ML retraining with `intraday_micro_v1` | P3 | Feature set defined, retraining not yet run |
| Register `position_manager` in Celery beat | P3 | Not yet wired |
| Validate ML holdout metrics pass promotion gate | P3 | Blocked on retraining |
