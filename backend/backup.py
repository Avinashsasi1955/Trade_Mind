"""Consistent SQLite backups with bounded retention."""
import sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
from typing import Dict

from .config import BACKUP_DIR,BACKUP_RETENTION_DAYS,DATABASE_PATH,DATABASE_URL,MARKET_HISTORY_PATH,ML_RESEARCH_PATH


def backup_databases(target_dir:Path=BACKUP_DIR)->Dict:
    target=Path(target_dir); target.mkdir(parents=True,exist_ok=True); stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); created=[]
    for source in (DATABASE_PATH,MARKET_HISTORY_PATH,ML_RESEARCH_PATH):
        if not Path(source).exists(): continue
        destination=target/f"{Path(source).stem}-{stamp}.db"
        with sqlite3.connect(source) as src,sqlite3.connect(destination) as dst: src.backup(dst)
        created.append(str(destination))
    cutoff=datetime.now(timezone.utc)-timedelta(days=BACKUP_RETENTION_DAYS); removed=0
    for path in target.glob("*.db"):
        if datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)<cutoff: path.unlink(); removed+=1
    result={"created":created,"removed":removed,"retention_days":BACKUP_RETENTION_DAYS}
    if DATABASE_URL:
        from .postgres_backup import backup_postgres
        result["postgres"]=backup_postgres(DATABASE_URL.replace("postgresql+psycopg2://","postgresql://",1),target)
    return result


if __name__=="__main__": print(backup_databases())
