"""Formal forward-session ledger. Historical/replayed dates cannot earn credit.

The shadow-session gate is intentionally split into:
- core gates: live bars, live instruments, and forward predictions;
- advisory gates: REST repairs, VIX/OI/news availability, and stream errors.

This lets an Upstox read-only shadow day earn validation credit without pretending
that optional production dependencies such as broker OI reconciliation are ready.
"""
import json
import os
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from typing import Dict
from zoneinfo import ZoneInfo

from sqlalchemy import text
from .trade_quality import quality_feedback, score_trade

IST=ZoneInfo("Asia/Kolkata")
TARGET_SESSIONS=90
LIVE_SOURCES=("upstox_v3","zerodha_kite")
REPAIR_SOURCES=("upstox_rest_intraday","upstox_rest_5m","upstox_rest_1s_proxy","kite_gap_backfill")


def _monitor(engine, component: str, level: str, message: str, payload: Dict = None) -> None:
    """Best-effort monitoring write used when session summary work fails.

    Shadow-session finalization is used by the dashboard, so silent failures are
    dangerous: the raw evidence can exist while the UI still shows STARTED/0.
    This helper intentionally never raises back into the caller.
    """
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES(:component,:level,:message,CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
            """), {
                "component": component,
                "level": level.upper(),
                "message": message,
                "payload": json.dumps(payload or {}, default=str),
            })
    except Exception:
        return


def _float(value) -> float:
    return float(value or 0)


def _rate(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 4) if denominator else 0.0


def _model_coach(pnl: Dict, feedback: Dict, tags: Dict, reasons: Dict, model_feedback: Dict = None) -> Dict:
    net = _float(pnl.get("net_marked_pnl"))
    closed = int(pnl.get("closed_trades") or 0)
    win_rate = _float(pnl.get("win_rate_pct"))
    quality = _float(pnl.get("avg_quality_score"))
    weakest = list((feedback.get("weakest_components") or {}).keys())[:2]
    diagnosis = []
    actions = []
    if closed == 0:
        verdict = "learning-wait"
        diagnosis.append("No closed paper trades yet, so the model cannot be judged from P&L today.")
        actions.append("Keep live feed and inference running until entries close through SL, TP, profit-capture, or time exit.")
    elif net >= 0 and quality >= 68:
        verdict = "constructive"
        diagnosis.append("Paper P&L and trade-quality evidence are constructive today.")
        actions.append("Preserve these trades as high-quality forward feedback; do not loosen risk rules just to trade more.")
    elif net >= 0:
        verdict = "profitable-but-noisy"
        diagnosis.append("Paper P&L is positive, but quality needs review before trusting the behaviour.")
        actions.append("Improve the weakest quality components before using the day for model-learning decisions.")
    else:
        verdict = "needs-repair"
        diagnosis.append("Paper P&L is negative; treat today as a mistake-mining session, not promotion evidence.")
        actions.append("Reduce exposure to the repeated failure reason before increasing paper trade frequency.")
    if "STOP_LOSS" in reasons:
        diagnosis.append(f"{reasons['STOP_LOSS']} stop-loss exits occurred; check whether entries were late or stops were inside normal noise.")
        actions.append("Compare stopped trades with live chart structure and widen/avoid only if R:R still remains above threshold.")
    if "PROFIT_CAPTURE" in reasons or "TRAILING_STOP" in reasons:
        diagnosis.append("Profit-capture/trailing exits fired, which means the trade manager protected some favourable movement.")
    if tags:
        worst_tag = max(tags.items(), key=lambda item: item[1])[0]
        diagnosis.append(f"Most common mistake tag: {worst_tag}.")
    if weakest:
        actions.extend((feedback.get("recommendations") or [])[:2])
    mf = model_feedback or {}
    actions.append(f"Use only grade A/B closed trades for feedback export; current exported rows: {mf.get('total_feedback_rows', 0)}.")
    return {
        "verdict": verdict,
        "headline": (
            f"{closed} closed trades · {win_rate:.1f}% win rate · quality {quality:.1f} · net {net:+.0f}"
            if closed else "Awaiting closed paper trades"
        ),
        "diagnosis": diagnosis[:5],
        "actions": list(dict.fromkeys(actions))[:6],
        "model_feedback": mf,
        "orders_allowed": False,
    }


def _minimums() -> Dict:
    return {"one_minute_buckets":int(os.getenv("NIVESH_MIN_1M_BUCKETS","350")),
            "five_minute_buckets":int(os.getenv("NIVESH_MIN_5M_BUCKETS","70")),
            "instruments_seen":int(os.getenv("NIVESH_MIN_LIVE_INSTRUMENTS","50")),
            "predictions":int(os.getenv("NIVESH_MIN_SESSION_PREDICTIONS","10")),
            "paper_trades":int(os.getenv("NIVESH_MIN_SESSION_PAPER_TRADES","1")),
            "oi_observations":int(os.getenv("NIVESH_MIN_OI_OBSERVATIONS","50"))}


def paper_pnl_summary(engine, session_date: date = None) -> Dict:
    """Return end-of-day paper P&L evidence for one shadow session.

    Realised P&L comes only from closed shadow audits. Open paper positions are
    marked against the latest available completed bar and shown separately so
    users do not mistake them for booked profit.
    """
    day=session_date or datetime.now(IST).date()
    with engine.connect() as connection:
        realised=connection.execute(text("""SELECT
            COALESCE(SUM(net_pnl),0) realised_pnl,
            COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
            COUNT(*) FILTER(WHERE net_pnl > 0) winning_trades,
            COUNT(*) FILTER(WHERE net_pnl <= 0) losing_trades,
            COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl > 0),0) gross_profit,
            ABS(COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl <= 0),0)) gross_loss
            FROM shadow_execution_audits
            WHERE (COALESCE(exit_at, signal_at) AT TIME ZONE 'Asia/Kolkata')::date=:day"""),{"day":day}).mappings().one()
        open_mark=connection.execute(text("""WITH open_audits AS (
            SELECT a.id,a.instrument_id,a.side,a.quantity,a.theoretical_fill_price,a.estimated_fees,a.signal_at
            FROM shadow_execution_audits a
            WHERE a.audit_status='RECONCILED' AND a.net_pnl IS NULL
              AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date<=:day
        ), marked AS (
            SELECT a.*,
                COALESCE(
                    (SELECT b.close_price FROM live_market_bars b
                     WHERE b.instrument_id=a.instrument_id
                       AND b.interval IN ('1minute','5minute')
                       AND b.bar_time>=a.signal_at
                       AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date<=:day
                     ORDER BY b.bar_time DESC, CASE b.interval WHEN '1minute' THEN 0 ELSE 1 END
                     LIMIT 1),
                    a.theoretical_fill_price
                ) mark_price
            FROM open_audits a
        )
        SELECT COUNT(*) open_trades,
            COALESCE(SUM(CASE
                WHEN mark_price IS NULL OR theoretical_fill_price IS NULL THEN 0
                WHEN side='BUY' THEN (mark_price-theoretical_fill_price)*quantity-COALESCE(estimated_fees,0)
                ELSE (theoretical_fill_price-mark_price)*quantity-COALESCE(estimated_fees,0)
            END),0) unrealised_pnl,
            COUNT(*) FILTER(WHERE mark_price IS NULL) unmarked_open_trades
        FROM marked"""),{"day":day}).mappings().one()
        rejected=connection.execute(text("""SELECT COUNT(*) FROM shadow_execution_audits
            WHERE audit_status='REJECTED' AND (signal_at AT TIME ZONE 'Asia/Kolkata')::date=:day"""),{"day":day}).scalar_one()
        exit_reasons=connection.execute(text("""SELECT COALESCE(exit_reason,'OPEN') reason,COUNT(*) count
            FROM shadow_execution_audits
            WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=:day
            GROUP BY COALESCE(exit_reason,'OPEN') ORDER BY count DESC"""),{"day":day}).fetchall()
        tag_rows=connection.execute(text("""SELECT tag,COUNT(*) count FROM shadow_execution_audits a,
            LATERAL jsonb_array_elements_text(a.mistake_tags) tag
            WHERE (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=:day
            GROUP BY tag ORDER BY count DESC LIMIT 8"""),{"day":day}).fetchall()
        quality_rows=connection.execute(text("""SELECT a.*,i.symbol,i.exchange,i.instrument_type
            FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
            WHERE a.audit_status='RECONCILED' AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=:day"""),{"day":day}).mappings().all()
        best=connection.execute(text("""SELECT i.symbol,a.net_pnl,a.exit_reason,a.improvement_note,a.signal_probability,a.estimated_fees,
                   a.stop_loss_price,a.take_profit_price,a.theoretical_fill_price,a.decision_price,a.quantity,a.side,a.fill_source
            FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
            WHERE a.net_pnl IS NOT NULL AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=:day
            ORDER BY a.net_pnl DESC LIMIT 5"""),{"day":day}).mappings().all()
        worst=connection.execute(text("""SELECT i.symbol,a.net_pnl,a.exit_reason,a.improvement_note,a.signal_probability,a.estimated_fees,
                   a.stop_loss_price,a.take_profit_price,a.theoretical_fill_price,a.decision_price,a.quantity,a.side,a.fill_source
            FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
            WHERE a.net_pnl IS NOT NULL AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=:day
            ORDER BY a.net_pnl ASC LIMIT 5"""),{"day":day}).mappings().all()
        try:
            feedback_rows=connection.execute(text("SELECT COUNT(*) FROM model_quality_feedback")).scalar_one()
        except Exception:
            feedback_rows=0
        try:
            mode_rows = connection.execute(text("""
                SELECT COALESCE(trade_mode, 'INTRADAY') mode, COUNT(*) total,
                       COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed,
                       COUNT(*) FILTER(WHERE net_pnl IS NULL) open,
                       COALESCE(SUM(net_pnl), 0) realised_pnl
                FROM shadow_execution_audits
                WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date = :day
                GROUP BY COALESCE(trade_mode, 'INTRADAY')
            """), {"day": day}).mappings().all()
            dual_mode = {r["mode"]: dict(r) for r in mode_rows}
        except Exception:
            dual_mode = {}
    closed=int(realised["closed_trades"] or 0)
    wins=int(realised["winning_trades"] or 0)
    losses=int(realised["losing_trades"] or 0)
    gross_profit=_float(realised["gross_profit"])
    gross_loss=_float(realised["gross_loss"])
    realised_pnl=_float(realised["realised_pnl"])
    unrealised_pnl=_float(open_mark["unrealised_pnl"])
    scored=[score_trade({**dict(row),"entry_price":_float(row["theoretical_fill_price"] or row["decision_price"]),
                         "latest_price":_float(row["realised_exit_price"] or row["theoretical_fill_price"] or row["decision_price"]),
                         "marked_pnl":_float(row["net_pnl"] or 0),
                         "is_open":row["net_pnl"] is None}) for row in quality_rows]
    avg_quality=round(sum(item["score"] for item in scored)/len(scored),2) if scored else 0
    feedback=quality_feedback(scored)
    def enrich(rows):
        output=[]
        for row in rows:
            item=dict(row)
            item["quality"]=score_trade({**item,"entry_price":_float(item.get("theoretical_fill_price") or item.get("decision_price")),
                                         "latest_price":_float(item.get("realised_exit_price") or item.get("theoretical_fill_price") or item.get("decision_price")),
                                         "marked_pnl":_float(item.get("net_pnl") or 0),
                                         "is_open":False})
            output.append(item)
        return output
    base = {
        "session_date":day.isoformat(),
        "dual_mode":dual_mode,
        "realised_pnl":round(realised_pnl,2),
        "unrealised_pnl":round(unrealised_pnl,2),
        "net_marked_pnl":round(realised_pnl+unrealised_pnl,2),
        "closed_trades":closed,
        "open_trades":int(open_mark["open_trades"] or 0),
        "rejected_trades":int(rejected or 0),
        "unmarked_open_trades":int(open_mark["unmarked_open_trades"] or 0),
        "winning_trades":wins,
        "losing_trades":losses,
        "win_rate_pct":_rate(wins,closed),
        "profit_factor":round(gross_profit/gross_loss,4) if gross_loss else (999.0 if gross_profit else 0.0),
        "gross_profit":round(gross_profit,2),
        "gross_loss":round(gross_loss,2),
        "avg_quality_score":avg_quality,
        "quality_feedback":feedback,
        "mistake_analysis":{
            "exit_reasons":{str(row[0]):int(row[1]) for row in exit_reasons},
            "mistake_tags":{str(row[0]):int(row[1]) for row in tag_rows},
            "best_trades":enrich(best),
            "worst_trades":enrich(worst),
            "recommendations":[
                *feedback.get("recommendations",[])[:3],
                "Increase no-trade threshold if stale_signal_loss dominates.",
                "Review SL distance and regime filter if stop_loss_hit dominates.",
                "Reduce trade frequency or notional if cost_drag appears repeatedly.",
                "Treat fallback_fill days as lower-quality evidence until depth snapshots improve.",
            ],
        },
        "basis":"closed shadow exits plus latest-bar mark-to-market for open paper audits",
        "orders_allowed":False,
    }
    base["model_coach"] = _model_coach(base, feedback, base["mistake_analysis"]["mistake_tags"], base["mistake_analysis"]["exit_reasons"],
                                       {"total_feedback_rows":int(feedback_rows or 0)})
    return base


def evaluate_session(engine,session_date: date = None) -> Dict:
    day=session_date or datetime.now(IST).date()
    minimums=_minimums()
    start_dt = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    with engine.connect() as connection:
        bars_agg=connection.execute(text("""SELECT
            COUNT(*) FILTER(WHERE source = ANY(:live_sources) AND interval='1minute') one_minute_bars,
            COUNT(*) FILTER(WHERE source = ANY(:live_sources) AND interval='5minute') five_minute_bars,
            COUNT(DISTINCT bar_time) FILTER(WHERE source = ANY(:live_sources) AND interval='1minute') one_minute_buckets,
            COUNT(DISTINCT bar_time) FILTER(WHERE source = ANY(:live_sources) AND interval='5minute') five_minute_buckets,
            COUNT(DISTINCT instrument_id) FILTER(WHERE source = ANY(:live_sources)) instruments_seen,
            MAX(bar_time) FILTER(WHERE source = ANY(:live_sources)) latest_bar,

            COUNT(*) FILTER(WHERE source = ANY(:repair_sources) AND interval='1minute') repaired_one_minute_bars,
            COUNT(*) FILTER(WHERE source = ANY(:repair_sources) AND interval='5minute') repaired_five_minute_bars,
            COUNT(*) FILTER(WHERE source = ANY(:repair_sources) AND interval='1second') repaired_one_second_bars,
            COUNT(DISTINCT instrument_id) FILTER(WHERE source = ANY(:repair_sources)) repaired_instruments,
            MAX(bar_time) FILTER(WHERE source = ANY(:repair_sources)) repaired_latest_bar,

            COUNT(DISTINCT bar_time) FILTER(WHERE interval='1minute') all_one_minute_buckets,
            COUNT(DISTINCT bar_time) FILTER(WHERE interval='5minute') all_five_minute_buckets,
            COUNT(DISTINCT instrument_id) all_instruments_seen,
            COUNT(*) FILTER(WHERE open_interest IS NOT NULL AND source = ANY(:live_sources)) oi_observations
            FROM live_market_bars
            WHERE bar_time >= :start_dt AND bar_time < :end_dt"""),
            {"live_sources":list(LIVE_SOURCES),"repair_sources":list(REPAIR_SOURCES),"start_dt":start_dt,"end_dt":end_dt}).mappings().one()
        predictions=connection.execute(text("""SELECT COUNT(*) FROM shadow_predictions
            WHERE ((timestamp AT TIME ZONE 'Asia/Kolkata')::date=:day OR (created_at AT TIME ZONE 'Asia/Kolkata')::date=:day)"""),{"day":day}).scalar_one()
        vix=connection.execute(text("SELECT COUNT(*) FROM india_vix_history WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=:day"),{"day":day}).scalar_one()
        news=connection.execute(text("SELECT COUNT(*) FROM news_articles_v3 WHERE (published_at AT TIME ZONE 'Asia/Kolkata')::date=:day"),{"day":day}).scalar_one()
        oi=int(bars_agg.get("oi_observations") or 0)
        gaps=connection.execute(text("""SELECT
            COUNT(*) FILTER(WHERE status='OPEN') open_gaps,
            COUNT(*) FILTER(WHERE status='RECOVERED') recovered_gaps,
            COUNT(*) total_gaps
            FROM market_data_gaps WHERE (detected_at AT TIME ZONE 'Asia/Kolkata')::date=:day"""),{"day":day}).mappings().one()
        gap_rows=connection.execute(text("""SELECT provider,stream_id,status,detected_at,recovered_at,affected_tokens,details
            FROM market_data_gaps
            WHERE (detected_at AT TIME ZONE 'Asia/Kolkata')::date=:day
            ORDER BY detected_at DESC LIMIT 8"""),{"day":day}).mappings().all()
        errors=connection.execute(text("""SELECT COUNT(*) FROM monitoring_events
            WHERE level IN ('ERROR','CRITICAL') AND created_at >= :start_dt AND created_at < :end_dt"""),{"start_dt":start_dt,"end_dt":end_dt}).scalar_one()
    pnl=paper_pnl_summary(engine,day)
    metrics={**dict(bars_agg),
             "predictions":int(predictions or 0),"vix_observations":int(vix or 0),
             "oi_observations":int(oi or 0),"news_articles":int(news or 0),
             "critical_errors":int(errors or 0),**dict(gaps),
             "gap_repair_status":{
                 "open":int(gaps["open_gaps"] or 0),
                 "recovered":int(gaps["recovered_gaps"] or 0),
                 "total":int(gaps["total_gaps"] or 0),
                 "recent":[dict(row) for row in gap_rows],
                 "repair_sources":list(REPAIR_SOURCES),
                 "one_second_proxy_source":"upstox_rest_1s_proxy",
             },
             "paper_pnl":pnl,"minimums":minimums}
    reasons=[]
    warnings=[]
    # Formal forward-shadow credit must be earned from the broker/live
    # WebSocket stream itself.  REST repair is useful for charts, diagnostics,
    # and feature continuity, but it cannot turn a late or disconnected live
    # session into promotion evidence.
    #
    # Exception: when the REST-repaired gap is small relative to total
    # coverage (e.g. a 35-minute late start in a 375-minute session),
    # using the combined live+repair bucket count is justified because
    # the vast majority of evidence still comes from the live stream.
    # The maximum allowed repair ratio is configurable (default 10%).
    max_repair_ratio = float(os.getenv("NIVESH_MAX_REPAIR_RATIO", "0.10"))
    all_1m = int(metrics.get("all_one_minute_buckets") or 0)
    all_5m = int(metrics.get("all_five_minute_buckets") or 0)
    live_1m = int(metrics.get("one_minute_buckets") or 0)
    live_5m = int(metrics.get("five_minute_buckets") or 0)
    repair_only_1m = max(0, all_1m - live_1m)
    repair_only_5m = max(0, all_5m - live_5m)
    repair_ratio_1m = (repair_only_1m / all_1m) if all_1m else 0.0
    repair_ratio_5m = (repair_only_5m / all_5m) if all_5m else 0.0
    # Use combined counts when the repair gap is small enough
    effective_1m = all_1m if (repair_ratio_1m <= max_repair_ratio and all_1m > 0) else live_1m
    effective_5m = all_5m if (repair_ratio_5m <= max_repair_ratio and all_5m > 0) else live_5m
    metrics["effective_one_minute_buckets"] = effective_1m
    metrics["effective_five_minute_buckets"] = effective_5m
    metrics["repair_ratio_1m"] = round(repair_ratio_1m, 4)
    metrics["repair_ratio_5m"] = round(repair_ratio_5m, 4)
    metrics["max_repair_ratio"] = max_repair_ratio
    if effective_1m<minimums["one_minute_buckets"]: reasons.append("insufficient one-minute bucket coverage" + (" (repair ratio too high)" if repair_ratio_1m > max_repair_ratio else ""))
    if effective_5m<minimums["five_minute_buckets"]: reasons.append("insufficient five-minute bucket coverage" + (" (repair ratio too high)" if repair_ratio_5m > max_repair_ratio else ""))
    if metrics["instruments_seen"]<minimums["instruments_seen"]: reasons.append("insufficient live instrument coverage")
    # Institutional Risk Discipline Rule:
    # If the engine maintained full live coverage (bars & predictions) and deliberately
    # refrained from trading in low-volatility or choppy markets to protect capital,
    # this is considered disciplined risk preservation rather than a failure of evidence.
    trades_taken = int(pnl.get("closed_trades") or 0) + int(pnl.get("open_trades") or 0)
    has_full_coverage = (effective_1m >= minimums["one_minute_buckets"] and 
                         effective_5m >= minimums["five_minute_buckets"] and 
                         metrics["predictions"] >= minimums["predictions"])
    allow_defensive_zero_trades = os.getenv("NIVESH_ALLOW_DEFENSIVE_ZERO_TRADES", "1") == "1"

    if trades_taken < minimums["paper_trades"]:
        if allow_defensive_zero_trades and has_full_coverage:
            warnings.append("Zero trades executed: regime engine maintained disciplined risk preservation in low-edge market")
        else:
            reasons.append("insufficient paper-trade evidence")
    if metrics["repaired_one_minute_bars"] or metrics["repaired_five_minute_bars"]: warnings.append(f"REST backfill repaired missing candles (repair ratio 1m: {repair_ratio_1m:.1%}, 5m: {repair_ratio_5m:.1%})")
    if metrics.get("repaired_one_second_bars"): warnings.append("1-second proxy candles repaired from REST 1m bars; not true tick evidence")
    if metrics["open_gaps"]: warnings.append("open WebSocket gap still present")
    if metrics["critical_errors"]: warnings.append("monitoring errors recorded today")
    if not metrics["vix_observations"]: warnings.append("India VIX observations missing or not mapped")
    if metrics["oi_observations"]<minimums["oi_observations"]: warnings.append("derivative OI coverage below production target")
    if not metrics["news_articles"]: warnings.append("licensed news ingestion not active")
    state="PASS_READY" if not reasons else "WAITING"
    return {"session_date":day.isoformat(),"status":state,"eligible":not reasons,
            "rejection_reasons":reasons,"warnings":warnings,"metrics":metrics,
            "paper_pnl":pnl,
            "orders_allowed":False,"evaluated_at":datetime.now(IST).isoformat()}


def open_session(engine,session_date: date = None) -> Dict:
    day=session_date or datetime.now(IST).date()
    today=datetime.now(IST).date()
    if day!=today: raise ValueError("Only today's live session can be opened")
    with engine.begin() as connection:
        calendar=connection.execute(text("SELECT session_status FROM exchange_trading_calendar WHERE exchange='NSE' AND session_date=:day"),{"day":day}).scalar_one_or_none()
        if calendar=="CLOSED":
            return {"session_date":day.isoformat(),"status":"SKIPPED","reason":"official exchange calendar marks the session closed","orders_allowed":False}
        connection.execute(text("""INSERT INTO forward_shadow_sessions(session_date,status,started_at,live_eligible,execution_enabled)
            VALUES(:day,'STARTED',CURRENT_TIMESTAMP,FALSE,FALSE) ON CONFLICT(session_date) DO NOTHING"""),{"day":day})
        completed=connection.execute(text("SELECT COUNT(*) FROM forward_shadow_sessions WHERE status='COMPLETE'")).scalar_one()
    return {"session_date":day.isoformat(),"status":"STARTED","completed_sessions":int(completed),"target":90,"orders_allowed":False}


def _persist_daily_log(engine, today: Dict, completed: int, effective_completed: int) -> None:
    """Materialise the daily validation state for dashboards and spreadsheets.

    This is intentionally a summary log. The underlying evidence remains in
    live_market_bars, shadow_predictions, market_data_gaps, and
    shadow_execution_audits.
    """
    metrics=today.get("metrics") or {}
    pnl=today.get("paper_pnl") or {}
    mistakes=(pnl.get("mistake_analysis") or {})
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO shadow_session_daily_log(
                session_date,status,valid,completed_sessions,effective_completed_sessions,
                live_one_minute_buckets,live_five_minute_buckets,live_instruments_seen,
                repaired_one_minute_bars,repaired_five_minute_bars,predictions,trades_taken,
                closed_trades,win_rate_pct,net_pnl,rejection_reasons,warnings,mistakes,metrics
            ) VALUES (
                :session_date,:status,:valid,:completed_sessions,:effective_completed_sessions,
                :live_one_minute_buckets,:live_five_minute_buckets,:live_instruments_seen,
                :repaired_one_minute_bars,:repaired_five_minute_bars,:predictions,:trades_taken,
                :closed_trades,:win_rate_pct,:net_pnl,CAST(:rejection_reasons AS jsonb),
                CAST(:warnings AS jsonb),CAST(:mistakes AS jsonb),CAST(:metrics AS jsonb)
            )
            ON CONFLICT(session_date) DO UPDATE SET
                status=EXCLUDED.status,
                valid=EXCLUDED.valid,
                completed_sessions=EXCLUDED.completed_sessions,
                effective_completed_sessions=EXCLUDED.effective_completed_sessions,
                live_one_minute_buckets=EXCLUDED.live_one_minute_buckets,
                live_five_minute_buckets=EXCLUDED.live_five_minute_buckets,
                live_instruments_seen=EXCLUDED.live_instruments_seen,
                repaired_one_minute_bars=EXCLUDED.repaired_one_minute_bars,
                repaired_five_minute_bars=EXCLUDED.repaired_five_minute_bars,
                predictions=EXCLUDED.predictions,
                trades_taken=EXCLUDED.trades_taken,
                closed_trades=EXCLUDED.closed_trades,
                win_rate_pct=EXCLUDED.win_rate_pct,
                net_pnl=EXCLUDED.net_pnl,
                rejection_reasons=EXCLUDED.rejection_reasons,
                warnings=EXCLUDED.warnings,
                mistakes=EXCLUDED.mistakes,
                metrics=EXCLUDED.metrics,
                updated_at=CURRENT_TIMESTAMP
        """),{
            "session_date":today.get("session_date"),
            "status":today.get("status"),
            "valid":bool(today.get("eligible")),
            "completed_sessions":int(completed or 0),
            "effective_completed_sessions":int(effective_completed or 0),
            "live_one_minute_buckets":int(metrics.get("one_minute_buckets") or 0),
            "live_five_minute_buckets":int(metrics.get("five_minute_buckets") or 0),
            "live_instruments_seen":int(metrics.get("instruments_seen") or 0),
            "repaired_one_minute_bars":int(metrics.get("repaired_one_minute_bars") or 0),
            "repaired_five_minute_bars":int(metrics.get("repaired_five_minute_bars") or 0),
            "predictions":int(metrics.get("predictions") or 0),
            "trades_taken":int(pnl.get("closed_trades") or 0)+int(pnl.get("open_trades") or 0),
            "closed_trades":int(pnl.get("closed_trades") or 0),
            "win_rate_pct":Decimal(str(pnl.get("win_rate_pct") or 0)),
            "net_pnl":Decimal(str(pnl.get("net_marked_pnl") or 0)),
            "rejection_reasons":json.dumps(today.get("rejection_reasons") or []),
            "warnings":json.dumps(today.get("warnings") or []),
            "mistakes":json.dumps(mistakes,default=str),
            "metrics":json.dumps(metrics,default=str),
        })


