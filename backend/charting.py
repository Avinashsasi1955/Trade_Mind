import math
import json
from datetime import datetime, timezone
from typing import Dict, List

from sqlalchemy import create_engine, text

from .config import DATABASE_URL, REDIS_URL, DEMO_MODE
from .market import market_snapshot, INDICES
from .technical_analysis import generate_candles
from .history_store import HistoryStore
from .security_master import resolve_security


TIMEFRAMES = {"1s": 1, "1m": 60, "5m": 300, "15m": 900, "1H": 3600, "1D": 86400}
LIVE_SOURCES = ("upstox_v3", "upstox_rest_intraday", "upstox_rest_5m", "zerodha_kite", "kite_gap_backfill")


def _redis_client():
    if not REDIS_URL:
        return None
    try:
        import redis
        return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
    except Exception:
        return None


def _ema(values: List[float], period: int) -> List[float]:
    alpha = 2 / (period + 1)
    result, current = [], values[0]
    for value in values:
        current = value * alpha + current * (1 - alpha)
        result.append(round(current, 2))
    return result


def _sma(values: List[float], period: int) -> List[float]:
    return [round(sum(values[max(0, i-period+1):i+1]) / len(values[max(0, i-period+1):i+1]), 2) for i in range(len(values))]


def _bollinger(values: List[float], period: int = 20) -> Dict[str, List[float]]:
    middle = _sma(values, period)
    upper, lower = [], []
    for idx, mean in enumerate(middle):
        window = values[max(0, idx-period+1):idx+1]
        variance = sum((value - mean) ** 2 for value in window) / len(window)
        deviation = math.sqrt(variance)
        upper.append(round(mean + deviation * 2, 2))
        lower.append(round(mean - deviation * 2, 2))
    return {"upper": upper, "middle": middle, "lower": lower}


def _rsi(values: List[float], period: int = 14) -> List[float]:
    result = [50.0]
    gains, losses = [], []
    for idx in range(1, len(values)):
        change = values[idx] - values[idx-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]
        avg_gain = sum(recent_gains) / len(recent_gains)
        avg_loss = sum(recent_losses) / len(recent_losses)
        value = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        result.append(round(value, 2))
    return result


def _indicators(candles: List[Dict]) -> Dict:
    closes = [item["close"] for item in candles]
    volumes = [item["volume"] for item in candles]
    return {"ema20": _ema(closes, 20), "ema50": _ema(closes, 50),
            "bollinger": _bollinger(closes), "rsi14": _rsi(closes), "volume_sma20": _sma(volumes, 20)}


def _candle_analysis(candles: List[Dict]) -> Dict:
    """Machine-readable candle facts for the chart and senior-review layer."""
    if not candles:
        return {}
    last = candles[-1]
    prev = candles[-2] if len(candles) > 1 else last
    open_price = float(last.get("open") or 0)
    high = float(last.get("high") or open_price)
    low = float(last.get("low") or open_price)
    close = float(last.get("close") or open_price)
    volume = int(last.get("volume") or 0)
    candle_range = max(0.0, high - low)
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    lower_wick = max(0.0, min(open_price, close) - low)
    prev_close = float(prev.get("close") or close)
    change = close - prev_close
    recent = candles[-20:]
    avg_volume = sum(int(item.get("volume") or 0) for item in recent) / max(1, len(recent))
    avg_range = sum(max(0.0, float(item.get("high") or 0) - float(item.get("low") or 0)) for item in recent) / max(1, len(recent))
    direction = "bullish" if close > open_price else "bearish" if close < open_price else "neutral"
    body_pct = round(body / candle_range * 100, 2) if candle_range else 0.0
    return {
        "time": last.get("time"),
        "direction": direction,
        "open": round(open_price, 4), "high": round(high, 4), "low": round(low, 4), "close": round(close, 4),
        "change": round(change, 4), "change_pct": round(change / prev_close * 100, 4) if prev_close else 0.0,
        "volume": volume, "avg_volume_20": round(avg_volume, 2),
        "volume_ratio_20": round(volume / avg_volume, 3) if avg_volume else 0.0,
        "range": round(candle_range, 4), "avg_range_20": round(avg_range, 4),
        "body": round(body, 4), "body_pct_of_range": body_pct,
        "upper_wick": round(upper_wick, 4), "lower_wick": round(lower_wick, 4),
        "source": str(last.get("source") or "unknown"),
        "senior_notes": [
            f"{direction} candle",
            f"body {body_pct:.1f}% of range",
            f"volume {round(volume / avg_volume, 2) if avg_volume else 0}x 20-bar average",
        ],
    }


