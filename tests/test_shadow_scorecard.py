"""Tests for Shadow-Trading Scorecard Engine (Phase 6a).

Evaluates all metrics, attributions, cost realism, layer vetoes, and promotion gates
against an isolated SQLite database fixture with exact hand-calculated numbers.
Zero dependencies on external staging DB. Runnable via standard python -m unittest.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from scripts.shadow_scorecard import (
    ShadowTrade,
    calculate_trade_r,
    compute_cost_realism,
    compute_exit_attribution,
    compute_layer_attribution,
    compute_metrics,
    evaluate_promotion_gates,
    generate_full_scorecard,
    load_shadow_trades_from_db,
)


class TestShadowScorecard(unittest.TestCase):
    def setUp(self):
        """Create a fresh in-memory SQLite database with required tables for each test."""
        self.engine = create_engine("sqlite:///:memory:", echo=False)
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE instrument_master (
                    id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    instrument_type TEXT NOT NULL
                );
            """))
            conn.execute(text("""
                CREATE TABLE shadow_execution_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_version TEXT NOT NULL,
                    instrument_id INTEGER NOT NULL,
                    signal_at TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    signal_probability REAL,
                    decision_price REAL NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    bid_quantity INTEGER,
                    ask_quantity INTEGER,
                    theoretical_fill_price REAL NOT NULL,
                    one_tick_penalty REAL,
                    estimated_fees REAL NOT NULL,
                    realised_exit_price REAL,
                    net_pnl REAL,
                    cost_reconciled INTEGER DEFAULT 0,
                    instrument_execution_verified INTEGER DEFAULT 0,
                    audit_status TEXT DEFAULT 'RECONCILED',
                    rejection_reason TEXT,
                    created_at TEXT,
                    stop_loss_price REAL,
                    take_profit_price REAL,
                    exit_at TEXT,
                    exit_reason TEXT,
                    fill_source TEXT DEFAULT 'DEPTH_OR_PENDING',
                    mistake_tags TEXT DEFAULT '[]',
                    improvement_note TEXT,
                    trade_mode TEXT DEFAULT 'INTRADAY',
                    reasoning_chain TEXT,
                    holding_days INTEGER DEFAULT 0,
                    max_holding_days INTEGER DEFAULT 5,
                    spread_partner_id INTEGER
                );
            """))
            conn.execute(text("""
                CREATE TABLE trade_candidate_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    model_version TEXT,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_id INTEGER,
                    instrument_type TEXT,
                    signal INTEGER DEFAULT 0,
                    probability REAL,
                    decision_price REAL,
                    selector_stage TEXT NOT NULL,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    rejection_reason TEXT,
                    chart_strategy TEXT,
                    route TEXT,
                    rr REAL,
                    expected_net_edge_bps REAL,
                    quality_score REAL,
                    selector_score REAL,
                    sector TEXT,
                    details TEXT DEFAULT '{}',
                    created_at TEXT,
                    trade_mode TEXT DEFAULT 'INTRADAY',
                    agent_deliberation TEXT
                );
            """))
            conn.execute(text("""
                CREATE TABLE counterfactual_daily_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_date TEXT NOT NULL UNIQUE,
                    total_rejected INTEGER DEFAULT 0,
                    saved_losses INTEGER DEFAULT 0,
                    missed_wins INTEGER DEFAULT 0,
                    neutral_count INTEGER DEFAULT 0,
                    gate_accuracy_pct REAL NOT NULL DEFAULT 100.0,
                    estimated_capital_saved REAL DEFAULT 0.0,
                    gate_breakdown TEXT DEFAULT '{}',
                    details TEXT DEFAULT '{}',
                    created_at TEXT,
                    updated_at TEXT
                );
            """))
            conn.execute(text("""
                CREATE TABLE counterfactual_candidate_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_audit_id INTEGER NOT NULL UNIQUE,
                    observed_at TEXT NOT NULL,
                    model_version TEXT,
                    exchange TEXT,
                    symbol TEXT NOT NULL,
                    instrument_id INTEGER,
                    instrument_type TEXT,
                    signal INTEGER DEFAULT 0,
                    route TEXT,
                    strategy TEXT,
                    selector_stage TEXT,
                    rejection_reason TEXT,
                    probability REAL,
                    rr REAL,
                    quality_score REAL,
                    accepted INTEGER DEFAULT 0,
                    outcome_5m REAL,
                    outcome_15m REAL,
                    outcome_30m REAL,
                    outcome_label TEXT,
                    details TEXT DEFAULT '{}',
                    created_at TEXT,
                    updated_at TEXT
                );
            """))
            conn.execute(text("""
                CREATE TABLE shadow_session_daily_log (
                    session_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    valid INTEGER DEFAULT 1,
                    warnings TEXT DEFAULT '[]',
                    mistakes TEXT DEFAULT '{}'
                );
            """))

    def test_calculate_trade_r_math(self):
        """Verify net R and gross R calculations by hand."""
        net_r, gross_r, risk_rs = calculate_trade_r(
            side="BUY",
            entry_price=100.0,
            exit_price=110.0,
            stop_loss_price=95.0,
            quantity=100,
            net_pnl=950.0,
            estimated_fees=50.0,
        )
        self.assertEqual(risk_rs, 500.0)
        self.assertEqual(gross_r, 2.0)
        self.assertEqual(net_r, 1.9)

        net_r, gross_r, risk_rs = calculate_trade_r(
            side="SELL",
            entry_price=200.0,
            exit_price=210.0,
            stop_loss_price=210.0,
            quantity=50,
            net_pnl=-550.0,
            estimated_fees=50.0,
        )
        self.assertEqual(risk_rs, 500.0)
        self.assertEqual(gross_r, -1.0)
        self.assertEqual(net_r, -1.1)

    def test_hand_calculated_metrics_on_known_trades(self):
        """Verify win rate, payoff, profit factor, max drawdown, streaks on exact hand-calculated trades."""
        dt1 = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc)

        # 6 trades: [+2.0, -1.0, +1.0, +2.0, -1.0, 0.0]
        trades = [
            ShadowTrade(
                id=1, symbol="INFY", side="BUY", trade_mode="INTRADAY",
                signal_at=dt1, exit_at=dt1 + timedelta(minutes=60), quantity=100,
                decision_price=100.0, theoretical_fill_price=100.0, realised_exit_price=110.0,
                stop_loss_price=95.0, take_profit_price=110.0, estimated_fees=0.0, net_pnl=1000.0,
                exit_reason="TAKE_PROFIT", net_r=2.0, gross_r=2.0, risk_rupees=500.0,
                hold_time_minutes=60.0, trade_date="2026-06-01",
            ),
            ShadowTrade(
                id=2, symbol="TCS", side="BUY", trade_mode="INTRADAY",
                signal_at=dt1 + timedelta(hours=1), exit_at=dt1 + timedelta(hours=2), quantity=100,
                decision_price=100.0, theoretical_fill_price=100.0, realised_exit_price=95.0,
                stop_loss_price=95.0, take_profit_price=110.0, estimated_fees=0.0, net_pnl=-500.0,
                exit_reason="STOP_LOSS", net_r=-1.0, gross_r=-1.0, risk_rupees=500.0,
                hold_time_minutes=60.0, trade_date="2026-06-01",
            ),
            ShadowTrade(
                id=3, symbol="WIPRO", side="SELL", trade_mode="INTRADAY",
                signal_at=dt1 + timedelta(hours=3), exit_at=dt1 + timedelta(hours=4), quantity=50,
                decision_price=200.0, theoretical_fill_price=200.0, realised_exit_price=190.0,
                stop_loss_price=210.0, take_profit_price=180.0, estimated_fees=0.0, net_pnl=500.0,
                exit_reason="TAKE_PROFIT", net_r=1.0, gross_r=1.0, risk_rupees=500.0,
                hold_time_minutes=60.0, trade_date="2026-06-01",
            ),
            ShadowTrade(
                id=4, symbol="RELIANCE", side="BUY", trade_mode="SWING",
                signal_at=dt2, exit_at=dt2 + timedelta(days=2), quantity=20,
                decision_price=500.0, theoretical_fill_price=500.0, realised_exit_price=550.0,
                stop_loss_price=475.0, take_profit_price=550.0, estimated_fees=0.0, net_pnl=1000.0,
                exit_reason="TAKE_PROFIT", net_r=2.0, gross_r=2.0, risk_rupees=500.0,
                hold_time_minutes=2880.0, trade_date="2026-06-02",
            ),
            ShadowTrade(
                id=5, symbol="HDFCBANK", side="SELL", trade_mode="SWING",
                signal_at=dt2 + timedelta(hours=1), exit_at=dt2 + timedelta(days=1), quantity=20,
                decision_price=500.0, theoretical_fill_price=500.0, realised_exit_price=525.0,
                stop_loss_price=525.0, take_profit_price=450.0, estimated_fees=0.0, net_pnl=-500.0,
                exit_reason="STOP_LOSS", net_r=-1.0, gross_r=-1.0, risk_rupees=500.0,
                hold_time_minutes=1440.0, trade_date="2026-06-02",
            ),
            ShadowTrade(
                id=6, symbol="SBIN", side="BUY", trade_mode="INTRADAY",
                signal_at=dt2 + timedelta(hours=2), exit_at=dt2 + timedelta(hours=3), quantity=100,
                decision_price=100.0, theoretical_fill_price=100.0, realised_exit_price=100.0,
                stop_loss_price=95.0, take_profit_price=110.0, estimated_fees=0.0, net_pnl=0.0,
                exit_reason="BREAKEVEN_STOP", net_r=0.0, gross_r=0.0, risk_rupees=500.0,
                hold_time_minutes=60.0, trade_date="2026-06-02",
            ),
        ]

        m = compute_metrics(trades, min_data_threshold=5)
        self.assertFalse(m.insufficient_data)
        self.assertEqual(m.trade_count, 6)
        self.assertEqual(m.sessions_covered, 2)
        self.assertEqual(m.win_rate_pct, 50.0)
        self.assertEqual(m.avg_win_r, 1.6667)
        self.assertEqual(m.avg_loss_r, -1.0)
        self.assertEqual(m.payoff_ratio, 1.67)
        self.assertEqual(m.profit_factor, 2.50)
        self.assertEqual(m.expectancy_net_r, 0.5000)
        self.assertEqual(m.max_drawdown_r, 1.0000)
        self.assertEqual(m.longest_losing_streak, 2)
        self.assertEqual(m.avg_hold_time_minutes, 760.0)
        self.assertLessEqual(m.expectancy_ci_95[0], m.expectancy_ci_95[1])

    def test_zero_trades_case(self):
        """Verify that empty trades return INSUFFICIENT DATA (n=0) and expectancy gate shows NO DATA."""
        m = compute_metrics([], min_data_threshold=5)
        self.assertTrue(m.insufficient_data)
        self.assertEqual(m.insufficient_reason, "INSUFFICIENT DATA (n=0)")
        self.assertEqual(m.trade_count, 0)

        gates = evaluate_promotion_gates(m, max_dd_limit=5.0)
        self.assertFalse(gates["all_gates_passed"])
        self.assertIn("short by 300 trades", gates["gates"]["min_trades"]["message"])
        # Expectancy gate must show "NO DATA"
        self.assertEqual(gates["gates"]["expectancy_ci_positive"]["message"], "NO DATA")
        self.assertEqual(gates["gates"]["expectancy_ci_positive"]["actual"], "NO DATA")

    def test_exit_attribution(self):
        """Verify exit attribution groups by reason correctly."""
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = [
            ShadowTrade(1, "S1", "BUY", "INTRADAY", dt, dt, 10, 100.0, 100.0, 110.0, 95.0, 110.0, 0, 100.0, "TAKE_PROFIT", net_r=2.0),
            ShadowTrade(2, "S2", "BUY", "INTRADAY", dt, dt, 10, 100.0, 100.0, 110.0, 95.0, 110.0, 0, 100.0, "TAKE_PROFIT", net_r=2.0),
            ShadowTrade(3, "S3", "BUY", "INTRADAY", dt, dt, 10, 100.0, 100.0, 95.0, 95.0, 110.0, 0, -50.0, "STOP_LOSS", net_r=-1.0),
        ]
        attr = compute_exit_attribution(trades)
        self.assertIn("TAKE_PROFIT", attr)
        self.assertEqual(attr["TAKE_PROFIT"]["trade_count"], 2)
        self.assertEqual(attr["TAKE_PROFIT"]["total_net_r"], 4.0)
        self.assertEqual(attr["TAKE_PROFIT"]["mean_net_r"], 2.0)
        self.assertIn("STOP_LOSS", attr)
        self.assertEqual(attr["STOP_LOSS"]["trade_count"], 1)
        self.assertEqual(attr["STOP_LOSS"]["total_net_r"], -1.0)

    def test_cost_realism_slippage(self):
        """Verify assumed versus realised slippage calculations."""
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = [
            ShadowTrade(1, "S1", "BUY", "INTRADAY", dt, dt, 100, decision_price=100.0, theoretical_fill_price=100.20,
                        realised_exit_price=105.0, stop_loss_price=95.0, take_profit_price=110.0, estimated_fees=0,
                        net_pnl=500.0, exit_reason="TAKE_PROFIT", one_tick_penalty=0.10, best_ask=100.25),
            ShadowTrade(2, "S2", "SELL", "INTRADAY", dt, dt, 100, decision_price=200.0, theoretical_fill_price=199.80,
                        realised_exit_price=190.0, stop_loss_price=210.0, take_profit_price=180.0, estimated_fees=0,
                        net_pnl=500.0, exit_reason="TAKE_PROFIT", one_tick_penalty=0.10, best_bid=199.70),
        ]
        cr = compute_cost_realism(trades)
        self.assertEqual(cr["fills_analyzed"], 2)
        self.assertEqual(cr["avg_assumed_slippage_points"], 0.10)
        self.assertEqual(cr["avg_realised_slippage_points"], 0.20)
        self.assertEqual(cr["slippage_drag_points"], 0.10)

    def test_layer_attribution_fidelity_pass_and_fail(self):
        """Test layer veto attribution with passing vs failing fidelity check."""
        since = date(2026, 6, 1)

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trade_candidate_audits (id, observed_at, exchange, symbol, selector_stage, rejection_reason, accepted, trade_mode)
                VALUES (1, '2026-06-01 09:30:00', 'NSE', 'INFY', 'chart_gate', 'SWING_REJECTED_STOP_CAP', 0, 'INTRADAY')
            """))
            conn.execute(text("""
                INSERT INTO trade_candidate_audits (id, observed_at, exchange, symbol, selector_stage, rejection_reason, accepted, trade_mode)
                VALUES (2, '2026-06-01 10:00:00', 'NSE', 'TCS', 'risk_brain', 'PORTFOLIO_RISK_LIMIT_EXCEEDED', 0, 'INTRADAY')
            """))
            conn.execute(text("""
                INSERT INTO trade_candidate_audits (id, observed_at, exchange, symbol, selector_stage, rejection_reason, accepted, trade_mode)
                VALUES (3, '2026-06-01 11:00:00', 'NSE', 'WIPRO', 'llm_validator', 'LLM_VETO_ADVERSE_NEWS', 0, 'INTRADAY')
            """))

        # When no counterfactual audit exists -> NOT MEASURABLE
        res_fail = compute_layer_attribution(self.engine, since)
        self.assertEqual(res_fail["status"], "NOT MEASURABLE")
        self.assertEqual(res_fail["layers"]["Chart Gate"]["counterfactual_result"], "NOT MEASURABLE")
        self.assertEqual(res_fail["layers"]["Brains"]["counterfactual_result"], "NOT MEASURABLE")
        self.assertEqual(res_fail["layers"]["LLM"]["counterfactual_result"], "NOT MEASURABLE")

        # Now insert passing counterfactual replay audit (accuracy = 85.0% >= 80%)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO counterfactual_daily_audits (audit_date, gate_accuracy_pct, total_rejected)
                VALUES ('2026-06-01', 85.0, 3)
            """))
            conn.execute(text("""
                INSERT INTO counterfactual_candidate_log (candidate_audit_id, observed_at, symbol, outcome_label)
                VALUES (1, '2026-06-01 09:30:00', 'INFY', 'SAVED_LOSS')
            """))
            conn.execute(text("""
                INSERT INTO counterfactual_candidate_log (candidate_audit_id, observed_at, symbol, outcome_label)
                VALUES (2, '2026-06-01 10:00:00', 'TCS', 'MISSED_WIN')
            """))
            conn.execute(text("""
                INSERT INTO counterfactual_candidate_log (candidate_audit_id, observed_at, symbol, outcome_label)
                VALUES (3, '2026-06-01 11:00:00', 'WIPRO', 'SAVED_LOSS')
            """))

        res_pass = compute_layer_attribution(self.engine, since)
        self.assertEqual(res_pass["status"], "PASS")
        cg = res_pass["layers"]["Chart Gate"]
        self.assertEqual(cg["counterfactual_result"], "MEASURED")
        self.assertEqual(cg["would_have_lost"], 1)

        brains = res_pass["layers"]["Brains"]
        self.assertEqual(brains["counterfactual_result"], "MEASURED")
        self.assertEqual(brains["would_have_won"], 1)

        llm = res_pass["layers"]["LLM"]
        self.assertEqual(llm["counterfactual_result"], "MEASURED")
        self.assertEqual(llm["would_have_lost"], 1)

    def test_full_scorecard_and_promotion_gates(self):
        """End-to-end integration test of full scorecard generation against SQLite fixture."""
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO instrument_master (id, symbol, exchange, instrument_type) VALUES (1, 'RELIANCE', 'NSE', 'EQ')"))
            for i in range(1, 7):
                pnl = 1000.0 if i % 2 == 1 else -500.0
                r_exit = 110.0 if i % 2 == 1 else 95.0
                conn.execute(text(f"""
                    INSERT INTO shadow_execution_audits (
                        id, model_version, instrument_id, signal_at, side, quantity,
                        decision_price, theoretical_fill_price, realised_exit_price,
                        stop_loss_price, take_profit_price, estimated_fees, net_pnl,
                        exit_reason, trade_mode, audit_status, exit_at
                    ) VALUES (
                        {i}, 'v1', 1, '2026-06-01 09:30:00', 'BUY', 100,
                        100.0, 100.0, {r_exit},
                        95.0, 110.0, 0.0, {pnl},
                        'TAKE_PROFIT', 'INTRADAY', 'RECONCILED', '2026-06-01 10:30:00'
                    )
                """))
            # Open trade (net_pnl is NULL)
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, model_version, instrument_id, signal_at, side, quantity,
                    decision_price, theoretical_fill_price, realised_exit_price,
                    stop_loss_price, take_profit_price, estimated_fees, net_pnl,
                    trade_mode, audit_status
                ) VALUES (
                    99, 'v1', 1, '2026-06-01 14:00:00', 'BUY', 100,
                    100.0, 100.0, NULL,
                    95.0, 110.0, 0.0, NULL,
                    'INTRADAY', 'RECONCILED'
                )
            """))

        scorecard = generate_full_scorecard(self.engine, since_date=date(2026, 6, 1), max_dd_limit=5.0)

        self.assertEqual(scorecard["total_closed_trades"], 6)
        self.assertEqual(scorecard["total_open_trades"], 1)
        self.assertEqual(len(scorecard["data_integrity"]["open_positions"]), 1)
        self.assertEqual(scorecard["data_integrity"]["open_positions"][0]["id"], 99)
        self.assertIn("database_source", scorecard)

        # Promotion Gates Check
        pg = scorecard["promotion_gates"]
        self.assertFalse(pg["all_gates_passed"])
        self.assertEqual(pg["gates"]["min_trades"]["actual"], 6)
        self.assertIn("short by 294 trades", pg["gates"]["min_trades"]["message"])
        self.assertEqual(pg["gates"]["min_sessions"]["actual"], 1)
        self.assertIn("short by 19 sessions", pg["gates"]["min_sessions"]["message"])


if __name__ == "__main__":
    unittest.main()
