"""Auditable local RAG context for the guarded trading bot.

The bot can use retrieved evidence to improve answers, but this module never
places orders and never promotes a model. It only returns compact source cards
that external/local models may explain.
"""
import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional

from sqlalchemy import create_engine, text

from .config import AI_RAG_ENABLED, AI_RAG_TOP_K, DATABASE_URL, LIVE_ELIGIBLE, LIVE_TRADING_ENABLED
from .market import market_snapshot
from .security_master import resolve_security
from .technical_analysis import GLOSSARY


STRATEGY_DOCS = [
    ("strategy:momentum", "Momentum", "Follow sustained relative strength confirmed by volume, breadth, and trend regime. Avoid chasing extended moves when volatility expands."),
    ("strategy:mean_reversion", "Mean Reversion", "Trade statistically stretched prices back toward VWAP or moving averages only after reversal evidence and a defined invalidation level."),
    ("strategy:breakout", "Breakout Trading", "Enter after price breaches support/resistance with volume confirmation. False breaks are common; completed-bar confirmation reduces noise."),
    ("strategy:rsi_divergence", "RSI Divergence", "Divergence is context, not a standalone entry. Confirm with structure, liquidity, and risk/reward before any paper trade."),
    ("strategy:covered_call", "Covered Call", "Sell a call against owned shares for income. Upside is capped and downside remains; model assignment and expiry risk."),
    ("strategy:protective_collar", "Protective Collar", "Own stock, buy a put, sell an OTM call. Defines a range but caps gains; useful for capital protection."),
    ("strategy:long_put", "Long Put", "Defined-risk bearish option. Needs enough downside before expiry to overcome premium, theta decay, and slippage."),
    ("strategy:iron_condor", "Iron Condor", "Defined-risk neutral income strategy. Profit is capped at net credit and volatility expansion can hurt."),
    ("risk:live_lock", "Execution lock", "Live trading is disabled until LIVE_ELIGIBLE, broker session, risk policy, kill switch, shadow validation, cost reconciliation, and human approval gates pass."),
    ("risk:paper_only", "Paper-only mode", "Shadow-paper trades are research evidence only. They should calculate P&L, mistakes, SL/TP exits, and validation quality without risking capital."),
    ("risk:cash_shorting", "Indian cash shorting", "Cash-equity short positions cannot be carried overnight. Bearish signals need F&O routing or must be rejected when derivatives are unavailable."),
]


def _tokens(text_value: str) -> List[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9_:%.-]{2,}", text_value.lower()) if token not in {"the","and","for","with","that","this","from","into","only"}]


def _score(query_terms: Counter, document: Dict) -> float:
    doc_terms=Counter(_tokens(document["title"]+" "+document["text"]))
    if not doc_terms:
        return 0.0
    overlap=sum(min(count, doc_terms.get(term,0)) for term,count in query_terms.items())
    boost=sum(1.25 for term in query_terms if term in document["id"].lower())
    return overlap + boost + math.log1p(sum(doc_terms.values())) * 0.03


def _base_docs() -> List[Dict]:
    docs=[{"id":doc_id,"title":title,"text":body,"source":"strategy_library"} for doc_id,title,body in STRATEGY_DOCS]
    docs.extend({"id":f"glossary:{term.lower()}","title":term,"text":desc,"source":"trading_glossary"} for term,desc in GLOSSARY.items())
    return docs


def _symbol_docs(symbol: Optional[str]) -> List[Dict]:
    if not symbol:
        return []
    docs=[]
    security=resolve_security(symbol)
    if security:
        docs.append({"id":f"security:{symbol}","title":f"{symbol} security master",
                     "text":f"{security.get('exchange','NSE')} listed instrument. ISIN {security.get('isin','unknown')}. Series/group {security.get('series') or security.get('group') or 'unknown'}.",
                     "source":"security_master"})
    quote=next((item for item in market_snapshot() if item["symbol"]==symbol),None)
    if quote:
        docs.append({"id":f"quote:{symbol}","title":f"{symbol} latest app quote",
                     "text":f"Latest app quote ₹{quote['price']:.2f}, change {quote['change_pct']:.2f}%, volume {quote.get('volume',0):,.0f}.",
                     "source":"market_snapshot"})
    return docs


