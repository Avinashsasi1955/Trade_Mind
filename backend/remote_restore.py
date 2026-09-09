"""One-shot, in-VPC S3-to-PostgreSQL restore with checksum reconciliation."""
import hashlib
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import boto3
import psycopg2

ROOT=Path(__file__).resolve().parent.parent
POST_RESTORE_MIGRATIONS=("v3_5_security.sql","v3_6_live_paper_pipeline.sql","v3_7_release_operations.sql","v3_8_model_policy_evidence.sql")


def _sha256(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def restore() -> dict:
    if os.getenv("ALLOW_REMOTE_RESTORE")!="YES": raise RuntimeError("ALLOW_REMOTE_RESTORE=YES is required for the one-shot migration task")
    bucket=os.environ["BACKUP_BUCKET"]; key=os.environ["BACKUP_OBJECT_KEY"]; expected_sha=os.environ["BACKUP_SHA256"].lower()
    expected_bars=int(os.getenv("EXPECTED_MARKET_BARS","6435334")); expected_features=int(os.getenv("EXPECTED_FEATURE_ROWS","3488676"))
    destination=Path("/tmp/nivesh-production.dump")
    boto3.client("s3").download_file(bucket,key,str(destination))
    actual_sha=_sha256(destination)
    if actual_sha!=expected_sha: raise RuntimeError("Backup checksum mismatch; restore refused")
    raw=os.environ["DATABASE_URL"].replace("postgresql+psycopg2://","postgresql://",1); parsed=urlparse(raw); query=parse_qs(parsed.query)
    env={**os.environ,"PGPASSWORD":unquote(parsed.password or ""),"PGSSLMODE":query.get("sslmode",["verify-full"])[0],
         "PGSSLROOTCERT":query.get("sslrootcert",["/etc/ssl/certs/rds-global-bundle.pem"])[0]}
    command=["pg_restore","--exit-on-error","--no-owner","--host",parsed.hostname or "","--port",str(parsed.port or 5432),
             "--username",unquote(parsed.username or ""),"--dbname",parsed.path.lstrip("/"),str(destination)]
    try: subprocess.run(command,check=True,env=env)
    except subprocess.CalledProcessError as exc: raise RuntimeError(f"pg_restore failed with exit code {exc.returncode}") from None
    connection=psycopg2.connect(raw)
    try:
        with connection:
          with connection.cursor() as cursor:
            for filename in POST_RESTORE_MIGRATIONS:
                cursor.execute((ROOT/"migrations"/"postgres"/filename).read_text(encoding="utf-8"))
            cursor.execute("SELECT COUNT(*) FROM live_market_bars"); bars=cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM feature_rows"); features=cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM schema_migrations WHERE version IN ('v3_5_security','v3_6_live_paper_pipeline','v3_7_release_operations','v3_8_model_policy_evidence')"); migrations=cursor.fetchone()[0]
    finally: connection.close(); destination.unlink(missing_ok=True)
    if bars!=expected_bars or features!=expected_features or migrations!=4: raise RuntimeError("Restored database reconciliation or post-restore migration check failed")
    return {"verified":True,"sha256":actual_sha,"bars":bars,"features":features,"post_restore_migrations":migrations}


if __name__=="__main__": print(restore())
