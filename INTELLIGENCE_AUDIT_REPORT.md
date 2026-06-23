# APEX TRADER — PRINCIPAL INTELLIGENCE-LAYER FORENSIC AUDIT

**Mandate:** Determine whether the intelligence layer actually generates positive decision-making value, and where exactly that value comes from. Trust nothing but execution paths.

**Method:** Static reconstruction of every live call chain from `main.py` → `platforms/main_loop.py` and its mixins, cross-checked against `scanner/`, `brain/`, `decision/`, `planning/`, `risk/`, `governor/`, `adaptive/`, `ml/`, `rl/`, `management/`. Every claim cites `file:line`. No code was changed.

> **One-sentence verdict (read this first):** Apex Trader is a **rule-based expert system with a bolted-on adaptive-statistics layer**; its real value is *defensive* (a deep stack of risk/correlation/drawdown vetoes), its alpha is a **hand-weighted 123-point confluence score plus a quality gate**, its "machine learning" is conservative bounded multipliers that mostly size *down*, and its reinforcement-learning brain is **wired but dormant (no trained checkpoint ships, so it is inert)**. The biggest systemic risk is that nearly every "intelligent" gate is **fail-open** — a swallowed exception silently degrades it to "allow the trade."

---

## 1. EXECUTIVE SUMMARY

