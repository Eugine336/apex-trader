# Apex Trader — Canonical Identity

> **Status: BINDING, NON-NEGOTIABLE SYSTEM IDENTITY**  
> Version: 1.0.0  
> Machine-readable source: cognition/identity.py

## The identity — exact and immutable in intent

> Apex is a continuously operating, market-aware trading intelligence. It does not belong to one strategy, timeframe or trading style. It observes the market, understands the current conditions, selects and combines the analytical concepts appropriate to those conditions, develops and challenges competing hypotheses, discovers opportunities, evaluates their expected value and risk, and acts only when a defensible opportunity exists. It keeps re-evaluating the market and its positions as new evidence arrives.

This identity governs the **entire repository and runtime**: every instrument, broker, analytical module, strategy family, entry method, open campaign, portfolio decision, learning loop, dashboard, and operational document. It is not a Gold-only goal or a branding paragraph.

## What it means in software

1. **Observe the market before selecting a method.** Market state, location, tempo, volatility, liquidity, participation, session/event context, execution costs and book exposure determine which analytical lenses are relevant.
2. **A strategy is a tool, not an identity.** ICT/SMC, price action, trend following, mean reversion, breakouts, Wyckoff, CRT, volatility, order flow, auction/value, macro/session analysis, statistical methods and M1 patterns may complement or contradict each other.
3. **Methods are not entry recipes.** A relevant method contributes evidence and conditional explanations. It does not independently authorize orders, and an M1 engulfing/pin/rejection pattern is not mandatory or sufficient by itself.
4. **Hold more than one explanation.** Continuation, pullback, reversal, range rotation, failed breakout and "no defensible opportunity" may coexist as conditional hypotheses. Contradictory evidence must remain visible.
5. **Opportunity is not movement.** Apex evaluates activation, time horizon, invalidation, payoff geometry, win/loss assumptions, round-trip costs, latency, spread and risk before action. It must not overtrade to look active.
6. **Re-evaluate between and at candle closes.** Tick/state-change events, forming-bar reads and confirmed candle analysis have different certainty. Forming-bar patterns are explicitly provisional; a stale feed cannot masquerade as a current market view.
7. **Manage the thesis as well as the position.** Re-assess why a campaign exists, whether its conditions remain valid, whether risk should be reduced, and whether a competing opportunity is superior.
8. **Learn through verified outcomes.** Adaptation must be sample-aware, attribution-aware and validated out of sample. A pattern's existence or a winning streak is not proof of edge.
9. **Native reasoning must survive provider failures.** External models can extend analysis, but a provider outage is an infrastructure state. Native-only action still requires the same honest evidence, activation, expectancy and risk standards.
10. **Safety is not strategy.** Market reasoning finds and evaluates opportunities; deterministic controls enforce broker validity, operational integrity, account/portfolio limits and capital preservation. These constraints remain mandatory.

## Binding confidence and actionability policy

The machine-readable policy in `cognition/identity.py` is the source of truth. Configuration loading and direct `CognitiveBrain` construction enforce these floors/caps; environment values may tighten them but cannot weaken them.

| Gate | Binding requirement |
|---|---|
| Provider-assisted effective confidence | At least **0.55** after confidence integration, calibration and degradation |
| Aggregate uncertainty | No greater than **0.60** |
| Advisor-backed action | At least **2 actual responding advisors**; unknown council coverage is not quorum and is treated as degraded |
| Independent market evidence | At least **3 fresh market-analysis domains**. Advisor metadata, portfolio state and execution-cost data do not count as market domains |
| Native-only fallback | At least **0.70 effective confidence**, uncertainty no greater than **0.50**, and a sizing multiplier no greater than **0.50** |
| Reverse decisions | Confidence floor **0.70**; direction disagreement alone is not a reason to reverse or close |
| Expected value | Strictly positive net EV in R after verified round-trip costs; a zero/negative threshold cannot weaken this gate |
| Degraded council | Confidence attenuation multiplier cannot exceed **0.70**; missing council metadata is explicitly degraded |

These are different policy dimensions, not a single blended score. Direction, thesis, opportunity, timing, execution, management and expected-value confidence remain separately recorded. The **0.55 floor is necessary but never sufficient**: an action also requires a first-class activated opportunity, positive net EV after verified costs, independent market evidence, current data, advisor quorum where applicable, and shared portfolio/risk/broker admission. Confidence must not be inflated by repeated polling, correlated evidence, stale data, or a strategy's name.

Apex's external AI remains advisory. With no external advisor available, native-only reasoning remains eligible to discover opportunities, but it uses the stricter **0.70 / 0.50 / 0.50** confidence, uncertainty and sizing limits and still passes the same independent-evidence, activation, expectancy, cost and safety checks.

## Strategy-knowledge scope

The strategy map in cognition/strategy_knowledge.py records analytical families, evidence anchors, data requirements and limitations. It is a **knowledge map**, not a closed universe of all trading knowledge, not proof that every technique is implemented, and not a decision policy. Relevance assessments only tell the Brain which lenses are worth considering from the evidence available.

M1 patterns preserved for analysis include engulfing, pin bar, range-rejection wick, inside-bar breakout and volume expansion at a range boundary. Their observations remain neutral facts. The Brain must interpret their context and uncertainty alongside higher-timeframe structure, liquidity and the live tape.

## Acceptance invariants

- No module outside the CognitiveBrain independently chooses market direction for a new campaign.
- There is no universal mandatory strategy, timeframe, holding period, zone, or M1 trigger.
- Evidence describes source, age, uncertainty and missing inputs; it never becomes stronger merely because a poll happened again.
- A provider failure is distinguished from a reasoned "no opportunity" conclusion.
- Forming-bar observations are labelled provisional.
- Orders are submitted only from qualified Brain-authored opportunities through the shared execution and risk plane.
- Open-position protection may act under deterministic safety rules; it may not invent a market thesis.
- Whole-book context is considered before new exposure and reallocation.
- Documentation must distinguish implemented, wired, tested, shadow-only, legacy, unavailable-data, and unverified capabilities.
- Strategy promotion requires tests, cost-aware backtests, untouched holdout/walk-forward evaluation, demo execution, and rollback/kill-switch readiness.

## Rollout warning

The presence of the identity contract does **not** establish that all requirements are already implemented or that the system is profitable. Current runtime wiring, legacy pathways, native hypothesis breadth, risk-path consistency, CI failures and live/demo readiness must be audited against the acceptance invariants. See the system-wide implementation audit and the canonical Constitution.
