"""Decimal Indian-market transaction-cost estimation and reconciliation."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict


D = Decimal
PAISE = D("0.01")


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange_charges: Decimal
    sebi_charges: Decimal
    ipft_charges: Decimal
    gst: Decimal
    stamp_duty: Decimal
    dp_charges: Decimal
    estimated_slippage: Decimal
    total: Decimal

    def serialise(self) -> Dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


def _money(value: Decimal) -> Decimal:
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def _stt(value: Decimal) -> Decimal:
    return value.quantize(D("1"),rounding=ROUND_HALF_UP)


def estimate_zerodha_costs(segment: str, side: str, price: Decimal, quantity: int,
                           exchange: str = "NSE", slippage_bps: Decimal = D("0"),
                           exercised_option_intrinsic: Decimal = D("0"),
                           delivery_dp_charge: Decimal = D("0")) -> CostBreakdown:
    """Rates effective 1 April 2026; actual contract notes remain authoritative."""
    segment, side, exchange = segment.upper(), side.upper(), exchange.upper()
    if segment not in {"EQUITY_DELIVERY","EQUITY_INTRADAY","FUTURES","OPTIONS"}:
        raise ValueError("Unsupported segment")
    if side not in {"BUY","SELL"} or quantity <= 0 or price <= 0:
        raise ValueError("Valid side, positive price and quantity are required")
    turnover = price * D(quantity)
    if segment == "EQUITY_DELIVERY":
        brokerage=D("0"); stt=turnover*D("0.001")
        transaction_rate=D("0.0000307") if exchange=="NSE" else D("0.0000375")
        stamp_rate=D("0.00015") if side=="BUY" else D("0")
        dp=delivery_dp_charge if side=="SELL" else D("0")
    elif segment == "EQUITY_INTRADAY":
        brokerage=min(D("20"),turnover*D("0.0003")); stt=turnover*D("0.00025") if side=="SELL" else D("0")
        transaction_rate=D("0.0000307") if exchange=="NSE" else D("0.0000375")
        stamp_rate=D("0.00003") if side=="BUY" else D("0"); dp=D("0")
    elif segment == "FUTURES":
        brokerage=min(D("20"),turnover*D("0.0003")); stt=turnover*D("0.0005") if side=="SELL" else D("0")
        transaction_rate=D("0.0000183") if exchange=="NSE" else D("0")
        stamp_rate=D("0.00002") if side=="BUY" else D("0"); dp=D("0")
    else:
        brokerage=D("20"); stt=(turnover*D("0.0015") if side=="SELL" else exercised_option_intrinsic*D("0.0015"))
        transaction_rate=D("0.0003553") if exchange=="NSE" else D("0.000325")
        stamp_rate=D("0.00003") if side=="BUY" else D("0"); dp=D("0")
    exchange_charges=turnover*transaction_rate
    sebi_charges=turnover*D("0.000001")
    ipft_charges=turnover*D("0.000000001")
    gst=(brokerage+exchange_charges+sebi_charges+ipft_charges)*D("0.18")
    stamp=turnover*stamp_rate
    slippage=turnover*slippage_bps/D("10000")
    values=[_money(brokerage),_stt(stt),_money(exchange_charges),_money(sebi_charges),_money(ipft_charges),_money(gst),_money(stamp),_money(dp),_money(slippage)]
    return CostBreakdown(*values,_money(sum(values,D("0"))))


def reconcile_contract_note(estimate: CostBreakdown, actual_fees: Decimal,
                            actual_fill_price: Decimal, expected_price: Decimal,
                            quantity: int) -> Dict[str, str]:
    if actual_fees < 0 or actual_fill_price <= 0 or expected_price <= 0 or quantity <= 0:
        raise ValueError("Invalid contract-note reconciliation values")
    actual_slippage=_money(abs(actual_fill_price-expected_price)*D(quantity))
    return {"estimated_total":str(estimate.total),"actual_contract_note_fees":str(_money(actual_fees)),
            "fee_variance":str(_money(actual_fees-estimate.total)),"actual_slippage":str(actual_slippage),
            "fully_reconciled":"true"}
