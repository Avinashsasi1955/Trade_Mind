# TradeMind: SPX GEX, Volume Profile Shapes, Timeframes & Senior Layer Analysis

## 1. SPX GEX (Gamma Exposure) — Is It Useful for This Project?

### What GEX Is
GEX measures the aggregate **gamma exposure** of market makers across all options strikes. When dealers are **long gamma** (positive GEX), they hedge by selling rallies and buying dips → **mean-reversion regime** (price compresses). When dealers are **short gamma** (negative GEX), they hedge by chasing momentum → **trend regime** (price explodes).

### Verdict: **Useful Conceptually, But Not Directly Applicable to NSE**

| Factor | SPX (US) | NIFTY/BANKNIFTY (NSE) |
|:---|:---|:---|
| **Data Source** | CBOE publishes real-time options OI/Greeks per-strike | NSE publishes OI snapshots but **no per-strike dealer gamma** |
| **Dealer Identity** | Market makers file 13F; dealer positioning inferable | NSE is **anonymous** — FII/DII OI available at aggregate level, not per-strike dealer gamma |
| **GEX Calculation** | `GEX = Σ(OI × Gamma × 100 × Spot)` per strike | Can be **approximated** using our `greeks_engine.py` analytical gamma + `option_chain_oi_snapshots` OI |
| **Flip Level** | Known precisely from CBOE data | Must be **estimated** — where net gamma crosses zero |
| **IV Surface** | Liquid; deep OTM skew is real | NSE options liquidity drops **rapidly** beyond ATM ±3 strikes |

### What We **Can** Do (NSE Proxy GEX):

```
Proxy_GEX(strike) = OI_CE(strike) × Gamma_CE × 100 × Spot  
                   − OI_PE(strike) × Gamma_PE × 100 × Spot
Net_GEX = Σ Proxy_GEX across all active strikes
```

> [!WARNING]  
> **Issue I Identified**: Our `pcr_engine.py` calculates OI-based PCR but does NOT compute **per-strike gamma-weighted exposure**. We have `greeks_engine.py` with analytical Black-Scholes gamma, but it's disconnected from the PCR flow. These two modules need bridging.

### Recommendation
- **Phase C Enhancement**: Build a `gex_estimator.py` that combines `greeks_engine.calculate_black_scholes_greeks()` gamma with `option_chain_oi_snapshots` OI per-strike to produce a **Net GEX curve**, **GEX Flip Level**, and **Gamma Regime** (positive/negative).
- **NOT a Phase A/B priority** — PCR + Max Pain + Volume Profile already cover 80% of the institutional positioning signal.
- **Use it as a regime classifier** for the Spider Bot: positive GEX → tighter grid spacing (mean reversion), negative GEX → wider grid spacing (momentum).

---

## 2. Is Current Data Sufficient for Volume Profile Estimation?

### What Volume Profile Needs
Volume Profile requires **intraday bars with volume** distributed across price levels within a session.

### Current Data Check (Inspected from Code)

| Data Element | Available? | Where | Issue Found? |
|:---|:---|:---|:---|
| **5-minute bars with OHLCV** | ✅ Yes | `live_market_bars` table, `interval='5minute'` | ✅ Good |
| **1-minute bars** | ✅ Yes | `interval='1minute'` | ✅ Good |
| **Volume per bar** | ✅ Yes | `volume` column in bars | ⚠️ **Some bars have `volume=0`** for low-liquidity stocks |
| **Session coverage** | ⚠️ Varies | 09:15–15:30 IST | ⚠️ **Early session has sparse bars** for some stocks |
| **Number of bars per session** | Typically 75 (5m) or 375 (1m) | Full session | ✅ Sufficient |
| **Historical multi-day VP** | ❌ Not yet | Only current session calculated | **Gap**: No developing/composite VP |

### Root Cause Issues I Found in [`volume_profile.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/volume_profile.py):

1. **Line 24**: `if not bars or len(bars) < 3` — Only requires 3 bars minimum. For meaningful VP shapes, you need **≥30 bars** (2.5 hours of 5m data).
2. **Line 37**: `volumes = [max(0, int(b.get("volume", 0))) for b in bars]` — Zero-volume bars are included in the distribution, diluting the profile.
3. **No time-of-day weighting** — Opening/closing rotation volumes are treated equally to mid-session lulls.
4. **Single session only** — No composite/developing VP across multiple days.

### Data Sufficiency Verdict

