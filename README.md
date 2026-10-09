# Nivesh.ai — full-stack paper-trading agent

A self-contained Indian-market paper-trading application with a premium dashboard, SQLite persistence, signed authentication, scheduled market scans, explainable strategy scoring, risk sizing, and simulated execution.

## Run

Requires Python 3.12. Local preview dependencies are installed from `requirements.txt`; production uses the ASGI/Uvicorn entry point.

Copy `.env.example` to `.env` and add provider credentials there. The server loads this file at startup without overriding environment variables supplied by a production secret manager. `.env` is excluded from Git and Docker builds.

```bash
python scripts/sync_trading_calendar.py --verify-today
python3 -m backend.server
```

The command above is loopback development only. Production containers run `uvicorn backend.asgi:app` behind the HTTPS load balancer and WAF. Note that the NSE trading calendar for 2026 is loaded from `data/nse_holidays_2026.json`. A CRITICAL alert is raised if fewer than 30 future session days remain.

Open `http://127.0.0.1:4173` and sign in with:

- Email: `arjun@example.com`
- Password: `nivesh123`

Run tests:

```bash
python scripts/sync_trading_calendar.py
python3 -m unittest discover -s tests -v
```

## Included

- PBKDF2 password hashing and HMAC-SHA256 signed bearer tokens
- SQLite users, portfolios, settings, holdings, trades, watchlists, and agent-run audit logs
- Deterministic NSE-style market simulator for safe offline development
- Explainable ensemble: momentum, volume breakout, RSI confirmation, and VWAP structure
- Minute-by-minute 1m/15m analysis with BOS, CHOCH, MSS, OB, FVG/IMB, EQH/EQL, BSL/SSL, SR, POI, SL/TP and R:R
- Optional OpenAI-compatible narrative model; deterministic calculations remain authoritative
- Provider router for Gemini, Ollama, Anthropic Claude, OpenAI, or any OpenAI-compatible endpoint
- Auditable sentiment analysis with provider ingestion, deduplication, event/negation detection, source credibility, novelty and time decay, plus price/volume/breadth confirmation
- Minute market updater for indices, quotes, breadth, sentiment, and persisted snapshots
- AI gateway with provider routing, response cache, per-user limits, circuit breaker, fallback, and request audit log
- Original TradingView-style chart workspace with 1m/5m/15m/1H/1D candles, EMA 20/50, Bollinger Bands, volume, RSI, and crosshair values
- One-year backtest lab with chronological 70/30 holdout testing, next-open execution, estimated costs, directional accuracy, win rate, profit factor, Sharpe ratio, drawdown, benchmark comparison, and an auditable trade list
- Filterable 36-playbook strategy catalogue covering hedging, bullish, bearish, neutral/income, volatility, relative-value, and systematic approaches, with corrected payoff definitions and explicit tail-risk labels
- Searchable official security master containing 2,372 NSE equity records and 4,648 active BSE company-equity records (7,020 exchange records), with fund units excluded, pagination, and exchange filters
- Position- and regime-aware algorithmic strategy selection; users choose risk tolerance, while the trading engine chooses the playbook and records why
- Confidence ranking, stop, target, and risk-profile-aware paper position sizing
- Manual agent runs that can place one paper order per run
- Scheduled scans during NSE hours that refresh intelligence without placing orders
- Authenticated dashboard, risk settings, history, positions, reset, and agent APIs
- Read-only Zerodha Kite adapter for instruments, quotes, and historical candles; live order execution remains disabled
- Resumable SQLite history warehouse and bulk one-year daily sync for every NSE/BSE equity matched to Kite instruments
- Credential-free, resumable official NSE/BSE daily-bhavcopy importer with SHA-256 provenance records and optional raw-file retention

## API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/signup` | Create user and paper portfolio |
| POST | `/api/auth/login` | Return signed session token |
| GET | `/api/dashboard` | Portfolio, trades, holdings, indices, agent state |
| POST | `/api/agent/run` | Analyse the market and place at most one paper order |
| GET | `/api/analysis?symbol=RELIANCE` | Latest multi-timeframe market-structure analysis |
| POST | `/api/analysis/run` | Recalculate a symbol immediately |
| GET | `/api/glossary` | Definitions for all supported trading terms |
| GET | `/api/models` | Active model and configured-provider status |
| GET | `/api/sentiment?symbol=RELIANCE` | Symbol sentiment, evidence, market mood and leaders |
| GET | `/api/market/update` | Latest quote, index, breadth and sentiment snapshot |
| GET | `/api/ai/gateway/status` | Provider connections, controls, latency, cache and recent health |
| GET | `/api/chart?symbol=RELIANCE&timeframe=5m` | Candles and calculated technical indicators |
| GET | `/api/securities?query=RELIANCE&exchange=ALL` | Search the NSE/BSE active security master |
| GET | `/api/market/history/status` | Kite readiness, history coverage and live-stream capacity |
| POST | `/api/backtest` | Run a cost-aware historical strategy report |
| PUT | `/api/settings` | Change risk profile |
| POST | `/api/portfolio/reset` | Clear trades and restore ₹10,00,000 |
| GET | `/api/health` | Service and broker status |

## Important boundary

This is a paper-trading and research system. It does not guarantee profits and cannot place live orders. The Kite adapter is read-only and requires your own Kite Connect app, credentials, subscription, and appropriate regulatory/compliance review.

