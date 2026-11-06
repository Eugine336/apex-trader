# Apex — five full trading cycles (target state, after the implementation plan is complete)

**Read this first.** These are *target-behaviour walkthroughs*, not outputs of running code. All prices and numbers are
illustrative (the arithmetic is internally consistent). In each stage, **[today]** marks behaviour that exists in the repo
now and **[WP-x]** marks the work package from `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md` that must land for it to be true.
Nothing here is a profit claim; the market scores every call.

Common to every cycle (the identity): the Brain sees the whole market state for every enabled instrument, decides which
concepts the moment needs, keeps competing hypotheses alive, trades only when a defensible opportunity exists after costs,
manages from the concept's own levels, and learns from the outcome. The account owner sets the hard execution limits; Apex
may tighten them from its analysis, never loosen them.

---

## Cycle 1 — EURUSD, London open: bullish CRT with a sell-side sweep (winner, runs to objective)

**Observe** `[today]` Every enabled pair is read on bar close. EURUSD is in an H4 uptrend (external structure up, higher low
held). Concept readings arrive as evidence: H1 CRT, M15 structure, liquidity pools, imbalances, session context `[WP-A]`.

**Select the knowledge** The state is "trend context + London session + a completed H1 sweep", so the relevant concepts are
CRT, structure and liquidity; mean reversion and range rotation read as *not applicable* (their own applicability fields say so).
Competing hypotheses are kept: continuation long (CRT), pullback deepening, and a range rotation. `[today: readings, WP-B1: selection/composition]`

**Analyse** H1 range C1 = 1.0800–1.0860. C2 swept the low to 1.0791 and closed back inside at 1.0812. CRT reads: invalidation
1.0791 (completed close beneath it), milestone 1.0830 (50%), objective 1.0860 (opposite extreme). Sweep rejection ranks in
the top decile of this pair's own H1 candles; the sweep also took an untouched H4 low (confluence); the M5 structure has
already shifted back inside. Strength is *reasoning strength*, not a probability. `[today]`

**Decide** Net EV after verified round-trip cost is positive using the historical expectancy of this concept family in
this state `[today: gate; WP-B4: hindcast supplies the artifact]`. Geometry comes from the concept: entry 1.0806, stop 1.0791
(risk 15 pips), target 1.0860 (54 pips, 3.6R), milestone 1.0830 (1.6R). Size: the owner's risk-per-trade limit scaled by
verified calibration `[today: scaling; WP-B7: owner surface]`.

**Why not the others** The pullback-deepening hypothesis is rejected because no completed close has gone beyond 1.0791;
the range-rotation hypothesis is not applicable in an H4 trend. Compliance (market open, broker alive, spread, duplicate,
heat) passes; the order goes through the single execution plane `[today]`.

**Manage** `[today: milestone → protect_profit; WP-B3: the rest]` Price passes 1.0830 on a completed M15 close: take a partial,
move the stop to breakeven. It stalls at the midpoint for four bars without a structure break against the trade, so the
thesis holds. A new higher low forms: the trail moves beneath it (concept-supplied level, not a fixed percentage).

**Close** Objective 1.0860 reached; the remainder closes. With half taken at the milestone (1.6R) and half at the objective (3.6R) the result is about +2.6R.

**Learn** The outcome is attributed to concept = CRT, state = {H4 trend up, London, sweep with HTF confluence}. The concept ×
state track record grows by one sample; Apex consults it next time and it never gates a thesis `[WP-B4]`.

---

## Cycle 2 — XAUUSD, NFP day: stand down, then a failed breakout after the release (small loss)

**Observe / stand down** `[today: news windows; WP-E]` Before the release, spread widens and the event's own history shows
volatility jumps. Apex records "high event risk, no thesis with defined invalidation survives the window" and does nothing. No
rule forbids trading; the analysis simply finds no defensible plan.

