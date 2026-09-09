"""PostgreSQL custom-format backup, checksum verification and restore drill."""
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import BACKUP_DIR, BACKUP_RETENTION_DAYS


def _sha256(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def backup_postgres(database: str,target_dir: Path=BACKUP_DIR) -> dict:
    target=Path(target_dir); target.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); destination=target/f"postgres-{stamp}.dump"
    subprocess.run(["pg_dump","--format=custom","--compress=6","--file",str(destination),database],check=True)
    subprocess.run(["pg_restore","--list",str(destination)],check=True,stdout=subprocess.DEVNULL)
    checksum=_sha256(destination); destination.with_suffix(".sha256").write_text(checksum+"\n",encoding="ascii")
    cutoff=datetime.now(timezone.utc)-timedelta(days=BACKUP_RETENTION_DAYS); removed=0
    for path in target.glob("postgres-*.dump"):
        if datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)<cutoff:
            path.unlink(); path.with_suffix(".sha256").unlink(missing_ok=True); removed+=1
    return {"path":str(destination),"sha256":checksum,"bytes":destination.stat().st_size,"verified":True,"removed":removed}


def restore_drill(database: str,dump_path: Path,expected_bars: int,expected_features: int = 0) -> dict:
    drill=f"{database}_restore_drill"
    subprocess.run(["dropdb","--if-exists",drill],check=True)
    try:
        subprocess.run(["createdb",drill],check=True)
        subprocess.run(["pg_restore","--exit-on-error","--no-owner","--dbname",drill,str(dump_path)],check=True)
        result=subprocess.run(["psql","-d",drill,"-Atc","SELECT COUNT(*) FROM live_market_bars;"],check=True,capture_output=True,text=True)
        bars=int(result.stdout.strip())
        if bars!=expected_bars: raise RuntimeError(f"restore count mismatch: expected {expected_bars}, found {bars}")
        features=int(subprocess.run(["psql","-d",drill,"-Atc","SELECT COUNT(*) FROM feature_rows;"],check=True,capture_output=True,text=True).stdout.strip())
        if features!=expected_features: raise RuntimeError(f"feature restore mismatch: expected {expected_features}, found {features}")
        return {"database":drill,"bars":bars,"expected_bars":expected_bars,"features":features,"expected_features":expected_features,"passed":True}
    finally:
        subprocess.run(["dropdb","--if-exists",drill],check=True)


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument("database"); parser.add_argument("--restore-drill",action="store_true"); args=parser.parse_args()
    report=backup_postgres(args.database)
    if args.restore_drill:
        count=int(subprocess.run(["psql","-d",args.database,"-Atc","SELECT COUNT(*) FROM live_market_bars;"],check=True,capture_output=True,text=True).stdout.strip())
        features=int(subprocess.run(["psql","-d",args.database,"-Atc","SELECT COUNT(*) FROM feature_rows;"],check=True,capture_output=True,text=True).stdout.strip())
        report["restore_drill"]=restore_drill(args.database,Path(report["path"]),count,features)
    print(json.dumps(report,indent=2))