Intraday candles remain simulated until Kite is configured. To sync one year of available daily history for all matched stocks, set `KITE_API_KEY` and `KITE_ACCESS_TOKEN`, then run `python3 -m backend.history_sync --days 365`. Use `--since YYYY-MM-DD` for a deeper provider-available history. Coverage is recorded per symbol because delisted instruments, token changes, IPO dates, suspensions, and provider retention mean “since listing” cannot be guaranteed uniformly. The model is never allowed to place orders or alter computed risk levels.

For credential-free official end-of-day research data, run:

```bash
python3 -m backend.official_history_sync --exchange BOTH --days 365
python3 -m backend.ml_jobs all
```

The importer reads exchange-published bhavcopies, keeps source URL/checksum/import status in SQLite, safely resumes completed sessions, and can retain the raw files under `data/raw/exchange_bhavcopy`. Use `--start YYYY-MM-DD` for deeper history, `--no-raw` to save disk space, or `--symbols RELIANCE,TCS` for a small trial. Daily bhavcopies are not a substitute for licensed minute/tick data, point-in-time index membership, or a complete corporate-action feed.

## Model providers

`NIVESH_MODEL_PROVIDER=auto` uses a configured NVIDIA NIM endpoint first, then Gemini, an enabled local Ollama server, and finally the built-in local explanation. Automatic one-minute scans never call an external model; only a manual analysis may do so.

- **Gemini free tier:** create your own Google AI Studio key, set `GEMINI_API_KEY`, and keep `GEMINI_MODEL=gemini-2.5-flash` or select another model available to your project.
- **Ollama, fully local:** install/run Ollama separately, pull `gpt-oss:20b` (or another compatible model), set `OLLAMA_ENABLED=1`, and choose `NIVESH_MODEL_PROVIDER=ollama`.
- **Claude:** set `ANTHROPIC_API_KEY` and `NIVESH_MODEL_PROVIDER=anthropic`. API usage is metered; a claude.ai consumer subscription/key cannot be reused as an API key.
- **OpenAI:** set `OPENAI_API_KEY` and `NIVESH_MODEL_PROVIDER=openai`. API usage is metered separately from ChatGPT subscriptions.
- **NVIDIA NIM:** set `NVIDIA_API_KEY`, select a model ID currently available to your NVIDIA account, and use `NIVESH_MODEL_PROVIDER=nvidia`. The hosted endpoint is OpenAI-compatible; trial/catalog quotas are account-dependent and are not treated as unlimited free production capacity.

Never place API keys in source control. Copy `.env.example` to your own environment manager or export the variables before starting the server.

All external model calls pass through `backend/ai_gateway.py`. The gateway caches identical analyses, limits calls per user, opens a temporary circuit after repeated provider failures, records provider/latency/status without recording keys, and falls back to the local explanation. It has no order-execution capability.

Quotes and offline fallback headlines are simulated. Set `NIVESH_NEWS_API_KEY` for the configured licensed feed and optionally set `NIVESH_SENTIMENT_MODEL_URL` for a FinBERT-compatible endpoint. Articles are cached and deduplicated in SQLite; live, cached, and offline modes remain visibly labelled. Never label scraped or delayed content as real-time exchange data.

For deployment, change `NIVESH_SECRET`, use HTTPS, place the service behind a reverse proxy, add rate limiting, and migrate SQLite to PostgreSQL if multiple server instances are needed.
# AI trade bot and production guardrails

The workspace now includes a persistent AI trade-bot desk. Conversations, research notes, and generated paper derivatives plans are stored in SQLite. The paper execution planner emits explicit legs for common defined-risk option structures and futures tickets, while the live-order gate remains locked.

The Operations page reports scheduler heartbeat, historical warehouse coverage, AI-gateway requests/failures, Zerodha credential readiness, and the order-execution safety state. Adding Kite credentials enables the existing read-only data adapter; it does not silently enable orders.

## ML research pipeline

The ML Research workspace builds 21 corporate-action-adjusted technical/market-regime features, creates ATR-based ten-session triple-barrier labels, compares logistic regression with two histogram-gradient-boosting candidates, applies leakage-safe isotonic probability calibration, preserves an untouched final time block, performs purged expanding walk-forward validation with charges/slippage/liquidity impact, records shadow predictions, and monitors drift. Run the complete local research cycle with `python3 -m backend.ml_jobs all`.

The active research model is ML v2.5. Its corrected six-fold walk-forward result is +27.90% with a 58.00% win rate, 1.31 profit factor and -7.32% maximum drawdown. It remains `live_eligible=false`: untouched log loss is 0.6937, 90 forward shadow sessions are not complete, and short-signal instrument executability has not been verified.

Refresh official daily history and corporate actions with:

```bash
python3 -m backend.official_history_sync --exchange BOTH --start 2021-06-30
python3 -m backend.corporate_actions_sync --start 2021-06-30
```

See [ML_READINESS_REPORT.md](ML_READINESS_REPORT.md) for the verified model metrics, remaining external data requirements, import formats, and live-promotion gates.

## Execution safety and deployment

The Execution Control workspace adds persistent risk policies, an emergency kill switch, idempotent order intents, human approval, broker submission gates and order reconciliation. Zerodha REST/login and WebSocket transports are implemented but dormant without credentials. Credentials alone do not enable trading: `NIVESH_LIVE_TRADING_ENABLED=1` is also required, and the model never receives broker-write access.

Authentication now uses an HttpOnly SameSite cookie, logout revocation and request rate limits. Security headers, daily consistent database backups, Docker packaging and CI tests are included. See [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).
