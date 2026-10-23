# Apex Gold Trading Desk — implementation branch

## Intent

Apex's Gold desk is designed as a continuously operating, market-data-driven
trading system: market data is authoritative; indicators, CRT/ICT structures,
statistical models, news context and optional AI are interpretations of that
data. AI is not required to calculate signals or to keep evaluating XAUUSD.
No component may claim an edge without out-of-sample evidence.

This change adds an isolated deterministic opportunity-research kernel. It is
not yet a claim that the full Apex production stack has been integrated or that
the strategies are profitable.

## Implemented in this branch

Eight named strategy families are available under `gold_desk.strategies`:

1. Trend continuation — EMA structure, pullback and close confirmation.
2. Liquidity-sweep reversal — prior-range sweep and reclaim.
3. Volatility-expansion breakout — range break plus true-range expansion.
4. Failed-breakout trap — wick beyond a recent range and close back inside.
5. Statistical mean reversion — rolling z-score gated by a trend-strength check.
6. Trend-exhaustion reversal — price/RSI divergence plus local structure break.
7. Event-driven Gold breakout — requires a verified event window and pre-event range supplied by an adapter.
8. Candle Range Theory (CRT) — completed H1/H4/D1 anchor, liquidity sweep, close back inside, and an M5 structure-shift confirmation. The anchor midpoint is the first objective; a weak reward/risk is rejected.

A strategy returns a `TradeIdea` with its own evidence, entry, invalidation stop,
target, source bar, expiry, ATR and quality ranking. The coordinator runs all
families, applies a strict Gold symbol allowlist, filters poor spread/reward-risk
setups, coalesces overlapping entries without adding scores, and abstains when
opposite-direction candidates are too close in ranking. Scores are not win
probabilities.

The async watcher keeps polling the latest quote/candle snapshot, rejects stale
data, maintains a heartbeat, and deduplicates publication by source bar. The
preflight gate is intentionally shadow-only unless a caller explicitly arms it.
It checks symbol, venue, quote freshness, entry drift, spread, reward/risk,
per-trade and aggregate risk, drawdown, position count and the manual kill switch.

## What is deliberately not enabled by this PR

The existing production engine already owns candle-close processing, WorldModel
facts, cognition, campaign/entry logic, position sizing, portfolio risk,
execution intents and broker routing. This PR does **not** bypass those controls
or create a parallel order path. The new watcher has to be wired into the active
event-driven bootstrap through a reviewed adapter that:
- obtains synchronized completed M5/M15/H1/H4/D1 bars and a fresh bid/ask;
- routes only XAUUSD to one explicitly configured execution venue;
- submits candidates into the existing opportunity/campaign pipeline;
- uses the existing position sizer, portfolio exposure model, execution RiskGate
  and ActionExecutor for every new order and pending order;
- reconciles orders and positions after disconnect/restart; and
- attributes fills, slippage, spread, rejected entries, strategy family and
  realized R to a durable outcome ledger.

Do not activate live trading by calling the new preflight gate alone. It is not
a replacement for the existing order-boundary guard, risk engine, portfolio
netting, broker volume/tick metadata or execution lifecycle.

## Required validation before live activation

1. Run the new unit tests and the existing regression suite.
2. Add source-level tests at the real ActionExecutor/order boundary proving
   every market/pending order, re-entry, news OCO leg, restart-recovered request
   and AI-originated request rejects all non-Gold symbols.
3. Verify no forming candle enters confirmed strategy facts; all timestamps and
   all timeframe bars must be synchronized and broker-specific symbol aliases
   must be allowlisted explicitly.
4. Backtest on tick-quality XAUUSD data with variable spread, commission,
   swap, slippage, rejected/partial fills, market gaps and stop-level constraints.
   Use rolling walk-forward and a final untouched holdout. Compare each family
   independently and combined; report net expectancy in R, profit factor,
   drawdown, trade count, exposure, session/regime breakdown and confidence
   intervals. Do not optimize on the final holdout.
5. Run shadow mode, then broker demo with the same execution path and sizing.
   Compare intended versus actual fills, stale quotes, missed signals and
   post-restart reconciliation before considering live capital.
6. Define an operator kill switch, risk caps and rollback procedure. AI provider
   outage must not stop rule-based market analysis; invalid/stale market data or
   execution-state uncertainty must stop new entries.

## Live-safety/security note

The repository had a tracked `.env` file on the base branch. This feature branch
removes that file from its current tree, but deletion does not remove secrets
from earlier commits. Rotate any credentials ever committed there and remove
them from history using a carefully coordinated history rewrite. Do not paste
credentials into issue or pull-request discussion.
