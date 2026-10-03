"""Durable intelligence memory and cross-process brain event journal.

The in-process brain bus is useful inside one Python process, but the live
stream, Celery worker, and web server run in different processes/containers.
This module gives the brain layer a Postgres-backed memory so Operations can
see real live-inference events and long-term candidate/regime outcomes.

Phase C.3 Core Architecture:
Dual-Tier Intelligence Memo Package:
- Tier 1: Session Working Memory (Trap Detection & Cool-Off Enforcer)
- Tier 2: Episodic Cross-Session Memory (Historical Edge & Failure Priors)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text


SCHEMA_SQL_PG = """
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

CREATE TABLE IF NOT EXISTS session_trap_memory(
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL DEFAULT CURRENT_DATE,
    symbol TEXT NOT NULL,
    trap_type TEXT NOT NULL,
    side TEXT NOT NULL,
    price NUMERIC(12, 4) NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cooloff_until TIMESTAMPTZ NOT NULL,
    notes TEXT,
    resolved BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_session_trap_lookup
    ON session_trap_memory(symbol, side, cooloff_until);

CREATE TABLE IF NOT EXISTS episodic_pattern_memory(
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    pattern_name TEXT NOT NULL,
    observations_count INT NOT NULL DEFAULT 1,
    success_count INT NOT NULL DEFAULT 0,
    fail_count INT NOT NULL DEFAULT 0,
    avg_pnl NUMERIC(12, 4) NOT NULL DEFAULT 0,
    last_observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(symbol, regime, pattern_name)
);
CREATE INDEX IF NOT EXISTS idx_episodic_pattern_sym_reg
    ON episodic_pattern_memory(symbol, regime);
"""

SCHEMA_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS brain_event_journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT,
    severity TEXT NOT NULL DEFAULT 'INFO',
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS counterfactual_candidate_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_audit_id BIGINT NOT NULL UNIQUE,
    observed_at DATETIME NOT NULL,
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
    probability REAL,
    rr REAL,
    quality_score REAL,
    accepted BOOLEAN NOT NULL DEFAULT 0,
    outcome_5m REAL,
    outcome_15m REAL,
    outcome_30m REAL,
    outcome_label TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS regime_strategy_performance(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date DATE NOT NULL,
    session_block TEXT NOT NULL,
    regime TEXT NOT NULL,
    strategy TEXT NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    traded INTEGER NOT NULL DEFAULT 0,
    missed INTEGER NOT NULL DEFAULT 0,
    missed_worked INTEGER NOT NULL DEFAULT 0,
    avg_confidence REAL NOT NULL DEFAULT 0,
    avg_rr REAL NOT NULL DEFAULT 0,
    call_setups INTEGER NOT NULL DEFAULT 0,
    put_setups INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_date, session_block, regime, strategy)
);

CREATE TABLE IF NOT EXISTS session_trap_memory(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    trap_type TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    detected_at DATETIME NOT NULL,
    cooloff_until DATETIME NOT NULL,
    notes TEXT,
    resolved BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS episodic_pattern_memory(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    pattern_name TEXT NOT NULL,
    observations_count INT NOT NULL DEFAULT 1,
    success_count INT NOT NULL DEFAULT 0,
    fail_count INT NOT NULL DEFAULT 0,
    avg_pnl REAL NOT NULL DEFAULT 0,
    last_observed_at DATETIME NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(symbol, regime, pattern_name)
);
"""

SCHEMA_SQL = SCHEMA_SQL_PG

_INTELLIGENCE_SCHEMA_INITIALIZED = False


def ensure_intelligence_schema(connection) -> None:
    global _INTELLIGENCE_SCHEMA_INITIALIZED
    dialect = getattr(connection.dialect, "name", "sqlite")
    chosen_sql = SCHEMA_SQL_PG if dialect == "postgresql" else SCHEMA_SQL_SQLITE
    try:
        for stmt in chosen_sql.strip().split(";"):
            if stmt.strip():
                connection.execute(text(stmt.strip()))
        _INTELLIGENCE_SCHEMA_INITIALIZED = True
    except Exception:
        _INTELLIGENCE_SCHEMA_INITIALIZED = True


def publish_brain_event(connection, event_type: str, source: str, payload: Dict[str, Any] | None = None,
                        *, symbol: str | None = None, severity: str = "INFO") -> Dict:
    ensure_intelligence_schema(connection)
    dialect = getattr(connection.dialect, "name", "sqlite")
    meta_val = json.dumps(payload or {}, default=str)
    meta_clause = "CAST(:payload AS jsonb)" if dialect == "postgresql" else ":payload"

    sql = f"""
        INSERT INTO brain_event_journal(event_type,source,symbol,severity,payload)
        VALUES(:event_type,:source,:symbol,:severity,{meta_clause})
    """
    if dialect == "postgresql":
        sql += " RETURNING id, occurred_at"
        row = connection.execute(text(sql), {
            "event_type": event_type,
            "source": source,
            "symbol": symbol,
            "severity": severity,
            "payload": meta_val,
        }).mappings().one()
        return {"id": int(row["id"]), "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None}
    else:
        res = connection.execute(text(sql), {
            "event_type": event_type,
            "source": source,
            "symbol": symbol,
            "severity": severity,
            "payload": meta_val,
        })
        return {"id": res.lastrowid or 1, "occurred_at": datetime.now(timezone.utc).isoformat()}


def persist_counterfactual_candidates(connection, *, limit: int = 1000) -> Dict:
    """Copy rejected/accepted candidate audits into durable memory."""
    ensure_intelligence_schema(connection)
    dialect = getattr(connection.dialect, "name", "sqlite")
    if dialect != "postgresql":
        return {"status": "skipped_sqlite", "upserted": 0}
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
        WHERE (outcome_5m IS NULL OR outcome_15m IS NULL OR outcome_30m IS NULL)
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
    dialect = getattr(connection.dialect, "name", "sqlite")
    if dialect != "postgresql":
        return {"status": "skipped_sqlite", "upserted": 0}
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


# =====================================================================
# TIER 1: SESSION WORKING MEMORY (TRAP MEMORY & COOLOFF ENGINE)
# =====================================================================

def record_session_trap(connection, symbol: str, trap_type: str, side: str,
                        price: float, cooloff_minutes: int = 30, notes: str = "",
                        detected_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Records a detected microstructure trap for the current trading day.

    Traps include:
    - 'WILDER_ASI_SWEEP': False breakout beyond Wilder ASI boundary.
    - 'POC_REJECTION': Immediate violent rejection off Point of Control.
    - 'FAILED_BREAKOUT': Breakout above VAH / below VAL that failed back into range.
    - 'LIQUIDITY_RUN': Stop hunt sweep with negative order flow imbalance.
    """
    ensure_intelligence_schema(connection)
    now_dt = detected_at or datetime.now(timezone.utc)
    cooloff_until = now_dt + timedelta(minutes=cooloff_minutes)
    trade_date = now_dt.date()

    sql = """
        INSERT INTO session_trap_memory(
            trade_date, symbol, trap_type, side, price, detected_at, cooloff_until, notes, resolved
        ) VALUES (
            :tdate, :sym, :ttype, :side, :price, :det_at, :cool_until, :notes, FALSE
        )
    """
    connection.execute(text(sql), {
        "tdate": trade_date,
        "sym": symbol.upper(),
        "ttype": trap_type,
        "side": side.upper(),
        "price": price,
        "det_at": now_dt,
        "cool_until": cooloff_until,
        "notes": notes,
    })

    return {
        "status": "RECORDED",
        "symbol": symbol.upper(),
        "trap_type": trap_type,
        "side": side.upper(),
        "price": price,
        "cooloff_until": cooloff_until.isoformat(),
        "cooloff_minutes": cooloff_minutes,
        "notes": notes,
    }


def is_trap_cooloff_active(connection, symbol: str, side: str,
                           at_time: Optional[datetime] = None) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Check if a symbol is under an active trap cool-off blackout for the specified side.

    Prevents re-entering on the same side for 30 minutes after a failed breakout or sweep.
    """
    ensure_intelligence_schema(connection)
    check_time = at_time or datetime.now(timezone.utc)

    row = connection.execute(text("""
        SELECT id, symbol, trap_type, side, price, detected_at, cooloff_until, notes
        FROM session_trap_memory
        WHERE symbol = :sym
          AND side = :side
          AND resolved = FALSE
          AND cooloff_until > :check_time
        ORDER BY cooloff_until DESC
        LIMIT 1
    """), {
        "sym": symbol.upper(),
        "side": side.upper(),
        "check_time": check_time,
    }).mappings().first()

    if row:
        return True, dict(row)
    return False, None


def get_active_session_traps(connection, symbol: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve all session traps detected today or currently active."""
    ensure_intelligence_schema(connection)
    where_clause = "WHERE resolved = FALSE"
    params: Dict[str, Any] = {"limit": limit}
    if symbol:
        where_clause += " AND symbol = :sym"
        params["sym"] = symbol.upper()

    rows = connection.execute(text(f"""
        SELECT id, trade_date, symbol, trap_type, side, price, detected_at, cooloff_until, notes, resolved
        FROM session_trap_memory
        {where_clause}
        ORDER BY detected_at DESC
        LIMIT :limit
    """), params).mappings().all()

    return [dict(r) for r in rows]


def resolve_session_trap(connection, trap_id: int) -> bool:
    """Manually resolve or dismiss a trap."""
    ensure_intelligence_schema(connection)
    res = connection.execute(text("""
        UPDATE session_trap_memory
        SET resolved = TRUE
        WHERE id = :id
    """), {"id": trap_id})
    return bool(res.rowcount and res.rowcount > 0)


# =====================================================================
# TIER 2: EPISODIC CROSS-SESSION MEMORY (HISTORICAL PATTERNS & PRIORS)
# =====================================================================

def record_episodic_observation(connection, symbol: str, regime: str, pattern_name: str,
                                outcome_pnl: float, was_success: bool,
                                metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Records cross-session outcome evidence for a symbol under a specific market regime."""
    ensure_intelligence_schema(connection)
    dialect = getattr(connection.dialect, "name", "sqlite")
    meta_val = json.dumps(metadata or {}, default=str)

    now_dt = datetime.now(timezone.utc)

    if dialect == "postgresql":
        sql = """
            INSERT INTO episodic_pattern_memory (
                symbol, regime, pattern_name, observations_count, success_count, fail_count,
                avg_pnl, last_observed_at, metadata
            ) VALUES (
                :sym, :reg, :pat, 1, :succ, :fail, :pnl, :now_dt, CAST(:meta AS jsonb)
            )
            ON CONFLICT (symbol, regime, pattern_name) DO UPDATE SET
                observations_count = episodic_pattern_memory.observations_count + 1,
                success_count = episodic_pattern_memory.success_count + EXCLUDED.success_count,
                fail_count = episodic_pattern_memory.fail_count + EXCLUDED.fail_count,
                avg_pnl = ((episodic_pattern_memory.avg_pnl * episodic_pattern_memory.observations_count) + EXCLUDED.avg_pnl) / (episodic_pattern_memory.observations_count + 1),
                last_observed_at = EXCLUDED.last_observed_at,
                metadata = EXCLUDED.metadata
        """
        connection.execute(text(sql), {
            "sym": symbol.upper(),
            "reg": regime.upper(),
            "pat": pattern_name,
            "succ": 1 if was_success else 0,
            "fail": 0 if was_success else 1,
            "pnl": outcome_pnl,
            "now_dt": now_dt,
            "meta": meta_val,
        })
    else:
        # SQLite upsert
        row = connection.execute(text("""
            SELECT id, observations_count, success_count, fail_count, avg_pnl
            FROM episodic_pattern_memory
            WHERE symbol = :sym AND regime = :reg AND pattern_name = :pat
        """), {"sym": symbol.upper(), "reg": regime.upper(), "pat": pattern_name}).mappings().first()

        if row:
            n = row["observations_count"] + 1
            succ = row["success_count"] + (1 if was_success else 0)
            fail = row["fail_count"] + (0 if was_success else 1)
            avg = ((row["avg_pnl"] * row["observations_count"]) + outcome_pnl) / n
            connection.execute(text("""
                UPDATE episodic_pattern_memory
                SET observations_count = :n, success_count = :succ, fail_count = :fail,
                    avg_pnl = :avg, last_observed_at = :now_dt, metadata = :meta
                WHERE id = :id
            """), {"n": n, "succ": succ, "fail": fail, "avg": avg, "now_dt": now_dt, "meta": meta_val, "id": row["id"]})
        else:
            connection.execute(text("""
                INSERT INTO episodic_pattern_memory (
                    symbol, regime, pattern_name, observations_count, success_count, fail_count,
                    avg_pnl, last_observed_at, metadata
                ) VALUES (
                    :sym, :reg, :pat, 1, :succ, :fail, :pnl, :now_dt, :meta
                )
            """), {
                "sym": symbol.upper(),
                "reg": regime.upper(),
                "pat": pattern_name,
                "succ": 1 if was_success else 0,
                "fail": 0 if was_success else 1,
                "pnl": outcome_pnl,
                "now_dt": now_dt,
                "meta": meta_val,
            })

    return {
        "status": "RECORDED",
        "symbol": symbol.upper(),
        "regime": regime.upper(),
        "pattern_name": pattern_name,
        "outcome_pnl": outcome_pnl,
        "was_success": was_success,
    }


def get_episodic_prior(connection, symbol: str, regime: str) -> Dict[str, Any]:
    """Calculate the historical episodic prior probability factor for a symbol under a regime.

    Returns:
    - total_observations: Sample count
    - win_rate: Historical win rate
    - avg_pnl: Average P&L per trade
    - prior_factor: Multiplier for policy edge confidence:
        - 1.15 if win_rate >= 60% with N >= 5
        - 0.85 if win_rate <= 40% with N >= 5
        - 1.00 neutral baseline
    """
    ensure_intelligence_schema(connection)
    rows = connection.execute(text("""
        SELECT observations_count, success_count, fail_count, avg_pnl
        FROM episodic_pattern_memory
        WHERE symbol = :sym AND regime = :reg
    """), {"sym": symbol.upper(), "reg": regime.upper()}).mappings().all()

    if not rows:
        return {
            "symbol": symbol.upper(),
            "regime": regime.upper(),
            "total_observations": 0,
            "win_rate": 0.50,
            "avg_pnl": 0.0,
            "prior_factor": 1.0,
            "prior_label": "NEUTRAL_UNOBSERVED",
        }

    total_obs = sum(r["observations_count"] for r in rows)
    total_succ = sum(r["success_count"] for r in rows)
    avg_pnl = sum(float(r["avg_pnl"]) * r["observations_count"] for r in rows) / max(1, total_obs)
    win_rate = (total_succ / total_obs) if total_obs > 0 else 0.50

    if total_obs >= 5 and win_rate >= 0.60:
        factor = 1.15
        label = "HIGH_CONVICTION_EDGE"
    elif total_obs >= 5 and win_rate <= 0.40:
        factor = 0.85
        label = "HISTORICAL_DRAG_PENALTY"
    else:
        factor = 1.00
        label = "BALANCED_BASE"

    return {
        "symbol": symbol.upper(),
        "regime": regime.upper(),
        "total_observations": total_obs,
        "win_rate": round(win_rate, 4),
        "avg_pnl": round(avg_pnl, 2),
        "prior_factor": factor,
        "prior_label": label,
    }


def get_top_episodic_patterns(connection, symbol: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Retrieve recurring episodic patterns ordered by sample count and performance."""
    ensure_intelligence_schema(connection)
    where_clause = ""
    params: Dict[str, Any] = {"limit": limit}
    if symbol:
        where_clause = "WHERE symbol = :sym"
        params["sym"] = symbol.upper()

    rows = connection.execute(text(f"""
        SELECT symbol, regime, pattern_name, observations_count, success_count, fail_count,
               avg_pnl, last_observed_at
        FROM episodic_pattern_memory
        {where_clause}
        ORDER BY observations_count DESC, avg_pnl DESC
        LIMIT :limit
    """), params).mappings().all()

    return [dict(r) for r in rows]
