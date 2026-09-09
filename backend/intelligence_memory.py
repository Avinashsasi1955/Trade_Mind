"""Durable intelligence memory and cross-process brain event journal.

The in-process brain bus is useful inside one Python process, but the live
stream, Celery worker, and web server run in different processes/containers.
This module gives the brain layer a Postgres-backed memory so Operations can
see real live-inference events and long-term candidate/regime outcomes.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, Iterable

from sqlalchemy import text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS brain_event_journal(
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT,
    severity TEXT NOT NULL DEFAULT 'INFO',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_brain_event_journal_recent
    ON brain_event_journal(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_brain_event_journal_type_recent
    ON brain_event_journal(event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS counterfactual_candidate_log(
    id BIGSERIAL PRIMARY KEY,
    candidate_audit_id BIGINT NOT NULL UNIQUE,
    observed_at TIMESTAMPTZ NOT NULL,
    model_version TEXT,
    exchange TEXT,
    symbol TEXT NOT NULL,
    instrument_id BIGINT,
    instrument_type TEXT,
    signal INTEGER NOT NULL DEFAULT 0,
    route TEXT,
    strategy TEXT,
    selector_stage TEXT,
    rejection_reason TEXT,
    probability NUMERIC(10,6),
    rr NUMERIC(10,4),
    quality_score NUMERIC(10,4),
    accepted BOOLEAN NOT NULL DEFAULT FALSE,
    outcome_5m NUMERIC(14,4),
    outcome_15m NUMERIC(14,4),
    outcome_30m NUMERIC(14,4),
    outcome_label TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_counterfactual_candidate_log_symbol_time
    ON counterfactual_candidate_log(symbol, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_counterfactual_candidate_log_outcome
    ON counterfactual_candidate_log(outcome_label, observed_at DESC);

CREATE TABLE IF NOT EXISTS regime_strategy_performance(
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    session_block TEXT NOT NULL,
    regime TEXT NOT NULL,
    strategy TEXT NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    traded INTEGER NOT NULL DEFAULT 0,
    missed INTEGER NOT NULL DEFAULT 0,
    missed_worked INTEGER NOT NULL DEFAULT 0,
    avg_confidence NUMERIC(10,4) NOT NULL DEFAULT 0,
    avg_rr NUMERIC(10,4) NOT NULL DEFAULT 0,
    call_setups INTEGER NOT NULL DEFAULT 0,
    put_setups INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_date, session_block, regime, strategy)
);
CREATE INDEX IF NOT EXISTS idx_regime_strategy_performance_recent
    ON regime_strategy_performance(trade_date DESC, session_block, regime);
"""


_INTELLIGENCE_SCHEMA_INITIALIZED = False


def ensure_intelligence_schema(connection) -> None:
    global _INTELLIGENCE_SCHEMA_INITIALIZED
    if _INTELLIGENCE_SCHEMA_INITIALIZED:
        return
    try:
        connection.execute(text(SCHEMA_SQL))
        _INTELLIGENCE_SCHEMA_INITIALIZED = True
    except Exception:
        _INTELLIGENCE_SCHEMA_INITIALIZED = True


