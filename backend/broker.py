import sqlite3
from typing import Dict, Optional

from .agent import Signal
from .database import now_iso


RISK_FRACTION = {"conservative": 0.01, "balanced": 0.02, "aggressive": 0.035}

# Documented margin ratio for paper shorting:
# 1.0 = 100% cash collateral required (conservative, cash-backed paper short).
# 0.20 = 20% SEBI MIS intraday margin requirement.
DEFAULT_SHORT_MARGIN_RATIO: float = 1.0


class PaperBroker:
    """Executes simulated orders against quotes with double-entry margin accounting.
    Adheres strictly to TARGET_ARCHITECTURE.md §3.2:
    - Never credits unearned short-sale proceeds to unrestricted cash.
    - Blocks collateral margin from available cash for open short positions.
    - Accurately tracks realized P&L on both long sales and short covers.
    - Prevents ZeroDivisionError when covering existing short holdings.
    """

    def __init__(self, margin_ratio: float = DEFAULT_SHORT_MARGIN_RATIO):
        if margin_ratio <= 0:
            raise ValueError("margin_ratio must be positive")
        self.margin_ratio = float(margin_ratio)

    def _portfolio(self, db: sqlite3.Connection, user_id: int):
        return db.execute("SELECT * FROM portfolios WHERE user_id=?", (user_id,)).fetchone()

    def _short_positions(self, db: sqlite3.Connection, user_id: int):
        return db.execute("SELECT * FROM holdings WHERE user_id=? AND quantity < 0", (user_id,)).fetchall()

    def _blocked_margin(self, db: sqlite3.Connection, user_id: int) -> float:
        shorts = self._short_positions(db, user_id)
        return round(sum(abs(r["quantity"]) * r["average_price"] * self.margin_ratio for r in shorts), 2)

    def _available_funds(
        self,
        db: sqlite3.Connection,
        user_id: int,
        portfolio: Optional[Dict] = None,
        prices: Optional[Dict[str, float]] = None,
    ) -> float:
        if portfolio is None:
            portfolio = self._portfolio(db, user_id)
        if not portfolio:
            return 0.0
        cash = float(portfolio["cash"])
        blocked = self._blocked_margin(db, user_id)
        unrealised_short_loss = 0.0
        if prices:
            shorts = self._short_positions(db, user_id)
            for r in shorts:
                sym = r["symbol"]
                if sym in prices:
                    loss = (prices[sym] - r["average_price"]) * abs(r["quantity"])
                    if loss > 0:
                        unrealised_short_loss += loss
        return max(0.0, round(cash - blocked - unrealised_short_loss, 2))

    def _position_size(self, portfolio, signal: Signal, risk_profile: str, is_short: bool = False, available_funds: Optional[float] = None) -> int:
        risk_budget = portfolio["starting_capital"] * RISK_FRACTION.get(risk_profile, .02)
        per_share_risk = max(.01, abs(signal.price - signal.stop_loss))
        risk_qty = int(risk_budget / per_share_risk)
        max_position_value = portfolio["starting_capital"] * .05
        funds = available_funds if available_funds is not None else portfolio["cash"]
        per_share_capital = signal.price * (self.margin_ratio if is_short else 1.0)
        if per_share_capital <= 0 or funds <= 0:
            return 0
        affordable_qty = int(funds / per_share_capital)
        qty_cap = int(max_position_value / signal.price)
        result = min(risk_qty, qty_cap, affordable_qty)
        return max(0, result)

    def execute_buy(self, db: sqlite3.Connection, user_id: int, signal: Signal, risk_profile: str, quantity: Optional[int] = None) -> Optional[Dict]:
        """Execute a simulated BUY order.

        Handles two distinct scenarios:
        1. Covering an existing SHORT holding (quantity < 0):
           - Clamps cover quantity to avoid flipping into long.
           - Calculates realized P&L: (entry_price - buy_price) * cover_qty.
           - Adjusts cash by net realized P&L and releases blocked margin.
           - Deletes holding if fully covered; preserves average_price on partial cover.
        2. Opening or adding to a LONG holding (quantity >= 0):
           - Requires full purchase cost from available unencumbered funds.
           - Debits purchase cost from cash.
           - Updates weighted average entry price.
        """
        if signal.price <= 0:
            return None

        portfolio = self._portfolio(db, user_id)
        if not portfolio:
            return None

        existing = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (user_id, signal.symbol)).fetchone()

        try:
            # SCENARIO 1: Covering an existing SHORT position
            if existing and existing["quantity"] < 0:
                short_held = abs(existing["quantity"])
                if quantity is None:
                    # By default, cover the entire short position or sized portion
                    quantity = short_held
                cover_qty = max(0, min(short_held, quantity))
                if cover_qty < 1:
                    return None

                buy_cost = round(cover_qty * signal.price, 2)
                entry_basis = round(cover_qty * existing["average_price"], 2)
                realised_pnl = round(entry_basis - buy_cost, 2)

                # Risk-reducing closure: covering an existing short position MUST be allowed
                # even if the realized loss exceeds cash (account deficit). Margin and available-funds
                # checks strictly apply to opening NEW risk, but must never prevent closing an existing short.

                stamp = now_iso()
                new_qty = existing["quantity"] + cover_qty
                if new_qty == 0:
                    db.execute("DELETE FROM holdings WHERE id=?", (existing["id"],))
                else:
                    # Partial cover: average entry price of remaining short shares is unchanged
                    db.execute("UPDATE holdings SET quantity=? WHERE id=?", (new_qty, existing["id"]))

                db.execute(
                    "UPDATE portfolios SET cash=cash+?, realised_pnl=realised_pnl+?, updated_at=? WHERE user_id=?",
                    (realised_pnl, realised_pnl, stamp, user_id),
                )
                cursor = db.execute(
                    "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,realised_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, signal.symbol, signal.name, "BUY", cover_qty, signal.price, signal.confidence, signal.strategy, f"Cover short (P&L: ₹{realised_pnl:+,.2f})", realised_pnl, stamp),
                )
                db.commit()
                return {
                    "id": cursor.lastrowid, "symbol": signal.symbol, "action": "BUY", "direction": "COVER",
                    "quantity": cover_qty, "price": signal.price, "value": buy_cost, "realised_pnl": realised_pnl,
                }

            # SCENARIO 2: Opening or adding to a LONG position
            available = self._available_funds(db, user_id, portfolio)
            if quantity is None:
                quantity = self._position_size(portfolio, signal, risk_profile, is_short=False, available_funds=available)

            cost = round(quantity * signal.price, 2)
            if quantity < 1 or cost > available:
                return None

            stamp = now_iso()
            if existing:
                total_qty = existing["quantity"] + quantity
                avg = (existing["quantity"] * existing["average_price"] + cost) / total_qty
                db.execute("UPDATE holdings SET quantity=?, average_price=? WHERE id=?", (total_qty, avg, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO holdings(user_id,symbol,name,quantity,average_price,opened_at) VALUES(?,?,?,?,?,?)",
                    (user_id, signal.symbol, signal.name, quantity, signal.price, stamp),
                )
            db.execute("UPDATE portfolios SET cash=cash-?, updated_at=? WHERE user_id=?", (cost, stamp, user_id))
            cursor = db.execute(
                "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,realised_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, signal.symbol, signal.name, "BUY", quantity, signal.price, signal.confidence, signal.strategy, signal.reasoning, 0.0, stamp),
            )
            db.commit()
            return {"id": cursor.lastrowid, "symbol": signal.symbol, "action": "BUY", "direction": "LONG", "quantity": quantity, "price": signal.price, "value": cost}
        except Exception:
            db.rollback()
            raise

    def execute_sell(self, db: sqlite3.Connection, user_id: int, signal: Signal, risk_profile: str, quantity: Optional[int] = None) -> Optional[Dict]:
        """Execute a simulated SELL order.

        Handles two distinct scenarios:
        1. Closing or reducing an existing LONG holding (quantity > 0):
           - Clamps quantity to held shares.
           - Calculates realized P&L: (sell_price - average_price) * reduce_qty.
           - Credits gross proceeds to cash and records realized P&L.
           - Deletes holding if fully closed; preserves average_price on partial sale.
        2. Opening a new SHORT holding (or adding to existing short):
           - Validates available funds against required margin.
           - NEVER credits short-sale proceeds to unrestricted cash.
           - Blocks margin collateral by tracking the negative quantity holding.
        """
        if signal.price <= 0:
            return None

        portfolio = self._portfolio(db, user_id)
        if not portfolio:
            return None

        existing = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (user_id, signal.symbol)).fetchone()

        try:
            # SCENARIO 1: Closing or reducing an existing LONG position
            if existing and existing["quantity"] > 0:
                long_held = existing["quantity"]
                if quantity is None:
                    quantity = self._position_size(portfolio, signal, risk_profile, is_short=False)
                reduce_qty = max(0, min(long_held, quantity))
                if reduce_qty < 1:
                    return None

                proceeds = round(reduce_qty * signal.price, 2)
                cost_basis = round(reduce_qty * existing["average_price"], 2)
                realised_pnl = round(proceeds - cost_basis, 2)
                stamp = now_iso()

                if long_held - reduce_qty == 0:
                    db.execute("DELETE FROM holdings WHERE id=?", (existing["id"],))
                else:
                    db.execute("UPDATE holdings SET quantity=? WHERE id=?", (long_held - reduce_qty, existing["id"]))

                db.execute(
                    "UPDATE portfolios SET cash=cash+?, realised_pnl=realised_pnl+?, updated_at=? WHERE user_id=?",
                    (proceeds, realised_pnl, stamp, user_id),
                )
                cursor = db.execute(
                    "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,realised_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, signal.symbol, signal.name, "SELL", reduce_qty, signal.price, signal.confidence, signal.strategy, f"Close long (P&L: ₹{realised_pnl:+,.2f})", realised_pnl, stamp),
                )
                db.commit()
                return {
                    "id": cursor.lastrowid, "symbol": signal.symbol, "action": "SELL", "direction": "LONG_CLOSE",
                    "quantity": reduce_qty, "price": signal.price, "value": proceeds, "realised_pnl": realised_pnl,
                }

            # SCENARIO 2: Opening or adding to a SHORT position
            available = self._available_funds(db, user_id, portfolio)
            if quantity is None:
                quantity = self._position_size(portfolio, signal, risk_profile, is_short=True, available_funds=available)

            if quantity < 1:
                return None

            notional = round(quantity * signal.price, 2)
            margin_required = round(notional * self.margin_ratio, 2)
            if margin_required > available:
                return None

            stamp = now_iso()
            if existing and existing["quantity"] < 0:
                current_short_qty = abs(existing["quantity"])
                current_notional = current_short_qty * existing["average_price"]
                new_total_qty = current_short_qty + quantity
                new_avg = round((current_notional + notional) / new_total_qty, 2)
                db.execute("UPDATE holdings SET quantity=?, average_price=? WHERE id=?", (-new_total_qty, new_avg, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO holdings(user_id,symbol,name,quantity,average_price,opened_at) VALUES(?,?,?,?,?,?)",
                    (user_id, signal.symbol, signal.name, -quantity, signal.price, stamp),
                )

            # NOTE: Cash is NOT credited with short proceeds.
            # Collateral margin is automatically tracked by _blocked_margin().
            cursor = db.execute(
                "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,realised_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, signal.symbol, signal.name, "SELL", quantity, signal.price, signal.confidence, signal.strategy, signal.reasoning, 0.0, stamp),
            )
            db.commit()
            return {
                "id": cursor.lastrowid, "symbol": signal.symbol, "action": "SELL", "direction": "SHORT",
                "quantity": quantity, "price": signal.price, "value": notional, "margin_required": margin_required,
            }
        except Exception:
            db.rollback()
            raise
