"""Post-market shadow-cost audit and immutable promotion-gate evaluation."""
import json
import os
from decimal import Decimal
from typing import Dict

from sqlalchemy import text

from backend.intelligence_memory import publish_brain_event
from backend.transaction_costs import estimate_zerodha_costs


STOP_LOSS_PCT = Decimal(os.getenv("NIVESH_PAPER_STOP_LOSS_PCT", "0.008"))
TAKE_PROFIT_PCT = Decimal(os.getenv("NIVESH_PAPER_TAKE_PROFIT_PCT", "0.018"))
OPTION_STOP_LOSS_PCT = Decimal(os.getenv("NIVESH_PAPER_OPTION_STOP_LOSS_PCT", "0.25"))
OPTION_TAKE_PROFIT_PCT = Decimal(os.getenv("NIVESH_PAPER_OPTION_TAKE_PROFIT_PCT", "0.50"))


def classify_probability(probability: float, lower: float = .35, upper: float = .65) -> int:
    if not 0<=lower<.5<upper<=1: raise ValueError("Thresholds must surround 0.5")
    return 1 if probability>=upper else -1 if probability<=lower else 0


def threshold_coverage(probabilities,lower=.35,upper=.65) -> Dict:
    values=list(probabilities); bullish=sum(x>=upper for x in values); bearish=sum(x<=lower for x in values)
    return {"samples":len(values),"bullish":bullish,"bearish":bearish,"neutral":len(values)-bullish-bearish,
            "coverage_pct":round((bullish+bearish)/max(1,len(values))*100,4),"lower":lower,"upper":upper}


