"""Persistent, guarded trading copilot. It can research and prepare paper plans, never place live orders."""
import json
import re
from typing import Dict, Optional

from sqlalchemy import create_engine

from .config import DATABASE_URL
from .database import now_iso
from .derivatives import build_derivative_plan
from .market import market_snapshot
from .news_gateway import articles_for_symbol
from .security_master import resolve_security
from .ai_gateway import AIGateway
from .rag_engine import retrieve_context
from .sentiment import analyse_sentiment
from .sentiment_decision import evaluate_trade_sentiment
from .technical_analysis import GLOSSARY


def _detect_symbol(message: str) -> Optional[str]:
    tokens = re.findall(r"\b[A-Z][A-Z0-9&-]{1,14}\b", message.upper())
    ignored = {"BUY","SELL","NSE","BSE","SL","TP","RSI","FVG","BOS","AI","THE","FOR","WITH"}
    for token in tokens:
        if token not in ignored and resolve_security(token): return token
    return None


def _detected_terms(message: str) -> Dict[str, str]:
    lower=f" {message.lower()} "
    found={}
    aliases={
        "SL":"stop loss", "TP":"target profit", "OB":"order block", "FVG":"fair value gap",
        "BOS":"break of structure", "CHOCH":"change of character", "MSS":"market structure shift",
        "EQH":"equal highs", "EQL":"equal lows", "BSL":"buy-side liquidity", "SSL":"sell-side liquidity",
        "SR":"support resistance", "POI":"point of interest", "LTF":"lower timeframe", "HTF":"higher timeframe",
        "R:R":"risk reward",
    }
    for term,desc in GLOSSARY.items():
        if re.search(rf"\b{re.escape(term.lower())}\b", lower):
            found[term]=desc
    for term,meaning in aliases.items():
        if term.lower() in lower and term not in found:
            found[term]=meaning
    return found


def _source_line(rag: Dict) -> str:
    docs=rag.get("documents",[])[:4]
    if not docs:
        return "Verified sources: none available in the app context."
    return "Verified sources: " + "; ".join(f"{doc['title']} ({doc['source']})" for doc in docs) + "."


def _decode_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value or {}


_pg_engine = None


def _postgres_engine():
    global _pg_engine
    if not DATABASE_URL:
        return None
    if _pg_engine is None:
        _pg_engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return _pg_engine


def _metric_view(metrics: Dict) -> Dict:
    return metrics.get("holdout") or metrics.get("summary") or metrics


def _sentiment_context(db, symbol: str, security: Optional[Dict], stock: Optional[Dict]) -> Optional[str]:
    if not symbol:
        return None
    try:
        market = market_snapshot()
        base_stock = stock or {"symbol": symbol, "name": security.get("name", symbol) if security else symbol,
                               "price": 0, "change_pct": 0, "volume_ratio": 1}
        news = articles_for_symbol(db, symbol, security.get("name", "") if security else "", allow_fetch=True)
        sentiment = analyse_sentiment(base_stock, market, news["articles"], news["mode"])
        lines=[f"- News/sentiment: {sentiment['label']} ({sentiment['score']:+.0f}/100, "
               f"{sentiment['confidence']}% confidence) from {news['provider']} / {news['mode']}; "
               f"{sentiment['coverage']['articles']} articles. {sentiment['summary']}"]
        engine=_postgres_engine()
        if engine is not None:
            bullish=evaluate_trade_sentiment(engine,{"symbol":symbol,"signal":1},gate_enabled=False,require_verified=False)
            bearish=evaluate_trade_sentiment(engine,{"symbol":symbol,"signal":-1},gate_enabled=False,require_verified=False)
            if bullish.get("available") or bearish.get("available"):
                lines.append(
                    f"- Trade-aware sentiment: bullish route is {bullish.get('alignment','neutral')} "
                    f"({bullish.get('label','neutral')}, {bullish.get('articles',0)} articles); "
                    f"bearish route is {bearish.get('alignment','neutral')} "
                    f"({bearish.get('label','neutral')}, {bearish.get('articles',0)} articles)."
                )
        return "\n".join(lines)
    except Exception:
        return None


