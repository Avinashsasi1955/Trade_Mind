# Nivesh.ai — full-stack paper-trading agent

A self-contained Indian-market paper-trading application with a premium dashboard, SQLite persistence, signed authentication, scheduled market scans, explainable strategy scoring, risk sizing, and simulated execution.

## Run

Requires Python 3.9+ and no third-party packages.

```bash
python3 -m backend.server
```

Open `http://127.0.0.1:4173` and sign in with:

- Email: `arjun@example.com`
- Password: `nivesh123`

Run tests:

```bash
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
- Auditable sentiment analysis from headline tone, price action, relative volume, and market breadth
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
- `ZerodhaAdapter` boundary with live access intentionally disabled

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
| POST | `/api/backtest` | Run a cost-aware historical strategy report |
| PUT | `/api/settings` | Change risk profile |
| POST | `/api/portfolio/reset` | Clear trades and restore ₹10,00,000 |
| GET | `/api/health` | Service and broker status |

## Important boundary

This is a paper-trading and research system. It does not guarantee profits and cannot place live orders. `backend/zerodha_adapter.py` is the only unfinished integration boundary; it requires your own Kite Connect app, credentials, live-data subscription, and appropriate regulatory/compliance review before implementation.

Intraday candles remain simulated until a licensed feed is connected. The Backtest Lab includes a downloaded one-year RELIANCE.NS daily sample; its source and data mode are shown on every report. Other symbols fall back to clearly labeled demo data until their history is imported. The model is never allowed to place orders or alter computed risk levels.

## Model providers

`NIVESH_MODEL_PROVIDER=auto` uses Gemini when `GEMINI_API_KEY` is available, then an enabled local Ollama server, then the built-in local explanation. Automatic one-minute scans never call an external model; only a manual analysis may do so.

- **Gemini free tier:** create your own Google AI Studio key, set `GEMINI_API_KEY`, and keep `GEMINI_MODEL=gemini-2.5-flash` or select another model available to your project.
- **Ollama, fully local:** install/run Ollama separately, pull `gpt-oss:20b` (or another compatible model), set `OLLAMA_ENABLED=1`, and choose `NIVESH_MODEL_PROVIDER=ollama`.
- **Claude:** set `ANTHROPIC_API_KEY` and `NIVESH_MODEL_PROVIDER=anthropic`. API usage is metered; a claude.ai consumer subscription/key cannot be reused as an API key.
- **OpenAI:** set `OPENAI_API_KEY` and `NIVESH_MODEL_PROVIDER=openai`. API usage is metered separately from ChatGPT subscriptions.

Never place API keys in source control. Copy `.env.example` to your own environment manager or export the variables before starting the server.

All external model calls pass through `backend/ai_gateway.py`. The gateway caches identical analyses, limits calls per user, opens a temporary circuit after repeated provider failures, records provider/latency/status without recording keys, and falls back to the local explanation. It has no order-execution capability.

The bundled headlines and quotes are simulated. For production, connect licensed market-data and news-provider adapters; never label scraped or delayed content as real-time exchange data.

For deployment, change `NIVESH_SECRET`, use HTTPS, place the service behind a reverse proxy, add rate limiting, and migrate SQLite to PostgreSQL if multiple server instances are needed.