| Question | Finding |
|---|---|
| What actually makes money? | Nothing in the code *proves* alpha — there is no shipped backtest/outcome data. The **structural** edge is the confluence score ([scanner/pair_scanner.py:430-685](#)) gated by SMC quality scores (`setup_quality.py`), plus directional consensus as a direction filter. |
| What actually prevents losses? | The **risk stack**: `DrawdownGuard` freeze, `PortfolioRiskStateMachine` heat states, `CorrelationEngine` exposure caps, per-account daily-loss halts, and `RiskEngine.assess()` as the final sizing/veto authority. This is the most genuinely engineered part of the system. |
| What is decorative? | `brain/mtf_orchestrator.py`, `RegimeDetector`-as-scorer, the entire `ml/` package (re-export shim), `get_recommendations()`, and three unused fields of `TradeAdjustments`. |
| What is dangerous? | Pervasive **fail-open** `try/except` around every learner and several gates; an inert RL path that *looks* active; lifetime-vs-rolling drawdown ambiguity; sizing authority spread across ~6 layers. |
| True intelligence level | **Expert system (rules) + shallow adaptive statistics.** Not a learning system in the live path; not an RL system in practice. |

The system **presents** as a multi-timeframe, multi-learner, RL-augmented portfolio intelligence. In production it **behaves** as: a weighted-confluence scanner → a quality gate → a continuous-scoring decision engine → a thick risk/veto sandwich → mechanical trade management. The learners apply small, heavily-sample-gated, mostly-shrinking multipliers on top.

---

## 2. COMPLETE INTELLIGENCE ARCHITECTURE MAP

### 2.1 Execution graph (live path)

```
main.py:main()
 └─ TradingLoop(config)                         platforms/main_loop.py:181
     └─ _run_supervised_cycle → _scan_and_enter / management cycle
        Market Data: PlatformManager.get_data/get_price   (mt5 + deriv connectors)
          │
          ▼
        SCAN+SCORE:  PairScanner.scan_pair             scanner/pair_scanner.py:430-685
          ├─ brain analysis modules feed points (structure/OB/FVG/session/news/
          │   currency/liquidity/volume/inducement/wyckoff)         :438-599
          ├─ penalties (vwap/momentum-div/atr/volume-profile)       :605-677
          ├─ directional_consensus.decide()  → direction gate       :394-402
          ├─ setup_quality OQ/EQ            → READY/WATCHLIST/WAIT   :878-892
          └─ RLBridge.augment_score        → score delta / veto     :698-774  (DORMANT)
          │
          ▼
        RANK:        PairRanker  (priority among READY)            scanner/pair_ranker.py
          │
          ▼
        ENTRY DECISION (per candidate)  _execute_entry_inner       main_loop.py:1026+
          ├─ DrawdownGuard.can_trade                                :683
          ├─ PortfolioRiskStateMachine freeze (DEFENSIVE/REDUCING)  :943
          ├─ portfolio-heat block ≥1.8%                             :953
          ├─ CorrelationEngine.can_open_trade                       :1005
          ├─ margin guardian floor / max_open_trades                :1010-1016
          ├─ per-account daily-loss halt / heat                     :1363-1370
          ├─ EntryEngine (SL/TP, entry mode, patterns)              :1402
          ├─ DecisionEngine.decide_entry + RiskGovernor.review_entry:1450-1471
          ├─ TradePlanner.plan_trade (+ PortfolioGovernor.check)    :1502-1534
          ├─ EntryValidator                                         :1599
          ├─ spread gate                                            :1619
          ├─ regime threshold (penalty/veto)                        :1641
          ├─ RiskEngine.assess()  ← FINAL sizing+veto authority     :1682
          ├─ EV gate / losing-pattern gate / ML should_trade        :1713-1762
          ├─ adaptive sizing multipliers (capped by risk ceiling)   :1773-1806
          ├─ sidedness validation / execution breaker / idempotency :1813-1862
          └─ PlatformManager.execute_entry → BROKER ORDER           :1969
          │
          ▼
        MANAGE (open positions)  _run_decision_engine               main_loop.py:3135
          ├─ SituationEngine.assess_open_trade                      decision/situation.py:75
          ├─ DecisionEngine.decide_management                       decision/engine.py:159
          ├─ RiskGovernor.review (override)                         main_loop.py:3161
          ├─ TradeManager tick exits (TP/SL/BE/trail/partial)       management/trade_manager.py
          └─ thesis-decay secure / severe-collapse close            decision/engine.py:382-456
          │
          ▼
        FEEDBACK:  trade close → journal/event store → register_new_trade
                   → should_retrain (every 50 trades/7d) → _run_ml_optimization
                                                            main_loop.py:4177,4508,4624
```

### 2.2 Influence hierarchy (who observes / advises / influences / vetoes / enforces)

| Role | Components |
|---|---|
| **OBSERVE (read-only/log)** | `brain/mtf_orchestrator.py` (dead), `RiskReporter`, `DecisionJournal`, `get_recommendations()`, RL `ShadowEngine` (paper), dashboard `state_*` |
| **ADVISE (suggest, can be overruled)** | `DecisionEngine` (conviction/size mult), `TradePlanner` (SL/TP/sizing strategy), adaptive multipliers (`PairLearner`/`SessionLearner`/`RegimeLearner`), `EVEstimator`, `PortfolioGovernor` (advisory-by-design) |
| **INFLUENCE (shape score)** | confluence point weights, penalties, `directional_consensus`, RL score delta (dormant) |
| **VETO (block a trade)** | `setup_quality` gate, `directional_consensus` NEUTRAL, `currency_strength` high-authority flip, `CorrelationEngine`, EV gate, losing-pattern gate, `RiskGovernor.review_entry`, RL veto (dormant) |
| **ENFORCE (final authority on capital/sizing)** | `RiskEngine.assess()`, `DrawdownGuard` (freeze + base risk %), `PortfolioRiskStateMachine`, per-account `AccountRiskManager` halts |

**Final authority on capital deployment & lot size = `RiskEngine.assess()` ([risk/risk_engine.py:112-387](#)).** Every layer after it can only further *reject* or *shrink*; nothing can enlarge past `assessment.position_size_lots` ([main_loop.py:1777-1785](#)).

---

## 3. DECISION AUTHORITY MAP

### 3.1 Ordered hard-veto gauntlet a trade must survive (live entry path)

1. `DrawdownGuard.can_trade` / FROZEN — daily ≤ −5% halts the whole loop — `main_loop.py:683`, re-checked `risk_engine.py:151`
2. `PortfolioRiskStateMachine` DEFENSIVE/REDUCING freeze (heat ≥1.5%/2.5%) — `main_loop.py:943`
3. Portfolio-heat block ≥1.8% — `main_loop.py:953`
4. `CorrelationEngine.can_open_trade` (max_correlated_trades=**2**, signed currency exposure, hedge conflicts) — `main_loop.py:1005`, again `risk_engine.py:199`
5. Margin-guardian entry floor 150% / `max_open_trades`=5 — `main_loop.py:1010-1016`
6. Per-account daily-loss halt 3% / heat 2% — `main_loop.py:1363-1370`
7. `DecisionEngine.decide_entry` SKIP (enter vs skip scoring) + `RiskGovernor.review_entry` (heat 1.8%, spread 3×, R:R<1.0) — `main_loop.py:1450-1471`
8. `TradePlanner.plan_trade` SKIP/WAIT (confidence<0.40 AND agreement<0.50) + `PortfolioGovernor.check` count caps — `trade_planner.py:153-200`, `main_loop.py:1502-1534`
9. `EntryValidator` — `main_loop.py:1599`
10. Spread gate (spread > typical×3) — `main_loop.py:1619`
11. Regime threshold (default = *penalty/size-down*, not veto) — `main_loop.py:1641`
12. **`RiskEngine.assess()` — consolidated final veto + binding lot size** — `main_loop.py:1682`
13. EV gate (neg EV AND pair_mult<1.0) / losing-pattern gate / ML `should_trade` — `main_loop.py:1713-1762`
14. Sizing ceiling, sub-min-lot rejection, sidedness, execution breaker, idempotency — `main_loop.py:1773-1862`

### 3.2 Hidden authority / bottlenecks

- **Hidden veto #1 — the quality gate, not the score.** With `LayeredDecisionConfig.enabled=True` (default), READY status is decided by `setup_quality` OQ≥5.0 **and** EQ≥5.0 ([pair_scanner.py:878-892](#)) — **the famous 123-point confluence score does not gate qualification at all**; it only ranks priority, feeds RL, and serves the legacy (disabled) path. This is the single most important and most counter-intuitive finding.
- **Hidden veto #2 — directional consensus.** A setup with a great score is silently killed if `|net vote|<1.5`, `agreement<0.55`, `<2` contributors, or `currency_strength` (the only `high_authority_module`) opposes with conf≥0.6 ([brain/directional_consensus.py:55-164](#)).
- **Hidden score cap.** RANGING regime caps score at 85 ([pair_scanner.py:680-682](#)).
- **Structural bottleneck.** The tightest recurring gates are `CorrelationEngine` (cap=2, fires twice) and the planner/decision conviction gates. The widest blast radius is the `DrawdownGuard` freeze (halts everything). The single highest-volume rejecter cannot be proven from code alone — it requires the `_log_rejection` telemetry counts.

**What truly decides whether capital is deployed:** a candidate must (a) be born READY from the **quality gate**, (b) survive the **direction/consensus** filter, then (c) run the **risk gauntlet** whose terminal authority is `RiskEngine.assess()`. The "decision intelligence" (DecisionEngine/Planner) sits in the middle as an *advisor that can only veto or shrink*.

---

## 4. ALPHA SOURCE ANALYSIS

### 4.1 The confluence score — point weights (the "source of alpha" map)
Built inline in `scan_pair` (no separate function; accumulation from `score=0`), `scanner/pair_scanner.py:430-685`:

| Component | Module | Max pts | Line |
|---|---|---|---|
| Structure aligned | `structure_engine` | +20 | 438-440 |
| H1 Order Block | `order_block` | +10/7/4 | 446-455 |
| M5 Order Block | `order_block` | +10/7/4 | 460-470 |
| FVG entry zone | `fvg_detector` | +15/10/6 | 477-489 |
| Multi-TF FVG confluence | `fvg_detector` | +15 | 492-498 |
| Session active | `session_engine` | +10 | 509-511 |
| News clear | `NewsGuard` | +10 | 514-520 |
| Currency strength aligned | `currency_strength` | +10 | 524-531 |
| Liquidity sweep | `liquidity_mapper` | +8 | 538-548 |
| Volume confirmed / climax | `volume_analyzer` | +5 / −5 | 551-571 |
| Inducement | `inducement_detector` | +5 | 576-584 |
| Wyckoff spring/upthrust | `wyckoff_engine` | +5 | 588-599 |
| **Penalties** | vwap −15, momentum-div −15/−7, atr −10, vol-profile −10 | — | 605-677 |

**Max base ≈ 123 pts.** Note: `ScoringConfig.order_block_points=20` is **read nowhere** — OB scoring is hardcoded per-TF. Adaptive weights (`ScoreOptimizer`) can replace these defaults within ±25% when `use_adaptive_scoring_weights=True` ([main_loop.py:186-195](#)).

### 4.2 Where alpha really originates (and the disappearance test)

| Layer | Live role | "If it vanished, expectancy…" |
|---|---|---|
| **Structure** (`structure_engine`) | +20 score, bias/direction, consensus vote | Would degrade most — it sets *direction* (H4/H1 bias). Genuine core. |
| **SMC zones** (OB/FVG) | up to +55 score, entry zones, EQ proximity | Core entry-location edge. Removing collapses entry precision. |
| **Directional consensus** | direction gate | Removing it would let counter-trend/low-agreement trades through — likely *worse* expectancy. Alpha-preserving. |
| **Setup quality (OQ/EQ)** | hard READY gate | The actual qualifier. Removing it floods entries → expectancy almost certainly down. |
| **Currency strength** | +10, high-authority veto | Real filter for FX; meaningful. |
| **Liquidity / inducement / wyckoff / volume** | +5–8 each | Marginal; small contributors. Borderline redundant. |
| **Regime (`RegimeDetector`)** | sizing only (`SystemVolatilityMonitor`); **not scoring** | Neutral-to-helpful for sizing; zero alpha as scorer (its scoring path is in dead `mtf_orchestrator`). |
| **Statistical learners** (`EV/Pair/Session/Regime`) | small multipliers, mostly down; some vetoes | Defensive. Disappearing → slightly larger/more trades; expectancy impact unproven, likely small. |
| **RL** | dormant | Zero impact today. |
| **Risk/portfolio** | vetoes + sizing | Not alpha — *drawdown reduction*. Huge value for survival, none for raw edge. |

**Genuine alpha generators:** structure + SMC zones + consensus + quality gate.
**False/illusory alpha:** RL (inert), `RegimeDetector` scoring (dead), `mtf_orchestrator`.
**Redundant:** inducement/wyckoff/volume micro-points overlap with structure/OB already counted.
**Alpha destroyers (potential):** over-tight gates (correlation cap=2; consensus agreement≥0.55) can suppress edge — see Phase 10.

---

## 5. LEARNING EFFECTIVENESS ANALYSIS

`self.ml = AdaptiveOptimizer` ([main_loop.py:47,234](#)). The `ml/` package is a **dead re-export shim** of `adaptive/` (only `tests/test_ml_adapter.py` imports it; every `ml/*.py:2` re-exports `adaptive`). 

| Learner | Persists? | Live influence | Min sample | Class |
|---|---|---|---|---|
| `AdaptiveOptimizer` | coordinates | `get_trade_adjustments`→size/should_trade `main_loop.py:1757`; `is_losing_pattern`→reject `:1738` | 50 trades / 7d retrain | **Active+useful** |
| `ScoreOptimizer` | `data/scoring_weights.json` | loaded→scanner weights `main_loop.py:186-195`, hot-reload `:4654` | 50 trades + OOS gate + ±25% clamp | **Active+useful** |
| `RegimeLearner` | `data/ml_regime_strategies.json` | score-bump/veto `:1629-1673`; planner TP/SL shaping `:3386-3393` | 30 trades, win<0.40 to block | **Active+useful** |
| `PairLearner` | `data/ml_pair_profiles.json` | size mult / AVOID→0.0 `optimizer.py:181-192`; EV gate `:1719` | 20 trades (default **0.8 = size-down**) | **Active+useful (pessimistic)** |
| `SessionLearner` | `data/ml_session_profiles.json` | size ×1.1/×0.85 / AVOID `optimizer.py:199-208` | 15 trades | **Active+useful** |
| `EVEstimator` | live-computed | scanner EV + RiskEngine EV veto `risk_engine.py:223-246` | 10 trades else EV=0 | **Active+useful (conservative)** |
| `GateTuner` | `data/gate_tuning.json` | tunes `ev_gate` + `entry_engine` bar within bounds `:4552` | 30 shadows | **Active+useful** |
| `TradeAnalyzer` | — | only losing-pattern path is live; `analyze_*`/`score_edge` logged | 20 / 10 | **Mixed: useful + logging-only** |
| `get_recommendations()` | — | **no live caller** | — | **Logging-only** |
| `TradeAdjustments.score_threshold_adjustment / tp_multiplier / sl_buffer_adjustment` | — | computed, **never read** in live path | — | **Dead fields** |
| entire `ml/` package | — | test-only | — | **Dead code** |

**"Would live results materially change if disabled?"** — Marginally. These learners are small, bounded, heavily sample-gated, and frequently default to *down-sizing* or *neutral*. Disabling them would mostly produce slightly larger and slightly more frequent trades. They are risk-dampeners, not edge-creators.

---

## 6. FEEDBACK LOOP ANALYSIS

- **Outcome → storage:** trade close → `TradeJournal` + event store; `register_new_trade()` increments the retrain counter ([main_loop.py:4177](#)).
- **Storage → learning:** `should_retrain()` true every **50 trades or 7 days** → `_run_ml_optimization()` retrains all learners on a **90-day / ≥50-trade recency window** ([optimizer.py:56-64,102-120](#); `main_loop.py:4508-4624`).
- **Learning → decision:** new `ScoringWeights` hot-reload into the scanner ([main_loop.py:4654-4662](#)); learner JSONs reload on init.
- **Latency / adaptation speed:** **slow.** Nothing adapts until ≥50 closed trades exist, then only every 50 trades/7 days, with ±25% per-cycle clamps and OOS validation. Per-trade learning does **not** exist.
- **Stability:** good — envelopes/clamps/min-samples prevent thrashing. **Persistence:** good — all state is on-disk JSON, reloaded on restart.

**"Can the system become smarter through experience?"** — Yes, but **weakly and slowly**, and only along narrow axes (scoring weights, per-pair/session/regime multipliers, two tunable gates). It cannot learn new structure, new features, or new strategies — only re-weight existing hand-built ones. **The RL path, which is the only component capable of genuine policy learning, is inert.**

---

## 7. TIMEFRAME INFLUENCE ANALYSIS (effective, not configured)

| Function | Dominant timeframe | Evidence |
|---|---|---|
| **Direction / bias** | **H4 + H1 (+D1 context)** | `structure.get_bias` `pair_scanner.py:279`; situation TF weights D1 0.40 / H4 0.35 / H1 0.25 `situation.py:71-73` |
| **Entry zone / timing** | **M5 / M15** | OB/FVG zones `pair_scanner.py:446-498`; entry mode from M1 confirmation `main_loop.py:1866` |
| **Entry conviction & SIZE** | **M5 structure + M1 momentum** | `DecisionWeights`: structure 0.40/0.45, momentum 0.30; HTF only 0.20 `engine.py:38-47` |
| **Exits / management** | **trade's own structure + momentum; HTF demoted** | mgmt HTF close coeff lowered 0.35→**0.15** `engine.py:199`; structure×1.0 dominates |
| **Rejection** | **H4/H1 (consensus) + M5 (quality)** | consensus votes + OQ/EQ |

**Effective hierarchy:** HTF (H4/H1/D1) sets *whether and which direction*; M5/M1 sets *when, how big, and when to exit*. The design **explicitly demotes HTF** in management so "a lagging HTF flip cannot force a close" ([engine.py:194-199](#)). RL's `MultiTFObservationBuilder` is the only true MTF-fusion object live, but it feeds the dormant bridge.

---

## 8. MANAGEMENT INTELLIGENCE ANALYSIS

Two layers run per cycle on open positions:
1. **Strategic — `DecisionEngine.decide_management`** ([engine.py:159-314](#)): continuous weighted scoring over HOLD / CLOSE / TIGHTEN_SL / MOVE_TO_BREAKEVEN; highest score wins; `RiskGovernor.review` can override ([main_loop.py:3157-3164](#)). The verdict is cached and the tick-level manager defers to it ([main_loop.py:3171](#)).
2. **Mechanical — `TradeManager`** (`management/`): TP1/TP2/TP3 ladder, breakeven, structure trailing, partial close — fires on tick.

**Why winners are held:** HTF aligned (+0.35×alignment), structure intact (+0.30), positive momentum/profit ([engine.py:175-184](#)).
**Why winners are exited/secured:** momentum fading at profit → TIGHTEN/BE ([engine.py:250-273](#)); **thesis-decay secure** banks profit when the *reason to hold* decays while in profit ([engine.py:382-456](#)) — and **severe collapse (≥0.80) closes a profitable trade at market** ([engine.py:416-426](#)).
**Why losers are held:** healthy pullback in intact trend (structure≥0.5 and momentum≥0) is explicitly kept ([engine.py:218](#), `_maybe_thesis_secure` healthy-pullback guard `:399-404`).
**Why losers are exited:** active loss-response once structure/momentum stop supporting + loss depth scaling ([engine.py:218-224](#)); structure broken (<0.25) ([engine.py:204-206](#)).

**What management protects:** primarily **capital and realized profit** (the whole thesis-secure machinery is profit-protection), and **expectancy** via early loss-response. It is **reactive-but-structured** — genuinely situation-driven rather than fixed R-rules, which is a real strength. **Failure flag:** `_run_decision_engine` wraps the entire strategic decision in `try/except … "falling back to legacy"` but the except branch **only logs** ([main_loop.py:3175-3179](#)) — on any error the strategic layer silently no-ops and management is left to the mechanical TradeManager.

**"Can it tell a healthy pullback from a dying trade?"** — Yes, deliberately and explicitly (`thesis_healthy_structure`/`thesis_healthy_momentum` guard vs `_thesis_deterioration_score`). This is the most sophisticated genuinely-working intelligence in the system.

---

## 9. PORTFOLIO INTELLIGENCE ANALYSIS

Portfolio behavior is **split across three components**, only one of which is true allocation logic:
- **`CorrelationEngine`** ([brain/correlation_engine.py:105-144](#)) — the real correlation-aware enforcer: signed currency exposure, asset-cluster same-direction caps, hedge-conflict detection. Hard veto. **Genuine value.**
- **`PortfolioRiskStateMachine`** ([risk/portfolio_risk_state.py:434-736](#)) — heat-based NORMAL→DEFENSIVE→REDUCING→EMERGENCY with hysteresis; freezes entries and drives trims/closes. **Genuine value (enforces).**
- **`PortfolioGovernor`** ([governor/portfolio_governor.py:72-194](#)) — **count-based caps only** (max positions 8, currency 3, sector 4, correlated 2, daily-loss halt). By its own docstring "advisory to the planner, NOT a hard gate" ([:10-11](#)). Consulted **only inside the planner** ([trade_planner.py:176](#)); if the planner were disabled it would silently stop enforcing on fresh entries. Scale-in governor check **fails open** (`except → return True`, `main_loop.py:3771`).

**"Does the portfolio layer create measurable value?"** — Yes for **drawdown control** (correlation + heat states are real and enforcing). It is **not** a portfolio *optimizer* — there is no capital-allocation optimization, no correlation matrix sizing, no Kelly/vol-targeting across the book; it is a set of exposure *limits*.

---

## 10. COUNTERFACTUAL REJECTION ANALYSIS

The system **already contains a counterfactual engine**: rejected setups persist as **shadow contracts** ([main_loop.py `_persist_shadow_contract`](#)) resolved on the live feed by a paper `TradeManager` ([main_loop.py:264-279](#)), and `GateTuner` ([adaptive/gate_tuner.py](#)) loosens/tightens `ev_gate` and `entry_engine` thresholds based on whether rejected setups *would have won*. This is the right architecture — but it is **bounded to two gates** and requires ≥30 resolved shadows.

| Gate | Likely effect on edge | Note |
|---|---|---|
| Setup-quality OQ/EQ ≥5.0 | **alpha-preserving** (primary qualifier) | but threshold is hand-set, never counterfactually tuned |
| Directional consensus (agreement≥0.55) | mixed — can suppress valid reversals | high-authority `currency_strength` flip is aggressive |
| Correlation cap = 2 | **most likely alpha-suppressing** | very tight; blocks otherwise-valid uncorrelated-enough setups |
| EV gate / losing-pattern | alpha-preserving (defensive) but **fail-open & double-conditioned** | EV block needs neg-EV *and* pair_mult<1.0 → rarely fires |
| Regime threshold (penalty mode) | mild, size-only | doesn't reject, only shrinks |

**Counterfactual coverage gap:** the *highest-authority* gates (quality OQ/EQ, consensus, correlation) are **not** wired into the shadow→tuner loop — only the two least-impactful gates are tuned. So the system cannot currently learn whether its *real* bottlenecks are helping or hurting. **This is the single biggest missed opportunity in the intelligence layer.**

---

## 11. FAILURE MODE ANALYSIS

- **Single points of failure:** `RiskEngine.assess()` (sole sizing authority); `DrawdownGuard` (whole-loop freeze); `PositionStore` availability (in-flight idempotency — unavailable store skips entries, fail-safe).
- **Pervasive fail-open (the dominant systemic risk):** `is_losing_pattern` `except→allow` (`main_loop.py:1753`); `get_trade_adjustments` `except→mult 1.0` (`:1794-1806`); EV pair_mult `except→1.0` makes the EV block unreachable (`:1717-1723`); scanner EV `except→0.0` (`pair_scanner.py:695`); regime gate `except→skip` (`:1672`); `SpreadMonitor` returns "allow" with no history (`spread_monitor.py:58-59`); scale-in governor `except→True` (`:3771`); strategic management `except→silent no-op` (`:3175`). **Net effect: any transient error silently downgrades an intelligent gate to "permit."**
- **Hidden assumptions:** confluence point weights are hand-tuned and assumed meaningful; OQ/EQ 5.0 threshold assumed correct; correlation map completeness (unmapped symbols fall back to same-instrument stacking cap, `correlation_engine.py:184-189` — does NOT fail open, good).
- **Adaptation instability:** mitigated by clamps/OOS/min-samples; low risk.
- **Regime/black-swan failure:** direction depends on H4/H1 structure trend strings; in violent regime flips the demoted-HTF management may hold losers longer than ideal; the drawdown freeze is the backstop.
- **Illusory-intelligence failure:** RL appears wired (score delta at stage 3+, veto at stage 5+) but **no checkpoint ships**, bridge sets `enabled=False` on load failure, authority seeds at stage 1 → `rl_delta=0` forever ([rl/bridge.py:99-114,139-146](#); `rl/authority.py:325-328`). An operator could believe RL is contributing when it is inert.

**Most likely conditions to break it:** (1) data/feed hiccups that trip fail-open gates into over-permissiveness during volatility; (2) sharp regime reversals where hand-weighted confluence + demoted HTF lag; (3) correlated cross-pair shocks if the correlation map is incomplete.

---

## 12. INSTITUTIONAL DUE DILIGENCE — WHAT IS THIS, REALLY?

- **Claimed:** "Institutional-grade," multi-learner, RL-augmented, multi-timeframe portfolio intelligence (`main.py:58`, dashboard `MLInsights`, RL authority staging).
- **Actual classification:** **Hybrid = rule-based expert system (dominant) + shallow adaptive-statistics layer (secondary) + dormant RL (decorative).** 
  - It is **not** a learning system in the live path (no online learning; slow bounded re-weighting only).
  - It is **not** an RL system in practice (inert).
  - It **is** a competent **expert system with an unusually strong risk/management spine**.
- **Claimed intelligence level: 8–9/10. Actual: ~4–5/10** for *decision* intelligence, **7/10** for *risk/management* engineering.

---

## DELIVERABLE 13 — TOP 20 MOST IMPORTANT COMPONENTS

1. `RiskEngine.assess()` — final veto + sizing authority (`risk_engine.py:112`)
2. `setup_quality` OQ/EQ gate — the real qualifier (`pair_scanner.py:878`)
3. `PairScanner.scan_pair` confluence score (`pair_scanner.py:430`)
4. `structure_engine` — direction/bias + biggest score weight
5. `DrawdownGuard` — freeze + base risk % (`drawdown_guard.py`)
6. `CorrelationEngine` — exposure veto (`correlation_engine.py:105`)
7. `PortfolioRiskStateMachine` — heat enforcement (`portfolio_risk_state.py:434`)
8. `directional_consensus` — direction gate (`directional_consensus.py:55`)
9. `DecisionEngine.decide_entry` — enter/skip + conviction sizing (`engine.py:553`)
10. `DecisionEngine.decide_management` + thesis-secure (`engine.py:159,382`)
11. `PositionSizer` — lot math (`position_sizer.py:54`)
12. `TradeManager` — mechanical exits (`management/trade_manager.py`)
13. `order_block` + `fvg_detector` — entry zones
14. `EntryEngine` — SL/TP/entry-mode (`trigger/entry_engine.py`)
15. `TradePlanner` — SL/TP/sizing strategy + governor host (`trade_planner.py`)
16. `AccountRiskManager` — per-account halts (`account_risk.py`)
17. `ScoreOptimizer` — adaptive weights (`adaptive/score_optimizer.py`)
18. `currency_strength` — high-authority direction filter
19. `RiskGovernor` — post-decision override (`decision/governor.py`)
20. `PositionStore` + idempotency — double-submit prevention

## DELIVERABLE 14 — TOP 20 LEAST VALUABLE COMPONENTS

1. entire `ml/` package — dead re-export shim
2. `brain/mtf_orchestrator.py` — not imported live; test asserts its absence
3. `RegimeDetector` as **scorer** — only reachable via dead orchestrator
4. RL `trainer.py`/`mtf_trainer.py`/`vec_env.py`/`evaluator.py` — offline only
5. RL `bridge.py`/`authority.py`/`shadow.py` — wired but inert (no checkpoint)
6. `AdaptiveOptimizer.get_recommendations()` — no live caller
7. `TradeAdjustments.score_threshold_adjustment` — computed, never read
8. `TradeAdjustments.tp_multiplier` — never read (regime learner used instead)
9. `TradeAdjustments.sl_buffer_adjustment` — never read
10. `ScoringConfig.order_block_points` — read nowhere
11. `inducement_detector` — +5, marginal/redundant with structure
12. `wyckoff_engine` — +5, profile-gated, marginal
13. `volume_profile` penalty — −10 only, narrow
14. `TradeAnalyzer.analyze_*`/`score_edge` — logging-only
15. `PortfolioGovernor` count caps — advisory, redundant with CorrelationEngine/PRSM
16. `RiskReporter` — telemetry only
17. `session_vwap` standalone — single penalty term
18. `atr_percentile` as scorer — single penalty term
19. `momentum_divergence` — overlaps situation momentum
20. dashboard `state_ml`/`MLInsights` — presents learners as more active than they are

## DELIVERABLE 15 — COMPONENTS CREATING REAL ALPHA
`structure_engine` (direction + zones), `order_block` + `fvg_detector` (entry location), `directional_consensus` (filters bad direction), `setup_quality` OQ/EQ (qualifies setups), `currency_strength` (FX direction). The conviction-based `DecisionEngine` sizing adds modest edge by sizing high-conviction setups up and counter-trend down.

## DELIVERABLE 16 — COMPONENTS REDUCING DRAWDOWN
`DrawdownGuard`, `PortfolioRiskStateMachine`, `CorrelationEngine`, `AccountRiskManager` (daily-loss/heat/flatten), `RiskEngine.assess()` sizing ceiling + score-scaling, `RiskGovernor`, thesis-secure profit protection (`engine.py:382-456`), spread/EV/losing-pattern gates (when not fail-opened), execution circuit breaker + idempotency. **This is the system's strongest cluster.**

## DELIVERABLE 17 — COMPONENTS SUPPRESSING OPPORTUNITY
`CorrelationEngine` max_correlated_trades=2 (tightest), consensus agreement≥0.55 + currency-strength high-authority flip, OQ/EQ both-≥5.0, regime score cap (85) in RANGING, `PairLearner` default 0.8 down-size for thin-history pairs, reversal evidence requirement (≥3) + reversal size haircut. None are counterfactually validated except indirectly via the (narrow) GateTuner.

## DELIVERABLE 18 — COMPONENTS WITH NO MEASURABLE VALUE
The entire `ml/` package; `mtf_orchestrator`; `RegimeDetector` scoring path; RL subsystem as shipped; `get_recommendations()`; three `TradeAdjustments` fields; `order_block_points` config. These execute or exist but change no live outcome.

## DELIVERABLE 19 — HIGHEST-RISK ASSUMPTIONS
1. **Fail-open everywhere** — errors silently permit/over-size trades (Phase 11 list). #1 risk.
2. **RL looks active but is inert** — operational misperception of capability.
3. **Hand-tuned confluence weights + OQ/EQ 5.0 thresholds assumed correct** and not counterfactually validated.
4. **Correlation-map completeness** assumed for exposure safety.
5. **Lifetime vs rolling drawdown** semantics gate sizing indefinitely on a drawn-down account (recent fix added rolling window — verify deployment).
6. **Sizing authority diffused** across ~6 layers; only the final `min(ceiling)` keeps it safe — a regression in any multiplier could compound.
7. **Slow feedback (≥50 trades)** means the system is effectively static on small accounts/low trade counts.

## DELIVERABLE 20 — FINAL VERDICT

- **What actually makes money:** the confluence score + SMC entry zones + directional consensus + quality gate, sized by conviction. That's the entire offensive edge, and it is **rule-based, not learned**.
- **What actually prevents losses:** the risk spine — `DrawdownGuard`, `PortfolioRiskStateMachine`, `CorrelationEngine`, per-account halts, and `RiskEngine.assess()`. This is the best-engineered part and the true reason the account survives.
- **What is decorative:** RL (inert), `mtf_orchestrator`, `RegimeDetector` scoring, `ml/` package, recommendation/analytics outputs, several dead config/fields.
- **What is redundant:** `PortfolioGovernor` (vs CorrelationEngine + PRSM); inducement/wyckoff/volume micro-points (vs structure/OB).
- **What is dangerous:** the **fail-open pattern** on every intelligent gate; the **illusion** of an active RL/ML brain; diffuse sizing authority.
- **What should be removed:** `ml/` package, `mtf_orchestrator`, dead `TradeAdjustments` fields, `order_block_points`. Either **train+ship an RL checkpoint or disable the RL path explicitly** so it isn't mistaken for active.
- **What should be strengthened:** (1) flip critical gates from fail-open to fail-closed (or fail-safe-skip); (2) wire the **shadow→counterfactual tuner into the high-authority gates** (quality, consensus, correlation), not just `ev_gate`/`entry_engine`; (3) consolidate sizing authority; (4) make RL status unambiguous on the dashboard.
- **True intelligence level:** **expert system + shallow adaptive statistics (~4–5/10 decision intelligence; ~7/10 risk engineering).** Not a learning or RL system in practice.
- **Biggest edge:** the **risk/management spine** (drawdown + correlation + heat + thesis-secure profit protection) — survival engineering.
- **Biggest weakness:** **fail-open gates** + **unvalidated, hand-tuned offensive thresholds** that are never counterfactually checked, while the only true learning capability (RL) sits dormant.

### If forced to keep only 10% of the intelligence layer, these survive (and why)
1. `RiskEngine.assess()` — without it there is no sizing or final veto.
2. `DrawdownGuard` — capital survival backstop.
3. `CorrelationEngine` — prevents correlated blow-ups (real drawdown reduction).
4. `PortfolioRiskStateMachine` — portfolio heat enforcement.
5. `PairScanner` confluence score + `structure_engine` — the only offensive edge and the direction source.
6. `setup_quality` OQ/EQ gate — the real qualifier.
7. `DecisionEngine` (entry conviction + management thesis-secure) — situation-aware sizing and the genuinely-smart profit protection.

Everything else — RL, the learner zoo, the orchestrators, the second governor — is **defense-in-depth, decoration, or dormant**, and the system would still trade and (more importantly) still *survive* without it.

---

## UPDATE — Instrument-awareness layer wired (Governor + 1B + 1C)

The instrument-blindness called out above has been addressed end-to-end
(see `SYSTEM_ARCHITECTURE.md` §3 for the full account):

* **ModuleGovernor + VoteCalibrator enabled.** Both were silently inert: their
  `enabled` flag was read off the top-level `AppConfig` instead of the nested
  `ModuleGovernorConfig` / `VoteCalibratorConfig`, so it always resolved `False`.
  `core/system_context.py` now passes the nested config — the Governor's
  shadow/disable machine and the calibrator's accuracy weighting are live
  (cold-start neutral until graded data accrues).
* **1B — Symbol-relative conviction.** `adaptive/symbol_conviction.\
SymbolConvictionStore` re-expresses each `form_thesis` conviction relative to the
  symbol's own distribution; wired identically into the live and backtest planes.
  `EURUSD 0.82` and `XAUUSD 0.82` no longer mean the same thing once each symbol
  warms up.
* **1C — Per-symbol sharding.** VoteCalibrator multipliers and ModuleGovernor
  suppression are now computed per symbol from that symbol's own graded accuracy
  (the `SignalLedger` per-pair query), with global fallback. GBPJPY's track record
  no longer recalibrates or shadows a module on EURUSD.

**Revised grade:** **B → approaching A.** Conviction is symbol-relative, the two
accuracy-driven learners isolate per symbol, and the per-symbol CalibrationEngine
is already enabled. Remaining toward a full **A**: shard `GateTuner` by gate-family
× symbol once per-symbol shadow-outcome volume supports it, and surface the
per-symbol overlays on the Learning dashboard.