def _structure_signal(candles: List[Dict]) -> Dict:
    """Deterministic chart-structure summary shared by UI and Senior review.

    This is not a trading permission.  It exposes the same BOS/CHoCH and
    call/put trail state that the chart draws, so the visual layer and the
    Senior/agentic explanation do not disagree.
    """
    if len(candles) < 12:
        return {"bias": "neutral", "mode": "WAIT", "events": [], "trail": [], "reason": "not enough candles"}
    pivots = []
    for idx in range(2, len(candles) - 2):
        candle = candles[idx]
        left = candles[idx - 2:idx]
        right = candles[idx + 1:idx + 3]
        if all(candle["high"] >= item["high"] for item in left) and all(candle["high"] > item["high"] for item in right):
            pivots.append({"type": "H", "i": idx, "price": float(candle["high"])})
        if all(candle["low"] <= item["low"] for item in left) and all(candle["low"] < item["low"] for item in right):
            pivots.append({"type": "L", "i": idx, "price": float(candle["low"])})
    events, last_high, last_low, trend = [], None, None, 0
    for idx, candle in enumerate(candles):
        for pivot in [item for item in pivots if item["i"] == idx]:
            if pivot["type"] == "H":
                last_high = pivot
            else:
                last_low = pivot
        close = float(candle["close"])
        if last_high and idx > last_high["i"] + 1 and close > last_high["price"]:
            kind = "CHoCH" if trend < 0 else "BOS"
            events.append({"kind": kind, "side": "bull", "from": last_high["i"], "to": idx, "price": round(last_high["price"], 4)})
            trend, last_high = 1, None
        if last_low and idx > last_low["i"] + 1 and close < last_low["price"]:
            kind = "CHoCH" if trend > 0 else "BOS"
            events.append({"kind": kind, "side": "bear", "from": last_low["i"], "to": idx, "price": round(last_low["price"], 4)})
            trend, last_low = -1, None
    ranges = []
    for idx, candle in enumerate(candles):
        prev_close = float((candles[idx - 1] if idx else candle)["close"])
        ranges.append(max(float(candle["high"]) - float(candle["low"]),
                          abs(float(candle["high"]) - prev_close),
                          abs(float(candle["low"]) - prev_close), 0.01))
    closes = [float(item["close"]) for item in candles]
    def ema(period: int) -> List[float]:
        k = 2 / (period + 1)
        value = closes[0] if closes else 0.0
        values = []
        for close in closes:
            value = close * k + value * (1 - k)
            values.append(value)
        return values
    ema_fast = ema(5)
    ema_mid = ema(13)
    latest_event_for_seed = events[-1] if events else {}
    if latest_event_for_seed.get("side") == "bear":
        mode = "put"
    elif latest_event_for_seed.get("side") == "bull":
        mode = "call"
    elif closes[-1] <= ema_fast[-1] <= ema_mid[-1]:
        mode = "put"
    else:
        mode = "call"
    final_upper = None
    final_lower = None
    trail = []
    flips = []
    for idx, candle in enumerate(candles):
        prev = candles[max(0, idx - 1)]
        window = ranges[max(0, idx - 9):idx + 1]
        atr = sum(window) / max(1, len(window))
        mid = (float(candle["high"]) + float(candle["low"])) / 2
        basic_lower = mid - atr * 1.55
        basic_upper = mid + atr * 1.55
        prev_upper = final_upper if final_upper is not None else basic_upper
        prev_lower = final_lower if final_lower is not None else basic_lower
        flipped = False
        if mode == "call" and idx > 1 and float(candle["low"]) <= prev_lower:
            mode, final_upper, flipped = "put", basic_upper, True
        elif mode == "put" and idx > 1 and float(candle["high"]) >= prev_upper:
            mode, final_lower, flipped = "call", basic_lower, True
        final_upper = basic_upper if (basic_upper < prev_upper or float(prev["close"]) > prev_upper) else prev_upper
        final_lower = basic_lower if (basic_lower > prev_lower or float(prev["close"]) < prev_lower) else prev_lower
        active = final_lower if mode == "call" else final_upper
        point = {"i": idx, "time": candle.get("time"), "mode": mode, "price": round(float(active), 4), "flipped": flipped}
        trail.append(point)
        if flipped:
            flips.append(point)
    latest = trail[-1] if trail else {}
    latest_event = events[-1] if events else {}
    return {
        "bias": "bullish" if trend > 0 else "bearish" if trend < 0 else "neutral",
        "mode": "CALL" if latest.get("mode") == "call" else "PUT" if latest.get("mode") == "put" else "WAIT",
        "active_line": latest.get("price"),
        "latest_event": latest_event,
        "events": events[-20:],
        "flips": flips[-20:],
        "trail": trail[-400:],
        "reason": "CALL line trails bullish structure; PUT line trails bearish structure; signal flips only when candle range touches the opposite trail.",
    }


