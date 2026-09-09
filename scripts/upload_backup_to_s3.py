"""Upload the verified PostgreSQL archive with mandatory KMS encryption."""
import argparse
import hashlib
import json
from pathlib import Path

import boto3


def sha256(path):
    digest=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def upload(path: Path,bucket: str,key: str,kms_key: str):
    checksum=sha256(path); client=boto3.client("s3")
    client.upload_file(str(path),bucket,key,ExtraArgs={"ServerSideEncryption":"aws:kms","SSEKMSKeyId":kms_key,
        "Metadata":{"sha256":checksum,"bars":"6435334","features":"3488676"}})
    head=client.head_object(Bucket=bucket,Key=key)
    if head.get("Metadata",{}).get("sha256")!=checksum: raise RuntimeError("S3 metadata checksum reconciliation failed")
    return {"bucket":bucket,"key":key,"bytes":path.stat().st_size,"sha256":checksum,"kms_key":kms_key,"uploaded":True}


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("path",type=Path); parser.add_argument("--bucket",required=True); parser.add_argument("--key",required=True); parser.add_argument("--kms-key",required=True)
    args=parser.parse_args(); print(json.dumps(upload(args.path,args.bucket,args.key,args.kms_key),indent=2))
