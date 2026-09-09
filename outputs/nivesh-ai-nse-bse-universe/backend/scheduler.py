import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .database import connect
from .service import refresh_market_analyses, refresh_sentiments, run_agent


def _worker():
    last_scan = {}
    last_minute_analysis = {}
    while True:
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        market_hours = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
        if market_hours:
            with connect() as db:
                users = db.execute("SELECT user_id FROM settings WHERE auto_scan=1").fetchall()
                for row in users:
                    user_id = row["user_id"]
                    if time.time() - last_minute_analysis.get(user_id, 0) >= 60:
                        refresh_market_analyses(db, user_id)
                        refresh_sentiments(db, user_id, int(time.time() // 60))
                        last_minute_analysis[user_id] = time.time()
                    if time.time() - last_scan.get(user_id, 0) >= 1800:
                        # Scheduled runs refresh intelligence only; manual approval places paper orders.
                        run_agent(db, user_id, execute_trade=False)
                        last_scan[user_id] = time.time()
        time.sleep(60)


def start_scheduler():
    thread = threading.Thread(target=_worker, name="nivesh-market-scanner", daemon=True)
    thread.start()
    return thread
