#!/usr/bin/env python3
"""Post-Market Execution & Verification Runner.

Target Window: After 15:30 IST (Market Close)
Purpose: Guides and validates the two key post-market deliverables:
  1. Phase 4: Frontend Native ES Module Breakdown (app.js -> metrics.js, sse.js, trade_book.js)
  2. Step 4 (Phase C): Backend Regime Router, GEX Engine, Spider Bot & Dual-Tier Memo Integration
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent


def check_market_session() -> tuple[bool, str]:
    now_ist = datetime.now(IST)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)

    is_live = market_open <= now_ist < market_close
    remaining_mins = max(0, int((market_close - now_ist).total_seconds() / 60))

    if is_live:
        msg = f"MARKET IN SESSION ({now_ist.strftime('%H:%M:%S IST')}). ~{remaining_mins} minutes until 15:30 IST close."
        return True, msg
    else:
        msg = f"MARKET CLOSED ({now_ist.strftime('%H:%M:%S IST')}). Safe for post-market modifications and service wiring."
        return False, msg


def verify_frontend_modules() -> dict:
    modules_dir = ROOT / "frontend" / "modules"
    expected = ["metrics.js", "sse.js", "trade_book.js"]
    status: dict = {
        "modules_dir_exists": modules_dir.is_dir(),
        "files": {},
        "index_html_module": False,
        "app_js_line_count": 0,
    }

    for fname in expected:
        fpath = modules_dir / fname
        exists = fpath.is_file()
        syntax_ok = False
        if exists:
            res = subprocess.run(["node", "--check", str(fpath)], capture_output=True, text=True)
            syntax_ok = (res.returncode == 0)
        status["files"][fname] = {"exists": exists, "syntax_ok": syntax_ok}

    index_html = ROOT / "frontend" / "index.html"
    if index_html.is_file():
        content = index_html.read_text(encoding="utf-8")
        status["index_html_module"] = 'type="module"' in content and "app.js" in content

    app_js = ROOT / "frontend" / "app.js"
    if app_js.is_file():
        status["app_js_line_count"] = len(app_js.read_text(encoding="utf-8").splitlines())

    return status


def verify_phase_c_backend() -> dict:
    status: dict = {}
    files_to_check = {
        "regime_router": ROOT / "backend" / "regime_router.py",
        "spider_bot": ROOT / "backend" / "spider_bot.py",
        "intelligence_memory": ROOT / "backend" / "intelligence_memory.py",
        "chart_gate": ROOT / "backend" / "strategies" / "chart_gate.py",
    }
    for name, path in files_to_check.items():
        status[name] = path.is_file()
    return status


def print_dashboard() -> None:
    is_live, session_msg = check_market_session()
    fe_status = verify_frontend_modules()
    be_status = verify_phase_c_backend()

    print("=" * 70)
    print(" 🚀 TRADEMIND POST-MARKET EXECUTION RUNNER")
    print("=" * 70)
    print(f" Session State : {session_msg}")
    print("-" * 70)

    print(" 📦 1. FRONTEND ES MODULE SPLIT STATUS (app.js -> modules/):")
    print(f"    • modules/ Directory Exists : {'✅ YES' if fe_status['modules_dir_exists'] else '❌ PENDING'}")
    for fname, info in fe_status["files"].items():
        tag = "✅ READY" if (info["exists"] and info["syntax_ok"]) else ("⚠️ SYNTAX ERROR" if info["exists"] else "❌ PENDING")
        print(f"    • frontend/modules/{fname:<14} : {tag}")
    print(f"    • index.html script type=module: {'✅ YES' if fe_status['index_html_module'] else '❌ PENDING'}")
    print(f"    • app.js Current Line Count    : {fe_status['app_js_line_count']} lines")
    print("-" * 70)

    print(" 🧠 2. PHASE C BACKEND DELIVERABLES STATUS:")
    for name, exists in be_status.items():
        print(f"    • backend/{name:<20}: {'✅ PRESENT' if exists else '❌ MISSING'}")
    print("-" * 70)

    print(" 📋 ACTION CHECKLIST FOR POST-MARKET HOURS (Post-15:30 IST):")
    if is_live:
        print("    🔒 LOCK IN PLACE: Zero code changes to trading pipeline during market hours.")
        print("    ⏳ Wait for 15:30 IST market close before executing refactoring.")
    else:
        print("    🟢 MARKET CLOSED: Safe to begin step-by-step execution!")

    print("\n    Step A: Extract frontend/modules/metrics.js (tickers, formatPnlEl, stats)")
    print("    Step B: Extract frontend/modules/sse.js (SSE listener, exponential backoff)")
    print("    Step C: Extract frontend/modules/trade_book.js (paper trades table, modal)")
    print("    Step D: Update index.html to <script type=\"module\" src=\"app.js\"></script>")
    print("    Step E: Wire RegimeRouter & GEX Flip Point into live inference signal gating")
    print("    Step F: Connect SpiderBot auto-balancing (|Delta| > 0.20) into worker task")
    print("    Step G: Run test suite (.venv/bin/python -m unittest discover -s tests)")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-Market Execution & Verification Runner")
    parser.add_argument("--check-only", action="store_true", help="Run status check without prompt")
    parser.add_argument("--run-tests", action="store_true", help="Run full test suite")
    args = parser.parse_args()

    if args.run_tests:
        print("[*] Running test suite via .venv/bin/python...")
        res = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])
        sys.exit(res.returncode)

    print_dashboard()


if __name__ == "__main__":
    main()