def publish_brain_event(connection, event_type: str, source: str, payload: Dict[str, Any] | None = None,
                        *, symbol: str | None = None, severity: str = "INFO") -> Dict:
    ensure_intelligence_schema(connection)
    row = connection.execute(text("""
        INSERT INTO brain_event_journal(event_type,source,symbol,severity,payload)
        VALUES(:event_type,:source,:symbol,:severity,CAST(:payload AS jsonb))
        RETURNING id, occurred_at
    """), {
        "event_type": event_type,
        "source": source,
        "symbol": symbol,
        "severity": severity,
        "payload": json.dumps(payload or {}, default=str),
    }).mappings().one()
    return {"id": int(row["id"]), "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None}


def persist_counterfactual_candidates(connection, *, limit: int = 1000) -> Dict:
    """Copy rejected/accepted candidate audits into durable memory."""
    ensure_intelligence_schema(connection)
    inserted = connection.execute(text("""
        INSERT INTO counterfactual_candidate_log(
            candidate_audit_id,observed_at,model_version,exchange,symbol,instrument_id,
            instrument_type,signal,route,strategy,selector_stage,rejection_reason,
            probability,rr,quality_score,accepted,details
        )
        SELECT id,observed_at,model_version,exchange,symbol,instrument_id,instrument_type,
               COALESCE(signal,0),route,chart_strategy,selector_stage,rejection_reason,
               probability,rr,quality_score,accepted,details
        FROM trade_candidate_audits
        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
        ORDER BY observed_at DESC, id DESC
        LIMIT :limit
        ON CONFLICT(candidate_audit_id) DO UPDATE SET
            accepted=EXCLUDED.accepted,
            rejection_reason=EXCLUDED.rejection_reason,
            details=EXCLUDED.details,
            updated_at=CURRENT_TIMESTAMP
    """), {"limit": limit}).rowcount or 0
    return {"status": "success", "upserted": int(inserted)}


def update_candidate_counterfactual_outcomes(connection, *, limit: int = 1000) -> Dict:
    ensure_intelligence_schema(connection)
    rows = connection.execute(text("""
        SELECT id,observed_at,instrument_id,signal
        FROM counterfactual_candidate_log
        WHERE observed_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
          AND (outcome_5m IS NULL OR outcome_15m IS NULL OR outcome_30m IS NULL)
        ORDER BY observed_at DESC
        LIMIT :limit
    """), {"limit": limit}).mappings().all()
    updated = 0
    for row in rows:
        direction = 1 if int(row["signal"] or 0) > 0 else -1 if int(row["signal"] or 0) < 0 else 0
        if not direction:
            continue
        entry = connection.execute(text("""
            SELECT close_price FROM live_market_bars
            WHERE instrument_id=:instrument_id AND interval='5minute' AND bar_time<=:observed_at
            ORDER BY bar_time DESC LIMIT 1
        """), {"instrument_id": row["instrument_id"], "observed_at": row["observed_at"]}).scalar_one_or_none()
        if entry is None:
            continue
        outcomes: Dict[str, Any] = {}
        for minutes in (5, 15, 30):
            price = connection.execute(text("""
                SELECT close_price FROM live_market_bars
                WHERE instrument_id=:instrument_id AND interval='5minute'
                  AND bar_time>=:target_time
                ORDER BY bar_time ASC LIMIT 1
            """), {"instrument_id": row["instrument_id"], "target_time": row["observed_at"] + timedelta(minutes=minutes)}).scalar_one_or_none()
            if price is not None:
                outcomes[f"outcome_{minutes}m"] = round((float(price) - float(entry)) * direction, 4)
        if not outcomes:
            continue
        best = max(outcomes.values())
        worst = min(outcomes.values())
        label = "would_have_worked" if best > abs(worst) and best > 0 else "would_have_failed" if worst < 0 else "unclear"
        connection.execute(text("""
            UPDATE counterfactual_candidate_log
            SET outcome_5m=COALESCE(:outcome_5m,outcome_5m),
                outcome_15m=COALESCE(:outcome_15m,outcome_15m),
                outcome_30m=COALESCE(:outcome_30m,outcome_30m),
                outcome_label=:outcome_label,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=:id
        """), {"id": row["id"], "outcome_label": label,
               "outcome_5m": outcomes.get("outcome_5m"),
               "outcome_15m": outcomes.get("outcome_15m"),
               "outcome_30m": outcomes.get("outcome_30m")})
        updated += 1
    return {"status": "success", "updated": updated}


def refresh_regime_strategy_performance(connection) -> Dict:
    ensure_intelligence_schema(connection)
    upserted = connection.execute(text("""
        INSERT INTO regime_strategy_performance(
            trade_date,session_block,regime,strategy,scanned,traded,missed,missed_worked,
            avg_confidence,avg_rr,call_setups,put_setups,updated_at
        )
        SELECT (observed_at AT TIME ZONE 'Asia/Kolkata')::date trade_date,
               COALESCE(session_block,'unknown') session_block,
               COALESCE(regime,'unknown') regime,
               COALESCE(strategy,'unknown') strategy,
               COUNT(*) scanned,
               COUNT(*) FILTER(WHERE senior_decision='TRADED') traded,
               COUNT(*) FILTER(WHERE senior_decision<>'TRADED') missed,
               COUNT(*) FILTER(WHERE outcome_label='would_have_worked') missed_worked,
               COALESCE(AVG(confidence),0) avg_confidence,
               COALESCE(AVG(risk_reward),0) avg_rr,
               COUNT(*) FILTER(WHERE opportunity_side='CALL') call_setups,
               COUNT(*) FILTER(WHERE opportunity_side='PUT') put_setups,
               CURRENT_TIMESTAMP
        FROM senior_market_opportunities
        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date>=CURRENT_DATE-INTERVAL '30 days'
        GROUP BY 1,2,3,4
        ON CONFLICT(trade_date,session_block,regime,strategy) DO UPDATE SET
            scanned=EXCLUDED.scanned,
            traded=EXCLUDED.traded,
            missed=EXCLUDED.missed,
            missed_worked=EXCLUDED.missed_worked,
            avg_confidence=EXCLUDED.avg_confidence,
            avg_rr=EXCLUDED.avg_rr,
            call_setups=EXCLUDED.call_setups,
            put_setups=EXCLUDED.put_setups,
            updated_at=CURRENT_TIMESTAMP
    """)).rowcount or 0
    return {"status": "success", "upserted": int(upserted)}


def refresh_intelligence_memory(connection) -> Dict:
    candidate = persist_counterfactual_candidates(connection)
    outcomes = update_candidate_counterfactual_outcomes(connection)
    regime = refresh_regime_strategy_performance(connection)
    return {"status": "success", "candidate_memory": candidate, "candidate_outcomes": outcomes, "regime_memory": regime}


def recent_brain_events(connection, *, limit: int = 20) -> Iterable[Dict]:
    ensure_intelligence_schema(connection)
    return connection.execute(text("""
        SELECT event_type,source,occurred_at,symbol,severity,payload
        FROM brain_event_journal
        ORDER BY occurred_at DESC,id DESC
        LIMIT :limit
    """), {"limit": limit}).mappings().all()
