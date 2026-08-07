# APEX PHILOSOPHY AUDIT — What Does the Live Execution Path Actually Implement?

**Method:** Static reconstruction of the live signal→entry path, reading raw code
only. Every claim cites `file:line`. Documentation, comments, and naming were
explicitly distrusted and cross-checked against the executed logic and the *live
config defaults* (not the in-code `.get(..., default)` fallbacks, which are dead
when the config supplies a value).

---

## 1. VERDICT

**APEX is fundamentally an opportunity-selection engine whose evidence layer is
genuinely direction-agnostic, but whose live execution path collapses that
evidence into exactly one net direction per instrument per scan and then kills
the setup whenever the timeframes disagree.** The constraint that made it blind
to the EURCAD counter-trend short is **not** an HTF directional bias and **not**
the OQ/EQ quality gates — those are direction-free. The constraint is the
**directional-consensus collapse + agreement gate** in
[`brain/directional_consensus.py`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/directional_consensus.py#L55-L164):
nine equally-weighted, direction-independent module votes are summed to a single
signed net, and any panel that does not reach **55% agreement on one side**
(`min_agreement=0.55`) returns `NEUTRAL` = no trade.

A counter-trend M5 scalp against an aligned HTF is precisely the "mixed panel"
case: the HTF-driven modules (structure, currency strength) vote one way, the
fast modules (momentum, OB, FVG, VWAP, liquidity) vote the other, agreement falls
below 0.55, and the consensus emits `NEUTRAL`. The opportunity is not *rejected
for being a short* — it is *dissolved for lacking a single-direction majority*.

Crucially, the rest of the path is already opportunity-/reversal-oriented:
- HTF is demoted to **1 of 9 equal votes** (config weight `1.0`, not the `3.0`
  the scanner's dead `.get` default implies) — [`config.py:258-268`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/config.py#L258-L268).
- The DecisionEngine entry path is **M5/M1-primary**, HTF at coefficient `0.20`,
  and it *explicitly supports counter-HTF reversals sized down* —
  [`decision/engine.py:636-778`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/engine.py#L636-L778).
- The H4 bias gate is `"penalty"`, not `"veto"`, by default —
  [`config.py:638-640`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/config.py#L638-L640).
- Scan cadence is **10 s** (15 s with positions open), not minutes —
  [`scanner/scan_scheduler.py:20-29`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/scan_scheduler.py#L20-L29).

So the honest answer is the first half of the question: **an opportunity-selection
engine constrained by a single-direction-per-scan voting architecture** — not an
intentional directional system. The directional behaviour is an emergent property
of *one* design decision (collapse-then-agreement-gate), not of an HTF dictator.

---

## 2. EVIDENCE TABLE

| Component | file:line | Direction handling | Bidirectional support |
|---|---|---|---|
| `StructureEngine.get_bias` | [`brain/structure_engine.py:327-392`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/structure_engine.py#L327-L392) | **DERIVES_DIRECTION** from H4+H1 only; D1 is context and explicitly *does not* override (counter-D1 allowed) | n/a (produces a bias string) |
| Per-module votes (`vote_from_*`) | [`brain/directional_consensus.py:171-411`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/directional_consensus.py#L171-L411) | **DIRECTION_AGNOSTIC** — each scores both sides, returns signed (LONG/SHORT/NEUTRAL, confidence) | already side-symmetric |
| `decide()` consensus collapse | [`brain/directional_consensus.py:55-164`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/directional_consensus.py#L55-L164) | **HARD_GATE** — sums to one net dir; `min_agreement`, `min_net_score`, `min_contributors`, high-authority veto → `NEUTRAL` | **major rewrite** to emit >1 candidate |
| `PairScanResult.direction` | [`scanner/pair_scanner.py:100`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L100), [`:487`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L487) | **HARD_GATE** — a single `direction` field per pair per scan | **major rewrite** (one result object = one side) |
| `compute_opportunity_quality` (OQ) | [`brain/setup_quality.py:73-258`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/setup_quality.py#L73-L258) | **DIRECTION_AGNOSTIC** — "A perfect BUY and a perfect SELL with mirror inputs produce the SAME score" (and the code matches the docstring) | zero changes |
| `compute_entry_quality` (EQ) | [`brain/setup_quality.py:261-414`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/setup_quality.py#L261-L414) | **DIRECTION_AWARE (location only)** — uses `trade_dir` to score proximity to OB/FVG/liq for that side; does **not** penalise counter-trend | zero changes |
| Consensus weights (live) | [`config.py:258-268`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/config.py#L258-L268) | **SOFT_WEIGHT** — all 9 modules `1.0`; structure is NOT privileged | zero changes |
| `EntryEngine.calculate_entry` | [`trigger/entry_engine.py:117-589`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/trigger/entry_engine.py#L117-L589) | **RECEIVES direction** as a param; mechanically side-symmetric (zone/SL/TP work either side) | zero changes (already two-sided) |
| H4 bias gate (entry) | [`trigger/entry_engine.py:207-223`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/trigger/entry_engine.py#L207-L223), [`config.py:638-640`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/config.py#L638-L640) | **SOFT_WEIGHT** in default `"penalty"` mode (−15 score); `"veto"` is opt-in legacy | zero changes |
| `DecisionEngine.decide_entry` | [`decision/engine.py:636-778`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/engine.py#L636-L778) | **SOFT_WEIGHT** — scores ENTER/SKIP on a *given* direction; never derives/flips it; HTF coeff `0.20`, reversal path built-in | zero changes (scores whatever side it's handed) |
| `_is_counter_htf` / `_reversal_evidence` | [`decision/engine.py:143-166`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/engine.py#L143-L166), [`:708-725`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/engine.py#L708-L725) | **DIRECTION_AGNOSTIC** reversal grader (M5 sweep + M1 BOS + momentum) | already present — reused |
| `tf_alignment` | [`decision/situation.py:109-131`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/situation.py#L109-L131), [`:246-270`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/situation.py#L246-L270) | **SOFT_WEIGHT** — continuous `[-1,1]` weighted D1/H4/H1 sum; not boolean | n/a |
| `CorrelationEngine.can_open_trade` | [`brain/correlation_engine.py:105-144`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/correlation_engine.py#L105-L144), [`:207-232`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/correlation_engine.py#L207-L232) | **HARD_GATE** for opposing same-pair/base when `allow_intentional_hedge=False` (default) | **minor change** (flag or close-and-flip) |
| Scan cadence | [`scanner/scan_scheduler.py:20-52`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/scan_scheduler.py#L20-L52) | **DIRECTION_AGNOSTIC** — 10 s active / 15 s with positions | zero changes |

---

## 3. THE DIRECTIONAL CHOKEPOINTS

These are the *only* places in the live path where direction becomes a hard
constraint. Everything else is direction-agnostic or a soft weight.

### CP1 — Consensus collapse to a single net direction (PRIMARY)
[`brain/directional_consensus.py:91-146`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/directional_consensus.py#L91-L146)
```python
net = sum(v.signed for v in votes)
raw_dir = "LONG" if net > 0 else ("SHORT" if net < 0 else "NEUTRAL")
...
elif agreement < min_agreement:        # 0.55 default
    direction = "NEUTRAL"
```
One net direction is produced; a mixed LTF-vs-HTF panel (the counter-trend scalp)
falls below `min_agreement` and is dissolved to `NEUTRAL`. This is *the* reason
the short never existed as a candidate.

### CP2 — One `direction` field per scan result
[`scanner/pair_scanner.py:100`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L100),
[`:487`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L487),
[`:979-1014`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L979-L1014).
`PairScanResult` carries a scalar `direction`. Even if CP1 were relaxed, the data
model cannot express "short now / long on reversal" — there is no second
candidate slot.

### CP3 — `min_net_score` / `min_contributors` / high-authority veto
[`brain/directional_consensus.py:122-146`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/directional_consensus.py#L122-L146)
(`min_net_score=1.5`, `min_contributors=2`, `high_authority_modules=["currency_strength"]`).
Secondary kills — a thin fast-only cluster (few voters, low net) is also dropped.

### CP4 — Same-pair opposing-position hedge block
[`brain/correlation_engine.py:228-231`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/brain/correlation_engine.py#L228-L231) +
[`config.py:424`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/config.py#L424) (`allow_intentional_hedge=False`).
Even with a counter-direction candidate, you cannot open a EURCAD short while a
EURCAD long is live — the flip must be a *close-then-reverse*, not a hedge.

**Non-chokepoints (explicitly cleared):** OQ/EQ (direction-free), DecisionEngine
(scores a given side, HTF demoted, reversal built-in), H4 gate (penalty),
scan cadence (10 s). The earlier hypotheses that "OQ/EQ would reject a
counter-trend scalp" and "HTF won't let a short through" are **not supported by
the code** — they describe CP1's emergent effect, misattributed to the quality
gates and an HTF dictator.

---

## 4. COUNTER-TREND SCALP FEASIBILITY ASSESSMENT

**Minimum viable path:** the *execution* machinery for a counter-trend trade
already exists end-to-end — the only missing piece is **generating the opposing
candidate** at CP1/CP2.

- **Reusable as-is:** every `vote_from_*` extractor (already two-sided), OQ/EQ
  (direction-free), `EntryEngine` (takes any `direction`), the DecisionEngine
  reversal path (`_is_counter_htf`, `_reversal_evidence`, `reversal_size_multiplier`
  — [`decision/engine.py:708-750`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/decision/engine.py#L708-L750)), RiskEngine, governor, validator.
- **Must be new / changed:**
  1. A **secondary candidate** out of `decide()` — when a coherent minority
     cluster opposes the net direction with high agreement *within that cluster*
     (e.g. momentum+OB+FVG+VWAP all SHORT on M5/M1), emit it as a
     `reversal_candidate` instead of discarding via `min_agreement`.
  2. A **second slot** on `PairScanResult` (`reversal_direction` / a small
     `candidates: list`) so CP2 can carry it.
  3. **Flip handling** at CP4 — either set `allow_intentional_hedge` for the
     scalp class, or implement explicit close-and-reverse.
- **Risk implications:** two positions (or a flip) on one instrument; the
  reversal is already auto-sized down (`reversal_size_multiplier=0.7`,
  [`config.py:792`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/config.py#L792)); the falling-knife guard
  (`reversal_required_evidence=3`) still applies. Main new risk is
  over-trading chop — must be gated behind evidence + the existing BE-stop
  cooldown.
- **Estimated scope:** ~3–5 files (`directional_consensus.py`, `pair_scanner.py`,
  `main_loop` entry dispatch, correlation flag, config), ~250–400 LOC, **medium**
  complexity — because it reuses the existing reversal/entry/risk stack rather
  than building a new one.

---

## 5. FAST-PATH FEASIBILITY

- **Is a lightweight scalp scanner needed for speed?** Largely **no** — cadence
  is already 10 s ([`scan_scheduler.py:20-29`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/scan_scheduler.py#L20-L29)). The
  bottleneck is not loop frequency; it's CP1 dissolving the candidate. A scalp
  that lives 15–25 min is well within a 10 s scan loop.
- **Minimum analysis subset a scalp needs:** the M5/M1-derived votes
  (`vote_from_momentum`, `vote_from_order_blocks`, `vote_from_fvg`,
  `vote_from_vwap`, `vote_from_liquidity`) + OQ (tradeability) + EQ (location) +
  the M1 confirmation already in `EntryEngine._detect_m1_choch`
  ([`trigger/entry_engine.py:1111-1173`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/trigger/entry_engine.py#L1111-L1173)). All present.
- **Can the path be shortened without losing risk controls?** Yes — a scalp
  candidate can **skip nothing on the risk side** (RiskEngine, governor,
  correlation, spread, margin all still apply) and only **bypass the HTF-heavy
  consensus agreement gate**, which is the part that is wrong for LTF reversals.
  The risk stack is already direction-agnostic and fast.

---

## 6. RECOMMENDATION (ordered by ROI)

### Phase 0 — Shadow/measure (no execution, lowest risk)
The scanner already builds counterfactual `RejectedSetup` shadows
([`scanner/pair_scanner.py:166-216`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L166-L216),
[`:933-977`](https://github.com/eugine336/apex-trader/blob/c6baece404e8fb714dddd72e459bba529306aab8/scanner/pair_scanner.py#L933-L977)). Extend `decide()` to also
emit the *suppressed minority cluster* as a shadow candidate (`rejecting_gate =
"consensus_agreement"`), persist it through the existing shadow engine, and let
the gate tuner answer **"how many counter-trend scalps would have won?"** before
any live execution. Smallest change, highest information value.

### Phase 1 — Minimum counter-trend execution
Implement CP1 secondary candidate + CP2 slot, route it through the **existing**
DecisionEngine reversal path (already sized down) and full risk stack, with CP4
close-and-flip (no hedging). Gate behind a config flag (`scalp_reversal_enabled`,
default off) and the existing `reversal_required_evidence`.

### Phase 2 — Full bidirectional opportunist mode
Generalise `PairScanResult` to a ranked `candidates` list, allow the loop to
trade the best-EV candidate per instrument regardless of HTF side, and add a
sequenced "scalp-then-trend-resume" plan object. Larger surface; only justified
if Phase 0 data shows material missed expectancy.

---

*End of audit. No trading logic was modified — only this document was added.*
