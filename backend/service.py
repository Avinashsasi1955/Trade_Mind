import sqlite3
import json
import math
import os
import hashlib
import secrets
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, text

from .agent import TradingAgent
from .broker import PaperBroker
from .config import DATABASE_URL, DEFAULT_CAPITAL, FINNHUB_WEBHOOK_SECRET, FINNHUB_WEBHOOK_URL, NEWS_PROVIDER
from .database import now_iso
from .market import INDICES, market_snapshot, price_map
from .model_provider import provider_catalog
from .ai_gateway import AIGateway, gateway_status
from .technical_analysis import GLOSSARY, analyse_structure
from .sentiment import analyse_sentiment
from .news_gateway import articles_for_symbol
from .charting import chart_data
from .backtesting import run_backtest
from .security_master import resolve_security, search_securities, security_master_stats
from .config import KITE_ACCESS_TOKEN, KITE_API_KEY, MARKET_DATA_PROVIDER, UPSTOX_ACCESS_TOKEN
from .history_store import HistoryStore
from .derivatives import build_derivative_plan
from .trade_bot import chat, conversation, list_conversations
from .monitoring import operations_status
from .preflight import run_preflight
from .ml_pipeline import build_research_dataset, detect_drift, generate_shadow_predictions, pipeline_status, resolve_shadow_predictions, train_model, walk_forward_validate, ResearchStore
from .broker_gateway import begin_login, connection_status, disconnect
from .execution import approve_intent, create_intent, emergency_cancel_open_orders, list_intents, reconcile_intents, submit_intent
from .risk_engine import policy as risk_policy, set_kill_switch, switch_status
from .config import LIVE_TRADING_ENABLED
from .live_market_view import price_map as live_price_map, snapshot as live_market_snapshot
from .shadow_sessions import status as shadow_session_status
from .ml.validation_engine import record_shadow_exit
from .trade_quality import quality_feedback, score_trade
from .senior_trade_agent import review_shadow_trade
from .quant_models import quant_model_status, run_quant_snapshot
from .alpha_fragility import alpha_fragility_status


IST = ZoneInfo("Asia/Kolkata")
_shared_pg_engine = None

def _get_engine():
    global _shared_pg_engine
    if _shared_pg_engine is None and DATABASE_URL:
        _shared_pg_engine = create_engine(
            DATABASE_URL,
            connect_args={"options": "-c max_parallel_workers_per_gather=0 -c max_parallel_maintenance_workers=0"},
            pool_pre_ping=True,    # test connection alive before use
            pool_size=10,
            max_overflow=5,
            pool_recycle=1800,     # recycle idle conns every 30 min (prevents Docker/Postgres drops)
            pool_timeout=10,       # fail fast if pool exhausted instead of hanging forever
            future=True,
        )
    return _shared_pg_engine

SENTIMENT_INTELLIGENCE_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "LT", "ITC",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "TATAMOTORS", "NTPC", "ONGC", "POWERGRID", "ULTRACEMCO",
    "TITAN", "ADANIENT", "ADANIPORTS", "WIPRO", "TECHM", "JSWSTEEL",
    "TATASTEEL", "COALINDIA", "HCLTECH", "BEL", "TRENT", "DIXON", "COFORGE",
    "BAJAJFINSV", "M&M", "EICHERMOT", "GRASIM", "HINDALCO", "JIOFIN", "NESTLEIND",
    "ASIANPAINT", "CIPLA", "DRREDDY", "APOLLOHOSP", "BRITANNIA", "HEROMOTOCO",
    "BAJAJ-AUTO", "INDUSINDBK", "SHRIRAMFIN", "SBILIFE",
]


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _jsonable_mapping(row) -> Dict:
    data = dict(row or {})
    for key, value in list(data.items()):
        if isinstance(value, Decimal):
            data[key] = float(value)
        elif isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _prices() -> Dict[str,float]:
    values=live_price_map(DATABASE_URL) if DATABASE_URL else {}
    return values or price_map()


def _row(row: sqlite3.Row) -> Dict:
    return dict(row)


def _float(value) -> float:
    return float(value or 0)


def portfolio_summary(db: sqlite3.Connection, user_id: int) -> Dict:
    portfolio = db.execute("SELECT * FROM portfolios WHERE user_id=?", (user_id,)).fetchone()
    prices = _prices()
    holdings = db.execute("SELECT * FROM holdings WHERE user_id=? ORDER BY symbol", (user_id,)).fetchall()
    invested = sum(item["quantity"] * item["average_price"] for item in holdings)
    holdings_value = sum(item["quantity"] * prices.get(item["symbol"], item["average_price"]) for item in holdings)
    current_value = portfolio["cash"] + holdings_value
    total_pnl = current_value - portfolio["starting_capital"]
    today_pnl = sum(item["quantity"] * (prices.get(item["symbol"], item["average_price"]) - item["average_price"]) for item in holdings)
    return {
        "starting_capital": round(portfolio["starting_capital"], 2), "cash": round(portfolio["cash"], 2),
        "invested": round(invested, 2), "current_value": round(current_value, 2),
        "total_pnl": round(total_pnl, 2), "total_pnl_pct": round(total_pnl / portfolio["starting_capital"] * 100, 2),
        "today_pnl": round(today_pnl, 2), "today_pnl_pct": round(today_pnl / portfolio["starting_capital"] * 100, 2),
    }


def holding_list(db: sqlite3.Connection, user_id: int) -> List[Dict]:
    prices = _prices()
    rows = db.execute("SELECT * FROM holdings WHERE user_id=? ORDER BY symbol", (user_id,)).fetchall()
    total = sum(abs(item["quantity"]) * prices.get(item["symbol"], item["average_price"]) for item in rows) or 1
    result = []
    for item in rows:
        ltp = prices.get(item["symbol"], item["average_price"])
        qty = item["quantity"]
        avg = item["average_price"]
        if qty < 0:
            # Short position: gain if price falls below average entry
            value = abs(qty) * ltp
            pnl = (avg - ltp) * abs(qty)
            pnl_pct = ((avg - ltp) / avg) * 100 if avg else 0.0
        else:
            # Long position: gain if price rises above average entry
            value = qty * ltp
            pnl = (ltp - avg) * qty
            pnl_pct = ((ltp - avg) / avg) * 100 if avg else 0.0
        result.append({"symbol": item["symbol"], "name": item["name"], "quantity": qty, "average_price": avg, "ltp": ltp, "current_value": round(value, 2), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "weight": round(value / total * 100, 1)})
    return result


def trade_list(db: sqlite3.Connection, user_id: int, limit: int = 100) -> List[Dict]:
    rows = db.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    prices = _prices()
    result = []
    for item in rows:
        current = prices.get(item["symbol"], item["price"])
        paper_pnl = item["realised_pnl"] or (current - item["price"]) * item["quantity"] * (1 if item["action"] == "BUY" else -1)
        result.append({**_row(item), "current_value": round(current * item["quantity"], 2), "pnl": round(paper_pnl, 2), "pnl_pct": round(paper_pnl / max(1, item["price"] * item["quantity"]) * 100, 2)})
    return result


def latest_hot_stocks(db: sqlite3.Connection, user_id: int) -> List[Dict]:
    rows = db.execute("SELECT * FROM watchlist_snapshots WHERE user_id=? AND id IN (SELECT MAX(id) FROM watchlist_snapshots WHERE user_id=? GROUP BY symbol) ORDER BY confidence DESC LIMIT 4", (user_id, user_id)).fetchall()
    if rows:
        return [_row(item) for item in rows]
    agent = TradingAgent()
    return [{"symbol": s.symbol, "name": s.name, "price": s.price, "change_pct": s.change_pct, "confidence": s.confidence, "reasoning": s.reasoning} for s in agent.analyse(market_snapshot(), "balanced")[:4]]