def _aggregate_candles(candles: List[Dict], seconds: int) -> List[Dict]:
    if seconds <= 60:
        return candles
    buckets = {}
    for candle in candles:
        stamp = datetime.fromisoformat(str(candle["time"]).replace("Z", "+00:00"))
        key = int(stamp.timestamp()) // seconds * seconds
        buckets.setdefault(key, []).append(candle)
    result = []
    for key in sorted(buckets):
        group = buckets[key]
        sources = sorted({str(item.get("source") or "unknown") for item in group})
        result.append({"time": datetime.fromtimestamp(key, timezone.utc).isoformat(),
                       "open": group[0]["open"], "high": max(item["high"] for item in group),
                       "low": min(item["low"] for item in group), "close": group[-1]["close"],
                       "volume": sum(item["volume"] for item in group),
                       "source": "mixed" if len(sources) > 1 else sources[0],
                       "sources": sources})
    return result


def _alias_keys(symbol: str) -> List[str]:
    key = str(symbol or "").upper().replace(" ", "")
    aliases = {key}
    if key in {"NIFTY", "NIFTY50"}:
        aliases.update({"NIFTY", "NIFTY50"})
    if key in {"NIFTYBANK", "BANKNIFTY"}:
        aliases.update({"NIFTYBANK", "BANKNIFTY"})
    if key in {"BSESENSEX", "SENSEX"}:
        aliases.update({"BSESENSEX", "SENSEX"})
    if key in {"INDIAVIX", "VIX"}:
        aliases.update({"INDIAVIX", "VIX"})
    return sorted(aliases)