**Select the knowledge** After the print the state is "post-event expansion". Relevant concepts: event reaction (surprise
size versus the event's own history) `[WP-E]`, liquidity sweeps `[today]`, failed breakout/acceptance `[today/WP-C]`.

**Analyse** The release surprise is large. Price spikes through the prior day high at 2,350 to 2,358 but the M15 bar closes back
at 2,344: a sweep of buy-side liquidity with rejection. The reading's objective is the nearest untouched pool below, the
previous day low at 2,321; invalidation is a completed close above the sweep extreme 2,358. `requires`: lower-timeframe
confirmation (M5 structure shift), and the *event reaction* reading flags that post-release moves often have two legs.

**Decide** Entry 2,344, stop 2,358 (14), target 2,321 (23): 1.6R. Costs are wider than normal after the release; EV is still
positive after verified cost, but marginally, so the size scale is small `[today: cost EV; WP-I]`.

**Manage** Two completed M15 closes stay below 2,350 (thesis intact), then the market whipsaws and a completed M15 closes at
2,360 above the sweep extreme: the concept's own invalidation fires. Exit immediately `[WP-B3]`.

**Close** About −1.1R including slippage.

**Learn** The loss is recorded against "failed breakout after high-impact event, first leg" with the observation that the
second-leg move occurred. Apex does not retune a threshold; it adds a sample to that concept × state record and, when the
record is large enough, the *event reaction* reading widens its `unknowns` for first-leg fades `[WP-B4]`.

---

## Cycle 3 — BTCUSD, weekend: liquidation cascade, long from exhaustion (target by volume node)

**Observe** `[today: structure, liquidity; WP-F: derivatives]` Weekend liquidity is thin. Funding was extremely positive, open
interest high, price then breaks the range low and prints a long-liquidation cascade.

**Select the knowledge** "Capitulation after crowded longs": crypto microstructure (funding/OI/liquidations) `[WP-F]`, effort versus
result (high volume, no further progress) `[WP-B]`, liquidity sweep of the range low `[today]`, volume profile (a naked POC above)
`[WP-A]`. Apex also holds the opposite hypothesis (trend-continuation short) alive.

**Analyse** After the last liquidation spike, effort/result shows absorption (volume surge, no new low), OI has flushed, the
sweep of the range low closed back inside, and a volume-profile naked POC sits 1.8× the risk distance above. The continuation
short is *not* discarded; its invalidation (a completed close above the sweep high) is monitored.

**Decide** Long with stop beneath the sweep extreme and target at the naked POC; size is small because weekend spread and
slippage estimates from Apex's own fills are larger `[WP-G market_impact]`.

**Manage** Partial at the value-area low, trail beneath the first higher low. Monday's session arrives with a calendar event
and rising volatility; the thesis *decays with time* (a thesis whose catalyst window has passed reduces the position instead of
holding it indefinitely) `[WP-B3]`.

**Close** Partial at the value-area low, remainder closed at the POC (1.8R) ahead of the Monday event window: about +1.5R overall.

**Learn** Track record: "capitulation with absorption + naked POC" gains a sample; the continuation-short hypothesis is logged as
the road not taken so its counterfactual outcome can also be scored.

---

## Cycle 4 — US30, New York open: range day, opening-range failure (short), with a correlated trade declined

**Observe / select** `[WP-A]` Session context says range day; the opening range is formed. The regime reading from realised
volatility and trend efficiency says "balance". Apex selects range rotation, opening-range behaviour and VWAP, not breakout
following. Trend following reads as not applicable *for this state*, not forbidden.

**Analyse** Price breaks the opening-range high, then a completed bar closes back inside: failed auction. VWAP is below and
the value-area high is the nearest draw; invalidation is a completed close back above the failed-breakout high.

**Decide** Short US30: entry at the OR high retest, stop above the failed-breakout high, target at VWAP/POC, with a milestone at
the range midpoint. **US100 shows the same setup**, but the portfolio reading shows the two are highly correlated and one
more trade would exceed the owner's total-risk limit once combined. Apex takes the better-ranked setup only and records the
second as declined for exposure reasons, not for lack of a thesis `[today: portfolio sizing; WP-B1 ranking]`.

**Manage** At the midpoint: partial, stop to breakeven. A news headline arrives; the event reading raises uncertainty and Apex
tightens to the concept's last lower high rather than holding through the window `[WP-B3]`.

**Close** Stopped at the tightened stop for about +0.6R.

**Learn** Range-rotation × balance-day gains a sample; the declined US100 trade is logged so Apex can later test whether
declining correlated duplicates was right.

---

## Cycle 5 — Synthetic Volatility index: no news, own-history regime, thesis invalidated (loss) and a "no trade" lesson

**Observe** `[WP-F synthetic mechanics]` There is no calendar. The concept set is limited to what applies: structure, liquidity,
realised-volatility regime and time-series dependence (is this instrument trending or reverting on this timeframe, measured
from its own history) `[WP-H]`. Macro and session concepts say "not applicable".

**Analyse** The series has shown negative autocorrelation at this horizon for the last month, so reversion readings carry
more weight than breakout readings *because the measurement says so*. A sweep of an equal-highs pool closes back inside.

**Decide** Short with the stop at the sweep extreme. Apex also records an unresolved unknown: "provider spike cadence is
uncertain", so the size scale is reduced `[today: scaling]`.

**Manage** The first milestone is never reached. A spike prints and a completed bar closes beyond the sweep extreme: the
thesis is invalidated by the concept's own rule; exit at once `[WP-B3]`.

**Close** About −1.0R.

**Learn** The outcome joins "sweep reversion on synthetic index" with a regime tag. Later the *anomaly/novelty* reading
`[WP-H]` flags that the moment before the spike was unlike the preceding sessions, so similar states now widen `unknowns`.
The cycle ends with a non-trade: a later setup has no available objective (no untouched pool beyond price) and Apex stands down
because the geometry cannot be built honestly, not because a threshold said so.

---

### What these five cycles require, in one list
Selection and composition of readings `[WP-B1]`; concept-supplied entries/stops/targets `[WP-B2]`; milestone partials, trailing,
time decay and invalidation exits `[WP-B3]`; hindcast calibration and per-concept track records `[WP-B4]`; the calendar, derivatives,
microstructure, multi-symbol and session channels `[§4 of the plan]`; owner-limits surface `[WP-B7]`; and the remaining concept
modules `[WP-A … WP-J]`.