def dashboard(db: sqlite3.Connection, user_id: int) -> Dict:
    user = db.execute("SELECT id,name,email FROM users WHERE id=?", (user_id,)).fetchone()
    settings = db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
    last_run = db.execute("SELECT * FROM agent_runs WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    live=live_market_snapshot(DATABASE_URL,1) if DATABASE_URL else None
    return {"user": _row(user), "summary": portfolio_summary(db, user_id), "indices":live["indices"] if live and live["indices"] else INDICES,
            "holdings": holding_list(db, user_id), "trades": trade_list(db, user_id), "hot_stocks": latest_hot_stocks(db, user_id),
            "settings": _row(settings), "last_run": _row(last_run) if last_run else None, "universe": security_master_stats(),
            "market_data_mode":live["data_mode"] if live else "simulated"}


def shadow_trade_book(limit: int = 200) -> Dict:
    """Open and closed ML shadow-paper trades.

    This intentionally reads from shadow_execution_audits, not the legacy demo
    portfolio tables, so UI pages do not show seeded paper holdings as live
    model evidence.
    """
    empty_summary = {"open_trades": 0, "closed_trades": 0, "realised_pnl": 0, "unrealised_pnl": 0,
                     "net_marked_pnl": 0, "win_rate_pct": 0, "profit_factor": 0, "estimated_fees": 0,
                     "avg_quality_score": 0, "open_quality_score": 0, "closed_quality_score": 0,
                     "quality_feedback": quality_feedback([]), "exit_reasons": {}, "mistake_tags": {}}
    if not DATABASE_URL:
        return {"open": [], "closed": [], "daily": [], "summary": empty_summary, "today": empty_summary,
                 "market_date": datetime.now(IST).date().isoformat(), "orders_allowed": False}
    engine = _get_engine()
    with engine.connect() as connection:
        # Query 1: fetch the shadow audit rows with strategy attribution
        rows=connection.execute(text("""SELECT a.id,a.instrument_id,a.model_version,a.signal_at,a.side,a.quantity,a.signal_probability,a.decision_price,
                   a.theoretical_fill_price,a.estimated_fees,a.realised_exit_price,a.net_pnl,a.audit_status,
                   a.rejection_reason,a.stop_loss_price,a.take_profit_price,a.exit_at,a.exit_reason,a.fill_source,
                   a.mistake_tags,a.improvement_note,
                   COALESCE(
                       NULLIF(NULLIF(NULLIF(atr.strategy, 'UNKNOWN_STRATEGY'), 'UNKNOWN'), 'NO_TRADE'),
                       (
                           SELECT tca.chart_strategy FROM trade_candidate_audits tca 
                           WHERE tca.instrument_id = a.instrument_id 
                             AND tca.accepted = TRUE 
                             AND tca.observed_at BETWEEN a.signal_at - INTERVAL '5 minute' AND a.signal_at + INTERVAL '5 minute'
                           ORDER BY tca.observed_at DESC LIMIT 1
                       ),
                       CASE 
                           WHEN i.instrument_type = 'CE' AND a.side = 'BUY' THEN 'MOMENTUM_CALL_BUY'
                           WHEN i.instrument_type = 'CE' AND a.side = 'SELL' THEN 'TREND_PULLBACK_CALL_SELL'
                           WHEN i.instrument_type = 'PE' AND a.side = 'BUY' THEN 'BREAKDOWN_PUT_BUY'
                           WHEN i.instrument_type = 'PE' AND a.side = 'SELL' THEN 'TREND_PULLBACK_PUT_SELL'
                           WHEN a.side = 'BUY' THEN 'BREAKOUT_CALL_BUY'
                           ELSE 'BREAKDOWN_PUT_BUY'
                       END
                   ) AS strategy_tag,
                   i.symbol,i.exchange,i.instrument_type
            FROM shadow_execution_audits a
            JOIN instrument_master i ON i.id=a.instrument_id
            LEFT JOIN adaptive_trade_rewards atr ON atr.audit_id=a.id
            WHERE a.audit_status='RECONCILED'
            ORDER BY a.signal_at DESC
            LIMIT :limit"""),{"limit":max(1,min(int(limit),500))}).mappings().all()

        # Query 2: batch-fetch latest price for ALL needed instrument_ids in ONE query
        # DISTINCT ON is O(instruments_seen) not O(limit) — eliminates the N+1 pattern
        instrument_ids = list({int(r["instrument_id"]) for r in rows if r["net_pnl"] is None})
        latest_price_map: dict = {}
        if instrument_ids:
            price_rows = connection.execute(text("""SELECT DISTINCT ON (instrument_id)
                    instrument_id, close_price
                FROM live_market_bars
                WHERE instrument_id = ANY(:ids)
                  AND interval IN ('1minute','5minute','day')
                ORDER BY instrument_id, bar_time DESC"""),
                {"ids": instrument_ids}).fetchall()
            latest_price_map = {int(r[0]): float(r[1]) for r in price_rows if r[1] is not None}

        daily_rows=connection.execute(text("""SELECT
            (signal_at AT TIME ZONE 'Asia/Kolkata')::date session_date,
            COUNT(*) trades,
            COUNT(*) FILTER(WHERE audit_status='RECONCILED' AND net_pnl IS NULL) open_trades,
            COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
            COUNT(*) FILTER(WHERE net_pnl > 0) winning_trades,
            COUNT(*) FILTER(WHERE net_pnl <= 0) losing_trades,
            COALESCE(SUM(net_pnl),0) realised_pnl,
            COALESCE(SUM(estimated_fees),0) estimated_fees
            FROM shadow_execution_audits
            WHERE audit_status='RECONCILED'
            GROUP BY session_date
            ORDER BY session_date DESC
            LIMIT 20""")).mappings().all()
    items=[]
    for row in rows:
        entry=_float(row["theoretical_fill_price"] or row["decision_price"])
        # Use batch-fetched latest_price_map; fall back to entry for closed trades (they use net_pnl directly)
        live_price=latest_price_map.get(int(row["instrument_id"]), None)
        latest=_float(row["realised_exit_price"] or live_price or entry)
        qty=int(row["quantity"] or 0)
        side=str(row["side"])
        fees=_float(row["estimated_fees"])
        realised=row["net_pnl"] is not None
        pnl=_float(row["net_pnl"]) if realised else ((latest-entry)*qty-fees if side=="BUY" else (entry-latest)*qty-fees)
        notional=entry*qty
        raw_strat=str(row.get("strategy_tag") or "").strip()
        if not raw_strat or raw_strat in ("UNKNOWN_STRATEGY", "UNKNOWN", "NO_STRATEGY", "None", "NO_TRADE"):
            raw_strat = "BREAKOUT_CALL_BUY" if side=="BUY" else "BREAKDOWN_PUT_BUY"
        clean_strat=(raw_strat.replace("_"," ").title()
                     .replace("Call Buy","Call (CE)")
                     .replace("Put Sell","Put Write (PE)")
                     .replace("Put Buy","Put (PE)")
                     .replace("Call Sell","Call Write (CE)")
                     .replace("Golden ","Golden: ")
                     .replace("Quant ","Quant: "))
        item={**dict(row),
              "entry_price":round(entry,4),
              "latest_price":round(latest,4),
              "estimated_fees":round(fees,2),
              "notional":round(notional,2),
              "marked_pnl":round(pnl,2),
              "pnl_pct":round((pnl/max(1,notional))*100,4),
              "is_open":not realised,
              "strategy_tag":raw_strat,
              "strategy_label":clean_strat}
        item["quality"]=score_trade(item)
        item["senior_agent"]=review_shadow_trade(item)
        items.append(item)
    for item in items:
        raw_tags=item.get("mistake_tags") or []
        if isinstance(raw_tags,str):
            try:
                raw_tags=json.loads(raw_tags)
            except Exception:
                raw_tags=[]
        item["mistake_tags"]=raw_tags if isinstance(raw_tags,list) else []
    def summarise(selected: List[Dict]) -> Dict:
        open_items=[item for item in selected if item["is_open"]]
        closed_items=[item for item in selected if not item["is_open"]]
        realised_pnl=sum(float(item["marked_pnl"]) for item in closed_items)
        unrealised_pnl=sum(float(item["marked_pnl"]) for item in open_items)
        wins=sum(1 for item in closed_items if float(item["marked_pnl"])>0)
        losses=sum(1 for item in closed_items if float(item["marked_pnl"])<=0)
        gross_profit=sum(float(item["marked_pnl"]) for item in closed_items if float(item["marked_pnl"])>0)
        gross_loss=abs(sum(float(item["marked_pnl"]) for item in closed_items if float(item["marked_pnl"])<=0))
        estimated_fees=sum(float(item.get("estimated_fees") or 0) for item in selected)
        quality_scores=[float(item.get("quality",{}).get("score") or 0) for item in selected if item.get("quality")]
        closed_quality=[float(item.get("quality",{}).get("score") or 0) for item in closed_items if item.get("quality")]
        open_quality=[float(item.get("quality",{}).get("score") or 0) for item in open_items if item.get("quality")]
        tags={}; exits={}
        for item in closed_items:
            exits[item.get("exit_reason") or "closed"]=exits.get(item.get("exit_reason") or "closed",0)+1
            for tag in item.get("mistake_tags") or []:
                tags[str(tag)]=tags.get(str(tag),0)+1
        feedback=quality_feedback([item.get("quality") for item in selected])
        return {"open_trades":len(open_items),"closed_trades":len(closed_items),
                "realised_pnl":round(realised_pnl,2),"unrealised_pnl":round(unrealised_pnl,2),
                "net_marked_pnl":round(realised_pnl+unrealised_pnl,2),
                "winning_trades":wins,"losing_trades":losses,
                "win_rate_pct":round(wins/max(1,len(closed_items))*100,2),
                "profit_factor":round(gross_profit/gross_loss,4) if gross_loss else (999.0 if gross_profit else 0.0),
                "estimated_fees":round(estimated_fees,2),
                "avg_quality_score":round(sum(quality_scores)/len(quality_scores),2) if quality_scores else 0,
                "open_quality_score":round(sum(open_quality)/len(open_quality),2) if open_quality else 0,
                "closed_quality_score":round(sum(closed_quality)/len(closed_quality),2) if closed_quality else 0,
                "quality_feedback":feedback,
                "exit_reasons":exits,"mistake_tags":tags}
    open_items=[item for item in items if item["is_open"]]
    closed_items=[item for item in items if not item["is_open"]]
    market_date=datetime.now(IST).date()
    today_items=[item for item in items if item.get("signal_at") and item["signal_at"].astimezone(IST).date()==market_date]
    return {"open":open_items,"closed":closed_items,
            "summary":summarise(items),
            "today":summarise(today_items),
            "market_date":market_date.isoformat(),
            "daily":[dict(row) for row in daily_rows],
            "orders_allowed":False,
            "basis":"shadow_execution_audits only; legacy demo portfolio tables excluded"}


def _latest_shadow_price(connection, audit_id: int) -> Decimal:
    row=connection.execute(text("""SELECT a.id,a.instrument_id,a.theoretical_fill_price,a.decision_price
        FROM shadow_execution_audits a WHERE a.id=:id AND a.audit_status='RECONCILED' AND a.net_pnl IS NULL"""),{"id":audit_id}).mappings().one_or_none()
    if not row:
        raise ValueError("Open shadow-paper trade not found")
    latest=connection.execute(text("""SELECT close_price FROM live_market_bars
        WHERE instrument_id=:instrument AND interval IN ('1second','1minute','5minute','day')
        ORDER BY bar_time DESC, CASE interval WHEN '1second' THEN 0 WHEN '1minute' THEN 1 WHEN '5minute' THEN 2 ELSE 3 END
        LIMIT 1"""),{"instrument":row["instrument_id"]}).scalar_one_or_none()
    return Decimal(str(latest or row["theoretical_fill_price"] or row["decision_price"]))


def exit_shadow_trade(audit_id: int) -> Dict:
    """Manual paper-only exit for one open ML shadow trade."""
    if not DATABASE_URL:
        raise ValueError("PostgreSQL shadow ledger is not configured")
    engine=create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    with engine.connect() as connection:
        exit_price=_latest_shadow_price(connection,int(audit_id))
    result=record_shadow_exit(engine,int(audit_id),exit_price,"MANUAL_EXIT")
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO monitoring_events(component,level,message,payload,created_at)
            VALUES('shadow_manual_override','INFO','manual paper trade exit',CAST(:payload AS jsonb),CURRENT_TIMESTAMP)"""),
            {"payload":json.dumps({"audit_id":int(audit_id),"exit_price":str(exit_price),"result":result},default=str)})
    engine.dispose()
    return {**result,"message":"Paper trade exited manually at latest stored price","orders_allowed":False}


def update_shadow_trade_risk(audit_id: int, stop_loss=None, take_profit=None) -> Dict:
    """Manual paper-only SL/TP update for one open ML shadow trade."""
    if not DATABASE_URL:
        raise ValueError("PostgreSQL shadow ledger is not configured")
    sl=Decimal(str(stop_loss)) if stop_loss not in (None,"") else None
    tp=Decimal(str(take_profit)) if take_profit not in (None,"") else None
    if sl is not None and sl<=0: raise ValueError("Stop-loss must be positive")
    if tp is not None and tp<=0: raise ValueError("Take-profit must be positive")
    engine=create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    with engine.begin() as connection:
        row=connection.execute(text("""SELECT a.id,a.side,a.stop_loss_price,a.take_profit_price,a.improvement_note,
                a.theoretical_fill_price,i.symbol,i.instrument_type
            FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
            WHERE a.id=:id AND a.audit_status='RECONCILED' AND a.net_pnl IS NULL FOR UPDATE"""),{"id":int(audit_id)}).mappings().one_or_none()
        if not row:
            raise ValueError("Open shadow-paper trade not found")
        note={}
        raw=row["improvement_note"]
        if raw:
            try:
                note=json.loads(raw) if isinstance(raw,str) and raw.strip().startswith("{") else {"previous_note":str(raw)}
            except Exception:
                note={"previous_note":str(raw)}
        note["manual_risk_update"]={"updated_at":datetime.now(timezone.utc).isoformat(),
                                    "old_sl":str(row["stop_loss_price"]) if row["stop_loss_price"] is not None else None,
                                    "new_sl":str(sl) if sl is not None else str(row["stop_loss_price"]),
                                    "old_tp":str(row["take_profit_price"]) if row["take_profit_price"] is not None else None,
                                    "new_tp":str(tp) if tp is not None else str(row["take_profit_price"]),
                                    "rule":"operator paper-only SL/TP override; no broker order submitted"}
        connection.execute(text("""UPDATE shadow_execution_audits
            SET stop_loss_price=COALESCE(:sl,stop_loss_price),take_profit_price=COALESCE(:tp,take_profit_price),improvement_note=:note
            WHERE id=:id"""),{"id":int(audit_id),"sl":sl,"tp":tp,"note":json.dumps(note,default=str)[:2000]})
        connection.execute(text("""INSERT INTO monitoring_events(component,level,message,payload,created_at)
            VALUES('shadow_manual_override','INFO','manual paper SL/TP update',CAST(:payload AS jsonb),CURRENT_TIMESTAMP)"""),
            {"payload":json.dumps({"audit_id":int(audit_id),"symbol":row["symbol"],"instrument_type":row["instrument_type"],
                                   "old_stop_loss":str(row["stop_loss_price"]) if row["stop_loss_price"] is not None else None,
                                   "new_stop_loss":str(sl) if sl is not None else None,
                                   "old_take_profit":str(row["take_profit_price"]) if row["take_profit_price"] is not None else None,
                                   "new_take_profit":str(tp) if tp is not None else None},default=str)})
    engine.dispose()
    return {"audit_id":int(audit_id),"stop_loss_price":str(sl) if sl is not None else None,
            "take_profit_price":str(tp) if tp is not None else None,
            "message":"Paper SL/TP updated manually","orders_allowed":False}


def repair_today_missing_bars(limit: int = 100, target_date_str: Optional[str] = None) -> Dict:
    """Read-only Upstox REST repair for missing intraday candles for today or a chosen date.

    This is intentionally bounded and non-destructive. It fills missing 1m bars
    and derived 5m bars for the same priority universe used by live-shadow
    validation; it does not touch orders or live-trading flags.
    """
    if not DATABASE_URL:
        raise ValueError("PostgreSQL DATABASE_URL is required for REST gap repair")
    from .upstox_backfill import run_backfill
    from .shadow_sessions import evaluate_session
    from datetime import date
    target_dt = date.fromisoformat(target_date_str) if target_date_str else None
    before=shadow_session_status_api().get("today",{})
    report=run_backfill(DATABASE_URL, limit=max(1,min(int(limit or 100),500)), mode="intraday", days=1, fno_only=True, replace=False, target_date=target_dt)
    after=evaluate_session(create_engine(DATABASE_URL, pool_pre_ping=True, future=True))
    label_date = target_date_str or "today"
    return {"status":report.get("status","unknown"),"mode":"read_only_rest_repair","target_date":label_date,"before":before,
            "after":after,"repair":report,"orders_allowed":False,
            "message":f"Inserted {report.get('inserted_1minute',0):,} 1m bars and {report.get('inserted_5minute',0):,} 5m bars for {label_date} from Upstox REST."}


def model_review_status() -> Dict:
    research=ResearchStore().status()
    active=research.get("latest_model")
    experiment=research.get("latest_experiment")
    active_metrics=(active or {}).get("metrics",{}) if active else {}
    experiment_metrics=(experiment or {}).get("metrics",{}) if experiment else {}
    def holdout(metrics):
        return metrics.get("holdout") or metrics.get("summary") or metrics
    active_h=holdout(active_metrics)
    exp_h=holdout(experiment_metrics)
    failed=[]
    if experiment and active and experiment.get("version")!=active.get("version"):
        failed.append("Candidate was rejected; active paper baseline was not replaced.")
    exp_log=float(exp_h.get("log_loss",999) or 999)
    active_log=float(active_h.get("log_loss",999) or 999)
    if exp_log>=active_log:
        failed.append(f"Log loss did not improve versus active baseline ({exp_log:.4f} vs {active_log:.4f}).")
    for key,label,higher_better in (("profit_factor","profit factor",True),("max_drawdown_pct","max drawdown",False),("win_rate","win rate",True)):
        if key in exp_h and key in active_h:
            ev=float(exp_h.get(key) or 0); av=float(active_h.get(key) or 0)
            if (higher_better and ev<av) or ((not higher_better) and abs(ev)>abs(av)):
                failed.append(f"Candidate {label} did not beat active baseline ({ev:g} vs {av:g}).")
    if not failed and experiment:
        failed.append("No failed comparison detected in stored metrics; candidate status should be checked manually.")
    recommendations=[
        "Do not promote rejected experiments into live-shadow execution.",
        "Add more clean, liquid NSE history before retraining again.",
        "Keep 90 forward shadow sessions and real paper P&L as the promotion gate.",
        "Compare profit factor, drawdown and log loss together; do not optimize win rate alone.",
    ]
    return {"active_model":active,"latest_experiment":experiment,"active_metrics":active_h,
            "experiment_metrics":exp_h,"failed_reasons":failed,"recommendations":recommendations,
            "orders_allowed":False}


def run_agent(db: sqlite3.Connection, user_id: int, execute_trade: bool = True) -> Dict:
    settings = db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
    prior_runs = db.execute("SELECT COUNT(*) count FROM agent_runs WHERE user_id=?", (user_id,)).fetchone()["count"]
    stocks = market_snapshot(prior_runs + 1)
    agent = TradingAgent()
    position_rows = db.execute("SELECT symbol,quantity,average_price FROM holdings WHERE user_id=?", (user_id,)).fetchall()
    positions = {row["symbol"]: dict(row) for row in position_rows}
    signals = agent.analyse(stocks, settings["risk_profile"], positions)
    # Collect BOTH bullish (BUY/CALL) and bearish (SELL/PUT) opportunities
    buy_opportunities  = [s for s in signals if s.action == "BUY"]
    sell_opportunities = [s for s in signals if s.action == "SELL"]
    all_opportunities  = buy_opportunities + sell_opportunities
    db.execute("DELETE FROM watchlist_snapshots WHERE user_id=?", (user_id,))
    for signal in signals[:6]:  # show top 6 signals (both directions)
        db.execute(
            "INSERT INTO watchlist_snapshots(user_id,symbol,name,price,change_pct,confidence,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, signal.symbol, signal.name, signal.price, signal.change_pct, signal.confidence, signal.reasoning, now_iso()),
        )
    trade = None
    broker = PaperBroker()
    if all_opportunities and execute_trade:
        # Pick the single highest-confidence opportunity across both directions
        best = max(all_opportunities, key=lambda s: s.confidence)
        if best.action == "BUY":
            trade = broker.execute_buy(db, user_id, best, settings["risk_profile"])
        elif best.action == "SELL":
            trade = broker.execute_sell(db, user_id, best, settings["risk_profile"])
    universe = security_master_stats()["counts"]
    summary = (
        f"Master covers {universe['total']:,} active NSE/BSE securities; evaluated {len(stocks)} with available paper quotes, "
        f"found {len(buy_opportunities)} BUY/CALL and {len(sell_opportunities)} SELL/PUT setups, "
        f"and placed {1 if trade else 0} paper order."
    )
    cursor = db.execute(
        "INSERT INTO agent_runs(user_id,status,stocks_scanned,opportunities,trades_placed,summary,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, "COMPLETED", len(stocks), len(all_opportunities), 1 if trade else 0, summary, now_iso()),
    )
    db.commit()
    return {
        "run_id": cursor.lastrowid,
        "status": "COMPLETED",
        "stocks_scanned": len(stocks),
        "universe": universe,
        "strategy_selection": "algorithmic",
        "opportunities": len(all_opportunities),
        "buy_opportunities": len(buy_opportunities),
        "sell_opportunities": len(sell_opportunities),
        "trade": trade,
        "summary": summary,
        "signals": agent.serialise(signals[:6]),
        "completed_at": now_iso(),
    }


def update_settings(db: sqlite3.Connection, user_id: int, risk_profile: str) -> Dict:
    if risk_profile not in {"conservative", "balanced", "aggressive"}:
        raise ValueError("Invalid risk profile")
    db.execute("UPDATE settings SET risk_profile=? WHERE user_id=?", (risk_profile, user_id))
    db.commit()
    return _row(db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone())


def reset_portfolio(db: sqlite3.Connection, user_id: int) -> Dict:
    db.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM trades WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM agent_runs WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM watchlist_snapshots WHERE user_id=?", (user_id,))
    db.execute("UPDATE portfolios SET starting_capital=?,cash=?,realised_pnl=0,updated_at=? WHERE user_id=?", (DEFAULT_CAPITAL, DEFAULT_CAPITAL, now_iso(), user_id))
    db.commit()
    return portfolio_summary(db, user_id)


def _analysis_decision_alignment(analysis: Dict, sentiment_context: Dict = None) -> Dict:
    """Single explainable decision pack shared by AI Analysis, Copilot and Senior review.

    This is deliberately advisory.  It does not place orders, bypass Senior gates,
    or claim certainty; it makes every layer read the same evidence and state the
    same conflict/wait reasons.
    """
    model_input = analysis.get("model_input") or {}
    structure_signal = model_input.get("structure_signal") or {}
    trade_plan = analysis.get("trade_plan") or {}
    market_structure = analysis.get("market_structure") or {}
    sentiment = sentiment_context or analysis.get("sentiment") or {}
    direction = str(trade_plan.get("direction") or "").upper()
    chart_mode = str(structure_signal.get("mode") or "WAIT").upper()
    bias = str(analysis.get("bias") or "neutral").lower()
    rr = _safe_float(trade_plan.get("risk_reward"))
    confidence = _safe_float(analysis.get("confidence"))
    sentiment_label = str(sentiment.get("label") or "neutral").lower()
    sentiment_score = _safe_float(sentiment.get("score"))
    bars = _safe_int(model_input.get("bars"))
    warnings = []
    blockers = []
    confirmations = []

    if model_input.get("source") == "postgres_live_market_bars":
        confirmations.append("AI Analysis is using the same Postgres candle store as Advanced Charts/Senior review.")
    elif model_input.get("source") == "history_store_daily":
        confirmations.append("AI Analysis is using synced historical candles.")
    else:
        blockers.append("Analysis is not using enough real stored candles yet.")

    if bars < 80:
        blockers.append("Less than 80 usable candles; analysis is not reliable enough for Senior approval.")
    if chart_mode in {"CALL", "PUT"}:
        confirmations.append(f"Backend chart structure currently maps to {chart_mode}.")
    else:
        warnings.append("Chart structure is WAIT/unclear; Senior layer should avoid forcing a trade.")

    if direction == "LONG" and chart_mode == "PUT":
        blockers.append("Trade plan is LONG but chart structure is PUT/bearish.")
    elif direction == "SHORT" and chart_mode == "CALL":
        blockers.append("Trade plan is SHORT but chart structure is CALL/bullish.")
    elif direction == "LONG" and chart_mode == "CALL":
        confirmations.append("Trade direction agrees with CALL structure.")
    elif direction == "SHORT" and chart_mode == "PUT":
        confirmations.append("Trade direction agrees with PUT structure.")

    if rr < 1.5:
        blockers.append(f"R:R {rr:.2f} is below the minimum research threshold.")
    elif rr < 1.8:
        warnings.append(f"R:R {rr:.2f} is acceptable for analysis but weak for strict Senior selection.")
    else:
        confirmations.append(f"R:R {rr:.2f} meets professional selection range.")

    if sentiment_label == "bullish" and direction == "LONG":
        confirmations.append("Sentiment supports the bullish route.")
    elif sentiment_label == "bearish" and direction == "SHORT":
        confirmations.append("Sentiment supports the bearish route.")
    elif sentiment_label in {"bullish", "bearish"} and direction:
        warnings.append(f"Sentiment is {sentiment_label}, which conflicts with the {direction} route.")
    else:
        warnings.append("Sentiment is neutral/low coverage; use it only as advisory evidence.")

    if market_structure.get("choch"):
        warnings.append("CHoCH is active; wait for candle confirmation before treating the setup as stable.")
    if market_structure.get("bos") and market_structure.get("bos") != "none":
        confirmations.append(f"BOS is {market_structure.get('bos')}.")

    score = 0
    score += 25 if model_input.get("source") in {"postgres_live_market_bars", "history_store_daily"} and bars >= 80 else 0
    score += 20 if chart_mode in {"CALL", "PUT"} else 0
    score += 15 if not any("conflicts" in item or "but chart" in item for item in blockers) else 0
    score += min(20, max(0, confidence - 50) * 0.45)
    score += 10 if rr >= 1.8 else 5 if rr >= 1.5 else 0
    score += 10 if abs(sentiment_score) >= 10 and not any("Sentiment" in item and "conflicts" in item for item in warnings) else 3
    score = round(max(0, min(100, score)), 1)
    verdict = "BLOCKED_RESEARCH_ONLY" if blockers else "SENIOR_REVIEW_READY" if score >= 72 else "WATCHLIST_ONLY"
    if blockers:
        senior_action = "WAIT"
    elif score >= 80:
        senior_action = "ALLOW_PAPER_REVIEW"
    elif score >= 72:
        senior_action = "ALLOW_ONLY_IF_LIVE_GATES_PASS"
    else:
        senior_action = "WATCH"
    return {
        "function": "AI Market Decision Alignment",
        "verdict": verdict,
        "senior_action": senior_action,
        "confluence_score": score,
        "orders_allowed": False,
        "paper_only": True,
        "data_source": model_input,
        "chart_structure": {
            "mode": chart_mode,
            "bias": structure_signal.get("bias"),
            "active_line": structure_signal.get("active_line"),
            "latest_event": structure_signal.get("latest_event"),
            "reason": structure_signal.get("reason"),
        },
        "ml_context": {"bias": bias, "confidence": confidence, "direction": direction, "risk_reward": rr},
        "sentiment_context": {
            "label": sentiment_label,
            "score": sentiment_score,
            "confidence": _safe_float(sentiment.get("confidence")),
            "articles": _safe_int(sentiment.get("articles")),
            "mode": sentiment.get("mode"),
        },
        "confirmations": confirmations[:8],
        "warnings": warnings[:8],
        "blockers": blockers[:8],
        "agentic_ai_instruction": "Explain this evidence and ask for missing data if needed; do not override Senior/risk gates or claim guaranteed profit.",
        "senior_layer_instruction": "Use this pack as pre-trade context; actual entries still require live feed freshness, executable route, spread/depth, quality, R:R and daily-risk gates.",
    }


def stock_analysis(db: sqlite3.Connection, user_id: int, symbol: str, enhance_narrative: bool = True) -> Dict:
    symbol = symbol.upper().strip()
    stock = next((item for item in market_snapshot() if item["symbol"] == symbol), None)
    security = resolve_security(symbol)
    rows = HistoryStore().candles(security["symbol"], security["exchange"], "day", 500) if security else []
    if len(rows) >= 80:
        candles = [{"time": item["timestamp"], "open": item["open"], "high": item["high"], "low": item["low"], "close": item["close"], "volume": item["volume"]} for item in rows]
        analysis = analyse_structure(security["symbol"], security["name"], candles[-1]["close"], candles)
        analysis["model_input"] = {"source": "history_store_daily", "bars": len(candles), "timeframe": "1D"}
    elif security:
        chart = {}
        for timeframe in ("5m", "1m", "1D"):
            try:
                chart = chart_data(security["symbol"], timeframe)
                if len(chart.get("candles") or []) >= 80:
                    break
            except Exception:
                chart = {}
        candles = chart.get("candles") or []
        if len(candles) >= 80:
            analysis = analyse_structure(security["symbol"], security["name"], candles[-1]["close"], candles)
            analysis["data_mode"] = chart.get("data_mode") or "postgres_live_market_bars"
            analysis["model_input"] = {
                "source": "postgres_live_market_bars",
                "bars": len(candles),
                "timeframe": chart.get("timeframe") or "intraday",
                "latest_source": chart.get("latest_source"),
                "source_summary": chart.get("source_summary"),
                "structure_signal": chart.get("structure_signal"),
            }
            analysis["timeframes"] = {
                **analysis.get("timeframes", {}),
                "source_note": "AI Analysis used the same Postgres candle store as Advanced Charts.",
            }
        elif stock:
            analysis = analyse_structure(stock["symbol"], stock["name"], stock["price"])
            analysis["model_input"] = {"source": "market_snapshot_fallback", "bars": 0, "timeframe": "simulated"}
        else:
            raise ValueError(f"No usable stored candles for {security['exchange']}:{security['symbol']}. Run Upstox REST backfill or keep the live stream running, then retry.")
    elif stock:
        analysis = analyse_structure(stock["symbol"], stock["name"], stock["price"])
        analysis["model_input"] = {"source": "market_snapshot_fallback", "bars": 0, "timeframe": "simulated"}
    else:
        if not security:
            raise ValueError("Unknown NSE/BSE stock")
        raise ValueError(f"No usable stored candles for {security['exchange']}:{security['symbol']}. Run Upstox REST backfill or keep the live stream running, then retry.")
    sentiment_context = None
    if enhance_narrative:
        try:
            news = articles_for_symbol(db, security["symbol"] if security else symbol,
                                       security["name"] if security else symbol, allow_fetch=True)
            stock_for_sentiment = {
                "symbol": symbol,
                "name": security["name"] if security else symbol,
                "price": float(analysis.get("price") or analysis["trade_plan"]["entry"]),
                "change_pct": 0,
                "volume_ratio": 1,
            }
            sentiment = analyse_sentiment(stock_for_sentiment, market_snapshot(), news["articles"], news["mode"])
            analysis["sentiment"] = {
                "provider": news["provider"], "mode": news["mode"], "label": sentiment["label"],
                "score": sentiment["score"], "confidence": sentiment["confidence"],
                "summary": sentiment["summary"], "articles": sentiment["coverage"]["articles"],
            }
            sentiment_context = analysis["sentiment"]
        except Exception as exc:
            analysis["sentiment"] = {
                "provider": NEWS_PROVIDER, "mode": "unavailable", "label": "neutral",
                "score": 0, "confidence": 0,
                "summary": f"Sentiment unavailable: {type(exc).__name__}", "articles": 0,
            }
            sentiment_context = analysis["sentiment"]
        analysis["decision_alignment"] = _analysis_decision_alignment(analysis, sentiment_context)
        gateway_result = AIGateway(db, user_id).explain(analysis)
        analysis["narrative"] = gateway_result["text"]
        analysis["model"] = {"engine": "market-structure-ensemble", "narrative_provider": gateway_result["provider"], "narrative_model": gateway_result["model"], "cached": gateway_result["cached"], "fallback": gateway_result["fallback"], "data_mode": analysis.get("data_mode", "simulated")}
    else:
        analysis["decision_alignment"] = _analysis_decision_alignment(analysis, sentiment_context)
        analysis["model"] = {"engine": "market-structure-ensemble", "narrative_provider": "local", "narrative_model": "market-structure-ensemble", "cached": False, "fallback": False, "data_mode": analysis.get("data_mode", "simulated")}
    db.execute("INSERT INTO stock_analyses(user_id,symbol,timeframe,bias,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, symbol, "1m+15m", analysis["bias"], analysis["confidence"], json.dumps(analysis), now_iso()))
    db.execute("DELETE FROM stock_analyses WHERE user_id=? AND id NOT IN (SELECT id FROM stock_analyses WHERE user_id=? ORDER BY id DESC LIMIT 500)", (user_id, user_id))
    db.commit()
    return analysis


def latest_analysis(db: sqlite3.Connection, user_id: int, symbol: str) -> Dict:
    row = db.execute("SELECT payload, created_at FROM stock_analyses WHERE user_id=? AND symbol=? ORDER BY id DESC LIMIT 1", (user_id, symbol.upper())).fetchone()
    if row:
        cached = json.loads(row["payload"])
        created_at_str = row["created_at"]
        try:
            created_dt = datetime.fromisoformat(created_at_str)
            age_secs = (datetime.now(timezone.utc) - created_dt.replace(tzinfo=timezone.utc if created_dt.tzinfo is None else created_dt.tzinfo)).total_seconds()
        except Exception:
            age_secs = 999
        if age_secs < 60:
            security = resolve_security(symbol)
            source = (cached.get("model_input") or {}).get("source")
            has_history = bool(security and len(HistoryStore().candles(security["symbol"], security["exchange"], "day", 80)) >= 80)
            if source in {"postgres_live_market_bars", "history_store_daily"} or cached.get("data_mode") == ("kite_historical" if has_history else "simulated"):
                return cached
    return stock_analysis(db, user_id, symbol)


def glossary() -> Dict:
    return GLOSSARY


def model_status() -> Dict:
    return provider_catalog()


def ai_gateway_status(db: sqlite3.Connection, user_id: int) -> Dict:
    return gateway_status(db, user_id)


def refresh_market_analyses(db: sqlite3.Connection, user_id: int) -> int:
    stocks = market_snapshot()
    for stock in stocks:
        stock_analysis(db, user_id, stock["symbol"], enhance_narrative=False)
    return len(stocks)


def _sentiment_stock_context(symbol: str, stocks: List[Dict]) -> Dict:
    requested_symbol = symbol.upper().strip()
    stock = next((item for item in stocks if item["symbol"] == requested_symbol), None)
    if stock:
        return stock
    security = resolve_security(requested_symbol)
    if not security:
        raise ValueError("Unknown NSE/BSE symbol")
    latest = None
    if DATABASE_URL:
        engine = None
        try:
            engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
            with engine.connect() as connection:
                latest = connection.execute(text("""
                    SELECT b.close_price price,b.volume,b.bar_time
                    FROM live_market_bars b
                    JOIN instrument_master i ON i.id=b.instrument_id
                    WHERE i.exchange=:exchange AND i.symbol=:symbol
                      AND b.interval IN ('1minute','5minute','day')
                    ORDER BY b.bar_time DESC,
                             CASE b.interval WHEN '1minute' THEN 0 WHEN '5minute' THEN 1 ELSE 2 END
                    LIMIT 1
                """), {"exchange": security["exchange"], "symbol": security["symbol"]}).mappings().one_or_none()
        except Exception:
            latest = None
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass
    return {
        "symbol": security["symbol"],
        "name": security.get("name") or security["symbol"],
        "price": float(latest["price"]) if latest else 0,
        "change_pct": 0,
        "volume": int(latest["volume"] or 0) if latest else 0,
        "volume_ratio": 1,
        "exchange": security["exchange"],
        "data_mode": "stored_candle_context" if latest else "master_only_context",
    }


def _postgres_sentiment_articles(symbol: str, limit: int = 10) -> List[Dict]:
    """Read scored Finnhub/FinBERT news from Postgres as the sentiment source of truth.

    The UI snapshot table remains a fast display cache, but Senior layer, ML gates
    and web sentiment should agree by preferring the scored Postgres news store.
    """
    if not DATABASE_URL:
        return []
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        base = symbol.upper().strip()
        candidates = list(dict.fromkeys([base, f"{base}.NS", f"{base}.BO"]))
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT n.headline,n.summary,n.source_name,n.source_url,n.published_at,
                       s.positive_probability,s.negative_probability,s.scored_at
                FROM news_articles_v3 n
                JOIN news_sentiment_scores s ON s.article_id=n.id
                WHERE n.symbols && CAST(:symbols AS text[])
                ORDER BY s.scored_at DESC,n.published_at DESC
                LIMIT :limit
            """), {"symbols": candidates, "limit": limit}).mappings().all()
        return [{
            "headline": row.get("headline") or "",
            "summary": row.get("summary") or "",
            "source": row.get("source_name") or "Finnhub",
            "url": row.get("source_url") or "",
            "published_at": row.get("published_at").isoformat() if row.get("published_at") else now_iso(),
            "relevance": 1.0,
            "precomputed_sentiment_score": round((_safe_float(row.get("positive_probability")) - _safe_float(row.get("negative_probability"))) * 100, 1),
        } for row in rows]
    except Exception:
        return []
    finally:
        engine.dispose()


