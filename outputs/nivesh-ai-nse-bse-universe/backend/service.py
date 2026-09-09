import sqlite3
import json
import math
from datetime import datetime, timezone
from typing import Dict, List

from .agent import TradingAgent
from .broker import PaperBroker
from .config import DEFAULT_CAPITAL
from .database import now_iso
from .market import INDICES, market_snapshot, price_map
from .model_provider import provider_catalog
from .ai_gateway import AIGateway, gateway_status
from .technical_analysis import GLOSSARY, analyse_structure
from .sentiment import analyse_sentiment
from .charting import chart_data
from .backtesting import run_backtest
from .security_master import search_securities, security_master_stats


def _row(row: sqlite3.Row) -> Dict:
    return dict(row)


def portfolio_summary(db: sqlite3.Connection, user_id: int) -> Dict:
    portfolio = db.execute("SELECT * FROM portfolios WHERE user_id=?", (user_id,)).fetchone()
    prices = price_map()
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
    prices = price_map()
    rows = db.execute("SELECT * FROM holdings WHERE user_id=? ORDER BY symbol", (user_id,)).fetchall()
    total = sum(item["quantity"] * prices.get(item["symbol"], item["average_price"]) for item in rows) or 1
    result = []
    for item in rows:
        ltp = prices.get(item["symbol"], item["average_price"])
        value = item["quantity"] * ltp
        pnl = item["quantity"] * (ltp - item["average_price"])
        result.append({"symbol": item["symbol"], "name": item["name"], "quantity": item["quantity"], "average_price": item["average_price"], "ltp": ltp, "current_value": round(value, 2), "pnl": round(pnl, 2), "pnl_pct": round((ltp / item["average_price"] - 1) * 100, 2), "weight": round(value / total * 100, 1)})
    return result


def trade_list(db: sqlite3.Connection, user_id: int, limit: int = 100) -> List[Dict]:
    rows = db.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    prices = price_map()
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
    return {"user": _row(user), "summary": portfolio_summary(db, user_id), "indices": INDICES, "holdings": holding_list(db, user_id), "trades": trade_list(db, user_id), "hot_stocks": latest_hot_stocks(db, user_id), "settings": _row(settings), "last_run": _row(last_run) if last_run else None, "universe": security_master_stats()}