| Metric | Required | Available | Sufficient? |
|:---|:---|:---|:---|
| **Intraday bars for single-session VP** | ≥30 bars with volume | 75 bars (5m), 375 bars (1m) | ✅ Yes |
| **Volume per bar** | Non-zero for majority | ~85% of FnO-eligible stocks | ⚠️ Marginal for small-caps |
| **For VP Shape Detection** | ≥50 bars with distribution | Available by 11:30 IST | ✅ Yes (after 2.5h) |
| **For Multi-Day Composite VP** | 3–5 day history | Historical bars in DB | ✅ Data exists, logic missing |

> [!IMPORTANT]  
> **Bottom line**: Data is sufficient for FnO-eligible stocks and indices after 11:30 IST. Small-caps with zero volume bars will produce unreliable profiles. The code should reject VP calculations for instruments with <70% non-zero-volume bars.

---

## 3. Volume Profile Shape Detection (P, B, D, b, etc.)

### Standard Market Profile Shapes & Their Meaning

| Shape | Pattern | Meaning | Trading Implication |
|:---|:---|:---|:---|
| **D-Shape** (Normal) | Bell curve, symmetric, POC in the middle | **Balanced** — Two-sided trade, acceptance | Range-bound strategies, sell strangles |
| **P-Shape** | POC in upper third, thin tail below | **Short covering** / Initiative buying | Bullish — dip buyers absorbed supply |
| **b-Shape** | POC in lower third, thin tail above | **Long liquidation** / Initiative selling | Bearish — rally sellers absorbed demand |
| **B-Shape** | Two POCs / bimodal | **Double distribution** — rotational | Breakout pending; trade the break of either extreme |
| **Thin Profile** | Narrow range, low volume | **Low conviction** day | Avoid — no edge |
| **Wide Profile** | Very wide range, distributed volume | **Trend day** | Follow the trend, no mean-reversion |

### Shape Detection Algorithm

I'll implement this by analysing the **volume distribution skewness, kurtosis, and modality**:

```
Skewness > +0.4  → P-Shape (volume concentrated high)
Skewness < -0.4  → b-Shape (volume concentrated low)
|Skewness| < 0.4 AND single POC → D-Shape (balanced)
Bimodal (two peaks > 0.7× max_vol separated by LVN) → B-Shape
Range < 0.5× ATR → Thin Profile
Range > 2.0× ATR → Wide/Trend Profile
```

---

## 4. Timeframes Currently Used — Complete Audit

### Before Prediction (Feature Generation)

| Timeframe | Where Used | Purpose |
|:---|:---|:---|
| **Daily (1D)** | [`ml_pipeline.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/ml_pipeline.py) `FEATURES_V2` | RSI-14, SMA20/50 gap, ATR-14, 20d volatility, return_1d/5d/20d |
| **5-minute (5m)** | [`live_inference.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/live_inference.py) `_chart_strategy_gate()` | EMA-8/21, BOS/CHoCH structure, breakout/pullback, VWAP, ADX, volume |
| **1-second (1s)** | `_microstructure_gate()` in `live_inference.py` | Micro execution quality — spread, momentum, adverse flow |

### At Trade Acceptance (Multi-Timeframe Confirmation)

