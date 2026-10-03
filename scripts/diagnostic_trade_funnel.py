#!/usr/bin/env python3
"""
Trade Funnel Diagnostic — Read-Only Investigation
===================================================
Why are few or no trades executing in the last 3-4 trading days?

This script performs a **read-only** analysis of:
1. Daily candidate rejection funnel breakdown (per-day, not aggregated)
2. Day classification: ZERO_CANDIDATES / ALL_REJECTED / SOME_EXECUTED
3. SAVED_LOSS vs MISSED_WIN analysis for rejected candidates
4. Execution-layer bleed detection (accepted=TRUE with no shadow execution)
5. Produces a per-day diagnostic report

Usage:
    # From project root (DATABASE_URL must be set in environment or .env):
    python3 scripts/diagnostic_trade_funnel.py

    # Or specify explicitly:
    DATABASE_URL="postgresql+psycopg2://user:pass@host/db" python3 scripts/diagnostic_trade_funnel.py
"""

import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from sqlalchemy import create_engine, text
except ImportError:
    print("ERROR: sqlalchemy is required. Install with: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)


def _get_database_url():
    """Resolve DATABASE_URL from environment or .env file."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        try:
            from backend.config import DATABASE_URL
            url = DATABASE_URL
        except Exception:
            pass
    if not url:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip().strip("'\"")
                    break
    return url


def _fmt_pct(num, denom):
    if denom == 0:
        return "N/A"
    return f"{num / denom * 100:.1f}%"


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def run_diagnostic():
    db_url = _get_database_url()
    if not db_url:
        print("=" * 72)
        print("ERROR: DATABASE_URL is not configured.")
        print("This diagnostic requires a PostgreSQL connection to the production DB.")
        print("")
        print("Set it via:")
        print("  export DATABASE_URL='postgresql+psycopg2://user:pass@host:port/dbname'")
        print("  python3 scripts/diagnostic_trade_funnel.py")
        print("=" * 72)
        sys.exit(1)

    engine = create_engine(db_url, pool_pre_ping=True, pool_size=2, max_overflow=0)

    print("=" * 72)
    print("TRADE FUNNEL DIAGNOSTIC — READ-ONLY INVESTIGATION")
    print(f"Ran at: {datetime.now().isoformat()}")
    print("=" * 72)

    with engine.connect() as conn:
        # ─────────────────────────────────────────────────────────────
        # STEP 0: Determine the last 4 trading days with any data
        # ─────────────────────────────────────────────────────────────
        days_result = conn.execute(text("""
            SELECT DISTINCT (observed_at AT TIME ZONE 'Asia/Kolkata')::date AS trading_day
            FROM trade_candidate_audits
            ORDER BY trading_day DESC
            LIMIT 10
        """)).fetchall()

        if not days_result:
            print("\n⚠️  ZERO rows in trade_candidate_audits — no candidates have EVER been generated.")
            print("   The screener/market_intel pipeline is likely not running at all.")
            print("   Check: Celery beat schedule, shadow_sessions task, market data connectivity.\n")
            return

        # Take the last 4 trading days
        trading_days = [str(r[0]) for r in days_result[:4]]
        all_days_raw = [str(r[0]) for r in days_result]
        print(f"\n📅 Last 10 trading days with audit data: {', '.join(all_days_raw)}")
        print(f"📅 Analyzing the last {len(trading_days)} days: {', '.join(trading_days)}")

        # ─────────────────────────────────────────────────────────────
        # STEP 1: Daily candidate rejection funnel (per-day breakdown)
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 1: DAILY CANDIDATE REJECTION FUNNEL")
        print("=" * 72)

        day_classifications = {}  # day -> classification

        for day in trading_days:
            print(f"\n{'─' * 60}")
            print(f"📆 Day: {day}")
            print(f"{'─' * 60}")

            # Total candidates
            total = conn.execute(text("""
                SELECT COUNT(*) FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day
            """), {"day": day}).scalar()

            accepted = conn.execute(text("""
                SELECT COUNT(*) FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day AND accepted = TRUE
            """), {"day": day}).scalar()

            rejected = total - accepted
            print(f"  Total candidates:  {total}")
            print(f"  Accepted:          {accepted} ({_fmt_pct(accepted, total)})")
            print(f"  Rejected:          {rejected} ({_fmt_pct(rejected, total)})")

            # Classification
            if total == 0:
                classification = "ZERO_CANDIDATES"
            elif accepted == 0:
                classification = "ALL_REJECTED"
            else:
                classification = "SOME_EXECUTED"
            day_classifications[day] = classification
            print(f"  Classification:    🏷️  {classification}")

            # Rejection funnel by stage + reason
            funnel = conn.execute(text("""
                SELECT COALESCE(selector_stage, 'unknown') AS stage,
                       COALESCE(rejection_reason, '(no reason logged)') AS reason,
                       COUNT(*) AS cnt
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day AND NOT accepted
                GROUP BY stage, reason
                ORDER BY cnt DESC
            """), {"day": day}).fetchall()

            if funnel:
                print(f"\n  Rejection funnel:")
                print(f"  {'Stage':<30} {'Reason':<50} {'Count':>6}")
                print(f"  {'─' * 30} {'─' * 50} {'─' * 6}")
                for row in funnel:
                    reason_text = str(row[1])[:50]
                    print(f"  {str(row[0]):<30} {reason_text:<50} {row[2]:>6}")

            # Rejection by stage only (summary)
            stage_summary = conn.execute(text("""
                SELECT COALESCE(selector_stage, 'unknown') AS stage, COUNT(*) AS cnt
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day AND NOT accepted
                GROUP BY stage
                ORDER BY cnt DESC
            """), {"day": day}).fetchall()

            if stage_summary:
                print(f"\n  Stage summary (rejected):")
                for row in stage_summary:
                    bar = "█" * min(50, int(row[1] / max(1, rejected) * 50))
                    print(f"    {str(row[0]):<30} {row[1]:>5} {bar}")

            # Hourly distribution
            hourly = conn.execute(text("""
                SELECT TO_CHAR(observed_at AT TIME ZONE 'Asia/Kolkata', 'HH24:00') AS hour_bucket,
                       COUNT(*) AS total,
                       COUNT(*) FILTER(WHERE accepted) AS accepted,
                       COUNT(*) FILTER(WHERE NOT accepted) AS rejected
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day
                GROUP BY hour_bucket ORDER BY hour_bucket
            """), {"day": day}).fetchall()

            if hourly:
                print(f"\n  Hourly breakdown:")
                print(f"  {'Hour':<8} {'Total':>6} {'Accepted':>9} {'Rejected':>9}")
                for row in hourly:
                    print(f"  {str(row[0]):<8} {row[1]:>6} {row[2]:>9} {row[3]:>9}")

            # Top symbols generating candidates
            top_symbols = conn.execute(text("""
                SELECT symbol, COUNT(*) AS cnt,
                       COUNT(*) FILTER(WHERE accepted) AS accepted_cnt,
                       COUNT(*) FILTER(WHERE NOT accepted) AS rejected_cnt
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day
                GROUP BY symbol ORDER BY cnt DESC LIMIT 10
            """), {"day": day}).fetchall()

            if top_symbols:
                print(f"\n  Top 10 symbols (by candidate volume):")
                print(f"  {'Symbol':<20} {'Total':>6} {'Accept':>7} {'Reject':>7}")
                for row in top_symbols:
                    print(f"  {str(row[0]):<20} {row[1]:>6} {row[2]:>7} {row[3]:>7}")

        # ─────────────────────────────────────────────────────────────
        # STEP 2: Day Classification Summary
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 2: DAY CLASSIFICATION SUMMARY")
        print("=" * 72)

        for day, cls in day_classifications.items():
            emoji = {"ZERO_CANDIDATES": "🔴", "ALL_REJECTED": "🟡", "SOME_EXECUTED": "🟢"}.get(cls, "⚪")
            print(f"  {emoji} {day}: {cls}")

        zero_days = [d for d, c in day_classifications.items() if c == "ZERO_CANDIDATES"]
        all_rej_days = [d for d, c in day_classifications.items() if c == "ALL_REJECTED"]
        exec_days = [d for d, c in day_classifications.items() if c == "SOME_EXECUTED"]

        if zero_days:
            print(f"\n  🔴 ZERO_CANDIDATES days ({len(zero_days)}): {', '.join(zero_days)}")
            print("     → The screener pipeline produced no candidates. Check market_intel, bar ingestion.")
        if all_rej_days:
            print(f"\n  🟡 ALL_REJECTED days ({len(all_rej_days)}): {', '.join(all_rej_days)}")
            print("     → Candidates were generated but ALL failed gate checks. Review gate strictness.")
        if exec_days:
            print(f"\n  🟢 SOME_EXECUTED days ({len(exec_days)}): {', '.join(exec_days)}")
            print("     → At least some candidates passed. System is functioning for these days.")

        # ─────────────────────────────────────────────────────────────
        # STEP 3: SAVED_LOSS vs MISSED_WIN for ALL_REJECTED days
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 3: COUNTERFACTUAL ANALYSIS (SAVED_LOSS vs MISSED_WIN)")
        print("=" * 72)

        # Check if counterfactual tables exist
        cf_tables = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('counterfactual_candidate_log', 'counterfactual_daily_audits',
                                 'senior_market_opportunities')
        """)).fetchall()
        cf_table_names = {r[0] for r in cf_tables}
        print(f"  Available counterfactual tables: {cf_table_names or '(none)'}")

        for day in all_rej_days:
            print(f"\n  📆 Counterfactual for {day}:")

            if "senior_market_opportunities" in cf_table_names:
                try:
                    cf = conn.execute(text("""
                        SELECT COALESCE(outcome_label, 'unknown') AS outcome,
                               COUNT(*) AS cnt,
                               AVG(CASE WHEN counterfactual_30m IS NOT NULL
                                   THEN counterfactual_30m ELSE NULL END) AS avg_30m_move
                        FROM senior_market_opportunities
                        WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day
                        GROUP BY outcome ORDER BY cnt DESC
                    """), {"day": day}).fetchall()

                    if cf:
                        print(f"    Outcome distribution (senior_market_opportunities):")
                        for row in cf:
                            avg_move = f"{float(row[2]):.2f}%" if row[2] is not None else "N/A"
                            print(f"      {str(row[0]):<20} count={row[1]:>4}  avg_30m_move={avg_move}")
                            if str(row[0]).lower() in ("win", "would_have_won", "missed_win"):
                                print(f"      ⚠️  These were MISSED WINS — the gate was too strict here.")
                    else:
                        print(f"    No counterfactual data available for this day.")
                except Exception as e:
                    print(f"    Error querying senior_market_opportunities: {e}")
            else:
                print(f"    senior_market_opportunities table not found — skipping counterfactual analysis.")

        # ─────────────────────────────────────────────────────────────
        # STEP 4: Execution-Layer Bleed Detection
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 4: EXECUTION-LAYER BLEED DETECTION")
        print("=" * 72)
        print("  Looking for accepted=TRUE candidates with no matching shadow execution audit...\n")

        sea_exists = "shadow_execution_audits" in {
            r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name='shadow_execution_audits'"
            )).fetchall()
        }

        if not sea_exists:
            print("  ⚠️  shadow_execution_audits table does not exist — cannot check bleed.")
        else:
            for day in trading_days:
                accepted_candidates = conn.execute(text("""
                    SELECT tca.id, tca.symbol, tca.observed_at, tca.chart_strategy, tca.signal
                    FROM trade_candidate_audits tca
                    WHERE (tca.observed_at AT TIME ZONE 'Asia/Kolkata')::date = :day
                      AND tca.accepted = TRUE
                    ORDER BY tca.observed_at
                """), {"day": day}).fetchall()

                if not accepted_candidates:
                    print(f"  📆 {day}: No accepted candidates → no bleed possible.")
                    continue

                # Find shadow executions for this day
                shadow_count = conn.execute(text("""
                    SELECT COUNT(*) FROM shadow_execution_audits
                    WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date = :day
                """), {"day": day}).scalar()

                print(f"  📆 {day}: {len(accepted_candidates)} accepted candidates, {shadow_count} shadow executions")

                if len(accepted_candidates) > shadow_count:
                    bleed_count = len(accepted_candidates) - shadow_count
                    print(f"    ⚠️  BLEED DETECTED: {bleed_count} accepted candidates have no matching shadow execution!")
                    print(f"    Accepted candidates without execution:")
                    for cand in accepted_candidates[:5]:
                        # Check if there's a matching shadow execution
                        matching = conn.execute(text("""
                            SELECT COUNT(*) FROM shadow_execution_audits sea
                            JOIN instrument_master im ON im.id = sea.instrument_id
                            WHERE im.symbol = :symbol
                              AND ABS(EXTRACT(EPOCH FROM sea.signal_at - :obs_at)) < 120
                        """), {"symbol": cand[1], "obs_at": cand[2]}).scalar()
                        if matching == 0:
                            print(f"      🔍 id={cand[0]}, symbol={cand[1]}, time={cand[2]}, strategy={cand[3]}")
                else:
                    print(f"    ✅ No bleed detected — all accepted candidates have shadow executions.")

        # ─────────────────────────────────────────────────────────────
        # STEP 5: Gate Strictness Analysis
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 5: GATE STRICTNESS ANALYSIS")
        print("=" * 72)

        # Aggregate rejection reasons across all analyzed days
        agg_reasons = conn.execute(text("""
            SELECT COALESCE(rejection_reason, '(accepted)') AS reason,
                   COUNT(*) AS cnt
            FROM trade_candidate_audits
            WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date::text = ANY(:days)
              AND NOT accepted
            GROUP BY reason
            ORDER BY cnt DESC
            LIMIT 20
        """), {"days": trading_days}).fetchall()

        total_rejected_all = sum(r[1] for r in agg_reasons)
        total_all = conn.execute(text("""
            SELECT COUNT(*) FROM trade_candidate_audits
            WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date::text = ANY(:days)
        """), {"days": trading_days}).scalar()

        print(f"\n  Aggregate across {len(trading_days)} days: {total_all} total candidates, {total_rejected_all} rejected")
        print(f"\n  {'Rejection Reason':<55} {'Count':>6} {'%':>7}")
        print(f"  {'─' * 55} {'─' * 6} {'─' * 7}")
        for row in agg_reasons:
            pct = f"{row[1] / max(1, total_rejected_all) * 100:.1f}%"
            print(f"  {str(row[0])[:55]:<55} {row[1]:>6} {pct:>7}")

        # Identify the #1 bottleneck
        if agg_reasons:
            top_reason = agg_reasons[0]
            print(f"\n  🔑 TOP BOTTLENECK: '{top_reason[0]}' is responsible for {_fmt_pct(top_reason[1], total_rejected_all)} of all rejections.")

        # ─────────────────────────────────────────────────────────────
        # STEP 6: Near-Miss Analysis (high-quality rejections)
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 6: NEAR-MISS ANALYSIS (high probability + high quality but rejected)")
        print("=" * 72)

        near_misses = conn.execute(text("""
            SELECT observed_at, symbol, probability, quality_score, rr, selector_stage,
                   rejection_reason, chart_strategy
            FROM trade_candidate_audits
            WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date::text = ANY(:days)
              AND NOT accepted
              AND probability >= 0.60
              AND quality_score >= 50
            ORDER BY probability DESC, quality_score DESC
            LIMIT 15
        """), {"days": trading_days}).fetchall()

        if near_misses:
            print(f"  Found {len(near_misses)} near-misses (prob≥0.60, quality≥50 but rejected):\n")
            print(f"  {'Date':<12} {'Symbol':<12} {'Prob':>5} {'Q':>5} {'R:R':>5} {'Stage':<25} {'Reason'}")
            print(f"  {'─' * 12} {'─' * 12} {'─' * 5} {'─' * 5} {'─' * 5} {'─' * 25} {'─' * 30}")
            for row in near_misses:
                d = str(row[0])[:10] if row[0] else "?"
                prob = f"{float(row[2]):.2f}" if row[2] else "?"
                qs = f"{float(row[3]):.0f}" if row[3] else "?"
                rr = f"{float(row[4]):.1f}" if row[4] else "?"
                print(f"  {d:<12} {str(row[1]):<12} {prob:>5} {qs:>5} {rr:>5} {str(row[5]):<25} {str(row[6])[:40]}")
        else:
            print("  No near-misses found — candidates may not be reaching sufficient probability/quality.")

        # ─────────────────────────────────────────────────────────────
        # STEP 7: Summary & Recommendations
        # ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 72)
        print("STEP 7: DIAGNOSTIC SUMMARY & RECOMMENDATIONS")
        print("=" * 72)

        print("\n  Day-level Summary:")
        for day, cls in day_classifications.items():
            print(f"    {day}: {cls}")

        if zero_days:
            print("\n  📋 For ZERO_CANDIDATES days:")
            print("    → Verify market_intel/screener task is scheduled and running.")
            print("    → Check live_market_bars for gaps on those dates.")
            print("    → Confirm Celery beat + worker processes are alive.")
            print("    → Check if those were NSE holidays (2026 holiday calendar).")

        if all_rej_days:
            print("\n  📋 For ALL_REJECTED days:")
            if agg_reasons:
                top = agg_reasons[0][0]
                if "chart_gate" in str(top):
                    print("    → The chart gate is the primary filter. Review chart_gate thresholds.")
                    print("    → Possibly: min R:R too high, local direction mismatch, or stop-loss too tight.")
                elif "quality" in str(top).lower():
                    print("    → Quality score gate is too strict. Consider lowering min_entry_quality.")
                elif "no_model_signal" in str(top):
                    print("    → ML model is producing 0 (neutral) signals. Check model predictions, class balance, or signal=0 threshold.")
                elif "nifty" in str(top).lower() or "macro" in str(top).lower():
                    print("    → Nifty macro directional veto is blocking trades. Market was trending against EQ entries.")
                    print("    → This is protective behavior — may be correct.")
                elif "morning_cooloff" in str(top):
                    print("    → Morning cool-off gate is active. Candidates generated too early before cutoff.")
                elif "sentiment" in str(top).lower():
                    print("    → Sentiment gate is blocking. Check news/sentiment pipeline health.")
                elif "market_quality" in str(top).lower():
                    print("    → Market quality (spread/depth/ATR) is failing. Check if data is fresh.")
                elif "stale" in str(top).lower() or "freshness" in str(top).lower():
                    print("    → Signal freshness gate is rejecting stale signals. Reduce max_signal_age_minutes?")
                elif "adaptive" in str(top).lower() or "trap" in str(top).lower():
                    print("    → Adaptive trap gate is active. Check if ASI/liquidity sweep is too sensitive.")
                else:
                    print(f"    → Top rejection reason: '{top}' — investigate gate implementation.")

        if exec_days:
            print("\n  📋 For SOME_EXECUTED days:")
            print("    → System is working as expected on these days.")
            print("    → Monitor shadow execution outcomes to validate gate quality.")

        print("\n" + "=" * 72)
        print("DIAGNOSTIC COMPLETE")
        print("=" * 72)


if __name__ == "__main__":
    run_diagnostic()
