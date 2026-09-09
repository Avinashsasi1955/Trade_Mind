"""High-quality shadow-trade feedback export for safer future retraining.

This module does not promote models and does not submit orders.  It creates an
auditable table of closed paper trades that are good enough to be used as
forward evidence.  The historical model can then be retrained only after this
evidence is reviewed instead of blindly learning from every noisy paper trade.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Dict

from sqlalchemy import create_engine, text

from .trade_quality import score_trade


DDL = """
CREATE TABLE IF NOT EXISTS model_quality_feedback(
    id BIGSERIAL PRIMARY KEY,
    audit_id BIGINT NOT NULL UNIQUE REFERENCES shadow_execution_audits(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id),
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    signal_at TIMESTAMPTZ NOT NULL,
    exit_at TIMESTAMPTZ,
    side TEXT NOT NULL,
    quality_score NUMERIC(8,2) NOT NULL,
    quality_grade TEXT NOT NULL,
    net_pnl NUMERIC(24,10) NOT NULL,
    outcome_label INTEGER NOT NULL CHECK(outcome_label IN (0,1)),
    components JSONB NOT NULL,
    inclusion_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_model_quality_feedback_model ON model_quality_feedback(model_version, signal_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_quality_feedback_symbol ON model_quality_feedback(exchange, symbol, signal_at DESC);
"""


def ensure_table(connection) -> None:
    for statement in [part.strip() for part in DDL.split(";") if part.strip()]:
        connection.execute(text(statement))


def export_high_quality_shadow_feedback(database_url: str, min_quality: float | None = None) -> Dict:
    threshold = float(min_quality if min_quality is not None else os.getenv("NIVESH_ML_FEEDBACK_MIN_QUALITY", "68"))
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    with engine.begin() as connection:
        ensure_table(connection)
        rows = connection.execute(text("""
            SELECT a.*, i.exchange, i.symbol, i.instrument_type
            FROM shadow_execution_audits a
            JOIN instrument_master i ON i.id=a.instrument_id
            WHERE a.audit_status='RECONCILED'
              AND a.net_pnl IS NOT NULL
              AND a.theoretical_fill_price IS NOT NULL
        """)).mappings().all()
        evaluated = included = skipped = 0
        grade_counts = {}
        for row in rows:
            evaluated += 1
            item = dict(row)
            entry = item.get("theoretical_fill_price") or item.get("decision_price")
            latest = item.get("realised_exit_price") or entry
            score = score_trade({
                **item,
                "entry_price": entry,
                "latest_price": latest,
                "marked_pnl": item.get("net_pnl"),
                "is_open": False,
            })
            quality = float(score.get("score") or 0)
            grade = str(score.get("grade") or "D")
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
            net_pnl = Decimal(str(item["net_pnl"]))
            is_loss = net_pnl < Decimal("0")
            is_win = net_pnl > Decimal("0")

            if is_loss:
                outcome_label = 0
                reason = "reconciled paper loss to train mistake avoidance"
            elif is_win and quality >= threshold:
                outcome_label = 1
                reason = f"closed paper winner with quality >= {threshold}; usable as reviewed forward feedback"
            else:
                skipped += 1
                continue

            included += 1
            connection.execute(text("""
                INSERT INTO model_quality_feedback(audit_id,model_version,instrument_id,exchange,symbol,instrument_type,
                    signal_at,exit_at,side,quality_score,quality_grade,net_pnl,outcome_label,components,inclusion_reason)
                VALUES(:audit_id,:model_version,:instrument_id,:exchange,:symbol,:instrument_type,:signal_at,:exit_at,
                    :side,:quality_score,:quality_grade,:net_pnl,:outcome_label,CAST(:components AS jsonb),:reason)
                ON CONFLICT(audit_id) DO UPDATE SET quality_score=EXCLUDED.quality_score,
                    quality_grade=EXCLUDED.quality_grade,net_pnl=EXCLUDED.net_pnl,outcome_label=EXCLUDED.outcome_label,
                    components=EXCLUDED.components,inclusion_reason=EXCLUDED.inclusion_reason,updated_at=CURRENT_TIMESTAMP
            """), {
                "audit_id": int(item["id"]),
                "model_version": item["model_version"],
                "instrument_id": int(item["instrument_id"]),
                "exchange": item["exchange"],
                "symbol": item["symbol"],
                "instrument_type": item["instrument_type"],
                "signal_at": item["signal_at"],
                "exit_at": item.get("exit_at"),
                "side": item["side"],
                "quality_score": Decimal(str(quality)),
                "quality_grade": grade,
                "net_pnl": net_pnl,
                "outcome_label": outcome_label,
                "components": json.dumps(score, default=str),
                "reason": reason,
            })
        total = connection.execute(text("SELECT COUNT(*) FROM model_quality_feedback")).scalar_one()
    engine.dispose()
    return {
        "status": "success",
        "evaluated_closed_trades": evaluated,
        "included": included,
        "skipped_low_quality": skipped,
        "min_quality": threshold,
        "grade_counts": grade_counts,
        "total_feedback_rows": int(total or 0),
        "orders_allowed": False,
    }
