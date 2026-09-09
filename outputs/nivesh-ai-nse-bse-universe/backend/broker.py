import sqlite3
from typing import Dict, Optional

from .agent import Signal
from .database import now_iso


RISK_FRACTION = {"conservative": 0.01, "balanced": 0.02, "aggressive": 0.035}


class PaperBroker:
    """Executes simulated orders against the latest mock quote."""

    def execute_buy(self, db: sqlite3.Connection, user_id: int, signal: Signal, risk_profile: str) -> Optional[Dict]:
        portfolio = db.execute("SELECT * FROM portfolios WHERE user_id=?", (user_id,)).fetchone()
        if not portfolio:
            return None
        risk_budget = portfolio["starting_capital"] * RISK_FRACTION.get(risk_profile, .02)
        per_share_risk = max(.01, signal.price - signal.stop_loss)
        risk_qty = int(risk_budget / per_share_risk)
        max_position_value = portfolio["starting_capital"] * .05
        quantity = max(1, min(risk_qty, int(max_position_value / signal.price), int(portfolio["cash"] / signal.price)))
        cost = round(quantity * signal.price, 2)
        if quantity < 1 or cost > portfolio["cash"]:
            return None
        existing = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (user_id, signal.symbol)).fetchone()
        if existing:
            total_qty = existing["quantity"] + quantity
            avg = (existing["quantity"] * existing["average_price"] + cost) / total_qty
            db.execute("UPDATE holdings SET quantity=?, average_price=? WHERE id=?", (total_qty, avg, existing["id"]))
        else:
            db.execute("INSERT INTO holdings(user_id,symbol,name,quantity,average_price,opened_at) VALUES(?,?,?,?,?,?)", (user_id, signal.symbol, signal.name, quantity, signal.price, now_iso()))
        db.execute("UPDATE portfolios SET cash=cash-?, updated_at=? WHERE user_id=?", (cost, now_iso(), user_id))
        cursor = db.execute(
            "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, signal.symbol, signal.name, "BUY", quantity, signal.price, signal.confidence, signal.strategy, signal.reasoning, now_iso()),
        )
        db.commit()
        return {"id": cursor.lastrowid, "symbol": signal.symbol, "action": "BUY", "quantity": quantity, "price": signal.price, "value": cost}
