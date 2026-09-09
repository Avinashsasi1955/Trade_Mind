"""Bulk-copy the audited SQLite instrument/day-bar warehouse into PostgreSQL v3."""
import argparse
import csv
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _write_instruments(path: Path) -> int:
    master=json.loads((ROOT/"data/security_master/security_master.json").read_text(encoding="utf-8"))
    with path.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.writer(handle); writer.writerow(["exchange","symbol","isin","lot_size","tick_size"])
        for item in master["securities"]:
            writer.writerow([item["exchange"],item["symbol"],item.get("isin") or "",int(item.get("lot") or 1),"0.05"])
    return len(master["securities"])


def _write_bars(path: Path, sqlite_path: Path, limit: int = 0) -> int:
    source=sqlite3.connect(sqlite_path)
    query="SELECT exchange,symbol,interval,timestamp,open,high,low,close,volume,oi,source FROM candles ORDER BY exchange,symbol,timestamp"
    if limit: query += f" LIMIT {int(limit)}"
    count=0
    with path.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.writer(handle); writer.writerow(["exchange","symbol","interval","bar_time","open_price","high_price","low_price","close_price","volume","open_interest","source"])
        cursor=source.execute(query)
        while True:
            rows=cursor.fetchmany(10000)
            if not rows: break
            writer.writerows(rows); count+=len(rows)
            if count%500000==0: print(f"exported {count:,} bars",flush=True)
    source.close(); return count


def migrate(database_url: str, sqlite_path: Path, limit: int = 0) -> dict:
    with tempfile.TemporaryDirectory(prefix="nivesh-pg-") as folder:
        folder=Path(folder); instruments=folder/"instruments.csv"; bars=folder/"bars.csv"; sql=folder/"load.sql"
        instrument_count=_write_instruments(instruments); bar_count=_write_bars(bars,sqlite_path,limit)
        sql.write_text(f"""
\\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE instrument_import(exchange text,symbol text,isin text,lot_size integer,tick_size numeric);
\\copy instrument_import FROM '{instruments}' WITH (FORMAT csv,HEADER true)
INSERT INTO instrument_master(exchange,symbol,isin,instrument_type,lot_size,tick_size,is_fno_eligible,is_active)
SELECT exchange,symbol,NULLIF(isin,''),'EQ',lot_size,tick_size,FALSE,TRUE FROM instrument_import
ON CONFLICT(exchange,symbol) DO UPDATE SET isin=EXCLUDED.isin,lot_size=EXCLUDED.lot_size,tick_size=EXCLUDED.tick_size,is_active=TRUE;
CREATE TEMP TABLE bar_import(exchange text,symbol text,interval text,bar_time timestamptz,open_price numeric,high_price numeric,low_price numeric,close_price numeric,volume bigint,open_interest bigint,source text);
\\copy bar_import FROM '{bars}' WITH (FORMAT csv,HEADER true)
INSERT INTO live_market_bars(instrument_id,interval,bar_time,open_price,high_price,low_price,close_price,volume,open_interest,source,exchange_timestamp)
SELECT i.id,b.interval::bar_interval,b.bar_time,b.open_price,b.high_price,b.low_price,b.close_price,b.volume,NULLIF(b.open_interest,0),b.source,b.bar_time
FROM bar_import b JOIN instrument_master i ON i.exchange=b.exchange AND i.symbol=b.symbol
ON CONFLICT(instrument_id,interval,bar_time) DO UPDATE SET open_price=EXCLUDED.open_price,high_price=EXCLUDED.high_price,
low_price=EXCLUDED.low_price,close_price=EXCLUDED.close_price,volume=EXCLUDED.volume,open_interest=EXCLUDED.open_interest,source=EXCLUDED.source;
DO $$
DECLARE imported bigint;
BEGIN
  SELECT COUNT(*) INTO imported FROM bar_import b JOIN instrument_master i ON i.exchange=b.exchange AND i.symbol=b.symbol;
  IF imported <> {bar_count} THEN RAISE EXCEPTION 'bar reconciliation failed: expected {bar_count}, matched %', imported; END IF;
END $$;
COMMIT;
""",encoding="utf-8")
        subprocess.run(["psql","-v","ON_ERROR_STOP=1",database_url,"-f",str(sql)],check=True)
    return {"instruments_source":instrument_count,"bars_source":bar_count,"status":"migrated_and_reconciled"}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--database",default="nivesh_v3_staging"); parser.add_argument("--sqlite",type=Path,default=ROOT/"data/market_history.db"); parser.add_argument("--limit",type=int,default=0)
    args=parser.parse_args(); print(json.dumps(migrate(args.database,args.sqlite,args.limit),indent=2))


if __name__=="__main__": main()
