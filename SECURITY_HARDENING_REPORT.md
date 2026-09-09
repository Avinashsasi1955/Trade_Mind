# Web application security hardening report

Updated: 3 July 2026 IST

## Implemented and verified

- Production startup now fails closed unless PostgreSQL, secure cookies, explicit HTTPS origins, a strong session secret, a separate model-artifact signing key, disabled demo/signup modes and an administrator allow-list are configured.
- Cookie sessions use `HttpOnly`, `SameSite=Strict`, `Secure` and the `__Host-` prefix in production.
- Cookie-authenticated write requests require an origin check and double-submit CSRF token. Bearer-token clients are not exposed to ambient-cookie CSRF.
- JWT-like session tokens now validate a fixed algorithm/type, issuer, audience, timestamps, integer subject and random token ID.
- Password hashing increased to PBKDF2-HMAC-SHA256 with 600,000 iterations. Existing hashes upgrade after a successful login.
- Production signup and demo-account creation are disabled by default. Startup rejects a database containing the known bundled demo account.
- Operations and ML mutation endpoints require an administrator allow-list in production.
- Health responses no longer reveal broker/mode state.
- HSTS, CSP, frame denial, MIME sniffing prevention, opener/resource policies, restrictive referrer and permissions policies are emitted.
- The pinned CDN script has Subresource Integrity and CORS verification.
- Browser rendering now escapes additional market, strategy and chat values; CSP blocks inline-script execution.
- Static serving has a strict allow-list and cannot return `.env`, databases or arbitrary files.
- Access logs remove query strings so Zerodha `request_token` values are not recorded. Production exception logs omit exception messages.
- Request bodies are capped at 1 MB and in-process rate-limit storage is bounded.
- FinBERT is pinned to an exact model revision, requires SafeTensors and disables remote model code.
- The active estimator serialization is HMAC authenticated before deserialization; unsigned legacy artifacts are rejected in production.
- Runtime and model-artifact secrets are independently generated and stored in KMS-encrypted AWS Secrets Manager.
- Python 3.12 dependency audit: no known vulnerabilities after upgrading Transformers, PyTorch and HTTP dependencies.
- Production transport now uses a native ASGI adapter under Uvicorn; the `http.server` entry point is retained only for loopback development.
- Zerodha broker sessions use AES-GCM authenticated encryption in TLS Redis, are bound to the user ID and expire automatically; multi-process workers no longer depend on process memory.
- Failed-login windows are persisted in PostgreSQL/SQLite, use constant-work password verification for unknown accounts, return generic errors and clear only after a successful login. This now works across web tasks.
- Production identity is delegated to Amazon Cognito managed login. Accounts are administrator-created, email-verified and protected by mandatory TOTP MFA; Cognito handles password recovery.
- The ALB authenticates users before forwarding. The application independently verifies the ES256 `x-amzn-oidc-data` signature, exact ALB signer ARN, expiry, subject and verified-email claim before mapping the Cognito subject to a local account.
- Local password login is rejected in production. A CSRF bootstrap endpoint establishes only the application CSRF cookie after managed authentication.
- Cognito/ALB logout expires application and ALB session-cookie shards and redirects through the Cognito logout endpoint.
- AWS now has an explicit, disabled-by-default web ingress: private ECS tasks, HTTPS-only ALB, ACM certificate input, invalid-header rejection, access logging, AWS WAF managed rules, IP reputation and rate limiting.
- Automated tests: 55 passed locally and 55 passed with PostgreSQL integration enabled.
- OpenTofu AWS validation: passed.
- End-to-end Uvicorn smoke: login, secure cookies, CSRF-protected paper-agent request and graceful shutdown passed.

## No secret exposure found

- No AWS access key or private-key pattern was found in the project source.
- `.env` is excluded from Git and Docker build context.
- No HTTP endpoint returns process environment variables or secret values.
- Status endpoints return only configured/not-configured booleans.

## Production blockers still open

1. The bundled known demo account must be removed or have its identity and password replaced before migrating users.
2. The current v2.5 model artifact predates HMAC signing. Retrain it under `NIVESH_MODEL_ARTIFACT_KEY`; production intentionally rejects it meanwhile.
3. A real domain, validated ACM certificate, DNS record, Cognito domain prefix, administrator allow-list and explicit approval for billable ALB/WAF/Cognito resources are required before setting `enable_web_ingress=true`.
4. A separate KMS-encrypted/versioned Terraform-state bucket must be created and supplied through the ignored `backend.hcl`; production state must not remain local.
5. Independent penetration testing, SAST/DAST in CI, container/SBOM scanning, restore/failover tests and incident-response exercises remain required.
6. AWS is not authenticated or provisioned, and no production secrets have been added. Both live execution fuses remain disabled.

## Deployment decision

Do not enable web ingress or add broker-write credentials yet. The local paper-trading preview and hardened ASGI transport are usable; production promotion remains blocked by the items above.