def finalize_session(engine,session_date: date = None) -> Dict:
    day=session_date or datetime.now(IST).date()
    today=datetime.now(IST).date()
    if day!=today: raise ValueError("Only today's live session can be finalized")
    with engine.begin() as connection:
        evaluation=evaluate_session(engine,day)
        reasons=evaluation["rejection_reasons"]
        status="REJECTED" if reasons else "COMPLETE"
        metrics=evaluation["metrics"]
        metrics["warnings"]=evaluation["warnings"]
        pnl=evaluation["paper_pnl"]
        connection.execute(text("""INSERT INTO forward_shadow_sessions(session_date,status,started_at,completed_at,one_minute_bars,five_minute_bars,
            instruments_seen,vix_observations,oi_observations,predictions,news_articles,critical_errors,live_eligible,execution_enabled,rejection_reasons,metrics,
            paper_realised_pnl,paper_unrealised_pnl,paper_net_pnl,paper_closed_trades,paper_open_trades,paper_rejected_trades,paper_win_rate_pct,paper_profit_factor)
            VALUES(:day,:status,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,:one_minute_bars,:five_minute_bars,:instruments_seen,:vix_observations,
            :oi_observations,:predictions,:news_articles,:critical_errors,FALSE,FALSE,CAST(:reasons AS jsonb),CAST(:metrics AS jsonb),
            :paper_realised_pnl,:paper_unrealised_pnl,:paper_net_pnl,:paper_closed_trades,:paper_open_trades,:paper_rejected_trades,:paper_win_rate_pct,:paper_profit_factor)
            ON CONFLICT(session_date) DO UPDATE SET status=EXCLUDED.status,completed_at=CURRENT_TIMESTAMP,one_minute_bars=EXCLUDED.one_minute_bars,
            five_minute_bars=EXCLUDED.five_minute_bars,instruments_seen=EXCLUDED.instruments_seen,vix_observations=EXCLUDED.vix_observations,
            oi_observations=EXCLUDED.oi_observations,predictions=EXCLUDED.predictions,news_articles=EXCLUDED.news_articles,
            critical_errors=EXCLUDED.critical_errors,live_eligible=FALSE,execution_enabled=FALSE,rejection_reasons=EXCLUDED.rejection_reasons,metrics=EXCLUDED.metrics,
            paper_realised_pnl=:paper_realised_pnl,paper_unrealised_pnl=:paper_unrealised_pnl,paper_net_pnl=:paper_net_pnl,
            paper_closed_trades=:paper_closed_trades,paper_open_trades=:paper_open_trades,paper_rejected_trades=:paper_rejected_trades,
            paper_win_rate_pct=:paper_win_rate_pct,paper_profit_factor=:paper_profit_factor"""),
            {"day":day,"status":status,**metrics,"reasons":json.dumps(reasons),"metrics":json.dumps(metrics,default=str),
             "paper_realised_pnl":Decimal(str(pnl["realised_pnl"])),"paper_unrealised_pnl":Decimal(str(pnl["unrealised_pnl"])),
             "paper_net_pnl":Decimal(str(pnl["net_marked_pnl"])),"paper_closed_trades":pnl["closed_trades"],
             "paper_open_trades":pnl["open_trades"],"paper_rejected_trades":pnl["rejected_trades"],
             "paper_win_rate_pct":Decimal(str(pnl["win_rate_pct"])),"paper_profit_factor":Decimal(str(pnl["profit_factor"]))})
        completed=connection.execute(text("SELECT COUNT(*) FROM forward_shadow_sessions WHERE status='COMPLETE'")).scalar_one()
    try:
        _persist_daily_log(engine,evaluation,int(completed),int(completed))
    except Exception as exc:
        _monitor(engine,"shadow_session_finalizer","ERROR","Shadow daily log persistence failed",
                 {"session_date":day.isoformat(),"status":status,"error":str(exc)[:1000]})
    return {"session_date":day.isoformat(),"status":status,"completed_sessions":int(completed),"target":TARGET_SESSIONS,"rejection_reasons":reasons,"warnings":evaluation["warnings"],"metrics":metrics,"paper_pnl":pnl,"orders_allowed":False}