def record_shadow_signal(engine,redis_client,model_version:str,instrument_token:int,side:str,quantity:int,probability:float,decision_price:Decimal,signal_at,strategy_note:str="") -> Dict:
    raw=redis_client.get(f"nivesh:depth:{int(instrument_token)}"); depth=json.loads(raw) if raw else {}
    if os.getenv("NIVESH_SHADOW_REQUIRE_DEPTH_FOR_ENTRIES", "0") == "1" and not depth:
        return {"recorded":False,"depth_available":False,"fill_source":"REJECTED_NO_DEPTH",
                "rejection_reason":"Depth snapshot required; fallback fills are disabled for senior-trader paper mode",
                "orders_allowed":False}
    with engine.begin() as connection:
        instrument=connection.execute(text("""
            SELECT i.id,i.symbol,i.instrument_type,i.tick_size,i.exchange,i.expiry,i.lot_size,i.is_active
            FROM instrument_master i
            LEFT JOIN instrument_provider_keys k ON k.instrument_id=i.id AND k.is_active
            WHERE i.is_active AND (i.instrument_token=:token OR k.provider_token=:token)
            ORDER BY CASE WHEN i.instrument_token=:token THEN 0 ELSE 1 END
            LIMIT 1
        """),{"token":instrument_token}).mappings().one_or_none()
        if not instrument: raise ValueError("Active instrument token is required")
        tick=Decimal(instrument["tick_size"] or "0.05")
        best_bid=depth.get("best_bid")
        best_ask=depth.get("best_ask")
        reference=Decimal(str(best_ask if side=="BUY" else best_bid)) if (best_ask if side=="BUY" else best_bid) is not None else decision_price
        fill=(reference+tick) if side=="BUY" else (reference-tick)
        trade_mode = "INTRADAY"
        reasoning_chain_json = None
        chart_sl = None
        chart_tp = None
        allow_strategy_risk = False
        if strategy_note:
            try:
                note=json.loads(strategy_note) if str(strategy_note).strip().startswith("{") else {}
                if note.get("trade_mode"):
                    trade_mode = str(note.get("trade_mode")).upper()
                if note.get("reasoning_chain"):
                    reasoning_chain_json = json.dumps(note.get("reasoning_chain"), default=str)
                chart_sl=note.get("strategy_stop_loss")
                chart_tp=note.get("strategy_take_profit")
                risk_price_basis=str(note.get("risk_price_basis") or "").lower()
                allow_strategy_risk = instrument["instrument_type"] not in {"CE","PE"} or risk_price_basis=="instrument"
            except Exception:
                pass
        segment="FUTURES" if instrument["instrument_type"]=="FUT" else "OPTIONS" if instrument["instrument_type"] in {"CE","PE"} else ("EQUITY_DELIVERY" if trade_mode=="SWING" else "EQUITY_INTRADAY")
        costs=estimate_zerodha_costs(segment,side,fill,int(quantity),"NSE" if instrument["exchange"] in {"NSE","NFO"} else "BSE")
        signal_date=signal_at.date()
        verified=(instrument["instrument_type"] in {"FUT","CE","PE"} and instrument["is_active"]
            and instrument["expiry"] is not None and instrument["expiry"]>=signal_date and int(quantity)%int(instrument["lot_size"])==0)
        sl_pct=OPTION_STOP_LOSS_PCT if instrument["instrument_type"] in {"CE","PE"} else STOP_LOSS_PCT
        tp_pct=OPTION_TAKE_PROFIT_PCT if instrument["instrument_type"] in {"CE","PE"} else TAKE_PROFIT_PCT
        stop_loss=(fill*(Decimal("1")-sl_pct)) if side=="BUY" else (fill*(Decimal("1")+sl_pct))
        take_profit=(fill*(Decimal("1")+tp_pct)) if side=="BUY" else (fill*(Decimal("1")-tp_pct))
        if allow_strategy_risk and chart_sl not in (None,""):
            candidate_sl=Decimal(str(chart_sl))
            if (side=="BUY" and candidate_sl<fill) or (side=="SELL" and candidate_sl>fill):
                stop_loss=candidate_sl
        if allow_strategy_risk and chart_tp not in (None,""):
            candidate_tp=Decimal(str(chart_tp))
            if (side=="BUY" and candidate_tp>fill) or (side=="SELL" and candidate_tp<fill):
                take_profit=candidate_tp
        fill_source="DEPTH_SNAPSHOT" if depth else "DECISION_PRICE_FALLBACK"
        inserted=connection.execute(text("""INSERT INTO shadow_execution_audits(model_version,instrument_id,signal_at,side,quantity,signal_probability,decision_price,
            best_bid,best_ask,bid_quantity,ask_quantity,theoretical_fill_price,one_tick_penalty,estimated_fees,cost_reconciled,
            instrument_execution_verified,audit_status,rejection_reason,stop_loss_price,take_profit_price,fill_source,improvement_note,
            trade_mode,reasoning_chain)
            VALUES(:model,:instrument,:signal_at,:side,:quantity,:probability,:price,:bid,:ask,:bid_qty,:ask_qty,:fill,:penalty,:fees,TRUE,
            :verified,'RECONCILED',:reason,:stop_loss,:take_profit,:fill_source,:strategy_note,
            :trade_mode,CASE WHEN :reasoning_chain IS NOT NULL THEN CAST(:reasoning_chain AS jsonb) ELSE NULL END)
            ON CONFLICT(model_version,instrument_id,signal_at,side) DO NOTHING RETURNING id"""),
            {"model":model_version,"instrument":instrument["id"],"signal_at":signal_at,"side":side,"quantity":quantity,"probability":probability,"price":decision_price,
             "bid":best_bid,"ask":best_ask,"bid_qty":depth.get("bid_quantity"),"ask_qty":depth.get("ask_quantity"),
             "fill":fill,"penalty":tick*int(quantity),"fees":costs.total,"verified":verified,
             "reason":None if depth else "Paper fallback: no contemporaneous depth snapshot",
             "stop_loss":stop_loss,"take_profit":take_profit,"fill_source":fill_source,
             "strategy_note":strategy_note[:2000] if strategy_note else None,
             "trade_mode":trade_mode,"reasoning_chain":reasoning_chain_json}).scalar_one_or_none()
        if inserted:
            payload={"audit_id":int(inserted),"instrument_id":int(instrument["id"]),"symbol":instrument["symbol"],
                     "instrument_type":instrument["instrument_type"],"side":side,"quantity":quantity,
                     "probability":probability,"decision_price":str(decision_price),"fill_price":str(fill),
                     "fill_source":fill_source,"model_version":model_version}
            publish_brain_event(connection,"ShadowTradeOpened","record_shadow_signal",payload,
                                symbol=instrument["symbol"],severity="INFO")
            try:
                from backend.brains import get_bus
                from backend.brains.bus import ShadowTradeOpened
                get_bus().publish(ShadowTradeOpened(source_brain="record_shadow_signal", audit_id=int(inserted),
                                                    symbol=instrument["symbol"], side=side,
                                                    quantity=int(quantity), route=instrument["instrument_type"],
                                                    payload=payload))
            except Exception:
                pass
            try:
                from backend.sse_broadcaster import publish_sse_event
                publish_sse_event("trade_opened", {
                    "audit_id": int(inserted),
                    "symbol": instrument["symbol"],
                    "side": side,
                    "quantity": int(quantity),
                    "trade_mode": trade_mode,
                    "fill_price": float(fill),
                    "fill_source": fill_source,
                })
            except Exception:
                pass
    return {"recorded":bool(inserted),"audit_id":int(inserted) if inserted else None,
            "depth_available":bool(depth),"fill_source":fill_source,"orders_allowed":False}


