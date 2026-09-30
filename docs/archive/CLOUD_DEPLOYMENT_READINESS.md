# Cloud deployment readiness

Updated: 2 July 2026 IST

## Completed locally

- Bearish ML cash-equity signals now pass through a mandatory derivative router.
- Active F&O symbols route to a fresh-price, lot-sized long Put by default or a short Future when explicitly selected.
- Missing chains or missing derivative prices produce `RISK_REJECTED` plus immutable `BEARISH_ROUTE_REJECTED` events.
- Daily Kite instrument synchronization marks cash equities as F&O-eligible only when active derivative contracts exist.
- Derivative orders still require the account risk policy's `allow_derivatives` permission and human approval.
- `LIVE_ELIGIBLE=FALSE` is an independent server-side submission fuse in addition to `NIVESH_LIVE_TRADING_ENABLED=0`.
- AWS CLI 2.35.14 is installed; default region is `ap-south-1`.
- The AWS stack now builds its own three-AZ VPC, NAT egress, private subnets, security groups, RDS PostgreSQL Multi-AZ, three-node Redis, TLS/KMS, Secrets Manager and SNS/CloudWatch alerts.
- OpenTofu provider validation passes.
- Cognito managed login, administrator-only enrollment, verified email, mandatory TOTP MFA and ALB-signed claim verification are implemented but remain undeployed while web ingress is disabled.
- Encrypted S3 backup transfer, checksum-verifying in-VPC restore, immutable ECR, ECS worker/Beat/stream and CloudWatch logs are implemented.
- The formal 90-session ledger rejects incomplete sessions and permanently records both execution fuses as false.

## Credential handling

Run `aws configure sso` with an AWS IAM Identity Center profile when available. If the account requires access keys, run `aws configure` directly in the macOS Terminal. Never paste keys into chat, source code, `.env`, shell arguments or screenshots.

The infrastructure creates `nivesh-production/live-integrations` containing only `LIVE_ELIGIBLE=FALSE`. After provisioning, use the AWS console to add `KITE_API_KEY`, `KITE_API_SECRET` and `NIVESH_NEWS_API_KEY`. Terraform ignores subsequent secret-value changes so it does not overwrite or import those values into source control.

## Corrections to the proposed deployment sequence

- `ap-south-1` is the selected deployment region, but it does not guarantee exchange colocation or deterministic low latency.
- Kite Connect requires its documented interactive login/token exchange and TOTP-enabled account. This project will not implement an automated TOTP or 2FA bypass. Access tokens expire at 6 AM the following day under the documented flow.
- A private RDS endpoint cannot accept a direct restore from an arbitrary local laptop. The backup must be transferred through an encrypted S3/VPC migration job, VPN, or an audited SSM-controlled runner inside the VPC.
- Ninety sessions are completed market sessions, not merely consecutive calendar days. They provide forward evidence; they cannot guarantee that a 1.31 historical profit factor will persist.

## Blocking operator actions

1. Sign into AWS in the opened browser and authenticate the CLI locally.
2. Review the OpenTofu plan and estimated charges, especially three NAT gateways, RDS and ElastiCache.
3. Explicitly approve `tofu apply`; this creates billable resources.
4. Add integration credentials to Secrets Manager yourself.
5. Choose and approve a private migration channel and remote application runtime before data upload.
6. Complete the daily Kite login and 90-session shadow collection without enabling either execution fuse.
