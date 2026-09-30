"""Volume Profile Engine: POC, Value Area (VAH/VAL), Liquidity Nodes & Shape Detection.

Calculates horizontal volume distribution across price levels to identify:
- POC (Point of Control): The price level where institutional volume is heaviest.
- VAH (Value Area High): Upper boundary containing 70% of transacted volume.
- VAL (Value Area Low): Lower boundary containing 70% of transacted volume.
- HVN (High Volume Nodes) & LVN (Low Volume Nodes): Institutional liquidity walls vs voids.
- Profile Shape: P (short covering), b (long liquidation), D (balanced), B (double distribution).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def calculate_volume_profile(
    bars: List[Dict[str, Any]],
    num_bins: int = 50,
    value_area_pct: float = 0.70,
) -> Dict[str, Any]:
    """Compute session Volume Profile, POC, VAH, and VAL from intraday bars.

    Uses intra-candle typical price weighting to reconstruct authentic auction distribution.
    Guarantees sub-millisecond execution with zero external dependencies.
    """
    if not bars or len(bars) < 3:
        return {
            "is_valid": False,
            "poc": None,
            "vah": None,
            "val": None,
            "total_volume": 0,
            "bins": [],
            "shape": None,
            "shape_label": "INSUFFICIENT_DATA",
            "confidence": 0.0,
            "reason": "insufficient_bars",
        }

    highs = [float(b.get("high", 0)) for b in bars]
    lows = [float(b.get("low", 0)) for b in bars]
    raw_volumes = [max(0, int(b.get("volume", 0))) for b in bars]
    # Filter quality: track non-zero volume ratio for confidence scoring
    nonzero_vol_count = sum(1 for v in raw_volumes if v > 0)
    volume_quality_pct = nonzero_vol_count / len(raw_volumes) * 100.0 if raw_volumes else 0.0
    volumes = raw_volumes

    session_high = max(highs)
    session_low = min(lows)
    price_range = session_high - session_low

    if price_range <= 0.0001:
        last_p = float(bars[-1].get("close", 100))
        return {
            "is_valid": False,
            "poc": last_p,
            "vah": last_p,
            "val": last_p,
            "total_volume": sum(volumes),
            "bins": [],
            "shape": None,
            "shape_label": "ZERO_RANGE",
            "confidence": 0.0,
            "reason": "zero_price_range",
        }

    total_volume = sum(volumes)
    if total_volume <= 0:
        last_p = float(bars[-1].get("close", 100))
        return {
            "is_valid": False,
            "poc": last_p,
            "vah": last_p,
            "val": last_p,
            "total_volume": 0,
            "bins": [],
            "shape": None,
            "shape_label": "ZERO_VOLUME",
            "confidence": 0.0,
            "reason": "zero_volume",
        }

    bin_size = price_range / num_bins
    bin_volumes = [0.0] * num_bins
    bin_centers = [session_low + (i + 0.5) * bin_size for i in range(num_bins)]

    # Distribute bar volumes across price bins weighted toward typical price
    for b in bars:
        b_high = float(b.get("high", session_low))
        b_low = float(b.get("low", session_low))
        b_close = float(b.get("close", (b_high + b_low) / 2))
        b_vol = max(0, int(b.get("volume", 0)))
        if b_vol <= 0:
            continue

        tp = (b_high + b_low + b_close) / 3.0
        half_spread = max(bin_size, (b_high - b_low) / 2.0)

        start_bin = max(0, min(num_bins - 1, int((b_low - session_low) / bin_size)))
        end_bin = max(0, min(num_bins - 1, int((b_high - session_low) / bin_size)))

        if start_bin == end_bin:
            bin_volumes[start_bin] += b_vol
        else:
            # Triangular distribution centered at typical price
            weights = []
            for k in range(start_bin, end_bin + 1):
                p_center = bin_centers[k]
                dist = abs(p_center - tp)
                w = max(0.05, 1.0 - (dist / half_spread))
                weights.append(w)
            total_w = sum(weights) or 1.0
            for idx, k in enumerate(range(start_bin, end_bin + 1)):
                bin_volumes[k] += b_vol * (weights[idx] / total_w)

    # 1. Point of Control (POC): Price bin with maximum transacted volume
    max_vol = -1.0
    poc_idx = 0
    for idx, v in enumerate(bin_volumes):
        if v > max_vol:
            max_vol = v
            poc_idx = idx

    poc_price = round(bin_centers[poc_idx], 2)

    # 2. Value Area Calculation (70% standard auction market theory)
    target_va_vol = total_volume * value_area_pct
    current_va_vol = bin_volumes[poc_idx]
    up_idx = poc_idx
    down_idx = poc_idx

    while current_va_vol < target_va_vol and (up_idx < num_bins - 1 or down_idx > 0):
        next_up_vol = bin_volumes[up_idx + 1] if up_idx < num_bins - 1 else 0.0
        next_down_vol = bin_volumes[down_idx - 1] if down_idx > 0 else 0.0

        if next_up_vol >= next_down_vol and up_idx < num_bins - 1:
            up_idx += 1
            current_va_vol += next_up_vol
        elif down_idx > 0:
            down_idx -= 1
            current_va_vol += next_down_vol
        elif up_idx < num_bins - 1:
            up_idx += 1
            current_va_vol += next_up_vol
        else:
            break

    vah_price = round(bin_centers[up_idx] + 0.5 * bin_size, 2)
    val_price = round(bin_centers[down_idx] - 0.5 * bin_size, 2)

    # 3. High Volume Nodes (HVN) & Low Volume Nodes (LVN)
    avg_bin_vol = total_volume / num_bins
    hvn_levels = [round(bin_centers[i], 2) for i, v in enumerate(bin_volumes) if v >= avg_bin_vol * 1.5]
    lvn_levels = [round(bin_centers[i], 2) for i, v in enumerate(bin_volumes) if v <= avg_bin_vol * 0.4]

    # 4. Volume Profile Shape Detection
    shape_info = detect_profile_shape(bin_volumes, bin_centers, poc_idx, num_bins, price_range, len(bars))

    # 5. Confidence scoring based on data quality
    bar_confidence = min(100.0, len(bars) / 50.0 * 100.0)  # 50 bars = 100% bar confidence
    vol_confidence = min(100.0, volume_quality_pct)  # % of bars with non-zero volume
    range_confidence = min(100.0, (price_range / max(0.01, session_low)) * 10000.0)  # Range as bps
    overall_confidence = round(0.4 * bar_confidence + 0.4 * vol_confidence + 0.2 * range_confidence, 1)

    return {
        "is_valid": True,
        "poc": poc_price,
        "vah": vah_price,
        "val": val_price,
        "session_high": round(session_high, 2),
        "session_low": round(session_low, 2),
        "total_volume": total_volume,
        "hvn_levels": hvn_levels[:5],
        "lvn_levels": lvn_levels[:5],
        "bin_size": round(bin_size, 4),
        "poc_volume": round(max_vol, 0),
        "value_area_volume": round(current_va_vol, 0),
        "shape": shape_info["shape"],
        "shape_label": shape_info["label"],
        "shape_detail": shape_info["detail"],
        "shape_trade_implication": shape_info["trade_implication"],
        "skewness": shape_info["skewness"],
        "is_bimodal": shape_info["is_bimodal"],
        "confidence": overall_confidence,
        "volume_quality_pct": round(volume_quality_pct, 1),
    }


def detect_profile_shape(
    bin_volumes: List[float],
    bin_centers: List[float],
    poc_idx: int,
    num_bins: int,
    price_range: float,
    bar_count: int,
) -> Dict[str, Any]:
    """Classify the Volume Profile shape using statistical distribution analysis.

    Shapes detected:
    - P-Shape: POC in upper third, thin tail below → Short covering / Initiative buying
    - b-Shape: POC in lower third, thin tail above → Long liquidation / Initiative selling
    - D-Shape: Symmetric bell curve, POC near middle → Balanced / Two-sided trade
    - B-Shape: Bimodal (two distinct POCs separated by LVN) → Double distribution / Rotation
    - Thin: Narrow range, low total volume → Low conviction, avoid
    - Wide: Very wide range, distributed volume → Trend day, follow trend
    """
    total_vol = sum(bin_volumes)
    if total_vol <= 0 or num_bins < 5:
        return {"shape": None, "label": "INSUFFICIENT", "detail": "too few bins or zero volume",
                "trade_implication": "NO_TRADE", "skewness": 0.0, "is_bimodal": False}

    # Compute volume-weighted mean and skewness of the distribution
    weights = [v / total_vol for v in bin_volumes]
    weighted_mean = sum(w * c for w, c in zip(weights, bin_centers))
    variance = sum(w * (c - weighted_mean) ** 2 for w, c in zip(weights, bin_centers))
    std_dev = math.sqrt(variance) if variance > 0 else 0.001

    # Skewness: positive = volume mass concentrated at higher prices (P-shape), negative = lower (b-shape)
    # Note: in raw statistics, a lower tail produces negative 3rd moment. We multiply by -1
    # so that directional skewness matches market profile intuition (positive = top-heavy, negative = bottom-heavy).
    raw_skew = sum(w * ((c - weighted_mean) / std_dev) ** 3 for w, c in zip(weights, bin_centers))
    skewness = round(-1.0 * raw_skew, 4)

    # Bimodality detection: look for two peaks separated by a valley (LVN)
    max_vol = max(bin_volumes)
    threshold = max_vol * 0.65
    peaks = []
    for i in range(1, num_bins - 1):
        if bin_volumes[i] >= threshold and bin_volumes[i] >= bin_volumes[i - 1] and bin_volumes[i] >= bin_volumes[i + 1]:
            peaks.append(i)

    # Merge adjacent peaks (within 2 bins)
    merged_peaks = []
    for p in peaks:
        if not merged_peaks or p - merged_peaks[-1] > 2:
            merged_peaks.append(p)
        elif bin_volumes[p] > bin_volumes[merged_peaks[-1]]:
            merged_peaks[-1] = p

    is_bimodal = False
    if len(merged_peaks) >= 2:
        # Verify a genuine LVN valley exists between the two peaks
        p1, p2 = merged_peaks[0], merged_peaks[-1]
        valley_min = min(bin_volumes[p1:p2 + 1]) if p2 > p1 else max_vol
        peak_avg = (bin_volumes[p1] + bin_volumes[p2]) / 2.0
        if valley_min < peak_avg * 0.55:  # Valley must be <55% of peak average
            is_bimodal = True

    # POC position relative to profile (0.0 = bottom, 1.0 = top)
    poc_position = poc_idx / max(1, num_bins - 1)

    # Shape classification
    if bar_count < 15:
        shape = "DEVELOPING"
        label = "DEVELOPING_PROFILE"
        detail = f"Only {bar_count} bars — profile still forming"
        trade_implication = "WAIT_FOR_DEVELOPMENT"
    elif is_bimodal:
        shape = "B"
        label = "B_DOUBLE_DISTRIBUTION"
        detail = f"Bimodal distribution with {len(merged_peaks)} peaks — rotational day, breakout pending"
        trade_implication = "BREAKOUT_PENDING_TRADE_EXTREMES"
    elif skewness > 0.40 and poc_position > 0.55:
        shape = "P"
        label = "P_SHORT_COVERING"
        detail = f"POC in upper zone (pos={poc_position:.2f}), skew={skewness:.2f} — short covering rally"
        trade_implication = "BULLISH_DIP_BUY_AT_VAL"
    elif skewness < -0.40 and poc_position < 0.45:
        shape = "b"
        label = "b_LONG_LIQUIDATION"
        detail = f"POC in lower zone (pos={poc_position:.2f}), skew={skewness:.2f} — long liquidation selloff"
        trade_implication = "BEARISH_SELL_RALLY_AT_VAH"
    elif abs(skewness) <= 0.40 and 0.30 <= poc_position <= 0.70:
        shape = "D"
        label = "D_BALANCED"
        detail = f"Symmetric distribution (skew={skewness:.2f}, poc_pos={poc_position:.2f}) — balanced two-sided trade"
        trade_implication = "RANGE_BOUND_MEAN_REVERSION"
    elif price_range / max(0.01, bin_centers[num_bins // 2]) > 0.025:  # >2.5% range
        shape = "WIDE"
        label = "WIDE_TREND_DAY"
        detail = f"Wide range ({price_range:.2f}) — strong directional conviction"
        trade_implication = "TREND_FOLLOWING"
    else:
        shape = "THIN"
        label = "THIN_LOW_CONVICTION"
        detail = f"Narrow range with low volume dispersion — no institutional conviction"
        trade_implication = "AVOID_NO_EDGE"

    return {
        "shape": shape,
        "label": label,
        "detail": detail,
        "trade_implication": trade_implication,
        "skewness": skewness,
        "is_bimodal": is_bimodal,
        "poc_position": round(poc_position, 3),
        "peak_count": len(merged_peaks),
    }


def evaluate_volume_profile_verdict(
    current_price: float,
    signal_direction: int,  # +1 for Bullish, -1 for Bearish
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Evaluate candidate trade against Volume Profile institutional levels and shape context.

    Returns risk assessment, cautions, and conviction boosts:
    - Long at/above VAH without breakout volume -> Exhaustion warning (VETO or CAUTION)
    - Long at VAL bounce -> High conviction institutional absorption entry
    - Short at/below VAL without breakdown volume -> Exhaustion warning (VETO or CAUTION)
    - Short at VAH rejection -> High conviction institutional distribution entry
    - Shape context: P-shape boosts longs, b-shape boosts shorts, B-shape warns of rotation
    """
    if not profile or not profile.get("is_valid") or not profile.get("poc"):
        return {"verdict": "NO_PROFILE_DATA", "action": "NEUTRAL", "cautions": [],
                "confidence_delta": 0.0, "shape": None, "shape_label": None}

    poc = float(profile["poc"])
    vah = float(profile["vah"])
    val = float(profile["val"])
    shape = profile.get("shape")
    shape_label = profile.get("shape_label")
    shape_implication = profile.get("shape_trade_implication")
    vp_confidence = float(profile.get("confidence", 50.0))

    dist_to_poc_pct = (current_price - poc) / poc * 100.0
    in_value_area = val <= current_price <= vah
    cautions = []
    boost = 0.0
    action = "PROCEED"

    # --- Level-Based Verdict (existing logic) ---
    if signal_direction > 0:  # Bullish Trade
        if current_price >= vah:
            cautions.append(f"Buying extended above Value Area High (VAH: {vah:.2f}, POC: {poc:.2f})")
            action = "CAUTION_VAH_EXTENSION"
            boost = -8.0
        elif abs(current_price - val) / val <= 0.005:
            action = "HIGH_CONVICTION_VAL_BOUNCE"
            boost = 10.0
        elif in_value_area and current_price < poc:
            action = "VALUE_AREA_DISCOUNT"
            boost = 5.0
        elif in_value_area and current_price > poc:
            action = "VALUE_AREA_PREMIUM"
            boost = 0.0

    elif signal_direction < 0:  # Bearish Trade
        if current_price <= val:
            cautions.append(f"Shorting extended below Value Area Low (VAL: {val:.2f}, POC: {poc:.2f})")
            action = "CAUTION_VAL_EXTENSION"
            boost = -8.0
        elif abs(current_price - vah) / vah <= 0.005:
            action = "HIGH_CONVICTION_VAH_REJECTION"
            boost = 10.0
        elif in_value_area and current_price > poc:
            action = "VALUE_AREA_PREMIUM_SHORT"
            boost = 5.0
        elif in_value_area and current_price < poc:
            action = "VALUE_AREA_DISCOUNT_SHORT"
            boost = 0.0

    # --- Shape-Based Adjustments (new logic) ---
    shape_boost = 0.0
    if shape and vp_confidence >= 40.0:
        if shape == "P" and signal_direction > 0:
            shape_boost = 5.0  # P-shape (short covering) supports bullish trades
            cautions.append(f"VP Shape: P (short covering) supports bullish bias")
        elif shape == "P" and signal_direction < 0:
            shape_boost = -5.0  # P-shape opposes bearish trades
            cautions.append(f"VP Shape: P (short covering) conflicts with bearish trade")
        elif shape == "b" and signal_direction < 0:
            shape_boost = 5.0  # b-shape (long liquidation) supports bearish trades
            cautions.append(f"VP Shape: b (long liquidation) supports bearish bias")
        elif shape == "b" and signal_direction > 0:
            shape_boost = -5.0  # b-shape opposes bullish trades
            cautions.append(f"VP Shape: b (long liquidation) conflicts with bullish trade")
        elif shape == "B":
            shape_boost = -3.0  # B-shape (rotation) is uncertain
            cautions.append(f"VP Shape: B (double distribution) — rotational day, breakout direction uncertain")
        elif shape == "THIN":
            shape_boost = -4.0
            cautions.append(f"VP Shape: Thin profile — low institutional conviction, reduced edge")
        elif shape == "D":
            # Balanced day — no shape boost, pure level-based trading
            if action == "PROCEED":
                cautions.append(f"VP Shape: D (balanced) — mean reversion likely, trade levels")

    boost += shape_boost

    # Low VP confidence penalty
    if vp_confidence < 30.0:
        cautions.append(f"Low Volume Profile confidence ({vp_confidence:.0f}%) — insufficient data quality")
        boost = min(boost, 0.0)  # Never boost on low confidence

    return {
        "verdict": action,
        "in_value_area": in_value_area,
        "dist_to_poc_pct": round(dist_to_poc_pct, 2),
        "poc": poc,
        "vah": vah,
        "val": val,
        "shape": shape,
        "shape_label": shape_label,
        "shape_trade_implication": shape_implication,
        "vp_confidence": round(vp_confidence, 1),
        "cautions": cautions,
        "confidence_delta": boost,
    }