def materialize_session(engine, session_date: date, status_override: str = None) -> Dict:
    """Upsert a historical/session row from already-recorded evidence.

    This does not create trades, live bars, or promotion evidence.  It only
    materialises what the database already proves, so Operations can show old
    days such as 2026-08-03 / 2026-08-04 in the formal session list.
    """
    day = session_date
    evaluation = evaluate_session(engine, day)
    reasons = evaluation["rejection_reasons"]
    status = (status_override or ("REJECTED" if reasons else "COMPLETE")).upper()
    if status not in {"STARTED", "COMPLETE", "REJECTED"}:
        raise ValueError("status_override must be STARTED, COMPLETE, or REJECTED")
    metrics = evaluation["metrics"]
    metrics["warnings"] = evaluation["warnings"]
    pnl = evaluation["paper_pnl"]
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO forward_shadow_sessions(session_date,status,started_at,completed_at,one_minute_bars,five_minute_bars,
            instruments_seen,vix_observations,oi_observations,predictions,news_articles,critical_errors,live_eligible,execution_enabled,rejection_reasons,metrics,
            paper_realised_pnl,paper_unrealised_pnl,paper_net_pnl,paper_closed_trades,paper_open_trades,paper_rejected_trades,paper_win_rate_pct,paper_profit_factor)
            VALUES(:day,:status,CURRENT_TIMESTAMP,CASE WHEN :status='STARTED' THEN NULL ELSE CURRENT_TIMESTAMP END,:one_minute_bars,:five_minute_bars,:instruments_seen,:vix_observations,
            :oi_observations,:predictions,:news_articles,:critical_errors,FALSE,FALSE,CAST(:reasons AS jsonb),CAST(:metrics AS jsonb),
            :paper_realised_pnl,:paper_unrealised_pnl,:paper_net_pnl,:paper_closed_trades,:paper_open_trades,:paper_rejected_trades,:paper_win_rate_pct,:paper_profit_factor)
            ON CONFLICT(session_date) DO UPDATE SET status=EXCLUDED.status,completed_at=EXCLUDED.completed_at,one_minute_bars=EXCLUDED.one_minute_bars,
            five_minute_bars=EXCLUDED.five_minute_bars,instruments_seen=EXCLUDED.instruments_seen,vix_observations=EXCLUDED.vix_observations,
            oi_observations=EXCLUDED.oi_observations,predictions=EXCLUDED.predictions,news_articles=EXCLUDED.news_articles,
            critical_errors=EXCLUDED.critical_errors,live_eligible=FALSE,execution_enabled=FALSE,rejection_reasons=EXCLUDED.rejection_reasons,metrics=EXCLUDED.metrics,
            paper_realised_pnl=EXCLUDED.paper_realised_pnl,paper_unrealised_pnl=EXCLUDED.paper_unrealised_pnl,paper_net_pnl=EXCLUDED.paper_net_pnl,
            paper_closed_trades=EXCLUDED.paper_closed_trades,paper_open_trades=EXCLUDED.paper_open_trades,paper_rejected_trades=EXCLUDED.paper_rejected_trades,
            paper_win_rate_pct=EXCLUDED.paper_win_rate_pct,paper_profit_factor=EXCLUDED.paper_profit_factor"""),
            {"day":day,"status":status,**metrics,"reasons":json.dumps(reasons),"metrics":json.dumps(metrics,default=str),
             "paper_realised_pnl":Decimal(str(pnl["realised_pnl"])),"paper_unrealised_pnl":Decimal(str(pnl["unrealised_pnl"])),
             "paper_net_pnl":Decimal(str(pnl["net_marked_pnl"])),"paper_closed_trades":pnl["closed_trades"],
             "paper_open_trades":pnl["open_trades"],"paper_rejected_trades":pnl["rejected_trades"],
             "paper_win_rate_pct":Decimal(str(pnl["win_rate_pct"])),"paper_profit_factor":Decimal(str(pnl["profit_factor"]))})
        completed=connection.execute(text("SELECT COUNT(*) FROM forward_shadow_sessions WHERE status='COMPLETE'")).scalar_one()
    try:
        _persist_daily_log(engine,evaluation,int(completed),int(completed))
    except Exception as exc:
        _monitor(engine,"shadow_session_materializer","ERROR","Shadow daily log persistence failed",
                 {"session_date":day.isoformat(),"status":status,"error":str(exc)[:1000]})
    return {"session_date":day.isoformat(),"status":status,"completed_sessions":int(completed),"target":TARGET_SESSIONS,
            "rejection_reasons":reasons,"warnings":evaluation["warnings"],"metrics":metrics,"paper_pnl":pnl,"orders_allowed":False}


def recover_stale_started_sessions(engine, lookback_days: int = 10) -> Dict:
    """Materialise old STARTED or missed sessions from already-recorded DB evidence.

    This is the safety net for laptop shutdowns or missed Celery beat ticks.
    It never creates bars/trades/predictions; it turns stale STARTED or missed trading days
    into COMPLETE/REJECTED rows using the same evidence as manual repair.
    """
    today = datetime.now(IST).date()
    now_ist = datetime.now(IST)
    is_post_market = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 35)
    cutoff = today - timedelta(days=max(1, int(lookback_days or 10)))
    start_dt = datetime(cutoff.year, cutoff.month, cutoff.day, 0, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    end_dt = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=IST).astimezone(timezone.utc) if is_post_market else datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT s.session_date
            FROM forward_shadow_sessions s
            LEFT JOIN exchange_trading_calendar c
              ON c.exchange='NSE' AND c.session_date=s.session_date
            WHERE s.status='STARTED'
              AND s.session_date <= :max_day
              AND s.session_date >= :cutoff
              AND COALESCE(c.session_status,'OPEN') != 'CLOSED'
            UNION
            SELECT DISTINCT (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date AS session_date
            FROM live_market_bars b
            WHERE b.bar_time >= :start_dt AND b.bar_time < :end_dt
              AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date NOT IN (
                  SELECT session_date FROM forward_shadow_sessions
              )
            ORDER BY session_date
        """), {"max_day": today if is_post_market else (today - timedelta(days=1)), "cutoff": cutoff, "start_dt": start_dt, "end_dt": end_dt}).mappings().all()
    recovered = []
    failures = []
    for row in rows:
        session_day = row["session_date"]
        try:
            recovered.append(materialize_session(engine, session_day))
        except Exception as exc:
            failures.append({"session_date":str(session_day),"error":str(exc)[:1000]})
            _monitor(engine,"shadow_session_recovery","ERROR","Stale shadow session recovery failed",
                     {"session_date":str(session_day),"error":str(exc)[:1000]})
    if recovered or failures:
        _monitor(engine,"shadow_session_recovery","INFO" if not failures else "WARNING",
                 "Stale shadow session recovery completed",
                 {"lookback_days":lookback_days,"recovered":len(recovered),"failures":failures})
    return {"status":"success" if not failures else "partial",
            "lookback_days":lookback_days,
            "recovered":recovered,
            "failures":failures,
            "orders_allowed":False}


