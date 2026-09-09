"""Migrate the operational SQLite application database to PostgreSQL."""
import argparse
import json
import sqlite3
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values


ROOT=Path(__file__).resolve().parent.parent
TABLES=("users","portfolios","settings","holdings","trades","agent_runs","watchlist_snapshots",
        "stock_analyses","sentiment_snapshots","news_articles","news_article_symbols","news_ingestion_logs",
        "ai_gateway_logs","derivative_plans","bot_conversations","bot_messages","monitoring_events",
        "risk_policies","kill_switches","order_intents","order_events","broker_connections",
        "broker_auth_states","revoked_tokens")
ID_TABLES={"users","holdings","trades","agent_runs","watchlist_snapshots","stock_analyses","sentiment_snapshots",
           "news_articles","news_ingestion_logs","ai_gateway_logs","derivative_plans","bot_conversations",
           "bot_messages","monitoring_events","order_intents","order_events"}


def migrate(sqlite_path: Path,database_url: str) -> dict:
    source=sqlite3.connect(sqlite_path); source.row_factory=sqlite3.Row
    target=psycopg2.connect(database_url.replace("postgresql+psycopg2://","postgresql://",1))
    report={}
    try:
        with target:
            with target.cursor() as cursor:
                for table in TABLES:
                    exists=source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
                    if not exists: continue
                    columns=[row[1] for row in source.execute(f"PRAGMA table_info({table})").fetchall()]
                    rows=[tuple(row[column] for column in columns) for row in source.execute(f"SELECT * FROM {table}").fetchall()]
                    if rows:
                        quoted=",".join(f'"{column}"' for column in columns)
                        execute_values(cursor,f'INSERT INTO "{table}"({quoted}) VALUES %s ON CONFLICT DO NOTHING',rows,page_size=1000)
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"'); target_count=cursor.fetchone()[0]
                    if target_count < len(rows): raise RuntimeError(f"{table} reconciliation failed: source {len(rows)}, target {target_count}")
                    report[table]={"source":len(rows),"target":target_count}
                    if table in ID_TABLES:
                        cursor.execute("SELECT pg_get_serial_sequence(%s,'id')",(table,)); sequence=cursor.fetchone()[0]
                        if sequence:
                            cursor.execute(f'SELECT MAX(id) FROM "{table}"'); maximum=cursor.fetchone()[0]
                            cursor.execute("SELECT setval(%s,%s,%s)",(sequence,maximum or 1,bool(maximum)))
                cursor.execute("INSERT INTO schema_migrations(version) VALUES('v3_2_sqlite_application_import') ON CONFLICT DO NOTHING")
        report["status"]="migrated_and_reconciled"; return report
    finally:
        source.close(); target.close()


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--sqlite",type=Path,default=ROOT/"data/nivesh.db"); parser.add_argument("--database",default="postgresql:///nivesh_v3_staging")
    args=parser.parse_args(); print(json.dumps(migrate(args.sqlite,args.database),indent=2))


if __name__=="__main__": main()
