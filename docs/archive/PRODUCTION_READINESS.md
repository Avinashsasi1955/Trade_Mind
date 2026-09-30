# Nivesh AI — Broker-independent software completion

The broker-independent safety and execution software is implemented. Live transmission remains double locked by both a valid daily broker session and `NIVESH_LIVE_TRADING_ENABLED=1`.

## Implemented

- HttpOnly, SameSite session cookie; persistent browser-token storage removed.
- Token revocation on logout, login/API rate limiting, CSP and security headers.
- Persistent risk policies and emergency kill switch.
- Automatic realised-loss kill-switch activation.
- Price-protected LIMIT/SL-only execution policy.
- Cash, inventory, order-value, position, gross-exposure, order-frequency, confidence, derivative-permission, quote-age and daily-loss checks.
- Idempotent order-intent identifiers.
- Human approval before broker submission.
- Immutable order-event audit trail.
- Zerodha interactive login/session-exchange implementation.
- Zerodha place/cancel/orders/positions/margins REST methods.
- Kite binary WebSocket parser, subscriptions, reconnect backoff, heartbeat timestamps and order-event handling.
- Broker-order reconciliation state machine.
- Partial-fill recognition and broker/local position-discrepancy reporting.
- Emergency cancellation attempts for open Nivesh-tagged broker orders when the kill switch activates.
- Scheduled reconciliation when live mode is explicitly enabled.
- Execution Control dashboard, broker readiness, approval queue and kill switch.
- Operations monitoring, daily consistent SQLite backups and retention.
- Container, health check and CI test workflow.

## Activation prerequisites

1. Create a Kite Connect application and configure its redirect URL.
2. Set `KITE_API_KEY`, `KITE_API_SECRET`, and production `KITE_REDIRECT_URL` in a secret manager.
3. Install `requirements.txt` and complete interactive Zerodha login each trading session.
4. Keep `NIVESH_LIVE_TRADING_ENABLED=0` through historical replay, shadow mode and supervised broker-connected paper validation.
5. Reconcile cost estimates to actual contract notes and load authoritative instrument/corporate-action data.
6. Complete exchange/broker algo requirements, static-IP configuration, tagging and operational review.
7. Change `NIVESH_SECRET`, enable HTTPS and set `NIVESH_COOKIE_SECURE=1`.
8. Only then enable the live flag for manually approved, tightly capped orders.

## Deliberately not automatic

- The AI model cannot call the broker adapter.
- A model prediction cannot approve an intent.
- The kill switch cannot be reset by the model.
- Credentials are not stored in SQLite or exposed to the browser/model.
- Live mode does not activate merely because Zerodha credentials exist.
