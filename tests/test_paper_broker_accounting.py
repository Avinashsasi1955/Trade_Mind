import sqlite3
import unittest
from unittest.mock import patch

from backend.agent import Signal
from backend.broker import PaperBroker, DEFAULT_SHORT_MARGIN_RATIO
from backend.database import SCHEMA, create_user, now_iso
from backend.service import portfolio_summary


def make_signal(symbol: str = "TCS", price: float = 3000.0, action: str = "BUY", stop_loss: float = 2950.0) -> Signal:
    return Signal(
        symbol=symbol,
        name=f"{symbol} Ltd",
        price=price,
        change_pct=1.2,
        action=action,
        confidence=85.0,
        strategy="Test Strategy",
        reasoning="Test Reasoning",
        stop_loss=stop_loss,
        target=price * 1.05,
        signal_direction="LONG" if action == "BUY" else "SHORT",
    )


class TestPaperBrokerAccounting(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.user_id = create_user(self.db, "Trader", "trader@example.com", "pass123")
        self.broker = PaperBroker()

    def tearDown(self):
        self.db.close()

    def test_selling_existing_long_full_and_partial(self):
        """Selling an existing long must close only held shares and record realized P&L correctly."""
        # 1. Buy 20 shares of INFY @ ₹1,500 = ₹30,000 cost
        buy_sig = make_signal("INFY", 1500.0, "BUY", stop_loss=1450.0)
        buy_trade = self.broker.execute_buy(self.db, self.user_id, buy_sig, "balanced", quantity=20)
        self.assertIsNotNone(buy_trade)
        self.assertEqual(buy_trade["quantity"], 20)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        self.assertEqual(portfolio["cash"], 1_000_000.0 - 30_000.0)
        self.assertEqual(portfolio["realised_pnl"], 0.0)

        # 2. Partial close: Sell 8 shares @ ₹1,600 (Profit = (1600 - 1500) * 8 = ₹800)
        sell_sig = make_signal("INFY", 1600.0, "SELL", stop_loss=1650.0)
        sell_trade = self.broker.execute_sell(self.db, self.user_id, sell_sig, "balanced", quantity=8)
        self.assertIsNotNone(sell_trade)
        self.assertEqual(sell_trade["quantity"], 8)
        self.assertEqual(sell_trade["realised_pnl"], 800.0)
        self.assertEqual(sell_trade["direction"], "LONG_CLOSE")

        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='INFY'", (self.user_id,)).fetchone()
        self.assertEqual(holding["quantity"], 12)
        self.assertEqual(holding["average_price"], 1500.0)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        # Cash: 970,000 + 8 * 1600 (12,800) = 982,800
        self.assertEqual(portfolio["cash"], 982_800.0)
        self.assertEqual(portfolio["realised_pnl"], 800.0)

        # 3. Full close: Sell remaining 12 shares @ ₹1,400 (Loss = (1400 - 1500) * 12 = -₹1,200)
        sell_sig_loss = make_signal("INFY", 1400.0, "SELL", stop_loss=1450.0)
        sell_trade_full = self.broker.execute_sell(self.db, self.user_id, sell_sig_loss, "balanced", quantity=15)
        self.assertIsNotNone(sell_trade_full)
        # Clamped to remaining 12
        self.assertEqual(sell_trade_full["quantity"], 12)
        self.assertEqual(sell_trade_full["realised_pnl"], -1200.0)

        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='INFY'", (self.user_id,)).fetchone()
        self.assertIsNone(holding)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        # Cash: 982,800 + 12 * 1400 (16,800) = 999,600
        self.assertEqual(portfolio["cash"], 999_600.0)
        # Net Realized P&L: 800 - 1200 = -400
        self.assertEqual(portfolio["realised_pnl"], -400.0)

    def test_short_opening_does_not_inflate_available_cash(self):
        """Opening a short must NOT add sale proceeds to cash or expand buying power."""
        portfolio_before = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        initial_cash = portfolio_before["cash"]

        short_sig = make_signal("TCS", 3000.0, "SELL", stop_loss=3100.0)
        trade = self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=10)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["direction"], "SHORT")
        self.assertEqual(trade["quantity"], 10)
        self.assertEqual(trade["value"], 30_000.0)
        self.assertEqual(trade["margin_required"], 30_000.0)

        portfolio_after = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        # CRITICAL INVARIANT: Cash must remain exactly initial_cash (NOT 1,030,000!)
        self.assertEqual(portfolio_after["cash"], initial_cash)

        # Available funds must reflect blocked collateral
        available = self.broker._available_funds(self.db, self.user_id)
        self.assertEqual(available, initial_cash - 30_000.0)

        # Holdings must store negative quantity
        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='TCS'", (self.user_id,)).fetchone()
        self.assertEqual(holding["quantity"], -10)
        self.assertEqual(holding["average_price"], 3000.0)

    def test_short_margin_rejection_when_funds_insufficient(self):
        """Short order exceeding available funds must be rejected without mutating state."""
        # Reduce cash to ₹10,000
        self.db.execute("UPDATE portfolios SET cash=10000.0 WHERE user_id=?", (self.user_id,))
        self.db.commit()

        # Attempt to short ₹30,000 notional (10 shares @ ₹3,000)
        short_sig = make_signal("TCS", 3000.0, "SELL", stop_loss=3100.0)
        trade = self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=10)
        self.assertIsNone(trade)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        self.assertEqual(portfolio["cash"], 10000.0)

        holdings = self.db.execute("SELECT COUNT(*) cnt FROM holdings WHERE user_id=?", (self.user_id,)).fetchone()["cnt"]
        self.assertEqual(holdings, 0)

        trades = self.db.execute("SELECT COUNT(*) cnt FROM trades WHERE user_id=?", (self.user_id,)).fetchone()["cnt"]
        self.assertEqual(trades, 0)

    def test_full_short_cover_with_profit(self):
        """Covering a short in profit must calculate positive realized P&L and not divide by zero."""
        short_sig = make_signal("WIPRO", 500.0, "SELL", stop_loss=520.0)
        self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=50)

        # Cover entire position at lower price ₹450 (Profit = (500 - 450) * 50 = ₹2,500)
        cover_sig = make_signal("WIPRO", 450.0, "BUY", stop_loss=430.0)
        trade = self.broker.execute_buy(self.db, self.user_id, cover_sig, "balanced", quantity=50)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["direction"], "COVER")
        self.assertEqual(trade["quantity"], 50)
        self.assertEqual(trade["realised_pnl"], 2500.0)

        # Holding must be cleanly deleted
        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='WIPRO'", (self.user_id,)).fetchone()
        self.assertIsNone(holding)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        self.assertEqual(portfolio["cash"], 1_000_000.0 + 2500.0)
        self.assertEqual(portfolio["realised_pnl"], 2500.0)

        # Margin should be fully released
        available = self.broker._available_funds(self.db, self.user_id)
        self.assertEqual(available, 1_002_500.0)

    def test_full_short_cover_with_loss(self):
        """Covering a short with a loss must debit cash and record negative realized P&L."""
        short_sig = make_signal("WIPRO", 500.0, "SELL", stop_loss=520.0)
        self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=50)

        # Cover entire position at higher price ₹550 (Loss = (500 - 550) * 50 = -₹2,500)
        cover_sig = make_signal("WIPRO", 550.0, "BUY", stop_loss=530.0)
        trade = self.broker.execute_buy(self.db, self.user_id, cover_sig, "balanced", quantity=50)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["direction"], "COVER")
        self.assertEqual(trade["realised_pnl"], -2500.0)

        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='WIPRO'", (self.user_id,)).fetchone()
        self.assertIsNone(holding)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        self.assertEqual(portfolio["cash"], 1_000_000.0 - 2500.0)
        self.assertEqual(portfolio["realised_pnl"], -2500.0)

    def test_partial_short_cover_preserves_average_entry_price(self):
        """Partial cover must reduce quantity without altering the entry average_price."""
        short_sig = make_signal("RELIANCE", 3000.0, "SELL", stop_loss=3050.0)
        self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=20)

        # Partial cover: 8 shares @ ₹2,900 (Profit = (3000 - 2900) * 8 = ₹800)
        cover_sig = make_signal("RELIANCE", 2900.0, "BUY", stop_loss=2850.0)
        trade = self.broker.execute_buy(self.db, self.user_id, cover_sig, "balanced", quantity=8)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["quantity"], 8)
        self.assertEqual(trade["realised_pnl"], 800.0)

        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='RELIANCE'", (self.user_id,)).fetchone()
        self.assertIsNotNone(holding)
        self.assertEqual(holding["quantity"], -12)
        # Entry average price must remain ₹3000.0
        self.assertEqual(holding["average_price"], 3000.0)

        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        self.assertEqual(portfolio["cash"], 1_000_000.0 + 800.0)
        self.assertEqual(portfolio["realised_pnl"], 800.0)

        # Margin blocked should now be 12 * 3000 = 36,000
        blocked = self.broker._blocked_margin(self.db, self.user_id)
        self.assertEqual(blocked, 36_000.0)

    def test_oversized_short_cover_does_not_flip_to_long(self):
        """An order to cover a short must clamp to the held short quantity to avoid accidental long creation."""
        short_sig = make_signal("SBIN", 800.0, "SELL", stop_loss=820.0)
        self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=10)

        # User attempts to buy 25 shares (15 more than the 10 held short)
        cover_sig = make_signal("SBIN", 750.0, "BUY", stop_loss=740.0)
        trade = self.broker.execute_buy(self.db, self.user_id, cover_sig, "balanced", quantity=25)
        self.assertIsNotNone(trade)
        # Clamped to 10
        self.assertEqual(trade["quantity"], 10)
        self.assertEqual(trade["direction"], "COVER")

        # Position is flat, not long
        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='SBIN'", (self.user_id,)).fetchone()
        self.assertIsNone(holding)

    def test_invalid_parameters_and_negative_prices(self):
        """Orders with non-positive price or quantity < 1 must be rejected."""
        bad_price_sig = make_signal("TCS", -10.0, "BUY")
        self.assertIsNone(self.broker.execute_buy(self.db, self.user_id, bad_price_sig, "balanced", quantity=5))

        zero_price_sig = make_signal("TCS", 0.0, "SELL")
        self.assertIsNone(self.broker.execute_sell(self.db, self.user_id, zero_price_sig, "balanced", quantity=5))

        zero_qty_sig = make_signal("TCS", 3000.0, "BUY")
        self.assertIsNone(self.broker.execute_buy(self.db, self.user_id, zero_qty_sig, "balanced", quantity=0))

    def test_portfolio_summary_equity_and_restart_persistence(self):
        """Portfolio summary must compute true equity according to TARGET_ARCHITECTURE.md §3.2."""
        # 1. Open long in INFY: 10 @ ₹1500 = ₹15,000
        buy_sig = make_signal("INFY", 1500.0, "BUY")
        self.broker.execute_buy(self.db, self.user_id, buy_sig, "balanced", quantity=10)

        # 2. Open short in TCS: 10 @ ₹3000 = ₹30,000 margin
        short_sig = make_signal("TCS", 3000.0, "SELL")
        self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=10)

        # Mock market prices: INFY rose to ₹1600 (+1000 profit), TCS fell to ₹2800 (+2000 short profit)
        with patch("backend.service._prices", return_value={"INFY": 1600.0, "TCS": 2800.0}):
            summary = portfolio_summary(self.db, self.user_id)
            # Cash = 1,000,000 - 15,000 = 985,000
            self.assertEqual(summary["cash"], 985_000.0)
            # Long value = 10 * 1600 = 16,000
            # Short unrealized = (3000 - 2800) * 10 = +2,000
            # Equity = 985,000 + 16,000 + 2,000 = 1,003,000.0
            self.assertEqual(summary["current_value"], 1_003_000.0)
            self.assertEqual(summary["total_pnl"], 3000.0)
            self.assertEqual(summary["margin_locked"], 30_000.0)
            # Available funds = 985,000 - 30,000 + min(0, 2000) = 955,000.0
            self.assertEqual(summary["available_funds"], 955_000.0)

    def test_transaction_rollback_on_failure(self):
        """A failure during execution must rollback all database mutations."""
        class FaultyDb:
            def __init__(self, real_db):
                self._db = real_db
                self.rolled_back = False
            def execute(self, *args, **kwargs):
                return self._db.execute(*args, **kwargs)
            def commit(self):
                raise sqlite3.OperationalError("Simulated disk error")
            def rollback(self):
                self.rolled_back = True
                return self._db.rollback()

        faulty = FaultyDb(self.db)
        short_sig = make_signal("TATAMOTORS", 1000.0, "SELL")
        with self.assertRaises(sqlite3.OperationalError):
            self.broker.execute_sell(faulty, self.user_id, short_sig, "balanced", quantity=10)

        self.assertTrue(faulty.rolled_back)
        # Database state must remain completely untouched
        holdings = self.db.execute("SELECT COUNT(*) cnt FROM holdings WHERE user_id=?", (self.user_id,)).fetchone()["cnt"]
        self.assertEqual(holdings, 0)
        trades = self.db.execute("SELECT COUNT(*) cnt FROM trades WHERE user_id=?", (self.user_id,)).fetchone()["cnt"]
        self.assertEqual(trades, 0)

    def test_underwater_short_cover_allowed_when_cash_deficit(self):
        """Underwater short cover MUST execute even if loss drives account cash negative (deficit)."""
        # Set account cash to ₹20,000
        self.db.execute("UPDATE portfolios SET cash=20000.0, starting_capital=20000.0 WHERE user_id=?", (self.user_id,))
        self.db.commit()

        # Open short: 10 shares of XYZ @ ₹1,000 = ₹10,000 margin
        short_sig = make_signal("XYZ", 1000.0, "SELL")
        trade_sell = self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=10)
        self.assertIsNotNone(trade_sell)

        # XYZ spikes to ₹5,000! Realized loss will be (1000 - 5000) * 10 = -₹40,000
        # Cash after cover will be 20,000 - 40,000 = -₹20,000 (< 0)
        cover_sig = make_signal("XYZ", 5000.0, "BUY")
        trade_cover = self.broker.execute_buy(self.db, self.user_id, cover_sig, "balanced", quantity=10)

        # CRITICAL FIX VERIFICATION: Must NOT return None / reject the cover order!
        self.assertIsNotNone(trade_cover)
        self.assertEqual(trade_cover["direction"], "COVER")
        self.assertEqual(trade_cover["quantity"], 10)
        self.assertEqual(trade_cover["realised_pnl"], -40000.0)

        # Portfolio cash is negative ₹20,000
        portfolio = self.db.execute("SELECT * FROM portfolios WHERE user_id=?", (self.user_id,)).fetchone()
        self.assertEqual(portfolio["cash"], -20000.0)
        self.assertEqual(portfolio["realised_pnl"], -40000.0)

        # Short holding is completely closed
        holding = self.db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol='XYZ'", (self.user_id,)).fetchone()
        self.assertIsNone(holding)

        # Blocked margin is released to ₹0
        self.assertEqual(self.broker._blocked_margin(self.db, self.user_id), 0.0)

        # Available funds is ₹0
        self.assertEqual(self.broker._available_funds(self.db, self.user_id), 0.0)

    def test_subsequent_new_orders_blocked_when_cash_in_deficit(self):
        """When cash is negative, new LONG and new SHORT orders must be strictly rejected."""
        self.db.execute("UPDATE portfolios SET cash=-5000.0 WHERE user_id=?", (self.user_id,))
        self.db.commit()

        # New long buy order rejected
        buy_sig = make_signal("INFY", 1500.0, "BUY")
        self.assertIsNone(self.broker.execute_buy(self.db, self.user_id, buy_sig, "balanced", quantity=5))

        # New short sell order rejected
        sell_sig = make_signal("TCS", 3000.0, "SELL")
        self.assertIsNone(self.broker.execute_sell(self.db, self.user_id, sell_sig, "balanced", quantity=5))

    def test_margin_ratio_consistency_in_portfolio_summary(self):
        """portfolio_summary must use configured margin ratio rather than hardcoding 1.0."""
        # Open short TCS: 10 @ ₹3,000 = ₹30,000 notional
        short_sig = make_signal("TCS", 3000.0, "SELL")
        self.broker.execute_sell(self.db, self.user_id, short_sig, "balanced", quantity=10)

        # With 20% margin ratio (0.20), margin locked is 30,000 * 0.20 = 6,000
        with patch("backend.service._prices", return_value={"TCS": 3000.0}):
            summary = portfolio_summary(self.db, self.user_id, margin_ratio=0.20)
            self.assertEqual(summary["margin_locked"], 6000.0)
            self.assertEqual(summary["available_funds"], 1_000_000.0 - 6000.0)