_SYSTEM_DOCS_CACHE: List[Dict] = []
_SYSTEM_DOCS_CACHED_AT: float = 0.0
_RAG_ENGINE = None


def _get_rag_engine():
    global _RAG_ENGINE
    if _RAG_ENGINE is None and DATABASE_URL:
        _RAG_ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True, future=True, pool_size=5, max_overflow=10)
    return _RAG_ENGINE


def _system_docs() -> List[Dict]:
    global _SYSTEM_DOCS_CACHE, _SYSTEM_DOCS_CACHED_AT
    import time
    now = time.time()
    if _SYSTEM_DOCS_CACHE and (now - _SYSTEM_DOCS_CACHED_AT < 45.0):
        return list(_SYSTEM_DOCS_CACHE)

    status="enabled" if LIVE_TRADING_ENABLED else "disabled"
    eligible="true" if LIVE_ELIGIBLE else "false"
    docs=[{"id":"system:execution_safety","title":"Execution safety state",
           "text":f"Live trading flag is {status}; LIVE_ELIGIBLE is {eligible}. Bot output cannot place or approve real orders.",
           "source":"runtime_config"}]
    if not DATABASE_URL:
        return docs
    try:
        engine = _get_rag_engine()
        with engine.connect() as connection:
            latest_bar = connection.execute(text("SELECT MAX(bar_time) FROM live_market_bars")).scalar_one_or_none()
            live_est = connection.execute(text("SELECT COALESCE((SELECT reltuples::bigint FROM pg_class WHERE relname='live_market_bars'), 0)")).scalar_one_or_none()
            latest_1s = connection.execute(text("SELECT MAX(bar_time) FROM live_market_bars WHERE interval='1second'")).scalar_one_or_none()
            predictions=connection.execute(text("SELECT COUNT(*) predictions,COUNT(DISTINCT instrument_id) instruments,MAX(created_at) latest FROM shadow_predictions")).mappings().one()
            audits=connection.execute(text("SELECT COUNT(*) trades,COUNT(DISTINCT instrument_id) instruments,COUNT(*) FILTER(WHERE net_pnl IS NULL AND audit_status='RECONCILED') open_trades,COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades FROM shadow_execution_audits")).mappings().one()
            today_trades=connection.execute(text("""SELECT COUNT(*) trades,
                    COUNT(*) FILTER(WHERE net_pnl IS NULL AND audit_status='RECONCILED') open_trades,
                    COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
                    COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) realised_pnl,
                    MAX(signal_at) latest_trade
                FROM shadow_execution_audits
                WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE""")).mappings().one()
            candidate_audits=connection.execute(text("""SELECT COUNT(*) evaluated,
                    COUNT(*) FILTER(WHERE accepted) accepted,
                    COUNT(*) FILTER(WHERE NOT accepted) rejected
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE""")).mappings().one()
            top_rejections=connection.execute(text("""SELECT COALESCE(rejection_reason,'unknown') reason,COUNT(*) count
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE AND NOT accepted
                GROUP BY COALESCE(rejection_reason,'unknown')
                ORDER BY count DESC LIMIT 5""")).mappings().all()
            if connection.execute(text("SELECT to_regclass('public.senior_market_opportunities')")).scalar_one_or_none():
                opportunities=connection.execute(text("""SELECT COUNT(*) scanned,
                        COUNT(*) FILTER(WHERE senior_decision='TRADED') traded,
                        COUNT(*) FILTER(WHERE senior_decision<>'TRADED') missed,
                        COUNT(*) FILTER(WHERE outcome_label='would_have_worked') missed_worked,
                        MAX(observed_at) latest_scan
                    FROM senior_market_opportunities
                    WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE""")).mappings().one_or_none()
                top_opportunities=connection.execute(text("""SELECT symbol,opportunity_side,regime,strategy,confidence,
                        senior_decision,rejection_reason,outcome_label,counterfactual_5m,counterfactual_15m,counterfactual_30m
                    FROM senior_market_opportunities
                    WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                    ORDER BY observed_at DESC,confidence DESC LIMIT 5""")).mappings().all()
            else:
                opportunities=None
                top_opportunities=[]
            sentiment=connection.execute(text("""SELECT COUNT(*) scored_articles,COUNT(DISTINCT symbol) symbols,MAX(scored_at) latest_score
                FROM (
                  SELECT sym.symbol,s.scored_at
                  FROM news_articles_v3 n
                  JOIN news_sentiment_scores s ON s.article_id=n.id,
                       LATERAL UNNEST(n.symbols) AS sym(symbol)
                ) scored""")).mappings().one()
        if latest_bar:
            docs.append({"id":"market:live_feed_coverage","title":"Live feed coverage",
                         "text":f"Live feed active: ~{int(live_est or 0):,} stored market bars; latest market tick at {latest_bar}.",
                         "source":"live_market_bars"})
        docs.append({"id":"ml:shadow_predictions","title":"Shadow prediction coverage",
                     "text":f"{predictions['predictions']} shadow predictions across {predictions['instruments']} instruments; latest {predictions['latest']}.",
                     "source":"shadow_predictions"})
        docs.append({"id":"paper:shadow_trade_ledger","title":"Shadow-paper trade ledger",
                     "text":f"{audits['trades']} paper trade audit rows across {audits['instruments']} instruments; {audits['open_trades']} open and {audits['closed_trades']} closed.",
                     "source":"shadow_execution_audits"})
        docs.append({"id":"paper:today_trade_context","title":"Today's paper trade context",
                     "text":f"Today has {today_trades['trades']} paper trades: {today_trades['open_trades']} open, {today_trades['closed_trades']} closed, realised P&L {today_trades['realised_pnl']}; latest trade {today_trades['latest_trade']}.",
                     "source":"shadow_execution_audits"})
        docs.append({"id":"selector:candidate_rejections","title":"Senior selector candidate audit",
                     "text":f"Today selector evaluated {candidate_audits['evaluated']} candidates, accepted {candidate_audits['accepted']} and rejected {candidate_audits['rejected']}. Top rejection reasons: "
                            + ("; ".join(f"{row['reason']}={row['count']}" for row in top_rejections) if top_rejections else "none recorded") + ".",
                     "source":"trade_candidate_audits"})
        if opportunities:
            docs.append({"id":"senior:missed_opportunities","title":"Senior missed-opportunity intelligence",
                         "text":f"Today Senior Market Intelligence scanned {opportunities['scanned']} top visible setups, traded {opportunities['traded']}, missed/rejected {opportunities['missed']}, and {opportunities['missed_worked']} missed setups later worked. Latest scan {opportunities['latest_scan']}. Top rows: "
                                + ("; ".join(f"{row['symbol']} {row['opportunity_side']} {row['regime']} conf {row['confidence']} decision {row['senior_decision']} reason {row['rejection_reason']} outcome {row['outcome_label']}" for row in top_opportunities) if top_opportunities else "none recorded") + ".",
                         "source":"senior_market_opportunities"})
        docs.append({"id":"sentiment:single_source","title":"Sentiment Intelligence source of truth",
                     "text":f"Postgres news_articles_v3/news_sentiment_scores contains {sentiment['scored_articles']} scored articles across {sentiment['symbols']} symbols; latest score {sentiment['latest_score']}. Web cache is display-only; Senior layer and ML gates should use scored Postgres news.",
                     "source":"news_sentiment_scores"})
        if latest_1s:
            docs.append({"id":"microstructure:one_second_status","title":"1-second microstructure status",
                         "text":f"Sub-second orderflow stream active; latest 1s bar at {latest_1s}. Use as entry/exit confirmation until continuous coverage is proven; do not promote a 1s-trained model yet.",
                         "source":"live_market_bars"})
        _SYSTEM_DOCS_CACHE = docs
        _SYSTEM_DOCS_CACHED_AT = now
    except Exception as exc:
        docs.append({"id":"system:rag_db_warning","title":"RAG database warning","text":f"Database context unavailable: {str(exc)[:180]}.", "source":"rag_engine"})
    return docs