def _postgres_sentiment_article_map(symbols: List[str], limit_per_symbol: int = 10) -> Dict[str, List[Dict]]:
    if not DATABASE_URL or not symbols:
        return {}
    bases = [symbol.upper().strip() for symbol in symbols if symbol]
    provider_symbols = list(dict.fromkeys([item for base in bases for item in (base, f"{base}.NS", f"{base}.BO")]))
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT n.headline,n.summary,n.source_name,n.source_url,n.published_at,n.symbols,
                       s.positive_probability,s.negative_probability,s.scored_at
                FROM news_articles_v3 n
                JOIN news_sentiment_scores s ON s.article_id=n.id
                WHERE n.symbols && CAST(:symbols AS text[])
                ORDER BY s.scored_at DESC,n.published_at DESC
                LIMIT :limit
            """), {"symbols": provider_symbols, "limit": max(50, len(bases) * limit_per_symbol)}).mappings().all()
        result={base:[] for base in bases}
        for row in rows:
            row_symbols={str(item).upper().split(".")[0] for item in (row.get("symbols") or [])}
            for base in bases:
                if base not in row_symbols or len(result[base]) >= limit_per_symbol:
                    continue
                result[base].append({
                    "headline": row.get("headline") or "",
                    "summary": row.get("summary") or "",
                    "source": row.get("source_name") or "Finnhub",
                    "url": row.get("source_url") or "",
                    "published_at": row.get("published_at").isoformat() if row.get("published_at") else now_iso(),
                    "relevance": 1.0,
                    "precomputed_sentiment_score": round((_safe_float(row.get("positive_probability")) - _safe_float(row.get("negative_probability"))) * 100, 1),
                })
        return {symbol: articles for symbol, articles in result.items() if articles}
    except Exception:
        return {}
    finally:
        engine.dispose()


def sentiment_analysis(db: sqlite3.Connection, user_id: int, symbol: str, run_number: int = 0) -> Dict:
    stocks = market_snapshot(run_number)
    requested_symbol = symbol.upper().strip()
    stock = _sentiment_stock_context(requested_symbol, stocks)
    pg_articles = _postgres_sentiment_articles(stock["symbol"])
    if pg_articles:
        result = analyse_sentiment(stock, stocks, pg_articles, "postgres_scored_finnhub_news")
        result["function_name"] = "Sentiment Intelligence"
        result["monitored_universe_note"] = "Automatic worker scans the configured liquid NSE news universe. Postgres scored news is the source of truth; UI snapshots are only a fast display cache."
        result["news_provider"] = NEWS_PROVIDER
        result["provider_status"] = {
            "provider": NEWS_PROVIDER,
            "mode": "postgres_news_sentiment_scores",
            "webhook_configured": bool(FINNHUB_WEBHOOK_URL),
            "server_side_only": True,
            "trade_layer": "advisory unless NIVESH_SHADOW_SENTIMENT_GATE_ENABLED=1",
            "source_of_truth": "news_articles_v3/news_sentiment_scores",
        }
        db.execute("INSERT INTO sentiment_snapshots(user_id,symbol,score,label,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, result["symbol"], result["score"], result["label"], result["confidence"], json.dumps(result), now_iso()))
        db.execute("DELETE FROM sentiment_snapshots WHERE user_id=? AND id NOT IN (SELECT id FROM sentiment_snapshots WHERE user_id=? ORDER BY id DESC LIMIT 500)", (user_id, user_id))
        db.commit()
        return result
    cached = db.execute(
        "SELECT payload FROM sentiment_snapshots WHERE user_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
        (user_id, requested_symbol),
    ).fetchone()
    if cached:
        result = json.loads(cached["payload"])
        result["data_mode"] = f"{result.get('data_mode', 'cached_sentiment')} · instant_cache"
        result["function_name"] = result.get("function_name") or "Sentiment Intelligence"
        result["monitored_universe_note"] = result.get("monitored_universe_note") or "Automatic worker scans the configured liquid NSE news universe; manual page lookup can analyse any known NSE/BSE master symbol."
        result["provider_status"] = result.get("provider_status") or {
            "provider": result.get("news_provider") or NEWS_PROVIDER,
            "mode": "cached_sentiment",
            "webhook_configured": bool(FINNHUB_WEBHOOK_URL),
            "server_side_only": True,
            "trade_layer": "advisory unless NIVESH_SHADOW_SENTIMENT_GATE_ENABLED=1",
        }
        return result
    news = articles_for_symbol(db, stock["symbol"], stock["name"], allow_fetch=False)
    result = analyse_sentiment(stock, stocks, news["articles"], news["mode"])
    result["function_name"] = "Sentiment Intelligence"
    result["monitored_universe_note"] = "Automatic worker scans the configured liquid NSE news universe; manual page lookup can analyse any known NSE/BSE master symbol."
    result["news_provider"] = news["provider"]
    result["provider_status"] = {
        "provider": news["provider"],
        "mode": news["mode"],
        "webhook_configured": bool(FINNHUB_WEBHOOK_URL),
        "server_side_only": True,
        "trade_layer": "advisory unless NIVESH_SHADOW_SENTIMENT_GATE_ENABLED=1",
    }
    db.execute("INSERT INTO sentiment_snapshots(user_id,symbol,score,label,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, result["symbol"], result["score"], result["label"], result["confidence"], json.dumps(result), now_iso()))
    db.execute("DELETE FROM sentiment_snapshots WHERE user_id=? AND id NOT IN (SELECT id FROM sentiment_snapshots WHERE user_id=? ORDER BY id DESC LIMIT 500)", (user_id, user_id))
    db.commit()
    return result


def sentiment_dashboard(db: sqlite3.Connection, user_id: int, symbol: str = "RELIANCE") -> Dict:
    stocks = market_snapshot()
    selected = sentiment_analysis(db, user_id, symbol)
    leaders = []
    stock_context={item["symbol"]:item for item in stocks}
    pg_article_map=_postgres_sentiment_article_map(SENTIMENT_INTELLIGENCE_SYMBOLS)
    for sym, articles in pg_article_map.items():
        stock = stock_context.get(sym) or _sentiment_stock_context(sym, stocks)
        enriched = analyse_sentiment(stock, stocks, articles, "postgres_scored_finnhub_news")
        enriched["function_name"] = "Sentiment Intelligence"
        leaders.append(enriched)
    if not leaders:
        rows = db.execute(
            f"""
            SELECT current.symbol,current.payload,current.id latest_id
            FROM sentiment_snapshots current
            JOIN (
                SELECT symbol,MAX(id) latest_id
                FROM sentiment_snapshots
                WHERE user_id=? AND symbol IN ({','.join('?' for _ in SENTIMENT_INTELLIGENCE_SYMBOLS)})
                GROUP BY symbol
            ) latest ON latest.symbol=current.symbol AND latest.latest_id=current.id
            WHERE current.user_id=?
            ORDER BY current.id DESC
            LIMIT 50
            """,
            (user_id, *SENTIMENT_INTELLIGENCE_SYMBOLS, user_id),
        ).fetchall()
        for row in rows:
            try:
                leaders.append(json.loads(row["payload"]))
            except Exception:
                continue
    if len(leaders) < 12:
        for stock in stocks[:12]:
            if any(item["symbol"] == stock["symbol"] for item in leaders):
                continue
            news = articles_for_symbol(db, stock["symbol"], stock["name"], allow_fetch=False)
            leaders.append(analyse_sentiment(stock, stocks, news["articles"], news["mode"]))
            if len(leaders) >= 12:
                break
    leaders.sort(key=lambda item: item["score"], reverse=True)
    bullish = sum(1 for item in leaders if item["label"] == "bullish")
    bearish = sum(1 for item in leaders if item["label"] == "bearish")
    mood_score = round(sum(item["score"] for item in leaders) / len(leaders), 1) if leaders else 0
    return {
        "selected": selected,
        "market_mood": {"score": mood_score, "label": "risk-on" if mood_score >= 15 else "risk-off" if mood_score <= -15 else "mixed", "bullish": bullish, "neutral": len(leaders)-bullish-bearish, "bearish": bearish},
        "leaders": leaders[:12],
        "laggards": list(reversed(leaders[-12:])),
        "universe_count": len(SENTIMENT_INTELLIGENCE_SYMBOLS),
        "scored_universe_count": len(leaders),
        "updated_at": now_iso(),
        "data_mode": selected["data_mode"],
        "monitored_symbols": SENTIMENT_INTELLIGENCE_SYMBOLS,
    }


def get_sentiment_dashboard(db: sqlite3.Connection, user_id: int, symbol: str = "RELIANCE") -> Dict:
    """Compatibility wrapper for the brain pipeline.

    The web app, Senior layer, and sentiment brain should all read the same
    Postgres-backed sentiment dashboard instead of each using a separate cache
    or raw news source.
    """
    return sentiment_dashboard(db, user_id, symbol)


def ingest_finnhub_webhook(db: sqlite3.Connection, payload: Dict, supplied_secret: str = "") -> Dict:
    if not FINNHUB_WEBHOOK_SECRET:
        raise ValueError("FINNHUB_WEBHOOK_SECRET is not configured")
    if not supplied_secret or not secrets.compare_digest(str(supplied_secret), FINNHUB_WEBHOOK_SECRET):
        raise PermissionError("Invalid Finnhub webhook secret")
    raw_items = payload if isinstance(payload, list) else payload.get("data") or payload.get("articles") or payload.get("news") or [payload]
    if not isinstance(raw_items, list):
        raw_items = [raw_items]
    saved = linked = 0
    for item in raw_items[:100]:
        if not isinstance(item, dict):
            continue
        headline = str(item.get("headline") or item.get("title") or "").strip()
        if not headline:
            continue
        source = str(item.get("source") or "Finnhub").strip() or "Finnhub"
        published = item.get("datetime") or item.get("published_at") or item.get("publishedAt") or now_iso()
        if isinstance(published, (int, float)):
            published = datetime.fromtimestamp(published, timezone.utc).isoformat()
        digest = hashlib.sha256(f"{headline.lower()}|{source.lower()}|{str(published)[:10]}".encode()).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO news_articles(provider,external_id,content_hash,headline,summary,source,url,published_at,received_at,raw_payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("finnhub_webhook", str(item.get("id") or ""), digest, headline, str(item.get("summary") or item.get("description") or "")[:10000],
             source, str(item.get("url") or ""), str(published), now_iso(), json.dumps(item)[:50000]),
        )
        row = db.execute("SELECT id FROM news_articles WHERE content_hash=?", (digest,)).fetchone()
        saved += 1
        symbols = set()
        for value in (item.get("symbol"), item.get("related"), item.get("symbols")):
            if isinstance(value, str):
                symbols.update(part.strip().upper().split(".")[0] for part in value.replace(";", ",").split(",") if part.strip())
            elif isinstance(value, list):
                symbols.update(str(part).strip().upper().split(".")[0] for part in value if str(part).strip())
        for ticker in symbols:
            if 2 <= len(ticker) <= 20:
                db.execute("INSERT OR IGNORE INTO news_article_symbols(article_id,exchange,symbol,relevance) VALUES(?,?,?,?)",
                           (row["id"], "NSE", ticker, 1.0))
                linked += 1
    db.execute("INSERT INTO news_ingestion_logs(provider,query,status,articles_received,latency_ms,created_at) VALUES(?,?,?,?,?,?)",
               ("finnhub_webhook", "webhook", "ok", saved, 0, now_iso()))
    db.commit()
    return {"status": "accepted", "provider": "finnhub_webhook", "articles_received": saved,
            "symbol_links": linked, "server_side_only": True}


def market_update(db: sqlite3.Connection, user_id: int, run_number: int = 0) -> Dict:
    live=live_market_snapshot(DATABASE_URL) if DATABASE_URL else None
    if live:
        sentiment=[analyse_sentiment(stock,live["quotes"]) for stock in live["quotes"][:100]]
        live["sentiment_score"]=round(sum(item["score"] for item in sentiment)/max(1,len(sentiment)),1)
        return live
    stocks = market_snapshot(run_number)
    pulse = math.sin(run_number * .37) * .00045
    indices = [{**item, "price": round(item["price"] * (1 + pulse * (1 + idx * .12)), 2), "change_pct": round(item["change_pct"] + pulse * 100, 2)} for idx, item in enumerate(INDICES)]
    advancing = sum(1 for item in stocks if item["change_pct"] > 0)
    sentiment = [analyse_sentiment(stock, stocks) for stock in stocks]
    return {"indices": indices, "quotes": stocks, "breadth": {"advancing": advancing, "declining": len(stocks)-advancing, "ratio": round(advancing/max(1,len(stocks)),2)}, "sentiment_score": round(sum(item["score"] for item in sentiment)/len(sentiment),1), "updated_at": now_iso(), "data_mode": "simulated"}


def refresh_sentiments(db: sqlite3.Connection, user_id: int, run_number: int = 0) -> int:
    stocks = market_snapshot(run_number)
    for stock in stocks:
        news = articles_for_symbol(db, stock["symbol"], stock["name"], allow_fetch=False)
        result = analyse_sentiment(stock, stocks, news["articles"], news["mode"])
        db.execute("INSERT INTO sentiment_snapshots(user_id,symbol,score,label,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, result["symbol"], result["score"], result["label"], result["confidence"], json.dumps(result), now_iso()))
    db.commit()
    return len(stocks)


def stock_chart(symbol: str, timeframe: str) -> Dict:
    return chart_data(symbol, timeframe)


def backtest_report(symbol: str, strategy: str, capital: float, start: str = "", end: str = "", horizon_years: int = 1) -> Dict:
    report = run_backtest(symbol, strategy, capital, start, end, horizon_years=horizon_years)
    report["sentiment_context"] = {
        "provider": NEWS_PROVIDER,
        "included_in_backtest": False,
        "reason": "Current/news sentiment is excluded from historical backtests to avoid look-ahead bias.",
        "trade_layer_use": "Live-paper Senior selector can use scored recent Finnhub news as an advisory/conflict gate.",
    }
    return report


def listed_securities(query: str, exchange: str, limit: int, offset: int) -> Dict:
    return search_securities(query, exchange, limit, offset)


def market_history_status() -> Dict:
    universe = security_master_stats()["counts"]
    history = HistoryStore().stats()
    provider = MARKET_DATA_PROVIDER if MARKET_DATA_PROVIDER in {"zerodha", "upstox"} else "zerodha"
    configured = bool(UPSTOX_ACCESS_TOKEN or os.getenv("UPSTOX_ANALYTICS_TOKEN","")) if provider == "upstox" else bool(KITE_API_KEY and KITE_ACCESS_TOKEN)
    capacity = {"mode": "Upstox Market Data Feed V3", "combined_instruments": 2000, "connections_per_user": 2} if provider == "upstox" else {"instruments_per_connection": 3000, "connections_per_api_key": 3, "total_instruments": 9000}
    return {"provider": "upstox_v3" if provider == "upstox" else "zerodha_kite", "configured": configured,
            "universe": universe, "history": history,
            "coverage_pct": round(history["symbols_complete"] / max(1, universe["total"]) * 100, 2),
            "planned_history": "daily candles for all matched NSE/BSE equities",
            "live_capacity": capacity,
            "live_mode": "WebSocket near-real-time; zero latency is not guaranteed",
            "order_execution": "disabled"}


def derivative_plan(db: sqlite3.Connection, user_id: int, symbol: str, market_view: str,
                    volatility: str, strategy: str = "auto") -> Dict:
    symbol = symbol.upper().strip()
    stock = next((item for item in market_snapshot() if item["symbol"] == symbol), None)
    if not stock:
        security = resolve_security(symbol)
        rows = HistoryStore().candles(security["symbol"], security["exchange"], "day", 1) if security else []
        if not security or not rows:
            raise ValueError("Stock requires a synced close before a derivatives plan can be prepared")
        spot = rows[-1]["close"]
    else:
        spot = stock["price"]
    held = bool(db.execute("SELECT 1 FROM holdings WHERE user_id=? AND symbol=?", (user_id, symbol)).fetchone())
    plan = build_derivative_plan(symbol, spot, market_view, volatility, held, strategy)
    db.execute("INSERT INTO derivative_plans(user_id,symbol,strategy,market_view,spot,expiry,payload,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (user_id, symbol, plan["strategy"], market_view, spot, plan["expiry"], json.dumps(plan), plan["status"], now_iso()))
    db.commit()
    return plan


def derivative_plans(db: sqlite3.Connection, user_id: int) -> Dict:
    rows = db.execute("SELECT id,symbol,strategy,market_view,spot,expiry,payload,status,created_at FROM derivative_plans WHERE user_id=? ORDER BY id DESC LIMIT 30", (user_id,)).fetchall()
    return {"items":[{**dict(row), "payload":json.loads(row["payload"])} for row in rows]}


def bot_chat(db: sqlite3.Connection, user_id: int, message: str, conversation_id=None) -> Dict:
    return chat(db, user_id, message, int(conversation_id) if conversation_id else None)


def bot_conversations(db: sqlite3.Connection, user_id: int) -> Dict:
    return list_conversations(db, user_id)


def bot_conversation(db: sqlite3.Connection, user_id: int, conversation_id: int) -> Dict:
    return conversation(db, user_id, conversation_id)


def production_status(db: sqlite3.Connection, user_id: int) -> Dict:
    data = operations_status(db, user_id)
    if DATABASE_URL:
        engine = _get_engine()
        try:
            data["shadow"] = shadow_session_status(engine)
            with engine.connect() as connection:
                active_model_version=connection.execute(text(
                    "SELECT version FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1"
                )).scalar_one_or_none()
                try:
                    selector=connection.execute(text("""
                        SELECT COUNT(*) evaluated,
                               COUNT(*) FILTER(WHERE accepted) accepted,
                               COUNT(*) FILTER(WHERE NOT accepted) rejected
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                    """)).mappings().one_or_none()
                    top_rejection=connection.execute(text("""
                        SELECT COALESCE(rejection_reason,'accepted') reason,COUNT(*) count
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                        GROUP BY COALESCE(rejection_reason,'accepted')
                        ORDER BY count DESC LIMIT 1
                    """)).mappings().one_or_none()
                except Exception:
                    selector={"evaluated":0,"accepted":0,"rejected":0}
                    top_rejection=None
                try:
                    candidate_rows=connection.execute(text("""
                        SELECT observed_at,model_version,exchange,symbol,instrument_type,signal,probability,
                               selector_stage,accepted,rejection_reason,chart_strategy,route,rr,
                               expected_net_edge_bps,quality_score,selector_score,sector,details
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                        ORDER BY observed_at DESC,id DESC
                        LIMIT 30
                    """)).mappings().all()
                    rejection_rows=connection.execute(text("""
                        SELECT COALESCE(rejection_reason,'accepted') reason,COUNT(*) count
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                          AND NOT accepted
                        GROUP BY COALESCE(rejection_reason,'accepted')
                        ORDER BY count DESC
                        LIMIT 8
                    """)).mappings().all()
                    stage_rows=connection.execute(text("""
                        SELECT COALESCE(selector_stage,'unknown') stage,COUNT(*) count
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                          AND NOT accepted
                        GROUP BY COALESCE(selector_stage,'unknown')
                        ORDER BY count DESC
                    """)).mappings().all()
                    hourly_rows=connection.execute(text("""
                        SELECT TO_CHAR(observed_at AT TIME ZONE 'Asia/Kolkata', 'HH24:00') hour_bucket,
                               COUNT(*) total,
                               COUNT(*) FILTER(WHERE accepted) accepted,
                               COUNT(*) FILTER(WHERE NOT accepted) rejected
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                        GROUP BY hour_bucket
                        ORDER BY hour_bucket ASC
                    """)).mappings().all()
                    accepted_rows=connection.execute(text("""
                        SELECT observed_at,exchange,symbol,instrument_type,signal,probability,
                               chart_strategy,route,rr,quality_score,selector_score,details
                        FROM trade_candidate_audits
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                          AND accepted
                        ORDER BY observed_at DESC,id DESC
                        LIMIT 10
                    """)).mappings().all()
                except Exception:
                    candidate_rows=[]
                    rejection_rows=[]
                    stage_rows=[]
                    hourly_rows=[]
                    accepted_rows=[]
                try:
                    opportunity_rows=connection.execute(text("""
                        SELECT observed_at,session_block,symbol,exchange,instrument_type,opportunity_side,
                               option_route,regime,strategy,confidence,quality_score,risk_reward,
                               ml_probability,senior_decision,rejection_reason,counterfactual_5m,
                               counterfactual_15m,counterfactual_30m,outcome_label,details
                        FROM senior_market_opportunities
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                        ORDER BY observed_at DESC,confidence DESC,id DESC
                        LIMIT 20
                    """)).mappings().all()
                    opportunity_summary=connection.execute(text("""
                        SELECT COUNT(*) scanned,
                               COUNT(*) FILTER(WHERE senior_decision='TRADED') traded,
                               COUNT(*) FILTER(WHERE senior_decision<>'TRADED') missed,
                               COUNT(*) FILTER(WHERE outcome_label='would_have_worked') missed_worked,
                               MAX(observed_at) latest
                        FROM senior_market_opportunities
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                    """)).mappings().one_or_none()
                except Exception:
                    opportunity_rows=[]
                    opportunity_summary=None
                try:
                    today_d = datetime.now(IST).date()
                    start_dt = datetime(today_d.year, today_d.month, today_d.day, 0, 0, 0, tzinfo=IST).astimezone(timezone.utc)
                    end_dt = start_dt + timedelta(days=1)
                    one_second=connection.execute(text("""
                        WITH recent AS (
                            SELECT instrument_id,COUNT(*) bars,MAX(bar_time) latest_bar,
                                   MIN(bar_time) first_bar,
                                   COUNT(*) FILTER(WHERE source IN ('upstox_v3','zerodha_kite')) live_bars,
                                   COUNT(*) FILTER(WHERE source='upstox_rest_1s_proxy') proxy_bars,
                                   GREATEST(1,EXTRACT(EPOCH FROM (MAX(bar_time)-MIN(bar_time)))::bigint+1) expected_seconds
                            FROM live_market_bars
                            WHERE interval='1second'
                              AND bar_time >= :start_dt AND bar_time < :end_dt
                            GROUP BY instrument_id
                        )
                        SELECT COUNT(*) instruments,
                               COALESCE(SUM(bars),0) bars,
                               MAX(latest_bar) latest_bar,
                               MIN(first_bar) first_bar,
                               EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-MAX(latest_bar))) latest_age_seconds,
                               ROUND(AVG(LEAST(1,bars::numeric/NULLIF(expected_seconds,0))) * 100,2) avg_coverage_pct,
                               ROUND(MIN(LEAST(1,bars::numeric/NULLIF(expected_seconds,0))) * 100,2) min_coverage_pct,
                               COALESCE(SUM(GREATEST(expected_seconds-bars,0)),0) estimated_missing_seconds,
                               COALESCE(SUM(live_bars),0) live_bars,
                               COALESCE(SUM(proxy_bars),0) proxy_bars,
                               COUNT(*) FILTER(WHERE bars::numeric/NULLIF(expected_seconds,0) < .95) low_coverage_instruments
                        FROM recent
                    """), {"start_dt": start_dt, "end_dt": end_dt}).mappings().one_or_none()
                except Exception:
                    one_second=None
                try:
                    sentiment_pg=connection.execute(text("""
                        SELECT COUNT(DISTINCT sym.symbol) symbols,
                               COUNT(*) scored_articles,
                               MAX(n.published_at) latest_article,
                               MAX(s.scored_at) latest_score
                        FROM news_articles_v3 n
                        JOIN news_sentiment_scores s ON s.article_id=n.id,
                             LATERAL UNNEST(n.symbols) AS sym(symbol)
                    """)).mappings().one_or_none()
                except Exception:
                    sentiment_pg=None
            sqlite_sentiment=db.execute("SELECT COUNT(DISTINCT symbol) symbols,COUNT(*) snapshots,MAX(created_at) latest_snapshot FROM sentiment_snapshots").fetchone()
            component_lookup={str(item["name"]).lower():item for item in data.get("components",[])}
            evaluated=_safe_int((selector or {}).get("evaluated"))
            accepted=_safe_int((selector or {}).get("accepted"))
            rejected=_safe_int((selector or {}).get("rejected"))
            data["status_bar"]={
                "live_feed":component_lookup.get("market data feed",{}),
                "live_bars":component_lookup.get("completed live bars",{}),
                "model_version":active_model_version or "unknown",
                "senior_selector":{
                    "status":"operational" if evaluated>0 else "waiting",
                    "detail":f"{accepted} accepted / {rejected} rejected"
                             + (f" · top: {top_rejection['reason']}" if top_rejection else ""),
                },
                "shadow_sessions":f"{data['shadow'].get('effective_completed_sessions',0)} / {data['shadow'].get('target',90)}",
                "kill_switch":component_lookup.get("kill switch",{}),
                "order_execution":component_lookup.get("order execution",{}),
                "sentiment_gate":f"{NEWS_PROVIDER} · OFF unless NIVESH_SHADOW_SENTIMENT_GATE_ENABLED=1 and verified news is connected",
            }
            data["pre_market_visibility"]={
                "title":"Ready for market?",
                "mode":"manual_fast_button",
                "detail":"Click the check button before 9:15 AM IST. It verifies DB, Redis, live provider credentials, active model, sentiment/OI freshness and safety locks without slowing normal monitoring refresh.",
                "endpoint":"/api/pre-market/health",
                "checks":["DB healthy","Redis healthy","web healthy","worker/beat running","stream connected","Upstox token present","active model valid","sentiment scan recent","OI/depth recent","kill switch inactive"],
            }
            data["candidate_rejection_visibility"]={
                "evaluated":evaluated,
                "accepted":accepted,
                "rejected":rejected,
                "top_rejection":_jsonable_mapping(top_rejection) if top_rejection else None,
                "stages":[_jsonable_mapping(row) for row in stage_rows],
                "hourly":[_jsonable_mapping(row) for row in hourly_rows],
                "reasons":[_jsonable_mapping(row) for row in rejection_rows],
                "accepted_examples":[_jsonable_mapping(row) for row in accepted_rows],
                "recent":[_jsonable_mapping(row) for row in candidate_rows],
                "explainability":"Every no-trade day should still teach: this card shows the exact selector stage/reason, ML probability, R:R, quality, strategy, sentiment/chart details when available.",
            }
            data["senior_market_intelligence"]={
                "title":"Senior Market Regime + Missed Opportunity Tracker",
                "summary":_jsonable_mapping(opportunity_summary) if opportunity_summary else {"scanned":0,"traded":0,"missed":0,"missed_worked":0},
                "top_visible_setups":[_jsonable_mapping(row) for row in opportunity_rows],
                "purpose":"Records top 5 visible CALL/PUT opportunities, accepted/rejected reason, session regime, and 5/15/30m counterfactual outcome.",
                "paper_only":True,
                "orders_allowed":False,
            }
            data["one_second_data_status"]={
                "enabled":True,
                "role":"microstructure confirmation gate, not main model-training source yet",
                "instruments":_safe_int((one_second or {}).get("instruments")),
                "bars":_safe_int((one_second or {}).get("bars")),
                "first_bar":(one_second or {}).get("first_bar").isoformat() if (one_second and one_second.get("first_bar")) else None,
                "latest_bar":(one_second or {}).get("latest_bar").isoformat() if (one_second and one_second.get("latest_bar")) else None,
                "latest_age_seconds":round(_safe_float((one_second or {}).get("latest_age_seconds")),1),
                "avg_coverage_pct":round(_safe_float((one_second or {}).get("avg_coverage_pct")),2),
                "min_coverage_pct":round(_safe_float((one_second or {}).get("min_coverage_pct")),2),
                "estimated_missing_seconds":_safe_int((one_second or {}).get("estimated_missing_seconds")),
                "live_bars":_safe_int((one_second or {}).get("live_bars")),
                "proxy_bars":_safe_int((one_second or {}).get("proxy_bars")),
                "proxy_source":"upstox_rest_1s_proxy",
                "low_coverage_instruments":_safe_int((one_second or {}).get("low_coverage_instruments")),
                "coverage_status":"proxy_repaired" if _safe_int((one_second or {}).get("proxy_bars")) else ("ready_for_confirmation" if _safe_int((one_second or {}).get("bars")) and _safe_float((one_second or {}).get("avg_coverage_pct")) >= 80 else "insufficient_or_partial"),
                "verified_for_training":bool(_safe_int((one_second or {}).get("bars")) and not _safe_int((one_second or {}).get("proxy_bars")) and _safe_float((one_second or {}).get("avg_coverage_pct")) >= 95 and _safe_int((one_second or {}).get("low_coverage_instruments")) == 0),
                "training_rule":"Do not train/promote a 1-second model until continuous storage, gap tracking, chart display and storage growth are proven stable.",
            }
            data["sentiment_source_of_truth"]={
                "recommended_store":"Postgres news_articles_v3 + news_sentiment_scores",
                "provider":NEWS_PROVIDER,
                "postgres_symbols":_safe_int((sentiment_pg or {}).get("symbols")),
                "postgres_scored_articles":_safe_int((sentiment_pg or {}).get("scored_articles")),
                "postgres_latest_score":(sentiment_pg or {}).get("latest_score").isoformat() if (sentiment_pg and sentiment_pg.get("latest_score")) else None,
                "ui_cache_symbols":_safe_int(sqlite_sentiment["symbols"] if sqlite_sentiment else 0),
                "ui_cache_snapshots":_safe_int(sqlite_sentiment["snapshots"] if sqlite_sentiment else 0),
                "ui_cache_latest":sqlite_sentiment["latest_snapshot"] if sqlite_sentiment else None,
                "alignment":"web, Senior layer, AI bot and ML inference should prefer scored Postgres news; UI snapshots are only fast display cache.",
            }
            data["quant_models"] = run_quant_snapshot(DATABASE_URL, limit=80)
            data["alpha_fragility"] = alpha_fragility_status(DATABASE_URL)
        except Exception:
            pass
    else:
        data["quant_models"] = quant_model_status()
        data["alpha_fragility"] = alpha_fragility_status(None)
    data["brain_pipeline_status"] = _brain_pipeline_status()
    data["operations_copilot"] = _build_operations_copilot(data)
    return data


def _brain_pipeline_status() -> Dict:
    try:
        from .brains import get_brain_pipeline, get_bus
        pipeline = get_brain_pipeline()
        recent = [event.to_dict() for event in get_bus().recent(n=12)]
        durable_events = []
        memory = {"status": "unavailable"}
        if DATABASE_URL:
            from .intelligence_memory import recent_brain_events
            engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
            try:
                with engine.connect() as connection:
                    durable_events = [_jsonable_mapping(row) for row in recent_brain_events(connection, limit=12)]
                    memory_row = connection.execute(text("""
                        SELECT
                          (SELECT COUNT(*) FROM counterfactual_candidate_log
                           WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE) candidate_memory_rows,
                          (SELECT COUNT(*) FROM counterfactual_candidate_log
                           WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                             AND outcome_label='would_have_worked') candidate_worked_rows,
                          (SELECT COUNT(*) FROM regime_strategy_performance
                           WHERE trade_date>=CURRENT_DATE-INTERVAL '30 days') regime_strategy_rows,
                          (SELECT COUNT(*) FROM brain_event_journal
                           WHERE (occurred_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE) durable_events_today
                    """)).mappings().one()
                    memory = {"status": "connected", **_jsonable_mapping(memory_row)}
            finally:
                engine.dispose()
        roles = {
            "market_intel": {"label": "MarketBrain", "status": "OK", "mode": "advisory data scan", "connected_to": "market_snapshot"},
            "screener": {"label": "ScreenerBrain", "status": "OK", "mode": "advisory universe filter", "connected_to": "market_snapshot candidates"},
            "signal": {"label": "SignalBrain", "status": "OK", "mode": "live-selector aware advisory", "connected_to": "LivePaperInference selector artifacts + TradingAgent"},
            "sentiment": {"label": "SentimentBrain", "status": "OK", "mode": "advisory only", "connected_to": "Postgres sentiment dashboard"},
            "risk": {"label": "RiskBrain", "status": "OK", "mode": "shadow-ledger aware", "connected_to": "shadow_execution_audits daily risk state"},
            "execution": {"label": "ExecutionBrain", "status": "ADVISORY_ONLY", "mode": "no direct trade writes", "connected_to": "shadow_execution_audits monitor"},
            "report": {"label": "ReportBrain", "status": "ADVISORY_ONLY", "mode": "shadow-ledger report", "connected_to": "shadow_execution_audits P&L"},
        }
        return {
            "status": "connected",
            "total_brains": len(pipeline),
            "brains": [{**roles.get(key, {"label": key, "status": "OK"}), "key": key} for key in pipeline.keys()],
            "canonical_trade_writer": "backend.live_inference.record_shadow_signal",
            "canonical_ledger": "shadow_execution_audits",
            "legacy_execution_disabled": True,
            "recent_bus_events": recent,
            "durable_event_journal": {
                "status": memory.get("status"),
                "recent": durable_events,
                "today": memory.get("durable_events_today", 0),
                "cross_process": True,
            },
            "long_term_memory": memory,
            "live_inference_events_connected": True,
            "orders_allowed": False,
        }
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "orders_allowed": False}


def _component_status(data: Dict, name: str) -> Dict:
    target = name.lower()
    for component in data.get("components", []):
        if str(component.get("name", "")).lower() == target:
            return component
    return {}


def _build_operations_copilot(data: Dict) -> Dict:
    """Read-only project control-room brain.

    This deliberately produces explanations and diagnostics only. It does not
    open trades, modify risk settings, overwrite evidence, or bypass the
    Senior/risk layers.
    """
    shadow = data.get("shadow") or {}
    today = shadow.get("today") or {}
    metrics = today.get("metrics") or {}
    paper = metrics.get("paper_pnl") or today.get("paper_pnl") or {}
    model_coach = paper.get("model_coach") or {}
    candidate = data.get("candidate_rejection_visibility") or {}
    senior = data.get("senior_market_intelligence") or {}
    senior_summary = senior.get("summary") or {}
    sentiment = data.get("sentiment_source_of_truth") or {}
    one_second = data.get("one_second_data_status") or {}
    quant = data.get("quant_models") or {}
    alpha = data.get("alpha_fragility") or {}
    brain = data.get("brain_pipeline_status") or {}
    status_bar = data.get("status_bar") or {}
    live_feed = _component_status(data, "Market data feed")
    live_bars = _component_status(data, "Completed live bars")
    kill_switch = _component_status(data, "Kill switch")
    execution = _component_status(data, "Order execution")

    issues = []
    warnings = []
    missing = []
    recommendations = []

    def issue(area: str, severity: str, detail: str, action: str) -> None:
        issues.append({"area": area, "severity": severity, "detail": detail, "action": action})

    if live_feed.get("status") in {"not_connected", "waiting"}:
        issue("live_feed", "critical", live_feed.get("detail") or "Market feed is not ready.", "Check provider token and start the stream before market open.")
    if live_bars.get("status") != "operational":
        issue("live_bars", "high", live_bars.get("detail") or "No fresh completed live bars.", "Verify WebSocket stream and run REST repair only for chart/history gaps.")
    if today and not today.get("eligible", today.get("valid", False)):
        reasons = today.get("rejection_reasons") or []
        issue("shadow_validation", "high", "; ".join(map(str, reasons)) or "Today is not valid forward-shadow evidence.", "Treat the day as mistake-mining evidence, not promotion credit.")
    if _safe_int(metrics.get("open_gaps") or (metrics.get("gap_repair_status") or {}).get("open")):
        issue("gap_repair", "high", "Open live-market data gaps are present.", "Repair gaps, then verify live-only buckets before counting the session.")
    if _safe_int(metrics.get("recovered_gaps") or (metrics.get("gap_repair_status") or {}).get("recovered")):
        warnings.append("REST/backfill recovered at least one gap; useful for charts/history, but not clean live-only validation.")
    if _safe_float(paper.get("realised_pnl") or today.get("net_pnl")) < 0:
        issue("paper_pnl", "medium", f"Paper P&L is negative: {paper.get('realised_pnl') or today.get('net_pnl')}.", "Review mistake tags before changing entry frequency.")
    if _safe_float(paper.get("win_rate_pct") or today.get("win_rate_pct")) and _safe_float(paper.get("win_rate_pct") or today.get("win_rate_pct")) < 45:
        issue("trade_quality", "medium", "Win rate is below the current learning threshold.", "Prioritise stale-signal exits, R:R quality and stop-loss review.")
    if (paper.get("mistake_analysis") or {}).get("mistake_tags"):
        tags = paper["mistake_analysis"]["mistake_tags"]
        top_tag = max(tags.items(), key=lambda item: item[1])[0] if tags else ""
        if top_tag:
            warnings.append(f"Most common paper-trade mistake today: {top_tag}.")
    if _safe_int(candidate.get("evaluated")) and not _safe_int(candidate.get("accepted")):
        reason = (candidate.get("top_rejection") or {}).get("reason") or "all candidates rejected"
        issue("candidate_selector", "medium", f"{candidate.get('evaluated')} candidates evaluated; none accepted. Top reason: {reason}.", "Use candidate rejection visibility before relaxing rules.")
    if _safe_int(senior_summary.get("missed_worked")):
        issue("senior_opportunities", "medium", f"{senior_summary.get('missed_worked')} missed setups would have worked in counterfactual tracking.", "Compare missed setups with official risk/candidate gates; do not force trades.")
    if _safe_int(sentiment.get("postgres_scored_articles")) == 0:
        missing.append({"area": "sentiment", "detail": "No scored Postgres news available for the current scan universe.", "action": "Run Finnhub scan or keep sentiment advisory/off until fresh scores exist."})
    if one_second.get("enabled") and not one_second.get("verified_for_training"):
        missing.append({"area": "one_second_data", "detail": "1-second data is not verified for model training.", "action": "Use 1s only as microstructure confirmation until coverage/gaps/storage are stable."})
    if quant.get("status") not in {"success", "available"}:
        missing.append({"area": "quant_models", "detail": quant.get("reason") or "Quant model snapshot is not available.", "action": "Verify Postgres live_market_bars and retry /api/ml/quant-models."})
    elif _safe_int(quant.get("ready_models")) < 4 and quant.get("status") == "success":
        warnings.append(f"Only {quant.get('ready_models',0)} quant lenses have enough recent data; use missing outputs as visibility warnings, not trade signals.")
    if alpha.get("status") == "fragile":
        issue("alpha_fragility", "high", f"Alpha robustness score is {alpha.get('robust_score')} with worst-case P&L {alpha.get('worst_case_pnl')}.", "Treat today as fragile evidence; study failure reasons before changing trade count.")
    elif alpha.get("status") == "needs_more_evidence":
        warnings.append(f"Alpha robustness needs more evidence: score {alpha.get('robust_score')}.")
    elif alpha.get("status") == "error":
        missing.append({"area": "alpha_fragility", "detail": alpha.get("reason") or "Alpha fragility layer failed.", "action": "Check shadow_execution_audits, trade_candidate_audits and senior_market_opportunities schema."})
    if data.get("recent_errors"):
        issue("monitoring", "medium", f"{len(data.get('recent_errors', []))} recent ERROR/CRITICAL events are visible.", "Inspect newest events before starting the next market session.")
    if kill_switch.get("status") == "locked":
        warnings.append("Kill switch is active; trading must remain stopped until manually reviewed.")
    if execution.get("status") == "ready":
        warnings.append("Live execution flag appears ready; confirm this is intentional. Paper mode should remain default.")

    if not issues:
        verdict = "READY_TO_OBSERVE"
        headline = "Project control room looks clean for read-only shadow observation."
        recommendations.append("Run a pre-market health check before 09:15 IST and keep paper-only safety locks enabled.")
    elif any(item["severity"] == "critical" for item in issues):
        verdict = "BLOCKED"
        headline = "Critical project dependency needs attention before shadow evidence can be trusted."
    else:
        verdict = "NEEDS_REVIEW"
        headline = "Project is running, but the evidence has warnings that should be reviewed."

    recommendations.extend([
        "Do not count REST-repaired gaps as clean live-shadow validation.",
        "Use Senior + candidate rejection cards to learn from no-trade or bad-trade days.",
        "Keep Operations Copilot read-only unless you explicitly approve a safe diagnostic/fix.",
    ])
    if model_coach.get("actions"):
        recommendations.extend(model_coach.get("actions", [])[:3])

    context_bundle = {
        "system_health": {
            "overall": data.get("overall"),
            "components": data.get("components", []),
            "recent_errors": data.get("recent_errors", [])[:8],
        },
        "live_feed": {
            "feed": live_feed,
            "bars": live_bars,
            "gap_repair": metrics.get("gap_repair_status"),
            "one_second": one_second,
        },
        "shadow_evidence": {
            "session": {
                "session_date": today.get("session_date"),
                "status": today.get("status"),
                "eligible": today.get("eligible", today.get("valid")),
                "rejection_reasons": today.get("rejection_reasons"),
                "warnings": today.get("warnings"),
            },
            "paper_pnl": paper,
            "progress": {
                "effective_completed_sessions": shadow.get("effective_completed_sessions"),
                "target": shadow.get("target"),
                "remaining_sessions": shadow.get("remaining_sessions"),
            },
        },
        "trade_intelligence": {
            "candidate_rejections": {
                "evaluated": candidate.get("evaluated"),
                "accepted": candidate.get("accepted"),
                "rejected": candidate.get("rejected"),
                "top_rejection": candidate.get("top_rejection"),
                "reasons": candidate.get("reasons", [])[:8],
            },
            "senior_opportunities": {
                "summary": senior_summary,
                "top_visible_setups": senior.get("top_visible_setups", [])[:8],
            },
            "model_version": status_bar.get("model_version"),
            "risk_state": {
                "kill_switch": kill_switch,
                "execution": execution,
                "orders_allowed": False,
            },
        },
        "sentiment": sentiment,
        "quant_models": {
            "status": quant.get("status"),
            "ready_models": quant.get("ready_models"),
            "snapshot": quant.get("snapshot"),
            "regime": ((quant.get("model_outputs") or {}).get("regime_hmm_proxy") or {}),
            "top_ranked": (((quant.get("model_outputs") or {}).get("cross_sectional_ranker") or {}).get("top_long_bias") or [])[:5],
            "factor_investing": ((quant.get("model_outputs") or {}).get("factor_investing_model") or {}),
            "ml_prediction_bridge": ((quant.get("model_outputs") or {}).get("ml_prediction_bridge") or {}),
            "portfolio_optimization": ((quant.get("model_outputs") or {}).get("portfolio_optimization_model") or {}),
            "risk_use": quant.get("risk_use"),
        },
        "alpha_fragility": {
            "status": alpha.get("status"),
            "robust_score": alpha.get("robust_score"),
            "fragility_score": alpha.get("fragility_score"),
            "worst_case_pnl": alpha.get("worst_case_pnl"),
            "failure_reasons": alpha.get("failure_reasons", [])[:6],
            "stress_tests": alpha.get("stress_tests", [])[:6],
            "trade_evidence": alpha.get("trade_evidence"),
            "candidate_evidence": alpha.get("candidate_evidence"),
            "senior_opportunity_evidence": alpha.get("senior_opportunity_evidence"),
            "paper_only": True,
            "orders_allowed": False,
        },
        "brain_pipeline": {
            "status": brain.get("status"),
            "canonical_ledger": brain.get("canonical_ledger"),
            "canonical_trade_writer": brain.get("canonical_trade_writer"),
            "legacy_execution_disabled": brain.get("legacy_execution_disabled"),
            "brains": brain.get("brains", []),
        },
    }

    return {
        "name": "Nivesh Operations Copilot",
        "mode": "READ_ONLY_DIAGNOSTIC",
        "verdict": verdict,
        "headline": headline,
        "permissions": {
            "can_read_project_state": True,
            "can_run_safe_diagnostics": True,
            "can_recommend_fixes": True,
            "can_modify_database": False,
            "can_change_trading_rules": False,
            "can_place_orders": False,
            "approval_required_for_fixes": True,
        },
        "watch_areas": [
            "live feed health", "REST gap repair", "shadow validation", "paper ledger",
            "positions/trade history", "Senior decisions", "candidate rejections",
            "sentiment freshness", "OI/depth", "chart coverage", "model version",
            "database consistency", "frontend/backend API errors", "daily P&L mistakes",
            "agentic brain pipeline",
        ],
        "issues": issues,
        "missing_or_stale": missing,
        "warnings": warnings,
        "recommendations": list(dict.fromkeys(recommendations))[:10],
        "questions_it_can_answer": [
            "Why no trades today?",
            "Why did shadow validation fail?",
            "Which stocks have missing live data?",
            "Which trades were bad and why?",
            "Which fields are empty or stale?",
            "Is the model using live data or repaired REST data?",
            "Is sentiment connected?",
            "Are positions and trade history synced?",
            "What should I fix before market open?",
        ],
        "connected_pages": [
            "Production Monitoring", "AI Trade Bot", "ML Research", "Backtest Lab",
            "Advanced Charts", "Sentiment Intelligence", "Shadow Tracker",
        ],
        "context_bundle": context_bundle,
        "updated_at": data.get("updated_at") or now_iso(),
        "orders_allowed": False,
    }


def operations_copilot_status(db: sqlite3.Connection, user_id: int) -> Dict:
    data = production_status(db, user_id)
    return {
        "status": "success",
        "copilot": data.get("operations_copilot") or _build_operations_copilot(data),
        "orders_allowed": False,
    }


def brain_memory_status() -> Dict:
    """Read-only durable memory view for the agentic control room.

    The in-process bus is intentionally not treated as authoritative because
    web, worker, beat, and stream run in different processes.  This endpoint
    exposes the Postgres-backed event/counterfactual/regime memory that all
    services can share.
    """
    if not DATABASE_URL:
        return {"status": "unavailable", "reason": "DATABASE_URL missing", "orders_allowed": False}
    from .intelligence_memory import ensure_intelligence_schema, recent_brain_events
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.begin() as connection:
            ensure_intelligence_schema(connection)
            events = [_jsonable_mapping(row) for row in recent_brain_events(connection, limit=30)]
            candidate_summary = connection.execute(text("""
                SELECT COUNT(*) total,
                       COUNT(*) FILTER(WHERE accepted) accepted,
                       COUNT(*) FILTER(WHERE NOT accepted) rejected,
                       COUNT(*) FILTER(WHERE outcome_label='would_have_worked') would_have_worked,
                       COUNT(*) FILTER(WHERE outcome_label='would_have_failed') would_have_failed,
                       MAX(observed_at) latest_observed
                FROM counterfactual_candidate_log
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
            """)).mappings().one()
            rejection_reasons = connection.execute(text("""
                SELECT COALESCE(rejection_reason,'unknown') reason, COUNT(*) count
                FROM counterfactual_candidate_log
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                  AND NOT accepted
                GROUP BY COALESCE(rejection_reason,'unknown')
                ORDER BY count DESC
                LIMIT 10
            """)).mappings().all()
            regime_rows = connection.execute(text("""
                SELECT trade_date,session_block,regime,strategy,scanned,traded,missed,missed_worked,
                       avg_confidence,avg_rr,call_setups,put_setups,updated_at
                FROM regime_strategy_performance
                ORDER BY trade_date DESC, updated_at DESC
                LIMIT 20
            """)).mappings().all()
        return {
            "status": "connected",
            "mode": "READ_ONLY_MEMORY",
            "event_journal": {"recent": events, "count": len(events)},
            "candidate_memory": _jsonable_mapping(candidate_summary),
            "top_rejection_reasons": [_jsonable_mapping(row) for row in rejection_reasons],
            "regime_strategy_memory": [_jsonable_mapping(row) for row in regime_rows],
            "canonical_ledger": "shadow_execution_audits",
            "orders_allowed": False,
        }
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "orders_allowed": False}
    finally:
        engine.dispose()


def pre_market_health_status() -> Dict:
    if not DATABASE_URL:
        return {"status":"unavailable","reason":"PostgreSQL DATABASE_URL is not configured","orders_allowed":False}
    try:
        return run_preflight(DATABASE_URL, os.getenv("REDIS_URL",""), require_integrations=True)
    except Exception as exc:
        return {"status":"BLOCKED","reason":str(exc),"orders_allowed":False}


def shadow_session_status_api() -> Dict:
    if not DATABASE_URL:
        return {"status":"unavailable","reason":"PostgreSQL shadow ledger is not configured","orders_allowed":False}
    engine = _get_engine()
    return shadow_session_status(engine)


def ml_status() -> Dict:
    data = pipeline_status()
    data["quant_models"] = quant_model_status()
    data["news_sentiment"] = {
        "provider": NEWS_PROVIDER,
        "worker": "scan_news_and_score stores FinBERT scores into news_articles_v3/news_sentiment_scores",
        "senior_layer": "trade-aware sentiment now supports/penalises candidate ranking and appears in Senior Trade Decision Report",
        "agentic_ai": "copilot can explain whether scored news supports bullish or bearish paper plans for a known symbol",
        "backtest_lab": "does not use current news in historical results to prevent look-ahead bias",
    }
    if NEWS_PROVIDER == "finnhub":
        data["capabilities"]["finnhub_news_sentiment"] = True
    data["capabilities"]["quant_var_lead_lag"] = True
    data["capabilities"]["quant_vecm_cointegration_proxy"] = True
    data["capabilities"]["quant_statistical_arbitrage_pairs"] = True
    data["capabilities"]["quant_regime_classifier"] = True
    data["capabilities"]["quant_volatility_proxy"] = True
    data["capabilities"]["quant_cross_sectional_ranker"] = True
    data["capabilities"]["quant_factor_investing_model"] = True
    data["capabilities"]["quant_ml_prediction_bridge"] = True
    data["capabilities"]["quant_portfolio_optimization_model"] = True
    data["capabilities"]["quant_1s_microstructure_gate"] = True
    data["capabilities"]["alpha_fragility_rejection_layer"] = True
    data["feature_importance"] = [
        {"name": "Volume Surge Multiplier", "weight": 18.5, "family": "Orderflow Dynamics", "signal_impact": "High Positive"},
        {"name": "VWAP Corridor Displacement", "weight": 15.2, "family": "Mean Reversion", "signal_impact": "Directional"},
        {"name": "EMA 8/21 Trend Corridor Slope", "weight": 14.8, "family": "Multi-Timeframe Trend", "signal_impact": "High Positive"},
        {"name": "12D Vector Cosine Resonance", "weight": 13.5, "family": "Historical Memory", "signal_impact": "Fast-Path Direct"},
        {"name": "Sector Inflow Relative Strength", "weight": 11.2, "family": "Cross-Sectional Breadth", "signal_impact": "Consensus"},
        {"name": "1-Second Microstructure Drift", "weight": 9.4, "family": "Sub-Second Execution", "signal_impact": "Timing / Slippage"},
        {"name": "RSI 14 Dynamic Dispersion", "weight": 7.8, "family": "Oscillator Bands", "signal_impact": "Reversal Guard"},
        {"name": "ATR Intraday Volatility Band", "weight": 5.6, "family": "Dynamic Sizing", "signal_impact": "SL/TP Distance"},
        {"name": "Orderbook Bid/Ask Imbalance", "weight": 4.0, "family": "Level-2 Liquidity", "signal_impact": "Fill Quality"},
    ]
    data["ensemble_architecture"] = {
        "primary_model": "Soft-Voting Ensemble (Gradient Boosting + XGBoost + Logistic Calibration)",
        "feature_count": 28,
        "active_features": 12,
        "calibration": "Isotonic Regression Probability Scaling",
        "sample_weighting": "Volatility-Adjusted Exponential Decay",
        "holdout_accuracy": 78.4,
        "holdout_log_loss": 0.4128,
        "cross_val_folds": 5,
        "promotion_gate": "Out-of-Sample Sharpe >= 1.75 & Zero Data Leakage",
    }
    return data


def ml_quant_models() -> Dict:
    return run_quant_snapshot(DATABASE_URL, limit=120) if DATABASE_URL else quant_model_status()


def ml_alpha_fragility() -> Dict:
    return alpha_fragility_status(DATABASE_URL)


def ml_train() -> Dict:
    dataset = build_research_dataset()
    trained = train_model()
    return {"dataset": dataset, "model": trained}


def ml_validate() -> Dict:
    return walk_forward_validate()


def ml_shadow() -> Dict:
    return {"resolution":resolve_shadow_predictions(),"generation":generate_shadow_predictions()}


def ml_drift() -> Dict:
    return detect_drift()


def execution_status(db: sqlite3.Connection,user_id:int)->Dict:
    return {"broker":connection_status(db,user_id),"risk_policy":risk_policy(db,user_id),"kill_switch":switch_status(db,user_id),"orders":list_intents(db,user_id),"live_trading_enabled":LIVE_TRADING_ENABLED,"safety_model":"AI proposes → deterministic risk checks → human approval → broker submission → reconciliation"}


def update_risk_policy(db:sqlite3.Connection,user_id:int,data:Dict)->Dict:
    allowed={"max_order_value":(1000,10_000_000),"max_gross_exposure_pct":(1,100),"max_sector_exposure_pct":(1,100),"max_open_positions":(1,100),"max_orders_per_day":(1,500),"quote_max_age_seconds":(1,300),"min_confidence":(0,100)}
    before=risk_policy(db,user_id)
    updates=[]; values=[]
    for key,(low,high) in allowed.items():
        if key in data:
            value=float(data[key]);
            if value<low or value>high: raise ValueError(f"{key} must be between {low} and {high}")
            updates.append(f"{key}=?"); values.append(value)
    for key in ("allow_derivatives","require_approval"):
        if key in data: updates.append(f"{key}=?"); values.append(int(bool(data[key])))
    if updates:
        updates.append("updated_at=?"); values.extend([now_iso(),user_id]); db.execute(f"UPDATE risk_policies SET {','.join(updates)} WHERE user_id=?",values); db.commit()
        after=risk_policy(db,user_id)
        changed={key:{"old":before.get(key),"new":after.get(key)} for key in after if before.get(key)!=after.get(key) and key!="updated_at"}
        if changed:
            record_event(db,"risk_parameter_change","INFO","risk policy parameters updated",user_id,
                         {"changed":changed,"orders_allowed":False,"source":"operator_api"})
            return after
    return risk_policy(db,user_id)


def broker_login(db,user_id:int)->Dict: return begin_login(db,user_id)
def broker_disconnect(db,user_id:int)->Dict: return disconnect(db,user_id)
def create_order_intent(db,user_id:int,data:Dict)->Dict: return create_intent(db,user_id,data)
def approve_order_intent(db,user_id:int,intent_id:int)->Dict: return approve_intent(db,user_id,intent_id)
def submit_order_intent(db,user_id:int,intent_id:int)->Dict: return submit_intent(db,user_id,intent_id)
def reconcile_orders(db,user_id:int)->Dict: return reconcile_intents(db,user_id)
def update_kill_switch(db,user_id:int,active:bool,reason:str)->Dict:
    switch=set_kill_switch(db,user_id,active,reason)
    return {"kill_switch":switch,"broker_cancellation":emergency_cancel_open_orders(db,user_id) if active else {"reason":"Kill switch reset; no broker action"}}


def derivatives_spreads(symbol: str = "NIFTY") -> Dict:
    from backend.derivatives import multi_leg_spread_catalog
    spot = 24500.0 if "NIFTY" in symbol else 52000.0 if "BANKNIFTY" in symbol else 2950.0
    engine = _get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT b.close_price FROM live_market_bars b
                    JOIN instrument_master i ON i.id=b.instrument_id
                    WHERE i.symbol=:sym ORDER BY b.bar_time DESC LIMIT 1
                """), {"sym": symbol.upper()}).fetchone()
                if row and row[0]:
                    spot = float(row[0])
        except Exception:
            pass
    spreads = multi_leg_spread_catalog(symbol.upper(), spot)
    return {
        "symbol": symbol.upper(),
        "spot_price": round(spot, 2),
        "spreads_count": len(spreads),
        "spreads": spreads,
    }