def reconcile_shadow_costs(engine) -> Dict:
    reconciled=rejected=0
    with engine.begin() as connection:
        rows=connection.execute(text("""SELECT a.*,i.exchange,i.instrument_type,i.tick_size,i.expiry,i.lot_size,i.is_active FROM shadow_execution_audits a
            JOIN instrument_master i ON i.id=a.instrument_id WHERE a.audit_status='PENDING' FOR UPDATE""")).mappings().all()
        for row in rows:
            side=row["side"]; reference=row["best_ask"] if side=="BUY" else row["best_bid"]
            if reference is None:
                connection.execute(text("UPDATE shadow_execution_audits SET audit_status='REJECTED',rejection_reason='Missing bid/ask depth' WHERE id=:id"),{"id":row["id"]}); rejected+=1; continue
            tick=Decimal(row["tick_size"]); fill=Decimal(reference)+tick if side=="BUY" else Decimal(reference)-tick
            segment="FUTURES" if row["instrument_type"]=="FUT" else "OPTIONS" if row["instrument_type"] in {"CE","PE"} else "EQUITY_INTRADAY"
            costs=estimate_zerodha_costs(segment,side,fill,int(row["quantity"]),"NSE" if row["exchange"] in {"NSE","NFO"} else "BSE")
            signal_date=row["signal_at"].date(); verified=(row["instrument_type"] in {"FUT","CE","PE"} and row["is_active"]
                and row["expiry"] is not None and row["expiry"]>=signal_date and int(row["quantity"])%int(row["lot_size"])==0)
            connection.execute(text("""UPDATE shadow_execution_audits SET theoretical_fill_price=:fill,one_tick_penalty=:penalty,estimated_fees=:fees,
                cost_reconciled=TRUE,instrument_execution_verified=:verified,audit_status='RECONCILED' WHERE id=:id"""),
                {"fill":fill,"penalty":tick*int(row["quantity"]),"fees":costs.total,"verified":verified,"id":row["id"]}); reconciled+=1
    return {"reconciled":reconciled,"rejected":rejected,"orders_allowed":False}


def _mistake_tags(row, net: Decimal, exit_reason: str) -> Dict:
    tags=[]; note="Paper trade behaved as planned; keep collecting evidence."
    if exit_reason=="STOP_LOSS":
        tags.append("stop_loss_hit"); note="Review entry timing, trend filter and stop distance; signal moved against the model before target."
    elif exit_reason=="TIME_EXIT" and net<0:
        tags.append("stale_signal_loss"); note="Signal did not work within the paper holding window; consider stricter no-trade zone or faster invalidation."
    elif exit_reason=="TIME_EXIT" and net>=0:
        tags.append("slow_winner"); note="Trade made money but did not reach target; evaluate partial-profit or trailing exit rules."
    elif exit_reason=="TAKE_PROFIT":
        tags.append("target_hit"); note="Target was reached; compare whether earlier exits leave profit on the table."
    elif exit_reason=="PROFIT_CAPTURE":
        tags.append("profit_captured"); note="Trade reached the configured paper profit capture threshold and was closed before the move faded."
    elif exit_reason=="TRAILING_STOP":
        tags.append("trailing_profit_protection"); note="Trade moved favourably and the trailing stop protected part of the move."
    elif exit_reason=="BREAKEVEN_STOP":
        tags.append("capital_protection"); note="Trade moved enough to arm break-even protection, then faded back without a material loss."
    if row["fill_source"]!="DEPTH_SNAPSHOT":
        tags.append("fallback_fill")
    if Decimal(row["estimated_fees"] or 0)>abs(net) and net<0:
        tags.append("cost_drag")
    return {"tags":tags,"note":note}


