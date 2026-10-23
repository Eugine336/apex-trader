# Apex Gold Desk — single-Brain integration

## Architecture rule

Apex has one market decision authority: the existing `CognitiveBrain`, driven
through `CognitionLoop`. The Gold desk is not a second brain and does not
arbitrate, rank, or issue trading decisions. It is a market-observation layer.

The broker feed is the source of market facts: bid/ask, spread, confirmed OHLC
and timestamps. Pattern detectors are fallible measurements of that data. CRT,
liquidity sweeps, trend continuation, breakouts, mean reversion, exhaustion and
event conditions can overlap. Their outputs are therefore consolidated into
**one neutral `gold_market_desk` Evidence record**; evidence polarity is zero,
scores are not added, and no detector can choose a trade direction.

The path is:

```text
Selected XAUUSD broker feed
   -> confirmed multi-timeframe OHLC + live bid/ask
   -> deterministic Gold pattern detectors (facts only)
   -> one neutral Gold evidence record
   -> existing CognitionLoop / MarketState
   -> existing CognitiveBrain (sole decision authority)
   -> existing campaign, portfolio risk, execution gates and ActionExecutor
```

The observer has no broker order API. It emits the monitoring event
`gold_market_evidence`, not `gold_desk_opportunity`. The actual latest payload
is read directly by the existing CognitionLoop. Detector results' internal
direction, entry, stop, target, rationale and ranking score are stripped before
the payload is stored or supplied to the Brain.

## Detector families

The deterministic pattern-observation library includes:

1. Trend continuation — EMA structure, pullback and close conditions.
2. Liquidity sweep — prior-range sweep and reclaim.
3. Volatility expansion — range break with true-range expansion.
4. Failed breakout — wick beyond the recent range and close back inside.
5. Statistical mean reversion — rolling z-score and trend-strength context.
6. Trend exhaustion — price/RSI divergence and local structure shift.
7. Event-related range breakout — requires an adapter-verified event window and pre-event range.
8. CRT — completed H1/H4/D1 anchor, sweep/reclaim and M5 structure shift.

These are detector families, not eight trading authorities. A detected pattern is
a measurement for the Brain to consider alongside the actual chart, structural
interactions, portfolio state, execution cost, memory and optional advisory
context. The Brain may reject the setup or keep observing. AI-provider availability
does not determine whether the deterministic market measurements are calculated.

## Runtime details

- `APEX_GOLD_DESK_SHADOW_ENABLED` defaults to `1` and disables this observer when set to `0`, `false`, `no` or `off`.
- `APEX_GOLD_DESK_VENUE` defaults to `MT5`. Only `MT5` or `DERIV` is accepted.
- The chosen venue must match the connector selected by `PlatformManager`; mixed MT5/Deriv ticks are discarded.
- The observer uses broker-fetch history with `include_forming=False`, filters forming/future bars, checks tick freshness, and updates a cached evidence payload.
- Evidence is freshness-limited at the CognitiveLoop adapter; an expired or unavailable Gold feed is reported as degraded evidence, not as a no-opportunity market conclusion.
- Pattern data is provided as one neutral Evidence object. Overlapping modules are not votes, and no additive score or coordinator chooses a winner.
- The preflight gate remains shadow-only by default and is not an order route. Risk/execution gates remain separate governance safeguards; they do not act as another market brain.

## Validation

The focused tests cover detector registration, CRT pattern conditions, strict
Gold symbol mapping, shadow-only risk authorization, drawdown blocking, candle
time integrity, one-record neutral evidence conversion, stale evidence rejection,
venue-routing mismatch and stripping of trade instructions before evidence
reaches the central Brain.

Run:

```bash
python -m pytest tests/test_gold_desk_core.py -q
ruff check gold_desk tests/test_gold_desk_core.py --select E,F,W --ignore E501,E402,F401,E701,E731,E741
```

The repository-wide CI has baseline problems outside this change, including
repository-wide lint errors and test-collection failures involving `tick`
imports. These are distinct from the focused Gold desk checks and still require
resolution.

## Production gates still open

This branch is not evidence of a profitable strategy and is not ready to send
live orders. Before enabling live trading:

1. Run a complete regression suite after resolving the repository-wide CI failures.
2. Verify all real market/pending order paths, re-entry, news OCO, restart recovery
   and AI-originated requests are guarded against non-Gold exposure at the
   ActionExecutor/order boundary.
3. Add Gold-specific tick-quality backtests with real spread, commission, swap,
   slippage, partial/rejected fills, gaps and broker stop/volume constraints.
4. Use rolling walk-forward tests plus a final untouched holdout; report net
   expectancy in R, profit factor, drawdown, sample size, session/regime splits,
   and uncertainty. Do not optimize on the holdout.
5. Run shadow mode and broker-demo validation using the same position sizing,
   portfolio risk, execution RiskGate, ActionExecutor and recovery path as live.
6. Define operational risk limits, an independent kill switch, alerts and rollback.
   A broken/stale/uncertain market feed must stop new entries. AI outage must not
   stop deterministic observation.

## Environment file

The `.env` file has been restored on this feature branch from the exact blob
present on `main`. Do not delete it as part of the Gold desk work. This update
did not print or rewrite its contents. If that file contains live credentials,
rotate them separately if they have been exposed in a shared repository; restoring
the tracked file does not remove prior Git history.
