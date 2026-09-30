# Nivesh AI — ML Research Readiness Report

Generated: 30 June 2026 (IST)

## Outcome

The project now contains a functional, auditable ML research pipeline and five years of official NSE/BSE end-of-day history. NSE corporate actions are synchronized from the official API and mirrored to BSE listings when ISINs match. It is not ready for live autonomous trading because point-in-time/delisted membership, licensed intraday coverage, complete BSE-only action coverage, and forward shadow observations are not yet available.

The current model must not be promoted. Its deduplicated five-year walk-forward return is positive, but profit factor 1.08 remains below the 1.20 promotion gate and drawdown is still material.

## Implemented

- Resumable Zerodha daily and intraday ingestion with interval-aware date chunking.
- Credential-free official NSE/BSE daily-bhavcopy ingestion with raw-file retention, SHA-256 provenance, safe resume, current/legacy formats, and equity-master filtering.
- A separate SQLite research warehouse.
- Split, bonus, and dividend back-adjustment logic.
- Official NSE corporate-action synchronization with raw JSON, checksums, dividend/bonus/split parsing, and BSE ISIN mirroring.
- Point-in-time universe membership schema and importer for delisted/suspended securities.
- Twenty-one deterministic technical, relative-strength, breadth, volatility, volume, gap and regime features.
- ATR-based ten-session triple-barrier labels; ambiguous same-day barrier hits and neutral timeouts are excluded.
- Regularised logistic regression plus conservative/flexible histogram-gradient-boosting candidates.
- Serialized, versioned model registry with active/archive states and train/holdout metrics.
- Development/selection/final chronological split with a ten-session purge and untouched final block.
- Ten-session-purged expanding walk-forward validation with cross-listed symbol deduplication.
- Cost simulation covering configurable charges, slippage, and participation impact.
- Liquidity-ranked training capped at the top 500 candidates per session and a ₹1 crore minimum 20-session average daily traded value.
- Non-overlapping ten-session portfolio evaluation using the top 10 signals, 5% allocation per position, and a 50% gross-exposure cap.
- Pending/resolved shadow-prediction storage with live orders hard-disabled.
- Feature-distribution drift checks with stable/watch/alert states.
- Configurable seven-day scheduled rebuilding, retraining, validation, shadow generation, and drift reporting.
- Authenticated ML status, train, validate, shadow, and drift API endpoints.
- ML Research dashboard in the web application.
- 27 automated tests passing.

## Current real-data result

- Source period: 30 June 2021 through 29 June 2026.
- Equity daily bars: 6,435,334 across 6,940 covered exchange symbols.
- Corporate-action records: 12,588, including 4,970 BSE ISIN mirrors; 21 source chunks are checksummed.
- V2 derived feature rows: 2,347,397 across 2,485 symbols passing the current liquidity floor.
- Training universe: top 500 liquid candidates per session, subject to ₹1 crore minimum average daily traded value.
- Training/refit samples: 468,393.
- Untouched final samples: 116,358 from 14 July 2025 through 25 June 2026.
- Untouched-final accuracy: 50.55%; log loss: 0.6941; Brier score: 0.2505.
- Gradient-boosting selection accuracy: 54.76%, but calibration/log loss was worse than logistic regression.
- Purged expanding walk-forward folds: 6.
- Non-overlapping cost-adjusted trades: 554.
- Cost-adjusted walk-forward return: +10.65%.
- Win rate: 52.71%.
- Profit factor: 1.08.
- Maximum drawdown: -12.84%.
- Current drift state: stable.
- Latest shadow predictions recorded: 2,485; order access remains disabled.

The result is an improvement, not proof of profitability. Performance is uneven by fold, the final classifier is only slightly better than chance, and estimated rather than broker-reconciled costs are still used.

## External work still required

### Market history

The free official daily archive is now imported and can be resumed with:

```bash
python3 -m backend.official_history_sync --exchange BOTH --start 2021-06-30
```

Longer daily history can use `--start YYYY-MM-DD`. Licensed intraday history still requires valid Zerodha/provider credentials and sufficient retention:

```bash
KITE_API_KEY=... KITE_ACCESS_TOKEN=... python3 -m backend.history_sync --since 2018-01-01 --interval day
KITE_API_KEY=... KITE_ACCESS_TOKEN=... python3 -m backend.history_sync --days 60 --interval minute
```

Start with `--limit 10`, verify quality, then expand in controlled batches. Full-universe minute history is large and must respect provider limits and storage capacity.

### Corporate actions

Refresh official NSE actions with:

```bash
python3 -m backend.corporate_actions_sync --start 2021-06-30
```

An additional authoritative BSE/vendor CSV can still be imported for BSE-only securities with:

`exchange,symbol,effective_date,action_type,ratio_from,ratio_to,cash_amount,source`

Then run:

```bash
python3 -m backend.ml_jobs import-actions --file corporate_actions.csv
```

### Survivorship corrections

Obtain point-in-time listings including delisted and suspended securities with:

`exchange,symbol,valid_from,valid_to,status,source`

Then run:

```bash
python3 -m backend.ml_jobs import-universe --file universe_membership.csv
```

### Research cycle

```bash
python3 -m backend.ml_jobs build
python3 -m backend.ml_jobs train
python3 -m backend.ml_jobs validate
python3 -m backend.ml_jobs shadow
python3 -m backend.ml_jobs drift
python3 -m backend.ml_jobs status
```

## Promotion requirements

Before a model can influence real orders, require at minimum:

- Broad NSE/BSE point-in-time coverage across bull, bear, sideways, high-volatility, and low-liquidity regimes.
- No survivorship leakage and no unresolved corporate-action anomalies.
- Positive results after all charges and conservative slippage in multiple untouched test periods.
- Stable performance across sectors rather than dependence on a few securities.
- Several months of resolved forward shadow predictions.
- Several additional weeks of broker-connected paper trading.
- Model-drift, stale-data, loss-limit, exposure, reconciliation, and kill-switch tests.
- Human approval during the initial live-capital phase.

## What Codex can and cannot complete

Codex can implement, test, operate, and improve the ingestion, adjustment, feature, training, evaluation, registry, monitoring, and shadow-validation software.

Codex cannot manufacture licensed historical data, broker credentials, authoritative corporate-action/delisting records, future shadow outcomes, or proof of profitability. Those require your provider access and elapsed live-market observation.