def _memory_docs(db, user_id: int, limit: int = 8) -> List[Dict]:
    try:
        rows=db.execute("""SELECT m.content,m.created_at FROM bot_messages m
            JOIN bot_conversations c ON c.id=m.conversation_id
            WHERE c.user_id=? AND m.role='assistant'
            ORDER BY m.id DESC LIMIT ?""",(user_id,limit)).fetchall()
        return [{"id":f"memory:{index}","title":"Recent bot memory","text":row["content"][:700],"source":"bot_memory"} for index,row in enumerate(rows,1)]
    except Exception:
        return []


def _analysis_docs(db, user_id: int, symbol: Optional[str]) -> List[Dict]:
    if not symbol:
        return []
    try:
        row = db.execute(
            "SELECT payload,created_at FROM stock_analyses WHERE user_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
            (user_id, symbol.upper()),
        ).fetchone()
        if not row:
            return []
        import json
        payload = json.loads(row["payload"])
        alignment = payload.get("decision_alignment") or {}
        model_input = payload.get("model_input") or {}
        chart = alignment.get("chart_structure") or {}
        ml = alignment.get("ml_context") or {}
        sentiment = alignment.get("sentiment_context") or {}
        blockers = "; ".join(alignment.get("blockers") or []) or "none"
        warnings = "; ".join(alignment.get("warnings") or []) or "none"
        text_value = (
            f"Latest AI Market Analysis for {payload.get('symbol', symbol)} from {row['created_at']}: "
            f"verdict {alignment.get('verdict','unknown')}, Senior action {alignment.get('senior_action','unknown')}, "
            f"confluence {alignment.get('confluence_score','unknown')}. "
            f"Data source {model_input.get('source','unknown')} {model_input.get('timeframe','')} with {model_input.get('bars','unknown')} bars. "
            f"Chart mode {chart.get('mode','WAIT')} at line {chart.get('active_line')}; reason {chart.get('reason')}. "
            f"ML bias {ml.get('bias')} confidence {ml.get('confidence')} direction {ml.get('direction')} R:R {ml.get('risk_reward')}. "
            f"Sentiment {sentiment.get('label')} score {sentiment.get('score')} from {sentiment.get('articles')} articles. "
            f"Blockers: {blockers}. Warnings: {warnings}."
        )
        return [{"id":f"analysis:{symbol.upper()}","title":f"{symbol.upper()} AI/Senior decision alignment","text":text_value,"source":"stock_analyses"}]
    except Exception as exc:
        return [{"id":f"analysis:{symbol.upper()}:warning","title":"AI Analysis context warning","text":f"Latest analysis unavailable: {str(exc)[:160]}.", "source":"stock_analyses"}]


def retrieve_context(db, user_id: int, query: str, symbol: Optional[str] = None, top_k: int = None) -> Dict:
    if not AI_RAG_ENABLED:
        return {"enabled":False,"query":query,"documents":[],"summary":"RAG disabled by configuration."}
    docs=_base_docs()+_symbol_docs(symbol)+_analysis_docs(db,user_id,symbol)+_system_docs()+_memory_docs(db,user_id)
    query_terms=Counter(_tokens(query+" "+(symbol or "")))
    ranked=sorted(({**doc,"score":round(_score(query_terms,doc),4)} for doc in docs), key=lambda item:item["score"], reverse=True)
    selected=[doc for doc in ranked if doc["score"]>0][:max(1,top_k or AI_RAG_TOP_K)]
    if not selected:
        selected=ranked[:max(1,top_k or AI_RAG_TOP_K)]
    summary=" | ".join(f"{doc['title']}: {doc['text'][:180]}" for doc in selected[:4])
    return {"enabled":True,"query":query,"documents":selected,"summary":summary,"safety":"Retrieved context is advisory. Deterministic risk/execution gates remain authoritative."}