def record_shadow_exit(engine,audit_id:int,exit_price:Decimal,exit_reason: str = "TIME_EXIT",exit_at=None) -> Dict:
    if exit_price<=0: raise ValueError("Positive exit price required")
    with engine.begin() as connection:
        row=connection.execute(text("""SELECT a.*,i.exchange,i.instrument_type,i.symbol FROM shadow_execution_audits a
            JOIN instrument_master i ON i.id=a.instrument_id 
            WHERE a.id=:id AND a.audit_status='RECONCILED' AND a.net_pnl IS NULL 
            FOR UPDATE"""),{"id":audit_id}).mappings().one_or_none()
        if not row:
            return {"recorded":False,"status":"ALREADY_CLOSED_OR_NOT_FOUND","audit_id":audit_id}
        entry=Decimal(row["theoretical_fill_price"]); quantity=int(row["quantity"]); side=row["side"]
        exit_side="SELL" if side=="BUY" else "BUY"
        trade_mode = str(row.get("trade_mode") or "INTRADAY").upper()
        segment="FUTURES" if row["instrument_type"]=="FUT" else "OPTIONS" if row["instrument_type"] in {"CE","PE"} else ("EQUITY_DELIVERY" if trade_mode=="SWING" else "EQUITY_INTRADAY")
        exit_costs=estimate_zerodha_costs(segment,exit_side,exit_price,quantity,"NSE" if row["exchange"] in {"NSE","NFO"} else "BSE")
        gross=(exit_price-entry)*quantity if side=="BUY" else (entry-exit_price)*quantity
        net=gross-Decimal(row["estimated_fees"])-exit_costs.total
        mistake=_mistake_tags(row,net,exit_reason)
        connection.execute(text("""UPDATE shadow_execution_audits SET realised_exit_price=:exit,net_pnl=:net,
            exit_at=COALESCE(:exit_at,CURRENT_TIMESTAMP),exit_reason=:reason,mistake_tags=CAST(:tags AS jsonb),improvement_note=:note
            WHERE id=:id AND net_pnl IS NULL"""),{"exit":exit_price,"net":net,"id":audit_id,"exit_at":exit_at,"reason":exit_reason,
                              "tags":json.dumps(mistake["tags"]),"note":mistake["note"]})
        publish_brain_event(connection,"ShadowTradeClosed","record_shadow_exit",
                            {"audit_id":audit_id,"instrument_id":int(row["instrument_id"]),"side":side,
                             "net_pnl":str(net),"exit_reason":exit_reason,"mistake_tags":mistake["tags"]},
                            severity="INFO" if net>=0 else "WARNING")
        if float(net) >= 100.0 or (row["stop_loss_price"] and float(abs(exit_price - entry) / max(Decimal("0.05"), abs(entry - Decimal(row["stop_loss_price"])))) >= 1.8):
            try:
                from backend.trade_memory import auto_ingest_winning_trade
                auto_ingest_winning_trade({
                    "id": audit_id,
                    "symbol": row.get("symbol", ""),
                    "side": side,
                    "net_pnl": float(net),
                    "entry_price": float(entry),
                    "exit_price": float(exit_price),
                    "stop_loss_price": float(row["stop_loss_price"] or entry * Decimal("0.98")),
                    "take_profit_price": float(row["take_profit_price"] or entry * Decimal("1.02")),
                    "signal_probability": float(row["signal_probability"] or 0.80),
                    "exit_reason": exit_reason,
                    "improvement_note": mistake.get("note", "")
                })
            except Exception:
                pass
        try:
            from backend.brains import get_bus
            from backend.brains.bus import ShadowTradeClosed
            get_bus().publish(ShadowTradeClosed(source_brain="record_shadow_exit", audit_id=int(audit_id),
                                                net_pnl=float(net), exit_reason=exit_reason,
                                                payload={"mistake_tags":mistake["tags"]}))
        except Exception:
            pass
        try:
            from backend.sse_broadcaster import publish_sse_event
            publish_sse_event("trade_closed", {
                "audit_id": int(audit_id),
                "symbol": str(row.get("symbol") or ""),
                "net_pnl": float(net),
                "gross_pnl": float(gross),
                "exit_reason": str(exit_reason),
                "trade_mode": row.get("trade_mode") or "INTRADAY",
            })
        except Exception:
            pass
    return {"audit_id":audit_id,"gross_pnl":str(gross),"net_pnl":str(net),"exit_reason":exit_reason,
            "mistake_tags":mistake["tags"],"round_trip_fees":str(Decimal(row["estimated_fees"])+exit_costs.total),"orders_allowed":False}


