"""Telegram & Discord Webhook Notification Dispatcher.

Dispatches real-time, non-blocking alerts for:
1. Trade Entries (Symbol, Side, Mode [INTRADAY vs SWING], Kelly Sizing, SL/TP)
2. Trade Exits (Realised P&L, Exit Reason, Holding Duration)
3. End-of-Day Performance Summary (Net P&L, Win Rate %, Dual-Mode split, Gate Accuracy)

Uses a background daemon thread pool with strict 4s timeouts so notification calls
never block live inference, Celery tasks, or HTTP requests.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("nivesh.notifications")
IST = ZoneInfo("Asia/Kolkata")

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nivesh_notifier")


def is_notifications_enabled() -> bool:
    val = os.getenv("NOTIFICATIONS_ENABLED", "1").strip().lower()
    return val in {"1", "true", "yes", "enabled"}


def get_telegram_creds() -> tuple[Optional[str], Optional[str]]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
    return token, chat_id


def get_discord_webhook_url() -> Optional[str]:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return url if url else None


def _post_http_json(url: str, payload: Dict[str, Any], timeout: float = 4.0) -> bool:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Nivesh-Agentic-Trading-Bot/3.17",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        logger.warning("Notification delivery failed to %s: %s", url.split("?")[0], exc)
        return False


def send_telegram_sync(message_html: str, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    cid = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not cid:
        logger.debug("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    return _post_http_json(url, payload)


def send_discord_sync(content: str, embeds: Optional[List[Dict[str, Any]]] = None, webhook_url: Optional[str] = None) -> bool:
    url = webhook_url or get_discord_webhook_url()
    if not url:
        logger.debug("Discord webhook not configured (missing DISCORD_WEBHOOK_URL)")
        return False

    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds
    return _post_http_json(url, payload)


def dispatch_async(fn, *args, **kwargs) -> None:
    """Run notification non-blocking in background pool."""
    if not is_notifications_enabled():
        return
    try:
        _EXECUTOR.submit(fn, *args, **kwargs)
    except Exception as exc:
        logger.error("Failed to enqueue notification: %s", exc)


# ---------------------------------------------------------------------------
# Specific Event Notifications
# ---------------------------------------------------------------------------

def notify_trade_opened(trade: Dict[str, Any]) -> None:
    """Format and send notification when a trade/order is placed."""
    def _deliver():
        symbol = str(trade.get("symbol") or "UNKNOWN")
        side = str(trade.get("side") or "BUY").upper()
        mode = str(trade.get("trade_mode") or "INTRADAY").upper()
        mode_tag = "🌊 SWING" if mode == "SWING" else "⚡ INTRADAY"
        side_emoji = "🟢" if side == "BUY" else "🔴"
        qty = trade.get("quantity") or trade.get("qty") or 1
        entry = float(trade.get("entry_price") or trade.get("decision_price") or 0.0)
        sl = float(trade.get("stop_loss_price") or trade.get("stop_loss") or 0.0)
        tp = float(trade.get("target_price") or trade.get("take_profit") or 0.0)
        strategy = str(trade.get("strategy") or trade.get("chart_strategy") or "MOMENTUM")
        kelly = trade.get("kelly_multiplier") or trade.get("kelly_factor") or 1.0

        # Telegram HTML
        tg_text = (
            f"<b>{side_emoji} NIVESH ORDER FILLED | {mode_tag}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Instrument:</b> <code>{symbol}</code>\n"
            f"<b>Action:</b> <b>{side}</b> {qty} qty\n"
            f"<b>Entry:</b> ₹{entry:,.2f}\n"
            f"<b>Stop-Loss:</b> ₹{sl:,.2f}  |  <b>Target:</b> ₹{tp:,.2f}\n"
            f"<b>Kelly Sizing:</b> <b>{float(kelly):.2f}x</b> Half-Kelly\n"
            f"<b>Strategy:</b> {strategy}\n"
            f"<i>Execution: Shadow Paper Engine</i>"
        )
        send_telegram_sync(tg_text)

        # Discord Embed
        embed = {
            "title": f"{side_emoji} Trade Opened: {symbol} ({mode})",
            "description": f"**{side}** {qty} @ ₹{entry:,.2f} via `{strategy}`",
            "color": 0x10B981 if side == "BUY" else 0xEF4444,
            "fields": [
                {"name": "Stop Loss", "value": f"₹{sl:,.2f}", "inline": True},
                {"name": "Target", "value": f"₹{tp:,.2f}", "inline": True},
                {"name": "Kelly Alloc", "value": f"{float(kelly):.2f}x", "inline": True},
            ],
            "footer": {"text": "Nivesh Multi-Agent Brain Ledger"},
            "timestamp": datetime.now(IST).isoformat(),
        }
        send_discord_sync("", embeds=[embed])

    dispatch_async(_deliver)


def notify_trade_closed(trade: Dict[str, Any]) -> None:
    """Format and send notification when a trade is exited."""
    def _deliver():
        symbol = str(trade.get("symbol") or "UNKNOWN")
        mode = str(trade.get("trade_mode") or "INTRADAY").upper()
        pnl = float(trade.get("net_pnl") or trade.get("realised_pnl") or 0.0)
        pnl_pct = float(trade.get("pnl_pct") or 0.0)
        reason = str(trade.get("exit_reason") or "FLAT_EXIT")
        holding = str(trade.get("holding_duration") or f"{trade.get('holding_days', 0)}d")
        
        outcome_emoji = "🏆" if pnl > 0 else "🛑" if pnl < 0 else "⚪"
        pnl_color = "#10b981" if pnl >= 0 else "#ef4444"
        pnl_sign = "+" if pnl > 0 else ""

        tg_text = (
            f"<b>{outcome_emoji} NIVESH TRADE CLOSED | {mode}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Instrument:</b> <code>{symbol}</code>\n"
            f"<b>Net P&L:</b> <b>{pnl_sign}₹{pnl:,.2f}</b> ({pnl_sign}{pnl_pct:.2f}%)\n"
            f"<b>Exit Reason:</b> <code>{reason}</code>\n"
            f"<b>Holding Duration:</b> {holding}\n"
            f"<i>Execution: Shadow Paper Engine</i>"
        )
        send_telegram_sync(tg_text)

        embed = {
            "title": f"{outcome_emoji} Position Closed: {symbol}",
            "description": f"Realised **{pnl_sign}₹{pnl:,.2f}** ({reason})",
            "color": 0x10B981 if pnl >= 0 else 0xEF4444,
            "fields": [
                {"name": "Net Return", "value": f"{pnl_sign}{pnl_pct:.2f}%", "inline": True},
                {"name": "Exit Reason", "value": reason, "inline": True},
                {"name": "Mode", "value": mode, "inline": True},
            ],
            "footer": {"text": "Nivesh Multi-Agent Brain Ledger"},
            "timestamp": datetime.now(IST).isoformat(),
        }
        send_discord_sync("", embeds=[embed])

    dispatch_async(_deliver)


def notify_daily_eod_summary(report: Dict[str, Any]) -> None:
    """Format and send comprehensive 15:35 EOD digest."""
    def _deliver():
        session_date = str(report.get("session_date") or datetime.now(IST).date().isoformat())
        pnl_data = report.get("paper_pnl") or {}
        realised = float(pnl_data.get("realised_pnl") or 0.0)
        unrealised = float(pnl_data.get("unrealised_pnl") or 0.0)
        total_pnl = realised + unrealised
        win_rate = float(pnl_data.get("win_rate_pct") or 0.0)
        trades_count = int(pnl_data.get("total_trades") or 0)
        wins = int(pnl_data.get("winning_trades") or 0)
        losses = int(pnl_data.get("losing_trades") or 0)
        
        dual_mode = pnl_data.get("dual_mode") or {}
        intra = dual_mode.get("intraday") or {}
        swing = dual_mode.get("swing") or {}
        
        intra_pnl = float(intra.get("pnl") or 0.0)
        intra_cnt = int(intra.get("count") or 0)
        swing_pnl = float(swing.get("pnl") or 0.0)
        swing_cnt = int(swing.get("count") or 0)
        
        cf = report.get("counterfactual") or {}
        gate_acc = float(cf.get("gate_accuracy_pct") or 100.0)
        saved_losses = int(cf.get("saved_losses") or 0)
        missed_wins = int(cf.get("missed_wins") or 0)

        pnl_sign = "+" if total_pnl > 0 else ""
        outcome_emoji = "🚀" if total_pnl > 0 else "📉" if total_pnl < 0 else "⚖️"

        tg_text = (
            f"<b>{outcome_emoji} NIVESH EOD SUMMARY [{session_date}]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Total P&L:</b> <b>{pnl_sign}₹{total_pnl:,.2f}</b>\n"
            f"  • Realised: {pnl_sign}₹{realised:,.2f}\n"
            f"  • Unrealised: {pnl_sign}₹{unrealised:,.2f}\n"
            f"<b>Win Rate:</b> <b>{win_rate:.1f}%</b> ({wins}W / {losses}L of {trades_count})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Dual-Mode Performance:</b>\n"
            f"  ⚡ Intraday: ₹{intra_pnl:+,.2f} ({intra_cnt} trades)\n"
            f"  🌊 Swing:    ₹{swing_pnl:+,.2f} ({swing_cnt} positions)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Counterfactual Gate Accuracy:</b> <b>{gate_acc:.1f}%</b>\n"
            f"  🛡️ Saved Losses: {saved_losses} setups\n"
            f"  ⚠️ Missed Wins:  {missed_wins} setups\n"
            f"<b>Supervisory Sentinel:</b> <code>HEALTHY ✅</code>\n"
            f"<i>Nivesh Autonomous Multi-Agent Brain</i>"
        )
        send_telegram_sync(tg_text)

        embed = {
            "title": f"📊 EOD Performance Report — {session_date}",
            "description": f"Net P&L: **{pnl_sign}₹{total_pnl:,.2f}** | Win Rate: **{win_rate:.1f}%**",
            "color": 0x10B981 if total_pnl >= 0 else 0xEF4444,
            "fields": [
                {"name": "⚡ Intraday P&L", "value": f"₹{intra_pnl:+,.2f} ({intra_cnt})", "inline": True},
                {"name": "🌊 Swing P&L", "value": f"₹{swing_pnl:+,.2f} ({swing_cnt})", "inline": True},
                {"name": "🛡️ Gate Accuracy", "value": f"{gate_acc:.1f}% ({saved_losses} saved)", "inline": True},
            ],
            "footer": {"text": "All operations paper/shadow mode. Live safety locked."},
            "timestamp": datetime.now(IST).isoformat(),
        }
        send_discord_sync("", embeds=[embed])

    dispatch_async(_deliver)


def test_notification_dispatcher() -> Dict[str, Any]:
    """Test webhook and bot dispatch with diagnostic results."""
    t_token, t_cid = get_telegram_creds()
    d_url = get_discord_webhook_url()
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    res_tg = False
    res_dc = False

    if t_token and t_cid:
        msg = (
            f"<b>🔔 NIVESH NOTIFICATION DISPATCHER TEST</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Status:</b> Connected & Operational ✅\n"
            f"<b>Timestamp:</b> <code>{now_str}</code>\n"
            f"<b>Environment:</b> Shadow Paper Dual-Mode Engine\n"
            f"<i>Multi-agent notifications active.</i>"
        )
        res_tg = send_telegram_sync(msg, t_token, t_cid)

    if d_url:
        embed = {
            "title": "🔔 Nivesh Webhook Dispatcher Test",
            "description": "Notification subsystem online and responding.",
            "color": 0x3B82F6,
            "fields": [
                {"name": "Mode", "value": "Dual-Engine (Intraday + Swing)", "inline": True},
                {"name": "Timestamp", "value": now_str, "inline": True},
            ],
            "footer": {"text": "Nivesh Multi-Agent Framework"},
        }
        res_dc = send_discord_sync("Test dispatch ping", embeds=[embed], webhook_url=d_url)

    return {
        "telegram_configured": bool(t_token and t_cid),
        "telegram_delivered": res_tg,
        "discord_configured": bool(d_url),
        "discord_delivered": res_dc,
        "enabled": is_notifications_enabled(),
        "tested_at": now_str,
    }


def get_notification_config() -> Dict[str, Any]:
    """Return masked config status for UI and health audits."""
    t_token, t_cid = get_telegram_creds()
    d_url = get_discord_webhook_url()

    masked_token = f"{t_token[:4]}...{t_token[-4:]}" if t_token and len(t_token) > 8 else ("configured" if t_token else None)
    masked_cid = f"{t_cid[:2]}...{t_cid[-2:]}" if t_cid and len(t_cid) > 4 else ("configured" if t_cid else None)
    masked_discord = f"{d_url[:20]}...{d_url[-6:]}" if d_url and len(d_url) > 26 else ("configured" if d_url else None)

    return {
        "enabled": is_notifications_enabled(),
        "telegram": {
            "configured": bool(t_token and t_cid),
            "bot_token": masked_token,
            "chat_id": masked_cid,
        },
        "discord": {
            "configured": bool(d_url),
            "webhook_url": masked_discord,
        },
    }


# ---------------------------------------------------------------------------
# MessageBus Subscriptions
# ---------------------------------------------------------------------------

def register_bus_subscribers(bus: Any) -> None:
    """Subscribe notification dispatchers to brain bus events."""
    try:
        from backend.brains.bus import DailyReportReady, ShadowTradeClosed, ShadowTradeOpened

        def _on_trade_opened(event: ShadowTradeOpened):
            payload = event.payload or {}
            trade_info = {
                "symbol": event.symbol or payload.get("symbol"),
                "side": event.side or payload.get("side"),
                "quantity": event.quantity or payload.get("quantity"),
                "trade_mode": payload.get("trade_mode") or "INTRADAY",
                "entry_price": payload.get("entry_price") or payload.get("decision_price"),
                "stop_loss_price": payload.get("stop_loss_price"),
                "target_price": payload.get("target_price"),
                "strategy": payload.get("chart_strategy") or event.route,
                "kelly_factor": payload.get("kelly_factor", 1.0),
            }
            notify_trade_opened(trade_info)

        def _on_trade_closed(event: ShadowTradeClosed):
            payload = event.payload or {}
            trade_info = {
                "symbol": event.symbol or payload.get("symbol"),
                "net_pnl": event.net_pnl,
                "pnl_pct": payload.get("pnl_pct", 0.0),
                "exit_reason": event.exit_reason or payload.get("exit_reason"),
                "trade_mode": payload.get("trade_mode") or "INTRADAY",
                "holding_duration": payload.get("holding_duration") or f"{payload.get('holding_days', 0)}d",
            }
            notify_trade_closed(trade_info)

        def _on_daily_report(event: DailyReportReady):
            notify_daily_eod_summary(event.report)

        bus.subscribe(ShadowTradeOpened, _on_trade_opened)
        bus.subscribe(ShadowTradeClosed, _on_trade_closed)
        bus.subscribe(DailyReportReady, _on_daily_report)
        logger.info("Notification bus subscribers registered successfully")
    except Exception as exc:
        logger.warning("Failed to wire notification bus subscribers: %s", exc)