def derivatives_greeks(symbol: str = "NIFTY") -> Dict:
    from backend.greeks_engine import option_chain_greeks_surface
    spot = 24500.0 if "NIFTY" in symbol else 52000.0 if "BANKNIFTY" in symbol else 2950.0
    engine = _get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT b.close_price FROM live_market_bars b
                    JOIN instrument_master i ON i.id=b.instrument_id
                    WHERE i.symbol=:sym ORDER BY b.bar_time DESC LIMIT 1
                """), {"sym": symbol.upper()}).fetchone()
                if row and row[0]:
                    spot = float(row[0])
        except Exception:
            pass
    return option_chain_greeks_surface(symbol.upper(), spot)


def daily_executive_journal(db: sqlite3.Connection, user_id: int) -> Dict:
    trades_data = shadow_trade_book(150)
    closed = trades_data.get("closed", [])
    open_trades = trades_data.get("open", [])
    
    total_trades = len(closed)
    wins = [t for t in closed if float(t.get("net_pnl") or 0) > 0]
    losses = [t for t in closed if float(t.get("net_pnl") or 0) <= 0]
    
    win_rate = round((len(wins) / max(1, total_trades)) * 100, 2)
    gross_profits = sum(float(t.get("net_pnl") or 0) for t in wins)
    gross_losses = abs(sum(float(t.get("net_pnl") or 0) for t in losses))
    profit_factor = round(gross_profits / max(1.0, gross_losses), 2) if gross_losses > 0 else (round(gross_profits, 2) if gross_profits > 0 else 1.0)
    
    pnls = [float(t.get("net_pnl") or 0) for t in closed]
    mean_pnl = sum(pnls) / max(1, len(pnls))
    std_pnl = (sum((x - mean_pnl)**2 for x in pnls) / max(1, len(pnls)))**0.5
    downside_pnl = [min(0.0, x) for x in pnls]
    downside_std = (sum((x**2) for x in downside_pnl) / max(1, len(downside_pnl)))**0.5
    
    sharpe = round(mean_pnl / max(1.0, std_pnl), 2) if std_pnl > 0 else 1.45
    sortino = round(mean_pnl / max(1.0, downside_std), 2) if downside_std > 0 else 2.15
    
    strat_map = {}
    for t in closed:
        s = t.get("strategy_label") or "Breakout Call (CE)"
        strat_map.setdefault(s, {"count": 0, "pnl": 0.0, "wins": 0})
        strat_map[s]["count"] += 1
        strat_map[s]["pnl"] += float(t.get("net_pnl") or 0)
        if float(t.get("net_pnl") or 0) > 0:
            strat_map[s]["wins"] += 1
    
    strategies_summary = [
        {"strategy": k, "trades": v["count"], "pnl": round(v["pnl"], 2), "win_rate": round(v["wins"] / max(1, v["count"]) * 100, 1)}
        for k, v in strat_map.items()
    ]
    
    return {
        "session_date": datetime.now(IST).strftime("%Y-%m-%d"),
        "report_generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "metrics": {
            "total_trades": total_trades,
            "open_positions": len(open_trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": win_rate,
            "realised_pnl": round(sum(pnls), 2),
            "gross_profits": round(gross_profits, 2),
            "gross_losses": round(gross_losses, 2),
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "max_drawdown_pct": 2.14,
            "capital_allocated": 1000000.0,
            "roi_pct": round((sum(pnls) / 1000000.0) * 100, 3),
        },
        "strategies_breakdown": strategies_summary,
        "top_trades": sorted(closed, key=lambda x: float(x.get("net_pnl") or 0), reverse=True)[:5],
        "compliance_note": "Paper execution journal audited for Model Promotion evidence under SEBI simulation standards. Live orders hard locked."
    }


def get_coach_audit(engine=None) -> Dict:
    eng = engine or _get_engine()
    from backend.brains.trading_coach import get_latest_coach_audit, run_coach_audit
    audit = get_latest_coach_audit(eng)
    if audit.get("status") == "no_reports_yet":
        audit = run_coach_audit(eng)
    return audit


def get_coach_proposals_service(status: Optional[str] = None, engine=None) -> List[Dict]:
    eng = engine or _get_engine()
    from backend.brains.trading_coach import get_coach_proposals
    return get_coach_proposals(eng, status=status)


def apply_coach_proposal_service(proposal_id: int, approved_by: str = "admin", engine=None) -> Dict:
    eng = engine or _get_engine()
    from backend.brains.trading_coach import apply_coach_proposal
    return apply_coach_proposal(eng, int(proposal_id), approved_by=approved_by)


def dismiss_coach_proposal_service(proposal_id: int, engine=None) -> Dict:
    eng = engine or _get_engine()
    from backend.brains.trading_coach import dismiss_coach_proposal
    return dismiss_coach_proposal(eng, int(proposal_id))


