# Apex Gold Desk — single-Brain integration


> **Identity migration status (2026-10-23):** System-wide identity applies here too: the Gold Desk is an observation layer for the shared CognitiveBrain, not the scope of Apex's identity. See docs/APEX_IDENTITY.md. Gold-specific wiring/guardrails do not prove that the same full intelligence is implemented across all instruments; remaining system-wide gaps are in docs/APEX_IDENTITY_IMPLEMENTATION_AUDIT.md.

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
is read directly by the existing CognitionLoop. Detector interfaces now return
`PatternObservation` records containing facts and raw measurements only. They
have no direction, entry, stop, target, rationale, rank or trade-candidate object
to strip or route into execution.

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
context. The Brain may reject the setup or keep observing. The existing CognitiveBrain
uses its native deterministic reasoning path to form and criticize hypotheses
even when no external reasoner is configured. It only originates without an
advisor when its structured-pattern, confidence, uncertainty and expected-value
thresholds pass; otherwise it observes rather than fabricating a trade.

## Runtime details

- `APEX_GOLD_DESK_SHADOW_ENABLED` defaults to `1` and disables this observer when set to `0`, `false`, `no` or `off`.
- `APEX_GOLD_DESK_VENUE` defaults to `MT5`. Only `MT5` or `DERIV` is accepted.
- The chosen venue must match the connector selected by `PlatformManager`; mixed MT5/Deriv ticks are discarded.
- The observer uses broker-fetch history with `include_forming=False`, filters forming/future bars, checks tick freshness, and updates a cached evidence payload.
- Evidence is freshness-limited at the CognitiveLoop adapter; an expired or unavailable Gold feed is reported as degraded evidence, not as a no-opportunity market conclusion.
- The observer queues a single-Brain re-read when completed-bar/pattern evidence changes or price/spread moves materially. Per-symbol throttling prevents a reasoner call on every tick; the request executes on the CognitionLoop thread, not the market-data thread.
- Pattern data is provided as one neutral Evidence object. Overlapping modules are not votes, and no additive score or coordinator chooses a winner.
- A final Gold-only check in the shared ActionExecutor rejects any non-allowlisted OPEN intent before the broker call; the event-driven runtime enables it explicitly. It also fails closed unless the observer has a fresh, open-market quote and completed M5 data whose close time is not more than two M5 bars old. These new-exposure checks do not block risk-reducing closes or management of any legacy open position.
- The direct pending-order helper has its own exact Gold allowlist because pending orders do not pass through ActionExecutor. The legacy NewsPlanner OCO staging update is suppressed while `CognitionConfig.single_path` is enabled, so a second independent strategy cannot stage entries beside the Brain.
- No parallel strategy selector or duplicate risk engine was introduced. Position sizing, portfolio exposure, the existing execution RiskGate, ActionExecutor and broker routing remain the authoritative execution controls.

## Validation

The focused tests cover evidence-only detector registration, CRT range/sweep/
reclaim observations, strict Gold symbol mapping, candle time integrity,
neutral evidence conversion with factual measurements, event-context verification,
stale evidence rejection, venue-routing mismatch, material market updates waking
the existing Brain with a debounce, and proof that no pattern detector output
contains trade direction or execution geometry. ActionExecutor regression tests
cover the Gold-only OPEN invariant, fail-closed feed-health validation, callback
failure, and preservation of risk-reducing exits.

Run:

```bash
python -m pytest tests/test_gold_desk_core.py -q
ruff check gold_desk tests/test_gold_desk_core.py --select E,F,W --ignore E501,E402,F401,E701,E731,E741
```

The repository-wide CI has baseline problems outside this change, including
repository-wide lint errors and test-collection failures involving `tick`
imports. These are distinct from the focused Gold desk checks and still require
resolution.

## External-advisor outages and execution authority

The native `CognitiveBrain` analyzes consolidated evidence before consulting
an external adviser. If the adviser fails, it attempts the same conservative
native-only origination gates. When those gates do not pass, the system must
stand down rather than invent an entry. The unavailable-provider decision now
also records the native analysis summary, hypotheses, detected patterns,
uncertainty, and the native-only qualification blockers. The cognition log
labels this as `reasoning_mode=native_fallback` with
`external_advisor=unavailable`, rather than implying that the market observer
has stopped.

`COGNITION_ORIGINATION_MODE=live` controls whether a qualified Brain
`OPEN_CAMPAIGN` is sent to the existing origination sink and shared execution
plane. It does not mean that every observation becomes an order. The Gold
observer itself remains orderless (`live_orders=disabled`) by design: it
publishes facts to the Brain and must not become a second trade authority.

## Production gates still open

This branch is not evidence of a profitable strategy and is not ready to send
live orders. Before enabling live trading:

1. Run a complete regression suite after resolving the repository-wide CI failures.
2. Complete an end-to-end audit proving no direct/legacy order path bypasses
   the Gold-only ActionExecutor guard or pending-order allowlist, including
   re-entry, restart recovery, news OCO cleanup and AI-originated requests.
   Cancel any pending orders created by a previous deployment before rollout:
   the legacy planner's in-memory OCO registry is not a broker-side order
   reconciliation service after a process restart.
3. Add Gold-specific tick-quality backtests with real spread, commission, swap,
   slippage, partial/rejected fills, gaps and broker stop/volume constraints.
4. Use rolling walk-forward tests plus a final untouched holdout; report net
   expectancy in R, profit factor, drawdown, sample size, session/regime splits,
   and uncertainty. Do not optimize on the holdout.
5. Run shadow mode and broker-demo validation using the same position sizing,
   portfolio risk, execution RiskGate, ActionExecutor and recovery path as live.
6. Define operational risk limits, an independent kill switch, alerts and rollback.
   A broken/stale/uncertain market feed must stop new entries. The native Brain
   can reason without AI, but provider-outage and no-provider behavior must be
   validated in an end-to-end runtime test, not inferred from unit tests alone.

## Environment file

The `.env` file has been restored on this feature branch from the exact blob
present on `main`. Do not delete it as part of the Gold desk work. This update
did not print or rewrite its contents. If that file contains live credentials,
rotate them separately if they have been exposed in a shared repository; restoring
the tracked file does not remove prior Git history.