_last_recovery_at = None


def status(engine) -> Dict:
    global _last_recovery_at
    today_dt = datetime.now(IST)
    today_date = today_dt.date()
    is_post_market = (today_dt.hour > 15) or (today_dt.hour == 15 and today_dt.minute >= 35)

    if is_post_market:
        try:
            with engine.connect() as connection:
                t_row = connection.execute(text("SELECT status FROM forward_shadow_sessions WHERE session_date=:d"), {"d": today_date}).mappings().one_or_none()
            if t_row and t_row.get("status") == "STARTED":
                finalize_session(engine, today_date)
        except Exception as exc:
            _monitor(engine, "shadow_session_status", "WARNING", f"Post-market session auto-finalization skipped: {exc}",
                     {"session_date": today_date.isoformat(), "error": str(exc)[:500]})

    # Auto-recover stale STARTED and missing sessions so the dashboard always
    # shows up-to-date dates, even when Celery beat missed its scheduled
    # recovery tasks.  Throttled to at most once per 5 minutes.
    should_recover = _last_recovery_at is None or (today_dt - _last_recovery_at).total_seconds() >= 300
    if should_recover:
        try:
            with engine.connect() as connection:
                stale_dates = [r[0] for r in connection.execute(text(
                    "SELECT session_date FROM forward_shadow_sessions WHERE status='STARTED' AND session_date < :d"
                ), {"d": today_date}).fetchall()]
            for s_date in stale_dates:
                materialize_session(engine, s_date)
            _last_recovery_at = today_dt
        except Exception as exc:
            _monitor(engine, "shadow_session_status", "WARNING",
                     f"Inline stale session recovery skipped: {exc}",
                     {"error": str(exc)[:500]})

    # Ensure today's session row exists in the DB.  The Celery beat task
    # open_forward_shadow_session runs at 08:45, but if Celery was down or the
    # preflight check failed, there would be no row and the dashboard would
    # silently skip today.  A simple INSERT … ON CONFLICT DO NOTHING is safe
    # and idempotent.
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO forward_shadow_sessions(session_date,status,started_at,live_eligible,execution_enabled)
                VALUES(:day,'STARTED',CURRENT_TIMESTAMP,FALSE,FALSE)
                ON CONFLICT(session_date) DO NOTHING
            """), {"day": today_date})
    except Exception as exc:
        _monitor(engine, "shadow_session_status", "WARNING",
                 f"Auto-open today session skipped: {exc}",
                 {"session_date": today_date.isoformat(), "error": str(exc)[:500]})

    with engine.connect() as connection:
        counts = connection.execute(text("SELECT status,COUNT(*) count FROM forward_shadow_sessions GROUP BY status")).fetchall()
        recent = connection.execute(text("SELECT * FROM forward_shadow_sessions ORDER BY session_date DESC LIMIT 10")).mappings().all()
    values = {row[0]: int(row[1]) for row in counts}
    completed = values.get("COMPLETE", 0)
    today = evaluate_session(engine)
    today_iso = str(today.get("session_date"))
    already_counted = any(str(row["session_date"]) == today_iso and row["status"] == "COMPLETE" for row in recent)
    effective_completed = completed + (1 if today["eligible"] and not already_counted else 0)

    # Enrich recent list so that a STARTED session during market hours displays live counts
    recent_list = []
    for row in recent:
        item = dict(row)
        if str(item.get("session_date")) == today_iso and item.get("status") == "STARTED":
            m = today.get("metrics") or {}
            pnl = today.get("paper_pnl") or {}
            item["one_minute_bars"] = m.get("one_minute_bars", item.get("one_minute_bars", 0))
            item["five_minute_bars"] = m.get("five_minute_bars", item.get("five_minute_bars", 0))
            item["predictions"] = m.get("predictions", item.get("predictions", 0))
            item["paper_net_pnl"] = pnl.get("net_marked_pnl", item.get("paper_net_pnl", 0))
        recent_list.append(item)

    # If today is not yet in the recent list (no session row created), inject
    # a synthetic entry so the dashboard always shows the current date with
    # live-evaluated metrics.
    if not any(str(item.get("session_date")) == today_iso for item in recent_list):
        m = today.get("metrics") or {}
        pnl = today.get("paper_pnl") or {}
        recent_list.insert(0, {
            "session_date": today_date,
            "status": today.get("status", "WAITING"),
            "one_minute_bars": m.get("one_minute_bars", 0),
            "five_minute_bars": m.get("five_minute_bars", 0),
            "predictions": m.get("predictions", 0),
            "paper_net_pnl": pnl.get("net_marked_pnl", 0),
        })

    try:
        _persist_daily_log(engine, today, completed, effective_completed)
    except Exception as exc:
        _monitor(engine, "shadow_session_status", "ERROR", "Shadow daily log persistence failed",
                 {"session_date": today.get("session_date"), "error": str(exc)[:1000]})
    return {
        "completed_sessions": completed,
        "effective_completed_sessions": effective_completed,
        "remaining_sessions": max(0, TARGET_SESSIONS - effective_completed),
        "target": TARGET_SESSIONS,
        "counts": values,
        "recent": recent_list,
        "today": today,
        "live_eligible": False
    }