def _trade_markers(connection, security: Dict, candles: List[Dict]) -> List[Dict]:
    """Return paper-trade entry/exit markers that belong on this chart.

    Equity charts show trades in that exact equity. Index charts also show the
    related CE/PE paper trades, e.g. NIFTY 50 displays NIFTY options. Markers
    are visual evidence only; they do not affect ML or order routing.
    """
    if not candles:
        return []
    try:
        start = datetime.fromisoformat(str(candles[0]["time"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(candles[-1]["time"]).replace("Z", "+00:00"))
    except Exception:
        return []
    aliases = _alias_keys(security.get("symbol"))
    rows = connection.execute(text("""
        SELECT a.id,a.signal_at,a.exit_at,a.side,a.quantity,a.signal_probability,
               a.decision_price,a.theoretical_fill_price,a.realised_exit_price,a.net_pnl,
               a.stop_loss_price,a.take_profit_price,a.audit_status,a.exit_reason,
               i.exchange,i.symbol,i.instrument_type,COALESCE(i.underlying_symbol,i.symbol) underlying_symbol
        FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
        WHERE a.signal_at BETWEEN :start_time AND :end_time
          AND (
            (i.exchange=:exchange AND i.symbol=:symbol)
            OR REPLACE(UPPER(COALESCE(i.underlying_symbol,i.symbol)),' ','') = ANY(:aliases)
            OR REPLACE(UPPER(i.symbol),' ','') = ANY(:aliases)
          )
        ORDER BY a.signal_at ASC
        LIMIT 250
    """), {"exchange": security["exchange"], "symbol": security["symbol"],
           "aliases": aliases, "start_time": start, "end_time": end}).mappings().all()
    markers = []
    for row in rows:
        instrument_type = str(row["instrument_type"] or "EQ")
        side = str(row["side"] or "BUY")
        option_word = "CALL" if instrument_type == "CE" else "PUT" if instrument_type == "PE" else instrument_type
        label = f"{side} {option_word}" if instrument_type in {"CE", "PE"} else side
        entry_price = row["theoretical_fill_price"] or row["decision_price"]
        markers.append({
            "id": int(row["id"]), "kind": "entry", "time": row["signal_at"].isoformat(),
            "side": side, "instrument_type": instrument_type, "label": label,
            "symbol": row["symbol"], "underlying_symbol": row["underlying_symbol"],
            "price": float(entry_price) if entry_price is not None else None,
            "quantity": int(row["quantity"] or 0), "probability": float(row["signal_probability"] or 0),
            "status": row["audit_status"], "stop_loss": float(row["stop_loss_price"]) if row["stop_loss_price"] is not None else None,
            "take_profit": float(row["take_profit_price"]) if row["take_profit_price"] is not None else None,
        })
        if row["exit_at"]:
            markers.append({
                "id": int(row["id"]), "kind": "exit", "time": row["exit_at"].isoformat(),
                "side": side, "instrument_type": instrument_type, "label": str(row["exit_reason"] or "EXIT"),
                "symbol": row["symbol"], "underlying_symbol": row["underlying_symbol"],
                "price": float(row["realised_exit_price"]) if row["realised_exit_price"] is not None else None,
                "pnl": float(row["net_pnl"]) if row["net_pnl"] is not None else None,
                "status": row["audit_status"], "exit_reason": row["exit_reason"],
            })
    return markers


def _live_chart(security: Dict, timeframe: str) -> Dict:
    if not DATABASE_URL:
        return {}
    interval = "1second" if timeframe == "1s" else "1minute"
    limit = 9000 if timeframe == "1s" else 1500
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT i.id instrument_id,b.bar_time,b.open_price,b.high_price,b.low_price,b.close_price,b.volume,b.source
                FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
                WHERE i.exchange=:exchange AND i.symbol=:symbol AND b.interval=:interval
                  AND b.source = ANY(:sources)
                  AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15:00'
                  AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time < TIME '15:30:00'
                ORDER BY b.bar_time DESC LIMIT :limit
            """), {"exchange": security["exchange"], "symbol": security["symbol"], "interval": interval,
                   "sources": list(LIVE_SOURCES), "limit": limit}).mappings().all()
            if not rows:
                instrument_id = connection.execute(text("""
                    SELECT id FROM instrument_master
                    WHERE exchange=:exchange AND symbol=:symbol AND is_active
                    ORDER BY id DESC LIMIT 1
                """), {"exchange": security["exchange"], "symbol": security["symbol"]}).scalar_one_or_none()
            else:
                instrument_id = int(rows[0]["instrument_id"])
            raw_marker_connection = connection
    finally:
        engine.dispose()
    candles = [{"time": row["bar_time"].isoformat(), "open": float(row["open_price"]),
                "high": float(row["high_price"]), "low": float(row["low_price"]),
                "close": float(row["close_price"]), "volume": int(row["volume"] or 0),
                "source": str(row["source"] or "unknown")}
               for row in reversed(rows)]
    used_partial = False
    client = _redis_client()
    if client and instrument_id:
        try:
            raw = client.get(f"nivesh:partial_bar:{instrument_id}:{interval}")
            partial = json.loads(raw) if raw else None
        except Exception:
            partial = None
        if partial:
            forming = {"time": partial["bar_time"], "open": float(partial["open"]), "high": float(partial["high"]),
                       "low": float(partial["low"]), "close": float(partial["close"]), "volume": int(partial.get("volume") or 0),
                       "source": "live_forming"}
            forming_time = datetime.fromisoformat(str(forming["time"]).replace("Z", "+00:00"))
            if candles:
                last_time = datetime.fromisoformat(str(candles[-1]["time"]).replace("Z", "+00:00"))
                if forming_time == last_time:
                    candles[-1] = forming
                    used_partial = True
                elif forming_time > last_time:
                    candles.append(forming)
                    used_partial = True
            else:
                candles.append(forming)
                used_partial = True
    if timeframe in {"5m", "15m", "1H"}:
        candles = _aggregate_candles(candles, TIMEFRAMES[timeframe])
    if len(candles) < 2:
        return {}
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            markers = _trade_markers(connection, security, candles)
    finally:
        engine.dispose()
    sources = sorted({str(item.get("source") or "unknown") for item in candles})
    has_live = any(source in {"upstox_v3", "zerodha_kite", "live_forming"} for source in sources)
    has_rest = any(source in {"upstox_rest_intraday", "upstox_rest_5m", "kite_gap_backfill"} for source in sources)
    change = candles[-1]["close"] - candles[-2]["close"]
    return {"symbol": security["symbol"], "name": security["name"], "exchange": security["exchange"], "timeframe": timeframe,
            "price": candles[-1]["close"], "change": round(change, 2),
            "change_pct": round(change / candles[-2]["close"] * 100, 2) if candles[-2]["close"] else 0.0,
            "candles": candles, "indicators": _indicators(candles),
            "candle_analysis": _candle_analysis(candles),
            "structure_signal": _structure_signal(candles),
            "markers": markers,
            "latest_source": candles[-1].get("source") or "unknown",
            "source_summary": {"sources": sources, "has_live_stream": has_live, "has_rest_repair": has_rest},
            "data_mode": ("provider_live_1second_microstructure" if timeframe == "1s" and not used_partial
                          else "provider_live_forming_candle" if used_partial else "provider_live_completed_bars"),
            "updated_at": datetime.now(timezone.utc).isoformat()}


def _daily_chart(security: Dict) -> Dict:
    stored = HistoryStore().candles(security["symbol"], security["exchange"], "day", 500)
    candles = [{"time": item["timestamp"], "open": item["open"], "high": item["high"], "low": item["low"], "close": item["close"], "volume": item["volume"]} for item in stored]
    live = _live_chart(security, "1m")
    has_live_session = False
    if live and live.get("candles"):
        session = live["candles"]
        live_day = {"time": session[-1]["time"], "open": session[0]["open"], "high": max(item["high"] for item in session),
                    "low": min(item["low"] for item in session), "close": session[-1]["close"],
                    "volume": sum(item["volume"] for item in session)}
        live_date = live_day["time"][:10]
        candles = [item for item in candles if str(item["time"])[:10] != live_date]
        candles.append(live_day)
        has_live_session = True
    if len(candles) < 2:
        return {}
    candles = candles[-500:]
    closes = [item["close"] for item in candles]
    change = closes[-1] - closes[-2]
    markers = []
    if DATABASE_URL:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection:
                markers = _trade_markers(connection, security, candles)
        finally:
            engine.dispose()
    return {"symbol": security["symbol"], "name": security["name"], "exchange": security["exchange"], "timeframe": "1D",
            "price": closes[-1], "change": round(change, 2), "change_pct": round(change / closes[-2] * 100, 2) if closes[-2] else 0.0,
            "candles": candles, "indicators": _indicators(candles),
            "candle_analysis": _candle_analysis(candles),
            "structure_signal": _structure_signal(candles),
            "markers": markers,
            "data_mode": "stored_daily_plus_live_session" if has_live_session else "kite_historical",
            "updated_at": datetime.now(timezone.utc).isoformat()}


def chart_data(symbol: str, timeframe: str = "5m") -> Dict:
    timeframe = timeframe if timeframe in TIMEFRAMES else "5m"
    security = resolve_security(symbol)

    if security and timeframe != "1D":
        live = _live_chart(security, timeframe)
        if live:
            return live

    daily = _daily_chart(security) if security and timeframe == "1D" else {}
    if daily:
        return daily

    # Graceful index fallback: support NIFTY 50, BANKNIFTY, SENSEX, INDIA VIX
    clean_sym = symbol.upper().replace(" ", "")
    idx_match = next((item for item in INDICES if item["symbol"].replace(" ", "") == clean_sym), None)
    if idx_match or (security and security.get("series") == "INDEX"):
        idx_sym = idx_match["symbol"] if idx_match else security["symbol"]
        idx_price = float(idx_match["price"]) if idx_match else 24800.0
        candles = generate_candles(idx_sym, idx_price, 180, max(1, TIMEFRAMES[timeframe] // 60))
        closes = [item["close"] for item in candles]
        change = closes[-1] - closes[-2]
        return {
            "symbol": idx_sym, "name": idx_sym, "exchange": "NSE", "timeframe": timeframe,
            "price": closes[-1], "change": round(change, 2), "change_pct": round(change / closes[-2] * 100, 2),
            "candles": candles, "indicators": _indicators(candles),
            "candle_analysis": _candle_analysis(candles),
            "structure_signal": _structure_signal(candles),
            "data_mode": "index_preview", "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    stock = next((item for item in market_snapshot() if item["symbol"] == symbol.upper()), None)
    if not stock:
        security = security or resolve_security(symbol)
        if not security:
            raise ValueError("Unknown NSE/BSE stock")
        stored = HistoryStore().candles(security["symbol"], security["exchange"], "day", 500)
        if len(stored) >= 2:
            candles = [{"time": item["timestamp"], "open": item["open"], "high": item["high"], "low": item["low"], "close": item["close"], "volume": item["volume"]} for item in stored]
            closes = [item["close"] for item in candles]
            change = closes[-1] - closes[-2]
            return {"symbol": security["symbol"], "name": security["name"], "exchange": security["exchange"], "timeframe": timeframe,
                    "price": closes[-1], "change": round(change, 2), "change_pct": round(change / closes[-2] * 100, 2),
                    "candles": candles, "indicators": _indicators(candles),
                    "candle_analysis": _candle_analysis(candles),
                    "structure_signal": _structure_signal(candles),
                    "data_mode": "kite_historical", "updated_at": datetime.now(timezone.utc).isoformat()}
        stock = {"symbol": security["symbol"], "name": security["name"], "price": 1000.0}

    candles = generate_candles(stock["symbol"], stock["price"], 180, max(1, TIMEFRAMES[timeframe] // 60))
    closes = [item["close"] for item in candles]
    change = closes[-1] - closes[-2]
    return {
        "symbol": stock["symbol"], "name": stock["name"], "exchange": "NSE", "timeframe": timeframe,
        "price": closes[-1], "change": round(change, 2), "change_pct": round(change / closes[-2] * 100, 2),
        "candles": candles, "indicators": _indicators(candles),
        "candle_analysis": _candle_analysis(candles),
        "structure_signal": _structure_signal(candles),
        "data_mode": "simulated", "updated_at": datetime.now(timezone.utc).isoformat(),
    }
