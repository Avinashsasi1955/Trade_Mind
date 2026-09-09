# Production PostgreSQL and Redis HA/TLS

This OpenTofu/Terraform stack creates a dedicated three-AZ VPC, public NAT subnets, isolated private application/database subnets, private RDS PostgreSQL Multi-AZ and an ElastiCache Redis replication group. PostgreSQL enforces TLS, both services encrypt data at rest, Redis requires TLS plus an auth token, and credentials are generated into AWS Secrets Manager.

Prerequisites: OpenTofu/Terraform, an authenticated AWS operator role and explicit approval for chargeable RDS, ElastiCache and three NAT gateway resources. Subscribe the emitted SNS topic to the production pager after creation.

Before the first production plan, create a separate versioned/KMS-encrypted Terraform-state bucket, copy `backend.hcl.example` to the ignored `backend.hcl`, and initialize with `tofu init -backend-config=backend.hcl`. Do not keep production state locally; it contains sensitive generated values.

```bash
cd infra/aws-ha
tofu init -backend-config=backend.hcl
tofu plan -out=production.tfplan
tofu apply production.tfplan
```

Retrieve `DATABASE_URL` and `REDIS_URL` at runtime from the emitted Secrets Manager ARN. Mount Amazon's current RDS global CA bundle at `/etc/ssl/certs/rds-global-bundle.pem`; do not weaken `sslmode=verify-full` or `ssl_cert_reqs=required`.

After provisioning, apply all SQL migrations in order, migrate data, execute `backend.postgres_backup --restore-drill` against a non-production restore target, test RDS failover and Redis primary promotion, and confirm alerts reach the on-call operator. Keep `NIVESH_LIVE_TRADING_ENABLED=0` during every infrastructure drill.

Public web ingress is intentionally absent by default. It creates billable ALB/WAF resources only when all of the following are explicitly supplied:

```hcl
enable_web_ingress   = true
web_desired_count    = 1
public_origin        = "https://trade.example.com"
administrator_emails = "operator@example.com"
acm_certificate_arn  = "arn:aws:acm:ap-south-1:ACCOUNT:certificate/ID"
cognito_domain_prefix = "globally-unique-nivesh-login"
```

The Cognito pool permits administrator-created users only, verifies email and requires TOTP MFA through Cognito managed login before ALB forwards a request. Point the domain to the emitted load-balancer DNS only after the target is healthy. Never enable either live-trading fuse as part of an infrastructure deployment.
