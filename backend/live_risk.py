"""PostgreSQL-backed account-equity kill switch for v3 infrastructure."""
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, text


def record_equity_and_enforce_drawdown(engine: Engine, account_ref: UUID,
                                       opening_equity: Decimal, current_equity: Decimal,
                                       realised_pnl: Decimal, unrealised_pnl: Decimal,
                                       observed_at) -> dict:
    if opening_equity <= 0 or current_equity < 0:
        raise ValueError("Invalid account equity")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO risk_control_state(account_ref,trading_enabled,kill_switch_active,kill_reason)
            VALUES(:account,FALSE,TRUE,'Initial production lock') ON CONFLICT(account_ref) DO NOTHING
        """), {"account":str(account_ref)})
        connection.execute(text("""
            INSERT INTO account_equity_snapshots(account_ref,observed_at,session_date,opening_equity,current_equity,realised_pnl,unrealised_pnl)
            VALUES(:account,:observed_at,CAST(:observed_at AS DATE),:opening,:current,:realised,:unrealised)
        """), {"account":str(account_ref),"observed_at":observed_at,"opening":opening_equity,
                "current":current_equity,"realised":realised_pnl,"unrealised":unrealised_pnl})
        state=connection.execute(text("SELECT trading_enabled,kill_switch_active,kill_reason,hard_daily_drawdown_pct FROM risk_control_state WHERE account_ref=:account FOR UPDATE"),{"account":str(account_ref)}).mappings().one()
    return dict(state)


def assert_trading_allowed(engine: Engine, account_ref: UUID) -> None:
    with engine.connect() as connection:
        state=connection.execute(text("SELECT trading_enabled,kill_switch_active,kill_reason FROM risk_control_state WHERE account_ref=:account"),{"account":str(account_ref)}).mappings().one_or_none()
    if not state or not state["trading_enabled"] or state["kill_switch_active"]:
        raise RuntimeError((state or {}).get("kill_reason") or "Production trading is not explicitly enabled")
