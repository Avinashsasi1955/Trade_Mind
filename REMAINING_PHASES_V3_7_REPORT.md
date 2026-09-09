# Remaining phases v3.7 report

Updated: 4 July 2026 IST

## Phase 4 — formal shadow orchestration

Software complete:

- Audited 08:15 IST pre-market gate.
- Instrument sync, session open/finalization, cost audit and PSI schedules.
- Weekend, closed-session and special-session calendar handling.
- Minimum bar, instrument, VIX, OI and prediction coverage.
- A blocked preflight cannot open or credit a session.

Current preflight correctly blocks on the unsigned model, missing broker tokens and missing Kite session. It never permits orders.

## Phase 5 — retrain and signed model release

Software complete:

- `python -m backend.model_release verify` verifies HMAC without exposing the key.
- The explicit retrain command performs training, signature verification, walk-forward validation and records a release manifest.
- AWS has a private one-shot model-release task using only runtime secrets.
- Production workers require a signed model.

Evidence not complete: the current v2.5 artifact remains unsigned. A production artifact key must exist in Secrets Manager before retraining.

## Phase 6 — managed deployment automation

Software complete:

- AWS image default updated to v3.7.
- Restore applies v3.5–v3.7 and verifies migration and row-count reconciliation.
- Cognito MFA, ALB, WAF, private ECS, RDS, Redis, KMS, Secrets Manager, backups and alerts remain defined.
- Worker, Beat and Stream desired counts remain zero until explicitly enabled.

External gate: AWS authentication, domain/ACM inputs and approval of billable resources are required.

## Phase 7 — 90-session forward evidence

Cannot be pre-completed. It requires 90 actual completed exchange sessions with broker-connected live data. Historical replay is deliberately rejected as session credit.

Current state: 0/90 sessions, 0/20 live-cost sessions and 0/20 verified derivative executions.

## Phase 8 — real-order automation

Not authorized or promoted. It requires broker/exchange eligibility confirmation, independent security testing, contract-note reconciliation, supervised microscopic-capital trials and every promotion gate passing.

No software result can guarantee profitable trades. Both execution fuses remain disabled.

## Verification

- 63/63 tests passed with PostgreSQL and Redis.
- OpenTofu configuration validates successfully.
- Docker v3.7 Web, Worker, Beat, PostgreSQL and Redis are healthy.
- Formal preflight is `BLOCKED` with `orders_allowed=false`.
- Current blockers: unsigned model, unsynchronized broker instruments, weekend calendar and missing Kite session.