def run_agent(db: sqlite3.Connection, user_id: int, execute_trade: bool = True) -> Dict:
    settings = db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
    prior_runs = db.execute("SELECT COUNT(*) count FROM agent_runs WHERE user_id=?", (user_id,)).fetchone()["count"]
    stocks = market_snapshot(prior_runs + 1)
    agent = TradingAgent()
    position_rows = db.execute("SELECT symbol,quantity,average_price FROM holdings WHERE user_id=?", (user_id,)).fetchall()
    positions = {row["symbol"]: dict(row) for row in position_rows}
    signals = agent.analyse(stocks, settings["risk_profile"], positions)
    opportunities = [signal for signal in signals if signal.action == "BUY"]
    db.execute("DELETE FROM watchlist_snapshots WHERE user_id=?", (user_id,))
    for signal in signals[:4]:
        db.execute("INSERT INTO watchlist_snapshots(user_id,symbol,name,price,change_pct,confidence,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?)", (user_id, signal.symbol, signal.name, signal.price, signal.change_pct, signal.confidence, signal.reasoning, now_iso()))
    trade = None
    if opportunities and execute_trade:
        # One order per manual run prevents accidental over-trading in the simulator.
        trade = PaperBroker().execute_buy(db, user_id, opportunities[0], settings["risk_profile"])
    universe = security_master_stats()["counts"]
    summary = f"Master covers {universe['total']:,} active NSE/BSE securities; evaluated {len(stocks)} with available paper quotes, found {len(opportunities)} qualified setups, and placed {1 if trade else 0} paper order."
    cursor = db.execute("INSERT INTO agent_runs(user_id,status,stocks_scanned,opportunities,trades_placed,summary,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, "COMPLETED", len(stocks), len(opportunities), 1 if trade else 0, summary, now_iso()))
    db.commit()
    return {"run_id": cursor.lastrowid, "status": "COMPLETED", "stocks_scanned": len(stocks), "universe": universe, "strategy_selection": "algorithmic", "opportunities": len(opportunities), "trade": trade, "summary": summary, "signals": agent.serialise(signals[:4]), "completed_at": now_iso()}


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


def stock_analysis(db: sqlite3.Connection, user_id: int, symbol: str, enhance_narrative: bool = True) -> Dict:
    symbol = symbol.upper().strip()
    stock = next((item for item in market_snapshot() if item["symbol"] == symbol), None)
    if not stock:
        raise ValueError("Unknown NSE symbol")
    analysis = analyse_structure(stock["symbol"], stock["name"], stock["price"])
    if enhance_narrative:
        gateway_result = AIGateway(db, user_id).explain(analysis)
        analysis["narrative"] = gateway_result["text"]
        analysis["model"] = {"engine": "market-structure-ensemble", "narrative_provider": gateway_result["provider"], "narrative_model": gateway_result["model"], "cached": gateway_result["cached"], "fallback": gateway_result["fallback"], "data_mode": "simulated"}
    else:
        analysis["model"] = {"engine": "market-structure-ensemble", "narrative_provider": "local", "narrative_model": "market-structure-ensemble", "cached": False, "fallback": False, "data_mode": "simulated"}
    db.execute("INSERT INTO stock_analyses(user_id,symbol,timeframe,bias,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, symbol, "1m+15m", analysis["bias"], analysis["confidence"], json.dumps(analysis), now_iso()))
    db.execute("DELETE FROM stock_analyses WHERE user_id=? AND id NOT IN (SELECT id FROM stock_analyses WHERE user_id=? ORDER BY id DESC LIMIT 500)", (user_id, user_id))
    db.commit()
    return analysis


def latest_analysis(db: sqlite3.Connection, user_id: int, symbol: str) -> Dict:
    row = db.execute("SELECT payload FROM stock_analyses WHERE user_id=? AND symbol=? ORDER BY id DESC LIMIT 1", (user_id, symbol.upper())).fetchone()
    return json.loads(row["payload"]) if row else stock_analysis(db, user_id, symbol)


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


def sentiment_analysis(db: sqlite3.Connection, user_id: int, symbol: str, run_number: int = 0) -> Dict:
    stocks = market_snapshot(run_number)
    stock = next((item for item in stocks if item["symbol"] == symbol.upper()), None)
    if not stock:
        raise ValueError("Unknown NSE symbol")
    result = analyse_sentiment(stock, stocks)
    db.execute("INSERT INTO sentiment_snapshots(user_id,symbol,score,label,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, result["symbol"], result["score"], result["label"], result["confidence"], json.dumps(result), now_iso()))
    db.execute("DELETE FROM sentiment_snapshots WHERE user_id=? AND id NOT IN (SELECT id FROM sentiment_snapshots WHERE user_id=? ORDER BY id DESC LIMIT 500)", (user_id, user_id))
    db.commit()
    return result


def sentiment_dashboard(db: sqlite3.Connection, user_id: int, symbol: str = "RELIANCE") -> Dict:
    stocks = market_snapshot()
    selected = sentiment_analysis(db, user_id, symbol)
    leaders = [analyse_sentiment(stock, stocks) for stock in stocks]
    leaders.sort(key=lambda item: item["score"], reverse=True)
    bullish = sum(1 for item in leaders if item["label"] == "bullish")
    bearish = sum(1 for item in leaders if item["label"] == "bearish")
    mood_score = round(sum(item["score"] for item in leaders) / len(leaders), 1)
    return {"selected": selected, "market_mood": {"score": mood_score, "label": "risk-on" if mood_score >= 15 else "risk-off" if mood_score <= -15 else "mixed", "bullish": bullish, "neutral": len(leaders)-bullish-bearish, "bearish": bearish}, "leaders": leaders[:5], "laggards": list(reversed(leaders[-5:])), "updated_at": now_iso(), "data_mode": "simulated"}


def market_update(db: sqlite3.Connection, user_id: int, run_number: int = 0) -> Dict:
    stocks = market_snapshot(run_number)
    pulse = math.sin(run_number * .37) * .00045
    indices = [{**item, "price": round(item["price"] * (1 + pulse * (1 + idx * .12)), 2), "change_pct": round(item["change_pct"] + pulse * 100, 2)} for idx, item in enumerate(INDICES)]
    advancing = sum(1 for item in stocks if item["change_pct"] > 0)
    sentiment = [analyse_sentiment(stock, stocks) for stock in stocks]
    return {"indices": indices, "quotes": stocks, "breadth": {"advancing": advancing, "declining": len(stocks)-advancing, "ratio": round(advancing/max(1,len(stocks)),2)}, "sentiment_score": round(sum(item["score"] for item in sentiment)/len(sentiment),1), "updated_at": now_iso(), "data_mode": "simulated"}


def refresh_sentiments(db: sqlite3.Connection, user_id: int, run_number: int = 0) -> int:
    stocks = market_snapshot(run_number)
    for stock in stocks:
        result = analyse_sentiment(stock, stocks)
        db.execute("INSERT INTO sentiment_snapshots(user_id,symbol,score,label,confidence,payload,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, result["symbol"], result["score"], result["label"], result["confidence"], json.dumps(result), now_iso()))
    db.commit()
    return len(stocks)


def stock_chart(symbol: str, timeframe: str) -> Dict:
    return chart_data(symbol, timeframe)


def backtest_report(symbol: str, strategy: str, capital: float, start: str = "", end: str = "") -> Dict:
    return run_backtest(symbol, strategy, capital, start, end)


def listed_securities(query: str, exchange: str, limit: int, offset: int) -> Dict:
    return search_securities(query, exchange, limit, offset)