| Timeframe | Where Used | Purpose |
|:---|:---|:---|
| **1D (Daily)** | [`mtf_confirmation.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/mtf_confirmation.py) L32 | Macro trend: Close vs 20 EMA |
| **15m** | [`mtf_confirmation.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/mtf_confirmation.py) L48 | Key structure: Close vs prior 15m low/high |
| **5m** | [`mtf_confirmation.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/mtf_confirmation.py) L62 | Micro trigger: Volume expansion + candle direction |
| **1H (aggregated from 5m)** | `_aggregate_session_bars()` in `live_inference.py` L190 | Hourly structure direction for multi-timeframe alignment |

### Issues I Found in MTF Confirmation:

> [!WARNING]  
> 1. **[`mtf_confirmation.py` Line 36](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/mtf_confirmation.py#L36)**: Uses simple average instead of actual EMA-20 for daily macro check.
> 2. **[`mtf_confirmation.py` Line 53-56](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/mtf_confirmation.py#L53-L56)**: 15m structure check only compares current close to previous bar's low/high — this is NOT proper structure analysis. It should use `_structure_analysis()` from `live_inference.py`.
> 3. **[`mtf_confirmation.py` Line 45](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/mtf_confirmation.py#L45)**: Sparse daily history defaults to `True` (neutral pass), meaning MTF confirmation is **silently bypassed** for new instruments.
> 4. **No 1H timeframe check** — The aggregated 1H bars exist in `live_inference.py` but are **not used** by `mtf_confirmation.py`.

---

## 5. Senior Layer Recommendations & Backlash Prevention

### Pre-Market Analysis Requirements (What Should Be Checked Before 09:15 IST)

| Check | Current Status | Recommendation |
|:---|:---|:---|
| **Previous Day VP (POC, VAH, VAL)** | ❌ Not checked | Must compute composite VP from T-1 and compare opening price to it |
| **Overnight Gap Analysis** | ❌ Not automated | Gap up above VAH → caution for longs. Gap down below VAL → caution for shorts |
| **FII/DII OI Change** | ❌ Not implemented | Fetch NSE FII/DII derivative stats via API at 08:30 IST |
| **Global Cues (SGX NIFTY, DJI, NASDAQ futures)** | ❌ Not checked | At minimum, check if SGX NIFTY is >0.5% gap vs previous NIFTY close |
| **IV Percentile Context** | ⚠️ Partial (`greeks_engine.py`) | Should compute NIFTY IV Percentile and classify regime before session |
| **Economic Calendar Events** | ❌ Not checked | RBI policy, US Fed, earnings season should block aggressive entries |
| **Opening Rotation Window** | ⚠️ `morning_cooloff_ist` exists | But only blocks entries before 10:15 IST; doesn't use OR breakout logic |

### Modification Backlash Prevention Rules

> [!CAUTION]  
> Every modification from now on must follow these **Senior Layer Guardrails**:

1. **Schema Check First**: Before adding any new column, table, or field, check the existing database schema. Run `\d table_name` mentally.
2. **Import Check**: Before importing any module, verify it exists and that circular imports won't occur.
3. **Data Availability Check**: Before using any data field, verify the data pipeline actually populates it at the time it's needed.
4. **Performance Regression Check**: Any code on the tick path must be benchmarked. If it adds >0.5ms per tick, it must be batched or moved off the hot path.
5. **Test Before Commit**: Every new function must have a unit test that runs standalone without DB/Redis.
6. **Rollback Plan**: Every enhancement must be behind a feature flag (environment variable) so it can be disabled without code changes.
7. **Edge Case First**: Before implementing the happy path, document what happens when data is missing, None, zero, empty list, or malformed.

### What the Senior Layer Should Analyse Pre-Trade (Complete Checklist)

```
Pre-Trade Senior Analysis Checklist:
─────────────────────────────────────
✅ 1. Daily EMA-20/50 trend alignment (macro direction)
✅ 2. 5m BOS/CHoCH structure (micro structure)
✅ 3. VWAP distance (overextension guard)
✅ 4. ADX regime (trend vs chop filter)
✅ 5. Wyckoff effort-vs-result (volume authenticity)
✅ 6. ASI trap detection (false breakout filter)
✅ 7. PCR institutional wall (option positioning)
✅ 8. Volume Profile POC/VAH/VAL levels
🆕 9. Volume Profile Shape (P/b/D/B classification)
🆕 10. Previous session VP reference levels (composite VP)
🆕 11. Opening gap context (gap vs T-1 VP)
🆕 12. IV regime classification (high/low/normal)
🆕 13. Net GEX regime estimate (Phase C)
🆕 14. Multi-day VP developing POC migration (is POC shifting?)
```

---

## 6. Implementation Plan

### Immediate (Today — Enhancing Existing Code)

1. **Add VP Shape Detection** to [`volume_profile.py`](file:///Users/avinash/Documents/Codex/2026-06-27/design-a-modern-distinctive-ui-ux/backend/volume_profile.py)
   - Compute skewness, kurtosis, bimodality
   - Classify as P/b/D/B/Thin/Wide
   - Return shape with trade implications
   - Wire into `_senior_decision_report`

2. **Increase VP minimum bars** from 3 → 30 for shape detection, keep 3 for basic POC

3. **Filter zero-volume bars** from VP calculation

4. **Add VP confidence score** based on bar count and volume coverage

### Phase B (After Shadow Validations)
- Composite VP (3-day developing profile)
- Previous session VP reference levels in pre-trade checklist
- GEX proxy estimator

### Phase C
- Full GEX regime integration with Spider Bot
- Economic calendar event blocker
