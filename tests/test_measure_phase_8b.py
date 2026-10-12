import importlib
import pytest
from decimal import Decimal
from io import StringIO

measure = None

@pytest.fixture(autouse=True)
def setup_measure(monkeypatch):
    global measure
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    import scripts.measure_phase_8b as measure_mod
    importlib.reload(measure_mod)
    measure = measure_mod
    return measure_mod

def run_query(conn, sql, params=None):
    return measure.run_query(conn, sql, params)

def _setup_schema(engine):
    with engine.begin() as conn:
        run_query(conn, 'DROP TABLE IF EXISTS shadow_execution_audits')
        run_query(conn, '''
            CREATE TABLE shadow_execution_audits (
                id INTEGER PRIMARY KEY,
                rupee_risk REAL,
                gross_pnl REAL,
                estimated_fees REAL,
                net_pnl REAL,
                side TEXT,
                decision_price REAL,
                best_bid REAL,
                best_ask REAL,
                realised_exit_price REAL,
                fill_source TEXT,
                instrument_type TEXT
            );
        ''')

def test_random_entry_null_check(capsys):
    measure.random_entry_null_check()
    captured = capsys.readouterr().out.strip()
    entry_price = 101.5
    exit_price = 101.0
    qty = 10
    stop = 100.0
    rupee_risk = (entry_price - stop) * qty
    cost = measure.estimate_zerodha_costs(
        segment='EQUITY_INTRADAY',
        side='BUY',
        price=Decimal(str(entry_price)),
        quantity=qty,
    )
    fee_rupees = float(cost.total)
    net_r = ((exit_price - entry_price) * qty - fee_rupees) / rupee_risk
    expected = f"Random‑entry null – mean net R={net_r:.4f} (95% CI {net_r:.4f}-{net_r:.4f})"
    assert captured == expected

def test_slippage_market_measure(capsys):
    engine = measure.engine
    _setup_schema(engine)
    rows = [
        {
            'id': 1,
            'side': 'BUY',
            'decision_price': 100.0,
            'best_bid': 99.9,
            'best_ask': 100.1,
            'realised_exit_price': 101.0,
            'rupee_risk': 1000.0,
        },
        {
            'id': 2,
            'side': 'SELL',
            'decision_price': 200.0,
            'best_bid': 199.8,
            'best_ask': 200.2,
            'realised_exit_price': 199.0,
            'rupee_risk': 1500.0,
        },
    ]
    with engine.begin() as conn:
        for r in rows:
            run_query(conn, "INSERT INTO shadow_execution_audits (id, side, decision_price, best_bid, best_ask, realised_exit_price, rupee_risk) VALUES (:id, :side, :decision_price, :best_bid, :best_ask, :realised_exit_price, :rupee_risk)", r)
    measure.slippage_market_measure()
    out = capsys.readouterr().out.strip().splitlines()
    signed_pts = [(101.0 - 100.0) * 1, (199.0 - 200.0) * -1]
    signed_r = [signed_pts[0] / 1000.0, signed_pts[1] / 1500.0]
    n = len(signed_pts)
    avg_pts = sum(signed_pts) / n
    avg_r = sum(signed_r) / n
    quoted = 2
    assert out[0] == f"Slippage – signed pts: n={n}, mean={avg_pts:.4f} (quotes={quoted})"
    assert out[1] == f"Slippage – signed R: n={n}, mean={avg_r:.4f}"

def test_trade_674_check_bad(capsys):
    engine = measure.engine
    _setup_schema(engine)
    with engine.begin() as conn:
        run_query(conn, "INSERT INTO shadow_execution_audits (id, decision_price, best_bid, best_ask) VALUES (674, 200, 100, 101)")
    measure.check_trade_674()
    out = capsys.readouterr().out.strip()
    assert "deviates from mid‑quote" in out

def test_trade_674_check_good(capsys):
    engine = measure.engine
    _setup_schema(engine)
    with engine.begin() as conn:
        run_query(conn, "INSERT INTO shadow_execution_audits (id, decision_price, best_bid, best_ask) VALUES (674, 100.5, 100, 101)")
    measure.check_trade_674()
    out = capsys.readouterr().out.strip()
    assert "appears reasonable" in out

def test_gross_edge_by_tier(capsys):
    import random
    random.seed(0)
    engine = measure.engine
    _setup_schema(engine)
    rows = []
    for i in range(30):
        risk = (i + 1) * 10.0
        rows.append({
            'id': i+1,
            'rupee_risk': risk,
            'gross_pnl': risk * 0.02,
            'estimated_fees': risk * 0.001,
            'net_pnl': risk * 0.018,
        })
    with engine.begin() as conn:
        for r in rows:
            run_query(conn, "INSERT INTO shadow_execution_audits (id, rupee_risk, gross_pnl, estimated_fees, net_pnl) VALUES (:id, :rupee_risk, :gross_pnl, :estimated_fees, :net_pnl)", r)
    measure.gross_edge_by_tier()
    out = capsys.readouterr().out
    for tier in ['T1', 'T2', 'T3']:
        assert f"{tier}: n=" in out
        assert "gross_R=0.020" in out
        assert "fee_R=0.001" in out
        assert "net_R=0.018" in out

def test_bar_source_audit(capsys):
    engine = measure.engine
    _setup_schema(engine)
    rows = [
        {'id': 1, 'fill_source': 'LIVE', 'net_pnl': 200, 'rupee_risk': 1000},
        {'id': 2, 'fill_source': 'BACKFILL', 'net_pnl': -150, 'rupee_risk': 800},
        {'id': 3, 'fill_source': 'REPAIR', 'net_pnl': 0, 'rupee_risk': 500},
    ]
    with engine.begin() as conn:
        for r in rows:
            run_query(conn, "INSERT INTO shadow_execution_audits (id, fill_source, net_pnl, rupee_risk) VALUES (:id, :fill_source, :net_pnl, :rupee_risk)", r)
    measure.bar_source_audit()
    out = capsys.readouterr().out
    assert 'LIVE' in out and 'mean net R=0.200' in out
    assert 'BACKFILL' in out and 'mean net R=-0.188' in out
    assert 'REPAIR' in out and 'mean net R=0.000' in out
