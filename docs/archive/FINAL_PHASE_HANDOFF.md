# Final phase handoff — credentials last

Updated: 2 July 2026 IST

## Everything completed without AWS credentials

- Bearish ML cash signals route to eligible puts/futures or are rejected and audited.
- `LIVE_ELIGIBLE=FALSE` and `NIVESH_LIVE_TRADING_ENABLED=0` independently block broker submission.
- AWS CLI and OpenTofu are installed; AWS region is `ap-south-1`.
- The infrastructure creates a three-AZ VPC, NAT gateways, private subnets, security groups, RDS PostgreSQL Multi-AZ, three-node Redis, KMS, Secrets Manager, encrypted backup S3, ECR, ECS, CloudWatch and SNS.
- ECS definitions exist for a concurrency-one FinBERT/Celery worker, exactly one Beat instance, one read-only Kite stream service managing up to three 3,000-token connections, and a one-shot restore task.
- The stream service stores 1m/5m bars, India VIX and derivative OI and raises stale-feed alerts. It has no order-submission code path.
- The 90-session ledger credits only today's completed live session and requires bars, VIX, OI, predictions and zero critical errors.
- The encrypted backup upload verifies SHA-256 metadata; the in-VPC restore verifies the archive before `pg_restore` and reconciles both market bars and ML features afterward.
- Final archive: `data/backups/postgres-20260702T194218Z.dump`, 989,190,999 bytes, SHA-256 `aa7d88e60e500040fc0c9d7e0d2ba59e54ef3044e49874b72412a6c7665a7ce4`.
- OpenTofu provider validation and 44 automated tests pass.
- The Linux/amd64 production image `nivesh-ai:v3.4` is 437,214,460 bytes (image ID `sha256:4bda7d096c42…`) and passed CPU PyTorch, confidence-floor, streaming-module and RDS CA smoke tests.

## Actions after AWS authentication

Do not paste credentials into chat. Authenticate locally with `aws configure sso` or `aws configure`, then verify the intended account:

```bash
aws sts get-caller-identity
```

Review and explicitly approve the billable plan:

```bash
cd infra/aws-ha
tofu plan -out=production.tfplan
tofu apply production.tfplan
```

The first apply keeps worker, Beat and stream desired counts at zero. This prevents tasks from starting before the image, secrets and restored database exist.

Build and push the immutable `v3.4` image:

```bash
ECR_REPOSITORY="$(tofu output -raw ecr_repository_url)"
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin "${ECR_REPOSITORY%%/*}"
docker tag nivesh-ai:v3.4 "${ECR_REPOSITORY}:v3.4"
docker push "${ECR_REPOSITORY}:v3.4"
```

Open the emitted `live_integrations_secret_arn` in Secrets Manager and add `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN`, `NIVESH_NEWS_API_KEY`, and `LIVE_ELIGIBLE` set to `FALSE`. The Kite access token must come from the documented interactive login and be refreshed after expiry; no TOTP automation is included.

Upload the verified baseline:

```bash
python ../../scripts/upload_backup_to_s3.py \
  ../../data/backups/postgres-20260702T194218Z.dump \
  --bucket "$(tofu output -raw backup_bucket)" \
  --key baseline/postgres-v3.4.dump \
  --kms-key "$(tofu output -raw backup_kms_key_arn)"
```

Run the private restore task; the helper waits for completion and fails unless ECS reports exit code zero. Confirm its CloudWatch output reports exactly 6,435,334 bars and 3,488,676 feature rows:

```bash
cd ../..
python scripts/run_aws_restore.py
```

Then enable shadow services only:

```bash
tofu -chdir=infra/aws-ha apply \
  -var='worker_desired_count=1' \
  -var='beat_desired_count=1' \
  -var='stream_desired_count=1'
```

## Gates that cannot be completed in advance

- AWS provisioning creates chargeable resources and requires explicit operator approval.
- Zerodha and news credentials must be entered by the account owner.
- A valid Kite session requires the documented interactive login/token exchange.
- Ninety completed live sessions require elapsed exchange sessions. Rejected/incomplete days do not count.
- The model remains `live_eligible=false`; the current untouched holdout remains weak even though walk-forward profit factor is 1.31.
- Forward testing cannot guarantee future profitability.