def _model_review_answer(db, rag: Dict) -> Optional[str]:
    try:
        active = db.execute("SELECT version,status,metrics,created_at FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        experiment = db.execute("SELECT version,status,metrics,created_at FROM model_versions ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        return None
    if not active and not experiment:
        return None
    active_d = dict(active) if active else {}
    exp_d = dict(experiment) if experiment else {}
    active_m = _metric_view(_decode_json(active_d.get("metrics")))
    exp_m = _metric_view(_decode_json(exp_d.get("metrics")))
    lines=["Model review from stored model registry:"]
    if active_d:
        lines.append(f"- Active paper model: {active_d.get('version')} ({active_d.get('status')})")
    if exp_d:
        lines.append(f"- Latest experiment: {exp_d.get('version')} ({exp_d.get('status')})")
    if active_d and exp_d and active_d.get("version") != exp_d.get("version"):
        lines.append("- Decision: latest experiment was not promoted; the active paper baseline remains in use.")
    for key,label in (("log_loss","log loss"),("profit_factor","profit factor"),("max_drawdown_pct","max drawdown %"),("win_rate","win rate")):
        if key in exp_m or key in active_m:
            lines.append(f"- {label}: experiment {exp_m.get(key,'n/a')} vs active {active_m.get(key,'n/a')}")
    if "log_loss" in exp_m and "log_loss" in active_m:
        try:
            if float(exp_m["log_loss"]) >= float(active_m["log_loss"]):
                lines.append(f"- Rejection reason: experiment log loss did not improve ({float(exp_m['log_loss']):.4f} vs {float(active_m['log_loss']):.4f}).")
        except Exception:
            pass
    lines.append("\nUse the active model for paper/shadow mode until a candidate beats the baseline under cost-aware validation and forward shadow evidence.")
    lines.append(_source_line(rag))
    return "\n".join(lines)


def _unknown_answer(message: str, rag: Dict) -> str:
    return ("I don’t have verified data inside Nivesh AI to answer that safely.\n\n"
            "What I can answer now: stock master details, synced/live candle status, glossary terms, strategy rules, "
            "shadow-session status, model promotion status, paper-trade checks, and risk/execution locks.\n\n"
            "To answer your question accurately, connect or provide the missing source data first, then ask again.\n"
            + _source_line(rag))


def _is_unknown_answer(answer: str) -> bool:
    return answer.strip().startswith("I don’t have verified data inside Nivesh AI")


def _known_local_answer(db, message: str, symbol: str, security: Optional[Dict], stock: Optional[Dict], metadata: Dict, rag: Dict) -> str:
    lower=message.lower()
    terms=_detected_terms(message)
    docs=rag.get("documents",[])
    source_line=_source_line(rag)

    if any(x in lower for x in ("live order","place order","execute live","real money","buy for me","sell for me")):
        return ("Live execution is locked. I can prepare a paper plan and audit risk, but I cannot place a real broker order.\n\n"
                "Required before live trading: broker session, kill-switch clear, risk policy pass, 90 forward shadow sessions, "
                "real-cost reconciliation, derivative executability checks, and explicit human approval.\n\n" + source_line)

    if terms:
        lines=["Known trading terms from the app glossary:"]
        for term,desc in terms.items():
            lines.append(f"- {term}: {desc}")
        lines.append("\nI can apply these terms to a specific synced stock if you ask with the symbol, e.g. “explain FVG and BOS for RELIANCE.”")
        lines.append(source_line)
        return "\n".join(lines)

    if any(x in lower for x in ("model","accuracy","win rate","log loss","rejected","performance")):
        model_answer=_model_review_answer(db, rag)
        if model_answer:
            return model_answer
        relevant=[doc for doc in docs if doc["source"] in {"shadow_predictions","shadow_execution_audits","runtime_config","live_market_bars"} or "model" in doc["title"].lower()]
        if relevant:
            return ("Model status answer, based only on stored app evidence:\n\n"
                    + "\n".join(f"- {doc['title']}: {doc['text']}" for doc in relevant[:4])
                    + "\n\nInterpretation: a model should not be promoted just because win rate looks higher. Use log loss, profit factor, drawdown, "
                      "cost-aware P&L, and live-shadow evidence together.\n\n" + source_line)
        return _unknown_answer(message, rag)

    if any(x in lower for x in ("shadow","paper trade","paper trading","session","live feed","missing bar","repair","gap")):
        relevant=[doc for doc in docs if doc["source"] in {"live_market_bars","shadow_predictions","shadow_execution_audits","runtime_config"}]
        if relevant:
            return ("Shadow-training status from verified app data:\n\n"
                    + "\n".join(f"- {doc['title']}: {doc['text']}" for doc in relevant[:5])
                    + "\n\nA valid shadow session needs completed live/provider bars, enough instruments, ML predictions, and paper-trade audit rows. "
                      "If bars are missing, use Production Monitoring → Repair today.\n\n" + source_line)
        return _unknown_answer(message, rag)

    if any(x in lower for x in ("why no trade","no trade","rejected","candidate","senior selector","senior layer")):
        relevant=[doc for doc in docs if doc["source"] in {"trade_candidate_audits","shadow_predictions","shadow_execution_audits","live_market_bars"}]
        if relevant:
            return ("Senior-selector explanation from verified app data:\n\n"
                    + "\n".join(f"- {doc['title']}: {doc['text']}" for doc in relevant[:5])
                    + "\n\nUse Production Monitoring → Candidate rejection visibility to see stock, strategy, ML probability, R:R, quality, sentiment/chart context, and exact rejection reason.\n\n"
                    + source_line)
        return _unknown_answer(message, rag)

    if any(x in lower for x in ("pre-market","premarket","ready for market","market ready","health check")):
        relevant=[doc for doc in docs if doc["source"] in {"runtime_config","live_market_bars","news_sentiment_scores","trade_candidate_audits"}]
        return ("Pre-market readiness is a deterministic check, not an AI guess.\n\n"
                "In Production Monitoring, use “Ready for market? → Run check”. It verifies DB, Redis, worker/beat, stream/provider token, active model, sentiment/OI freshness, and safety locks.\n\n"
                + ("\n".join(f"- {doc['title']}: {doc['text']}" for doc in relevant[:4]) + "\n\n" if relevant else "")
                + source_line)

    if any(x in lower for x in ("1 second","1-second","1s","microstructure")):
        relevant=[doc for doc in docs if doc["source"] in {"live_market_bars"} or "1-second" in doc["title"].lower()]
        if relevant:
            return ("1-second data status from verified app data:\n\n"
                    + "\n".join(f"- {doc['title']}: {doc['text']}" for doc in relevant[:4])
                    + "\n\nRule: use 1-second bars as an entry/exit timing confirmation first. Do not train/promote a 1-second model until continuous coverage, gap tracking, chart display, and storage growth are proven stable.\n\n"
                    + source_line)
        return _unknown_answer(message, rag)

    if any(x in lower for x in ("sentiment","news","finnhub","finbert")):
        relevant=[doc for doc in docs if doc["source"] in {"news_sentiment_scores","security_master","market_snapshot"}]
        if relevant:
            return ("Sentiment Intelligence from verified app data:\n\n"
                    + "\n".join(f"- {doc['title']}: {doc['text']}" for doc in relevant[:5])
                    + "\n\nCurrent design: sentiment supports/penalises the Senior layer. Keep it advisory until you have clean sessions; enable blocking only after coverage is reliable.\n\n"
                    + source_line)
        return _unknown_answer(message, rag)

    if any(x in lower for x in ("option","future","hedge","derivative","spread","straddle","condor","collar")) and metadata.get("plan"):
        plan=metadata["plan"]
        return (f"Known paper derivatives plan for {plan['symbol']}:\n\n"
                f"- Strategy: {plan['strategy']}\n- Market view: {plan['market_view']}\n- Expiry: {plan['expiry']}\n"
                f"- Legs: {len(plan['legs'])}\n- Status: {plan['status'].replace('_',' ')}\n\n"
                "This is paper-only. Premium, Greeks, margin and fill quality require a live option-chain/broker data source.\n\n" + source_line)

    if security or stock:
        lines=[f"Known symbol context for {symbol}:"]
        if security:
            lines.append(f"- Master: {security.get('exchange','NSE')}:{security.get('symbol',symbol)} · series/group {security.get('series') or security.get('group') or 'unknown'} · ISIN {security.get('isin','unknown')}")
        if stock:
            lines.append(f"- App quote snapshot: ₹{float(stock['price']):,.2f}, change {float(stock.get('change_pct',0)):.2f}%, volume {int(stock.get('volume',0)):,.0f}")
        sentiment_line = _sentiment_context(db, symbol, security, stock)
        if sentiment_line:
            lines.append(sentiment_line)
        lines.append("- I will not call this a live tradable signal unless live/provider candles and paper-trade validation are present.")
        lines.append("\nAsk a specific task next: “analyse structure”, “prepare hedge”, “explain risk”, or “check shadow status”.")
        lines.append(source_line)
        return "\n".join(lines)

    return _unknown_answer(message, rag)


def list_conversations(db, user_id: int) -> Dict:
    rows = db.execute("SELECT id,title,created_at,updated_at FROM bot_conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 30", (user_id,)).fetchall()
    return {"items":[dict(x) for x in rows]}


def conversation(db, user_id: int, conversation_id: int) -> Dict:
    thread = db.execute("SELECT * FROM bot_conversations WHERE id=? AND user_id=?", (conversation_id,user_id)).fetchone()
    if not thread: raise ValueError("Conversation not found")
    messages = db.execute("SELECT id,role,content,metadata,created_at FROM bot_messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
    return {"conversation":dict(thread),"messages":[{**dict(x),"metadata":json.loads(x["metadata"])} for x in messages]}


def chat(db, user_id: int, message: str, conversation_id: Optional[int] = None) -> Dict:
    message = message.strip()
    if not message: raise ValueError("Message is required")
    if conversation_id:
        thread = db.execute("SELECT id FROM bot_conversations WHERE id=? AND user_id=?", (conversation_id,user_id)).fetchone()
        if not thread: raise ValueError("Conversation not found")
    else:
        title = (message[:54] + "…") if len(message)>55 else message
        cursor = db.execute("INSERT INTO bot_conversations(user_id,title,created_at,updated_at) VALUES(?,?,?,?)", (user_id,title,now_iso(),now_iso()))
        conversation_id = cursor.lastrowid
    db.execute("INSERT INTO bot_messages(conversation_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)", (conversation_id,"user",message,"{}",now_iso()))
    detected_symbol = _detect_symbol(message)
    symbol = detected_symbol or ""
    stock = next((x for x in market_snapshot() if x["symbol"] == symbol), None) if symbol else None
    security = resolve_security(symbol) if symbol else None
    lower = message.lower()
    if any(word in lower for word in ("live order","place order","execute live","real money","buy for me","sell for me")):
        answer = "I can prepare and audit a paper order, but live execution is locked. Zerodha credentials alone are not enough: broker session validation, limits, kill switch, basket reconciliation and explicit approval must pass first."
        metadata = {"intent":"blocked_live_order","symbol":symbol,"live_order_allowed":False}
    elif any(word in lower for word in ("option","future","hedge","derivative","spread","straddle","condor","collar")):
        if not symbol:
            metadata = {"intent":"unknown_or_missing_data","symbol":"","known_only":True,"known_answer":False}
            rag = retrieve_context(db, user_id, message, None)
            answer = _unknown_answer(message, rag) + "\n\nFor derivatives, ask with a symbol, e.g. “prepare a bullish option plan for RELIANCE.”"
            model = {"text":answer,"provider":"local","model":"known-only-copilot","fallback":True,"reviews":[{"provider":"local","model":"known-only-copilot","status":"missing_symbol"}]}
            metadata["model"] = {key:model[key] for key in ("provider","model","fallback")}
            metadata["model_reviews"] = model.get("reviews", [])
            metadata["rag"] = {"enabled":rag.get("enabled",False),"sources":[{key:doc.get(key) for key in ("id","title","source","score")} for doc in rag.get("documents",[])],"safety":rag.get("safety","")}
            db.execute("INSERT INTO bot_messages(conversation_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)", (conversation_id,"assistant",answer,json.dumps(metadata),now_iso()))
            db.execute("UPDATE bot_conversations SET updated_at=? WHERE id=?", (now_iso(),conversation_id))
            db.commit()
            return {"conversation_id":conversation_id,"message":{"role":"assistant","content":answer,"metadata":metadata,"created_at":now_iso()}}
        view = "bearish" if any(x in lower for x in ("bear","down","fall")) else "bullish" if any(x in lower for x in ("bull","up","rise")) else "neutral"
        spot = stock["price"] if stock else 1000.0
        requested = "Index Futures Hedge" if "hedge" in lower and "future" in lower else "Short Future" if "short future" in lower else "Long Future" if "future" in lower else "auto"
        plan = build_derivative_plan(symbol, spot, view, "contracting" if view=="neutral" else "normal", False, requested)
        answer = f"Paper plan prepared for {symbol}: {plan['strategy']} at spot ₹{spot:,.2f}, expiry {plan['expiry']}. It has {len(plan['legs'])} legs and requires an atomic basket. Premium, Greeks and margin are indicative until a live option chain is connected."
        metadata = {"intent":"derivative_plan","symbol":symbol,"plan":plan}
    else:
        context = f"{security['exchange']}:{symbol}" if security else symbol
        answer = f"I’ve stored this research note under {context}. I can analyse its structure, compare a strategy, prepare a paper derivatives ticket, or explain SL/TP, OB, FVG, BOS and CHOCH. I will not claim a guaranteed return or send a live order."
        metadata = {"intent":"research","symbol":symbol,"memory_saved":True}
    rag = retrieve_context(db, user_id, message, symbol or None)
    if rag.get("enabled") and rag.get("documents"):
        answer += "\n\nRetrieved context checked: " + "; ".join(f"{doc['title']} ({doc['source']})" for doc in rag["documents"][:4]) + "."
    answer=_known_local_answer(db, message, symbol, security, stock, metadata, rag)
    known_answer = not _is_unknown_answer(answer)
    metadata["known_only"] = True
    metadata["known_answer"] = known_answer
    if known_answer:
        model = AIGateway(db, user_id).multi_rag_assist(message, answer, {"symbol":symbol,"intent":metadata["intent"],"plan":metadata.get("plan"),"known_only":True}, rag)
        answer = model["text"]
    else:
        model = {
            "text": answer,
            "provider": "local",
            "model": "known-only-copilot",
            "fallback": True,
            "reviews": [{"provider":"local","model":"known-only-copilot","status":"unknown_blocked_external_models"}],
        }
    metadata["model"] = {key:model[key] for key in ("provider","model","fallback")}
    metadata["model_reviews"] = model.get("reviews", [])
    metadata["rag"] = {"enabled":rag.get("enabled",False),
                       "sources":[{key:doc.get(key) for key in ("id","title","source","score")} for doc in rag.get("documents",[])],
                       "safety":rag.get("safety","")}
    db.execute("INSERT INTO bot_messages(conversation_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)", (conversation_id,"assistant",answer,json.dumps(metadata),now_iso()))
    db.execute("UPDATE bot_conversations SET updated_at=? WHERE id=?", (now_iso(),conversation_id))
    if metadata.get("plan"):
        p=metadata["plan"]
        db.execute("INSERT INTO derivative_plans(user_id,symbol,strategy,market_view,spot,expiry,payload,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (user_id,p["symbol"],p["strategy"],p["market_view"],p["spot"],p["expiry"],json.dumps(p),p["status"],now_iso()))
    db.commit()
    return {"conversation_id":conversation_id,"message":{"role":"assistant","content":answer,"metadata":metadata,"created_at":now_iso()}}