def record_contract_note_reconciliation(engine,broker_order_id:str,actual_fees:Decimal,
                                        actual_fill_price:Decimal) -> Dict:
    """Attach authoritative broker evidence to one completely filled live order.

    This function is deliberately separate from shadow-cost reconciliation: a
    theoretical fill can never satisfy the real-cost promotion gate.
    """
    if not broker_order_id or actual_fees<0 or actual_fill_price<=0:
        raise ValueError("Valid broker order, non-negative fees and positive fill price are required")
    with engine.begin() as connection:
        row=connection.execute(text("""SELECT id,limit_price,filled_quantity,status FROM order_execution_ledger
            WHERE broker_order_id=:broker FOR UPDATE"""),{"broker":broker_order_id}).mappings().one_or_none()
        if not row: raise ValueError("Broker order is not present in the execution ledger")
        if row["status"]!="FILLED" or int(row["filled_quantity"])<=0:
            raise ValueError("Only a filled broker order can be reconciled")
        slippage=abs(actual_fill_price-Decimal(row["limit_price"]))*int(row["filled_quantity"])
        connection.execute(text("""UPDATE order_execution_ledger SET average_fill_price=:fill,
            actual_contract_note_fees=:fees,actual_slippage=:slippage,
            contract_note_reconciled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=:id"""),
            {"fill":actual_fill_price,"fees":actual_fees,"slippage":slippage,"id":row["id"]})
    return {"broker_order_id":broker_order_id,"actual_contract_note_fees":str(actual_fees),
            "actual_slippage":str(slippage),"evidence_source":"ZERODHA_CONTRACT_NOTE"}


def evaluate_promotion(engine,starting_capital:Decimal=Decimal("1000000")) -> Dict:
    with engine.begin() as connection:
        completed=int(connection.execute(text("SELECT COUNT(*) FROM forward_shadow_sessions WHERE status='COMPLETE'")).scalar_one())
        model=connection.execute(text("SELECT metrics FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1")).scalar_one_or_none() or {}
        if isinstance(model,str): model=json.loads(model)
        log_loss=float(model.get("holdout",{}).get("log_loss",999))
        pnl=[float(row[0]) for row in connection.execute(text("SELECT net_pnl FROM shadow_execution_audits WHERE net_pnl IS NOT NULL ORDER BY signal_at"))]
        gains=sum(x for x in pnl if x>0); losses=abs(sum(x for x in pnl if x<=0)); profit_factor=gains/losses if losses else (999 if gains else 0)
        equity=peak=float(starting_capital); max_dd=0.0
        for value in pnl: equity+=value; peak=max(peak,equity); max_dd=min(max_dd,(equity/peak-1)*100)
        cost_sessions=int(connection.execute(text("""SELECT COUNT(DISTINCT (created_at AT TIME ZONE 'Asia/Kolkata')::date)
            FROM order_execution_ledger WHERE contract_note_reconciled_at IS NOT NULL
              AND actual_contract_note_fees IS NOT NULL AND status='FILLED'""")).scalar_one())
        verified=int(connection.execute(text("SELECT COUNT(*) FROM shadow_execution_audits WHERE instrument_execution_verified AND audit_status='RECONCILED'")).scalar_one())
        gates={"sessions_gate":completed>=90,"performance_gate":profit_factor>=1.20 and max_dd>=-7.32 and bool(pnl),
               "log_loss_gate":log_loss<.693,"costs_gate":cost_sessions>=20,"instrument_gate":verified>=20}
        eligible=all(gates.values()); reasons=[name for name,passed in gates.items() if not passed]
        connection.execute(text("""UPDATE system_promotion_ledger SET completed_sessions=:sessions,rolling_profit_factor=:pf,max_drawdown_pct=:dd,
            untouched_log_loss=:loss,reconciled_cost_sessions=:costs,verified_derivative_executions=:verified,
            cost_evidence_source=:cost_source,instrument_evidence_source=:instrument_source,sessions_gate=:sessions_gate,
            performance_gate=:performance_gate,log_loss_gate=:log_loss_gate,costs_gate=:costs_gate,instrument_gate=:instrument_gate,
            live_eligible=:eligible,reasons=CAST(:reasons AS jsonb),evaluated_at=CURRENT_TIMESTAMP WHERE singleton"""),
            {"sessions":completed,"pf":profit_factor,"dd":max_dd,"loss":log_loss,"costs":cost_sessions,"verified":verified,
             "cost_source":"ZERODHA_CONTRACT_NOTE" if cost_sessions else "NONE",
             "instrument_source":"BROKER_CONNECTED_SHADOW" if verified else "NONE",
             **gates,"eligible":eligible,"reasons":json.dumps(reasons)})
        row=connection.execute(text("SELECT * FROM system_promotion_ledger WHERE singleton")).mappings().one()
    return {**dict(row),"orders_allowed":False if not row["live_eligible"] else "operator_review_required"}
