"""Stream SQLite history metadata and ML research tables into PostgreSQL v3.2."""
import argparse
import csv
import io
import json
import sqlite3
from pathlib import Path

import psycopg2

ROOT=Path(__file__).resolve().parent.parent

TABLES={
    "corporate_actions":("exchange,symbol,effective_date,action_type,ratio_from,ratio_to,cash_amount,source,created_at",None),
    "corporate_action_ingestions":("exchange,period_start,period_end,url,sha256,row_count,status,imported_at",None),
    "universe_membership":("exchange,symbol,valid_from,valid_to,status,source,created_at",None),
    "feature_rows":("exchange,symbol,timestamp,feature_set,features,label,label_return,source_hash,created_at",None),
    "model_versions":("id,model_name,version,feature_set,algorithm,payload,training_start,training_end,training_samples,metrics,status,created_at","model_versions_id_seq"),
    "validation_runs":("id,model_version,validation_type,period_start,period_end,symbols,trades,metrics,created_at","validation_runs_id_seq"),
    "shadow_predictions":("id,model_version,exchange,symbol,timestamp,probability,signal,realised_return,status,created_at","shadow_predictions_id_seq"),
    "drift_reports":("id,model_version,feature,reference_mean,current_mean,standardised_shift,status,created_at","drift_reports_id_seq"),
}


def _copy_table(source,connection,table,columns,batch_size=25000):
    source_count=source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table}"); target_count=cursor.fetchone()[0]
    if target_count==source_count: return {"source":source_count,"target":target_count,"status":"already_reconciled"}
    if target_count: raise RuntimeError(f"{table}: target has {target_count} rows but source has {source_count}; refusing an ambiguous merge")
    names=columns.split(","); query=f"SELECT {columns} FROM {table}"; reader=source.execute(query); copied=0
    while True:
        rows=reader.fetchmany(batch_size)
        if not rows: break
        buffer=io.StringIO(); writer=csv.writer(buffer,lineterminator="\n")
        writer.writerows(rows); buffer.seek(0)
        with connection.cursor() as cursor:
            cursor.copy_expert(f"COPY {table} ({','.join(names)}) FROM STDIN WITH (FORMAT CSV)",buffer)
        connection.commit(); copied+=len(rows)
        if copied%250000==0: print(f"{table}: copied {copied:,}/{source_count:,}",flush=True)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table}"); target_count=cursor.fetchone()[0]
    if target_count!=source_count: raise RuntimeError(f"{table}: reconciliation failed ({source_count} != {target_count})")
    return {"source":source_count,"target":target_count,"status":"migrated"}


def _copy_history_metadata(source,connection):
    result={}
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM history_sync_coverage"); existing=cursor.fetchone()[0]
        source_count=source.execute("SELECT COUNT(*) FROM sync_coverage").fetchone()[0]
        if not existing:
            cursor.execute("""INSERT INTO history_sync_coverage(instrument_id,interval,first_timestamp,last_timestamp,bars,status,error,updated_at)
                SELECT i.id,s.interval::bar_interval,s.first_timestamp::timestamptz,s.last_timestamp::timestamptz,s.bars,s.status,s.error,s.updated_at::timestamptz
                FROM (SELECT NULL::text exchange,NULL::text symbol,NULL::text interval,NULL::text first_timestamp,NULL::text last_timestamp,0::bigint bars,NULL::text status,NULL::text error,NULL::text updated_at WHERE FALSE) s
                JOIN instrument_master i ON FALSE""")
            buffer=io.StringIO(); writer=csv.writer(buffer,lineterminator="\n")
            writer.writerows(source.execute("SELECT exchange,symbol,interval,first_timestamp,last_timestamp,bars,status,error,updated_at FROM sync_coverage")); buffer.seek(0)
            cursor.execute("CREATE TEMP TABLE coverage_import(exchange text,symbol text,interval text,first_timestamp text,last_timestamp text,bars bigint,status text,error text,updated_at text) ON COMMIT DROP")
            cursor.copy_expert("COPY coverage_import FROM STDIN WITH (FORMAT CSV)",buffer)
            cursor.execute("""INSERT INTO history_sync_coverage(instrument_id,interval,first_timestamp,last_timestamp,bars,status,error,updated_at)
                SELECT i.id,c.interval::bar_interval,NULLIF(c.first_timestamp,'')::timestamptz,NULLIF(c.last_timestamp,'')::timestamptz,c.bars,c.status,c.error,c.updated_at::timestamptz
                FROM coverage_import c JOIN instrument_master i ON i.exchange=c.exchange AND i.symbol=c.symbol""")
        connection.commit()
        cursor.execute("SELECT COUNT(*) FROM history_sync_coverage"); target=cursor.fetchone()[0]
        if target!=source_count: raise RuntimeError(f"history coverage reconciliation failed ({source_count} != {target})")
        result["history_sync_coverage"]={"source":source_count,"target":target}

        source_count=source.execute("SELECT COUNT(*) FROM history_ingestions").fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM history_ingestions"); existing=cursor.fetchone()[0]
        if not existing:
            buffer=io.StringIO(); writer=csv.writer(buffer,lineterminator="\n")
            writer.writerows(source.execute("SELECT exchange,trade_date,source,url,sha256,byte_count,row_count,status,error,imported_at FROM history_ingestions")); buffer.seek(0)
            cursor.copy_expert("COPY history_ingestions(exchange,trade_date,source,url,sha256,byte_count,row_count,status,error,imported_at) FROM STDIN WITH (FORMAT CSV)",buffer)
        connection.commit(); cursor.execute("SELECT COUNT(*) FROM history_ingestions"); target=cursor.fetchone()[0]
        if target!=source_count: raise RuntimeError(f"history ingestions reconciliation failed ({source_count} != {target})")
        result["history_ingestions"]={"source":source_count,"target":target}
    return result


def migrate(database,history_path,research_path):
    dsn=database.replace("postgresql+psycopg2://","postgresql://",1)
    connection=psycopg2.connect(dsn) if "://" in dsn or "=" in dsn else psycopg2.connect(dbname=dsn)
    history=sqlite3.connect(history_path); research=sqlite3.connect(research_path); report={}
    try:
        report.update(_copy_history_metadata(history,connection))
        for table,(columns,sequence) in TABLES.items():
            report[table]=_copy_table(research,connection,table,columns)
            if sequence:
                with connection.cursor() as cursor: cursor.execute(f"SELECT setval(%s,COALESCE((SELECT MAX(id) FROM {table}),1),true)",(sequence,))
                connection.commit()
    finally:
        history.close(); research.close(); connection.close()
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--database",default="nivesh_v3_staging")
    parser.add_argument("--history",type=Path,default=ROOT/"data/market_history.db")
    parser.add_argument("--research",type=Path,default=ROOT/"data/ml_research.db")
    args=parser.parse_args(); print(json.dumps(migrate(args.database,args.history,args.research),indent=2))
