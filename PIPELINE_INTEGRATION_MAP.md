# APEX TRADER — PIPELINE INTEGRATION MAP

**Purpose:** A complete, end-to-end blueprint of the intelligence pipeline — every layer, every component, every data handoff, every staleness gap, and every place a rich signal is reduced to a scalar or dropped entirely. This is the reference for designing a proper integration (syncing the three independent stages: Scanner → Decision/Planning → Risk/Execution).

**Method:** Static reconstruction from the live call chain (`platforms/main_loop.py` + mixins) cross-checked against `scanner/`, `brain/`, `decision/`, `planning/`, `governor/`, `risk/`, `adaptive/`, `management/`. Every claim cites `file:line`. **No code was changed.**

> **One-line orientation:** Three stages each independently re-derive the market and do **not** share their conclusions. The **Scanner** computes a 0–123 confluence `score` *and* two 0–10 quality scores (`OQ`/`EQ`). `OQ/EQ` alone gate `READY` (`pair_scanner.py:878-892`); the `score` becomes a sizing dial later (`risk_engine.py:497-504`). After `READY`, the **Decision/Planning** stage re-runs structure/momentum from *fresh* candles and never reads `OQ/EQ` again. The **Risk** stage owns the final lot size. The biggest integration gaps are: (1) `OQ/EQ` are computed once and never re-validated; (2) the `score` used for sizing is **stale**; (3) the `TradePlanner` was designed as the integration point but several of its richest context fields collapse to defaults; (4) nearly every "intelligent" gate is **fail-open**.

---

## TABLE OF CONTENTS
1. Layer-by-layer reference (data sources, I/O, consumers)
2. Complete data-flow diagram
3. Handoff table (what is passed / what is dropped)
4. Staleness map
5. Authority map
6. Information-loss table
7. Proposed integration points (concrete, implementable)

---

# 1. LAYER-BY-LAYER REFERENCE

## LAYER 1 — Market Data

| Item | Detail | Cite |
|---|---|---|
| Source | `self.platforms.fetch_all_market_data(now_utc=now)` (MT5 + Deriv connectors via `PlatformManager`) | `platforms/main_loop.py:861` |
| Timeframes | Per pair dict `{H4,H1,M15,M5}` fetched each cycle; `D1` fetched separately and **cached ~1h** then merged | `main_loop.py:869-892` |
| D1 cache | `_get_d1_cached`; refresh if `>3600s` old, else stale cache reused | `main_loop.py:858, 870-888` |
| Currency data | `currency_data = {pair: frames["H1"] ...}` — H1 close series keyed by symbol, fed to the strength meter | `main_loop.py:898` |
| Cached for mgmt | `self._last_market_data = market_data` reused for in-trade analysis this cycle | `main_loop.py:895` |
| **Re-fetch at entry** | `_execute_entry_inner` fetches **fresh** market data again (needs ≥4 TFs + M5/M1/H1) | `main_loop.py:1323-1336` |
| Format | `pandas.DataFrame` OHLC with columns `open/high/low/close/time` (+ optional `tick_volume`/`volume`) | (consumed throughout `brain/*`) |

**Key fact:** the scan cycle and the per-candidate entry use **two separate fetches** of market data. The scanner's `score`/`OQ`/`EQ` are computed on the scan-cycle data; the Decision/Planner re-derive structure on the entry-time fetch. This is the structural root of the staleness gaps (§4).

---

## LAYER 2 — Brain Modules

Each brain module is a pure analyzer. The scanner calls them twice in spirit: once to cast a **directional vote** (consensus) and once to accumulate **score points**. The table gives inputs/outputs and which consumer reads each.

| Module / class | File | Main method (line) | Inputs (TF/data) | Output (fields) | Consumed by |
|---|---|---|---|---|---|
| `StructureEngine` | `brain/structure_engine.py` | `get_bias(h4,h1,d1)` (327) | H4+H1 (+optional D1) OHLC | dict: `direction, h4_trend, h1_trend, d1_trend, d1_aligned, d1_confidence, strength, swing_high/low, confidence, tradeable` | Score `+20` (`pair_scanner.py:438`); consensus `vote_from_structure`; D1/H4/H1 trend fields → `PairScanResult` |
| `StructureEngine.analyze` | same | `analyze(df)` (63) | single OHLC df | `StructureAnalysis(trend,last_event,swing_high/low,confidence,...)` | Re-run **fresh** in `_build_entry_context`/`_build_trade_context` for D1/H4/H1/M1 trend/event/swing (`main_loop.py:3262-3277, 3522-3540`) |
| `OrderBlockDetector` | `brain/order_block.py` | `detect(df,tf)` (66); `get_entry_ob` (235) | H1 & M5 OHLC | `list[OrderBlock]` (kind,top,bottom,midpoint,strength,status) | H1 OB `+10/7/4` (`:446`), M5 OB `+10/7/4` (`:460`); consensus `vote_from_order_blocks`; EQ OB-distance (`:839`) |
| `FVGDetector` | `brain/fvg_detector.py` | `detect` (55); `get_entry_fvg` (164); `get_confluence_fvgs` (204) | M5 & M15 OHLC | `list[FairValueGap]`; confluence dict `{has_confluence,strength,...}` | FVG `+15/10/6` (`:477`), MTF confluence `+15` (`:492`); consensus `vote_from_fvg`; EQ FVG-distance (`:845`) |
| `SessionEngine` | `brain/session_engine.py` | `get_status(utc_now)` (68) | clock only | `SessionStatus(current_session,is_tradeable,liquidity,session_open_minutes,minutes_to_next_session,...)` | Score `+10` (`:509`); OQ session component (`:826`); penalties (VWAP) |
| `NewsGuard` | `brain/session_engine.py:218` | `check(pairs,utc_now)` (231) | pair list + ForexFactory RSS | `NewsStatus(is_clear,next_high_impact,affected_currencies,...)` | Score `+10` (`:514`); OQ news component (`:824`) |
| `CurrencyStrengthMeter` | `brain/currency_strength.py` | `calculate(price_data)` (69); `get_pair_alignment` (209) | dict{pair:H1 df} | `StrengthAnalysis(rankings,strongest,weakest,...)`; alignment dict | Score `+10` (`:524`); consensus `vote_from_currency_strength` — **the only `high_authority` veto module** (`config.py:278`) |
| `LiquidityMapper` | `brain/liquidity_mapper.py` | `map(df)` (46); `detect_sweep` (220); `classify_sweep_reaction` (249) | H1 (map) + M5 (sweep) OHLC | `LiquidityMap(nearest_buy/sell_liq,liquidity_bias,...)`; `(kind,dir,conf)` | Score `+8` sweep (`:546`); consensus `vote_from_liquidity`; EQ liq-distance (`:851`); side-agnostic R:R (`:817`) |
| `VolumeAnalyzer` | `brain/volume_analyzer.py` | `analyze(df)` (48) | M5 OHLC(+vol) | `VolumeAnalysis(volume_ratio,has_spike,confirmation_bias,climax_detected,...)` | Score `+5/-5` (`:551`); consensus `vote_from_volume`; OQ volume health (`:830`) |
| `InducementDetector` | `brain/inducement_detector.py` | `analyze(df)` (38) | M5 OHLC | `InducementAnalysis(inducement_detected,type,expected_direction,confidence)` | Score `+5` (`:576`); ranking bonus |
| `WyckoffEngine` | `brain/wyckoff_engine.py` | `analyze(df)` (58) | H1 OHLC | `WyckoffAnalysis(phase,sub_phase,spring/upthrust_detected,...)` | Score `+5` (SPRING/UPTHRUST) (`:588`); consensus `vote_from_wyckoff` |
| momentum (functions) | `brain/momentum_divergence.py` | `momentum_divergence_penalty` (90); `calculate_rsi/macd` | M5+H1 close | `(penalty,reason)` | Penalty `-15/-7` (`:628`); consensus `vote_from_momentum` |
| vwap (functions) | `brain/session_vwap.py` | `session_vwap_penalty` (55) | M5 + session_open_minutes | `(penalty,reason)` | Penalty `-15` (`:615`); consensus `vote_from_vwap` |
| atr-pctl (functions) | `brain/atr_percentile.py` | `atr_percentile_penalty` (45); `compute_atr_percentile` (22) | OHLC | `(penalty,reason)`; pctl 0–100 | Penalty `-10` (`:644`); OQ volatility component (`:794`) |
| vol-profile (functions) | `brain/volume_profile.py` | `volume_profile_poc_penalty` (59) | H4 OHLC(+vol) | `(penalty,reason)` | Penalty `-10` (`:657`) — only commodity/index/crypto |
| `RegimeDetector` | `brain/regime_detector.py` | `analyze(df)` (59); `adjust_score` (103) | single OHLC | `RegimeAnalysis(regime,volatility_ratio,tradeable,...)` | **Scoring path only reachable via dead `mtf_orchestrator`** — in the live path regime = `bias["h4_trend"]` string (`pair_scanner.py:680`) |
| `SystemVolatilityMonitor` | `brain/regime_detector.py:234` | `update(analyses)` (265) | `list[RegimeAnalysis]` | `SystemVolatilityState(state,size_multiplier,...)` | Batch sizing (`vol_mult`) in entry path |
| `CorrelationEngine` | `brain/correlation_engine.py` | `can_open_trade` (105); `calculate_exposure` (74) | open trades list | `(bool,reason)`; `ExposureMap` | Hard veto in `_scan_and_enter` (`:1012`) **and** inside `RiskEngine.assess` (`risk_engine.py:202`) |
| `setup_quality` (functions) | `brain/setup_quality.py` | `compute_opportunity_quality` (73); `compute_entry_quality` (261) | market-condition scalars (OQ) / location scalars (EQ) | `OpportunityQuality(score,components,reasons)`; `EntryQuality(...)` | **The READY gate** (`pair_scanner.py:881`); ranking (`:984`) |
| `directional_consensus` | `brain/directional_consensus.py` | `decide(votes,...)` (55) | list of `Vote` | `DirectionDecision(direction,net_score,agreement,contributors,opposed_by)` | **Sets `trade_dir`** (`pair_scanner.py:402`); silent direction veto |

### Score weights vs config (a hidden dead-config note)
- Live point weights are accumulated inline from `score=0` in `scan_pair` (`pair_scanner.py:430-684`). Adaptive weights `_w` (from `ScoreOptimizer`, ±25% envelope) replace defaults when `use_adaptive_scoring_weights=True` (default; `config.py:227`).
- The consensus `cc.weights.get("structure", 3.0)` style fallbacks in `pair_scanner.py:291,324,...` are **dead defaults** — `ConsensusConfig.weights` always supplies every key at `1.0` (`config.py:258-268`), so every module votes at weight `1.0` regardless of the `3.0/2.0/1.5` literals in the scanner.

---

## LAYER 3 — Scanner (`scanner/pair_scanner.py`)

**`scan_pair`** (233-936) flow:
1. Market-hours gate → returns `MARKET_CLOSED` early (`:250-276`).
2. `bias = structure.get_bias(h4,h1,d1)` (`:279`).
3. **Consensus voting** (`:285-403`): builds `Vote`s from 9 modules, `decide(...)` → `trade_dir` + `DirectionDecision`. Defaults: `min_net_score=1.5`, `min_agreement=0.55`, `min_contributors=2`, `high_authority_modules=["currency_strength"]`, `high_authority_oppose_confidence=0.6` (`config.py:275-281`).
4. **Score accumulation** (`:430-603`): structure/OB/FVG/MTF/session/news/currency/liquidity/volume/inducement/wyckoff.
5. **Penalties** (`:605-677`): VWAP/momentum-div/ATR/vol-profile (shadow-mode aware).
6. **Regime caps** (`:679-684`): `RANGING → min(score, ranging_score_cap)`; inactive session `-10`.
7. **EV estimate** (`:686-696`) — fail-open `except → 0.0`.
8. **RL augmentation** (`:698-777`) — `RLBridge.augment_score`; **dormant** (no checkpoint → `enabled=False` → `rl_delta=0`); can early-return `WAITING` if `vetoed` (never fires today).
9. **OQ/EQ computation** (`:779-875`) — only when `ld_cfg.enabled and trade_dir in (LONG,SHORT)`. Fail-open `except → OQ=EQ=0.0` (`:871-875`).
10. **Status decision** (`:877-900`):
    ```
    if layered_decision.enabled:                       # default True
        if dir not in (LONG,SHORT):        WAITING
        elif OQ>=5.0 and EQ>=5.0:          READY        # <-- the gate
        elif OQ>=5.0 or EQ>=5.0:           WATCHLIST
        else:                              WAITING
    else:  # legacy (off)
        if score>=profile.min_entry_score: READY        # score only gates here
    ```
11. Returns `PairScanResult` (`:902-936`) carrying both `score` and `opportunity_quality`/`entry_quality`, plus `consensus_*`.

**`scan_all`** (942-1018): loops pairs, sorts. With layered on, sort key = `consensus_agreement + opportunity_quality + entry_quality` (`:982-987`) — **`score` is not in the sort key here**. `best_setup = ready[0]`.

**`PairRanker`** (`scanner/pair_ranker.py`):
- `rank()` (29): `priority = score + regime_bonus + session_bonus + sweep(5) + volume(3) + inducement(3) + wyckoff(5) − already_open(30) − corr_conflict(15)` (`:41-83`).
- `rank_opportunities()` (95): `opportunity_score = score × pair_mult × (1 + ev_estimate)` (`:106`) — **this is the ranker used in the live entry path** via `pair_mult_map` (`main_loop.py:977-984`).

---

## LAYER 4 — Decision (`decision/`)

**`SituationEngine`** (`decision/situation.py`) — turns a context into continuous dimensions:
- `assess_entry(EntryContext)` (109): computes `tf_alignment` (D1 .40 / H4 .35 / H1 .25, `:71-73,117-122`), `momentum` (M1 candles + events, `:133-146`), `structure_integrity` (zone-type + HTF/D1 events, `:148-179`), `urgency`, `read_confidence`, `primary_label`.
- `assess_open_trade(TradeContext)` (75): same dimensions + `profit_state` (R), `maturity`, and **score-trajectory** momentum term (`:299-304`).

**`DecisionEngine`** (`decision/engine.py`):
- `decide_entry(ctx,sa)` (553): ENTER vs SKIP from `sa` dims with regime-aware weights (`DecisionWeights`: structure .45 / momentum .30 / htf .20, `:43-47`). Outputs `EntryDecision(action,conviction,size_multiplier,...)`. `size_multiplier = 0.5+conviction` (0.5–1.5), reversal haircut ×0.5, HTF-aligned bonus +15% (`:663-679,731-734`). Reads `ctx.scan_score` only for MARKET-vs-PENDING nudge (`:711`) and R:R (`:582`).
- `decide_management(ctx,sa)` (159): weighted scoring over HOLD/CLOSE/TIGHTEN_SL/MOVE_TO_BREAKEVEN; mgmt HTF-close coeff demoted to **0.15** (`:199`); opposing-scan boost only if `ctx.scan_score>=65` (`:235`).
- **Thesis-secure** (`_maybe_thesis_secure`, 382): only overrides a would-be HOLD; banks profit when econ-profit exists AND deterioration ≥ 0.35; severe ≥ 0.80 → CLOSE at market (`:416-426`). Healthy-pullback guard at `:399-404`.

**`RiskGovernor`** (`decision/governor.py`):
- `review_entry` (104): physical vetoes only — max trades, heat ≥1.8%, spread >3×, R:R<1.0 (`:117-133`).
- `review` (27): veto SCALE_IN at heat>1.5% (`:36`); override HOLD→CLOSE if catastrophic (`:51-78`); never worsen SL (`:80-100`).

**Context dataclasses** (`decision/context.py`): `EntryContext` (106) and `TradeContext` (12) are **rich and uncompressed** — but several fields end up at defaults (see §6). Notably `EntryContext` has NO `oq_score`/`eq_score` fields at all.

---

## LAYER 5 — Planning (`planning/`)

**`TradePlanner.plan_trade(ctx)`** (`planning/trade_planner.py:141`) — the intended integration point. Produces a `TradePlan` (entry mode, SL strategy, TP strategy, `risk_pct`, BE/trail, scale-in).
- Gate order: low-conviction SKIP (`confidence<0.40 AND agreement<0.50`, `:153`) → WAIT (session/spread, `:162`) → **PortfolioGovernor.check inside planner** (`:174-207`) → ENTER.
- `_advisor_agreement` (249): blends scanner_score/100, `de_tf_alignment`, RL (dormant), pair_win_rate.
- `_size_plan` (412): `base_risk_pct` × correlation-reduction (if `correlated_exposure≥0.4`) × drawdown-reduction (if `current_drawdown_pct≥5.0`) × conviction-boost × pair_multiplier, clamped `[0.1,2.0]%`.
- `_sl_plan` (334) / `_tp_plan` (365): structure-vs-ATR SL; regime-learned `tp_mult`/`sl_buffer`/`runner` (neutral until learner confident).

**`TradePlanContext`** (`planning/models.py:25`) — designed to carry **all** advisor outputs (correlation, drawdown, zone_quality, regime shaping, open_position_book). Whether each field is actually populated is the crux — see §3/§6.

---

## LAYER 6 — Risk (`risk/`)

**`RiskEngine.assess(...)`** (`risk/risk_engine.py:115`) — **the final authority on capital + lot size.** Ordered checks (each can hard-reject):
1. Balance valid (`:132`).
2. DrawdownGuard mode; `FROZEN` → reject (`:154`); sets `risk_pct_decimal` from mode (`:152`).
3. Daily P&L limit → FROZEN (`:165`).
4. Weekly limit → RECOVERY (`:178`).
5. `max_open_trades` (`:189`).
6. **CorrelationEngine.can_open_trade** (`:202`) — second correlation check.
7. Duplicate pair+direction (`:214`).
8. **EV gate** (`:226-249`) — rejects if `ev<ev_threshold AND confidence in (high,medium)`.
9. **Score→risk scaling** (`:251-259` → `_scale_risk_by_score` 494): `score≥92→1.0× | ≥88→0.85× | ≥85→0.7× | else 0.5×`; `is_at_peak & score≥90 → ×1.1`; `drawdown_from_peak>0.10 → ×0.85`; hard cap `0.025`.
10. Instrument registry / pip economics (`:265`).
11. `PositionSizer.calculate(_stake)` → `position_size_lots` (`:280-305`).
12. Daily-budget size reduction (`:315-345`).
13. Spread monitor (`:352`).
Returns `RiskAssessment(approved, risk_pct, position_size_lots, max_loss_dollars, ...)`.

**`PositionSizer`** (`risk/position_sizer.py`): `lots = (balance×risk_pct)/(risk_pips×pip_value)` clamped `[0.01,100]` (`:110-111`); micro-account skip if min-lot risk > `max_risk_pct_per_trade` (`:200-219`). Deriv: `stake = risk_amount` (`:146-173`).

**`PortfolioRiskStateMachine`** (`risk/portfolio_risk_state.py:451`): ladder `NORMAL→DEFENSIVE(heat≥1.5%)→REDUCING(heat≥2.5% or persisted)→EMERGENCY(heat≥4.0% or triggers)` with hysteresis (recovery heat<1.0%) (`:476-504, 521-685`). Batch-level entry freeze in DEFENSIVE/REDUCING (`main_loop.py:947`).

**`AccountRiskManager`** (`risk/account_risk.py`): per-account silos — daily-loss halt at `3.0%` (recovery `1.5%`), heat block at `2.0%`, flatten at `5.0%` (`:24-34`); combines realized+unrealized (`:111-118`).

**`DrawdownGuard`** (`brain/drawdown_guard.py`): modes NORMAL/CAUTION/RECOVERY/FROZEN; risk_map scales **down only**; rolling-window drawdown-from-peak (default 30d, `:18,153-189`); lifetime preserved for logging (`:191-208`).

---

## LAYER 7 — Adaptive / Learning (`adaptive/`)

| Learner | Persists | Live influence (where) | Min sample | Class |
|---|---|---|---|---|
| `AdaptiveOptimizer` (`optimizer.py:49`, alias `self.ml`, `main_loop.py:236`) | — (delegates) | `get_trade_adjustments`→`position_size_multiplier`/`should_trade`/`reason` (`main_loop.py:1764-1781`); `is_losing_pattern`→veto (`:1743`) | retrain @50 trades/7d | active+useful |
| `ScoreOptimizer` (`score_optimizer.py:131`) | `data/scoring_weights.json` | scanner weights hot-reload (`main_loop.py:4657-4668`); ±25% envelope; OOS gate | 50 trades | active+useful |
| `RegimeLearner` (`regime_learner.py:29`) | `data/ml_regime_strategies.json` | planner `regime_tp_mult/sl_buffer/runner` (`models.py:76-78`); `optimal_tp_multiplier` direct (`main_loop.py:3398`) | 30 trades | active+useful |
| `PairLearner` (`pair_learner.py:29`) | `data/ml_pair_profiles.json` | `get_pair_multiplier` → size + EV gate (`optimizer.py:181`; `main_loop.py:1727`); default 0.8 (thin history) | 20 trades | active+pessimistic |
| `SessionLearner` (`session_learner.py:26`) | `data/ml_session_profiles.json` | `get_session_aggression` → size/AVOID (`optimizer.py:182`) | 15 trades | active+useful |
| `EVEstimator` (`ev_estimator.py:33`) | stateless | scanner EV (`pair_scanner.py:688`) + RiskEngine EV veto (`risk_engine.py:228`) | 10 trades | active+conservative |
| `GateTuner` (`gate_tuner.py:26`) | `data/gate_tuning.json` | tunes **only** `ev_gate` + `entry_engine` (`gate_tuner.py:33-42`); used `main_loop.py:1719`, `entry_engine.py:233` | 30 shadows | active+narrow |
| `TradeAnalyzer` (`trade_analyzer.py:34`) | — | only losing-pattern path live; `analyze_*`/`score_edge` logging | 20/10 | mixed |

**Dead `TradeAdjustments` outputs** (`optimizer.py:26-34`): `score_threshold_adjustment`, `tp_multiplier`, `sl_buffer_adjustment` are computed but **never read** by any consumer; `confidence` read only in tests. Only `should_trade`, `reason`, `position_size_multiplier` reach live trading.

---

## LAYER 8 — Execution (`platforms/main_loop.py`)

**Entry pipeline** — `_scan_and_enter` (857) batch gates, then `_execute_entry` → `_execute_entry_inner` (1302). Full ordered gauntlet is in §5 (Authority Map). Final sizing (§ Layer-D below) then `platforms.execute_entry` (`:1976`).

**RISK_PCT FLOW (critical):**
- `plan.risk_pct` is **NOT** passed to `RiskEngine.assess` (no such parameter; `assess` args at `:1689-1705`). Instead it becomes a **bounded conviction multiplier**: `conviction_mult = max(0.3, min(2.0, plan.risk_pct/base_pct))` where `base_pct = _exec_risk*100` (`:1527-1528`). This **overwrites** the DecisionEngine `size_multiplier` (set at `:1481`) whenever the planner is enabled and ENTERs.
- Final lot formula (`:1780-1792`):
  ```
  adjusted_lots = signal.position_size_lots          # base from EntryEngine, sized off _exec_risk
                × adjustments.position_size_multiplier  # adaptive (L1781)
                × density_mult × vol_mult × exec_mult    # opportunity density / sys-vol / exec quality
                × conviction_mult                        # planner risk_pct ratio (or DE size if planner off)
  risk_ceiling  = min(signal.position_size_lots, assessment.position_size_lots)   # RiskEngine caps
  adjusted_lots = min(adjusted_lots, risk_ceiling); max(0.01, ...)
  ```
  So **RiskEngine only sets a ceiling**; the base lots come from the EntryEngine signal, and everything in between only scales between ~0.3×–2.0× before the clamp.

---

## LAYER 9 — Management (`management/` + `decision/engine.py` mgmt path)

Two layers per cycle on open positions (`_analyse_open_trades` 2981 → `_run_decision_engine` 3142):
1. **Strategic** — `SituationEngine.assess_open_trade` (`:3163`) → `DecisionEngine.decide_management` (`:3164`) → `RiskGovernor.review` (`:3167`) → `_execute_management_decision` (`:3181`). Wrapped in one try/except that on error only logs *"falling back to legacy"* (`:3182-3186`) — strategic layer silently no-ops.
2. **Mechanical** — `TradeManager.update` per tick (`management/trade_manager.py`; called `main_loop.py:2542`): TP1/TP2/TP3 ladder, breakeven, structure trailing, partial close. Terminal discretionary closes are **deferred** if a fresh DE HOLD-ish verdict exists or `pnl_r>0.3` (`:2565-2593`).
- `TradeContext` built by `_build_trade_context` (3457): live price, `pnl_dollars` prefers broker truth (`:3476`), structure/momentum re-derived fresh (`:3522-3570`), `score_history` from `_position_scores`.

---

## LAYER 10 — Feedback (journal / event store / retrain)

| Step | What | Cite |
|---|---|---|
| Close → store | `_record_closed_trade` journals `TradeRecord`, `ml.register_new_trade()`, appends to `scanner._trade_history` (cap 500), `_outcome_logger.log_outcome` | `main_loop.py:4159-4218` |
| Retrain trigger | `if self.ml.should_retrain(): self._run_ml_optimization()` — every 50 trades or 7 days | `main_loop.py:4515-4516`; `optimizer.py:56-57,260-271` |
| Retrain run | `run_optimization(raw_trades)` over 90-day/≥50-trade recency window; feeds `scanner._trade_history` | `main_loop.py:4631-4652`; `optimizer.py:96-175` |
| Weights hot-reload | `scanner._adaptive_weights = load_saved_weights().clamped_to_envelope(...)` — reaches scanner immediately | `main_loop.py:4657-4668` |
| Counterfactual | rejected setups → `_persist_shadow_contract` (`ShadowContract`) resolved by paper `TradeManager`; `GateTuner.calibrate` tunes ev_gate/entry_engine | `main_loop.py:4308-4385, 4522, 4562` |

**Latency:** nothing adapts until ≥50 closed trades, then every 50/7d, ±25% clamp + OOS gate. No per-trade learning. RL (the only true policy learner) is inert.

---

# 2. COMPLETE DATA-FLOW DIAGRAM

Arrows show the **exact variables passed**. `✂` marks where rich data is dropped/reduced.

```
┌─ LAYER 1: MARKET DATA ──────────────────────────────────────────────────────┐
│ platforms.fetch_all_market_data()  → market_data{pair:{H4,H1,M15,M5,D1}}      │
│ currency_data{pair:H1}                                       (main_loop:861)  │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ DataFrames (per pair, per TF)
                ▼
┌─ LAYER 2/3: SCANNER (scan_pair) ────────────────────────────────────────────┐
│ brain modules → (a) Votes → consensus.decide() → trade_dir                    │
│                (b) points  → score (0..123)                                   │
│ setup_quality → OQ(0..10), EQ(0..10)                                          │
│ EV → ev_estimate ;  RL → rl_* (DORMANT)                                       │
│ STATUS: OQ>=5 & EQ>=5 → READY        (pair_scanner:881)                       │
│ ✂ score NOT used for READY ; OQ/EQ NOT carried past the scan for re-check     │
│ OUT → PairScanResult{pair,direction,score,regime,OQ,EQ,consensus_*,ev,confl.} │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ PairScanResult  (sort: agreement+OQ+EQ ; ranker: score×pair_mult×(1+ev))
                ▼
┌─ LAYER 8: _scan_and_enter BATCH GATES ──────────────────────────────────────┐
│ PRSM freeze(947) · heat block(960) · rank top3(977) · per-pair loop          │
│ CorrelationEngine.can_open_trade(1012) · margin(1017) · max_trades(1023)      │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ result (READY)  ───►  _execute_entry_inner (RE-FETCHES market data 1323)
                ▼
┌─ LAYER 8/4: PER-CANDIDATE ENTRY ────────────────────────────────────────────┐
│ EntryEngine.calculate_entry → signal{entry,sl,tp1,tp2,position_size_lots,...} │
│ _build_entry_context → EntryContext  (structure RE-DERIVED fresh @3262)       │
│   ✂ NO oq/eq fields exist on EntryContext ; score carried STALE              │
│ SituationEngine.assess_entry(EntryContext) → sa{tf_align,momentum,struct,...} │
│ DecisionEngine.decide_entry → EntryDecision{conviction,size_multiplier}       │
│   ✂ sa rich dims → collapsed to ONE number conviction                        │
│ RiskGovernor.review_entry (physical vetoes)                                   │
│ _build_plan_context → TradePlanContext  (zone_quality=sa.struct ; corr/dd …)  │
│ TradePlanner.plan_trade → TradePlan{risk_pct,sl,tp,...}                       │
│   ✂ plan.risk_pct → conviction_mult (0.3..2.0), OVERWRITES DE size (1528)     │
│   PortfolioGovernor.check INSIDE planner (174)                               │
│ EntryValidator · spread gate · regime threshold(0.7×/veto)                    │
│ RiskEngine.assess(score, …) → assessment{approved, position_size_lots}        │
│   ✂ uses STALE score for sizing ; OQ/EQ never seen                           │
│ EV gate · losing-pattern gate · ML get_trade_adjustments(should_trade,size)   │
│ FINAL: adjusted_lots = signal.lots × adj.size × density × vol × exec × conv   │
│        clamp to min(signal.lots, assessment.lots)                             │
│ sidedness · circuit breaker · idempotency → platforms.execute_entry(1976)     │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ trade opened
                ▼
┌─ LAYER 9: MANAGEMENT (per cycle / per tick) ────────────────────────────────┐
│ scan_pair re-score → adjusted_score ; _build_trade_context (fresh structure)  │
│ SituationEngine.assess_open_trade → DecisionEngine.decide_management          │
│  → RiskGovernor.review → _execute_management_decision                         │
│ TradeManager.update (TP/SL/BE/trail/partial) ; thesis-secure defers closes    │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ trade closed
                ▼
┌─ LAYER 10: FEEDBACK ────────────────────────────────────────────────────────┐
│ journal + register_new_trade → should_retrain(50/7d) → run_optimization       │
│ → scoring_weights.json → scanner._adaptive_weights (hot reload 4668)          │
│ rejected setups → shadow contracts → GateTuner (ev_gate, entry_engine only)   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

# 3. HANDOFF TABLE

| From | To | Data passed | Available but NOT passed | Impact of gap |
|---|---|---|---|---|
| Brain modules | Scanner score | graded points per module | — | (full) |
| Brain modules | Consensus | `(direction,confidence)` per module | the magnitude/strength behind each vote beyond confidence | direction decided on confidence only |
| Scanner | `scan_all` sort | `agreement+OQ+EQ` | `score` | best-setup ordering ignores confluence score |
| Scanner | `PairRanker` | `score, pair_mult, ev` | `OQ, EQ, consensus_agreement` | priority uses score; OQ/EQ that *gated* it are dropped from ranking math |
| Scanner result | `_execute_entry_inner` | `result.{pair,direction,score,regime,ev_estimate,confluences}` | `opportunity_quality, entry_quality`, `consensus_*` | **OQ/EQ never reach entry/decision/planner/risk** — qualification logic is forgotten after the gate |
| `_build_entry_context` | `EntryContext` | symbol, direction, scan_score, entry/sl/tp, RR, spreads, regime, ev, **fresh D1/H4/H1/M1 structure** | `oq_score`, `eq_score` (no fields exist), scanner's confluence list is passed but unused by DE | DE re-derives structure from scratch; OQ/EQ context lost |
| `SituationEngine` | `DecisionEngine` | `SituationAssessment` (all dims) | — (full object passed) | (full) |
| `DecisionEngine` | entry pipeline | `EntryDecision.{conviction,size_multiplier,action}` | the underlying `sa` dims (structure/momentum/alignment) that produced conviction | downstream sees one number, not the reasoning |
| `_build_plan_context` | `TradePlanContext` | scanner_score, `de_tf_alignment=sa.tf_alignment`, `de_confidence`, `zone_quality=sa.structure_integrity`, `correlated_exposure`(computed), `current_drawdown_pct`(computed), `pair_multiplier`, regime shaping, `open_position_book`, `base_risk_pct=exec_risk×100` | `oq_score`/`eq_score` (no fields), raw `sa.momentum`/`sa.structure_integrity` as separate signals (only `de_*` proxies), live portfolio **heat** (`PRSM` heat not passed; only correlation count) | planner can shape SL/TP/size but is blind to OQ/EQ and to portfolio heat; `correlated_exposure`/`drawdown` are real **only when** book non-empty / no exception, else `0.0` |
| `TradePlanner` | entry pipeline | `plan.risk_pct` → `conviction_mult` (0.3–2.0); `plan.sl_*`,`tp_*` set the **signal** levels | the rest of the rich `TradePlan` (trail/scale-in survive; sizing reasoning dropped) | planner's risk_pct becomes a bounded ratio, not a direct risk% |
| entry pipeline | `RiskEngine.assess` | `pair, direction, entry, sl, open_trades, balance, context, spread, score, regime, session, trade_history` | `plan.risk_pct`, `EntryDecision.conviction`, `OQ/EQ`, `sa` dims | RiskEngine recomputes its own risk% from drawdown+score; planner/DE sizing intent only survives as the external `conviction_mult` multiply |
| `RiskEngine` | final sizing | `assessment.position_size_lots` (ceiling), `risk_pct` | — | acts as ceiling only |
| Scanner re-score (mgmt) | `DecisionEngine.decide_management` | `adjusted_score`(via `scan_direction`/`scan_score`), fresh structure, `score_history` | `OQ/EQ` (not recomputed for mgmt) | management never uses OQ/EQ |
| Feedback | Scanner | `scoring_weights.json` → `_adaptive_weights` | per-trade outcome detail beyond aggregates | slow, aggregate-only adaptation |

---

# 4. STALENESS MAP

“Computed at” = scan cycle time **T**; entry happens at **T+N** (separate fetch). 

| Data | Computed at | Used at | Time gap | Refreshed before use? |
|---|---|---|---|---|
| Confluence `score` | T (scan) | T+N RiskEngine sizing (`risk_engine.py:251`), regime threshold (`main_loop.py:1636`), ranker, mgmt opposing-boost | scan→entry latency | ❌ **No** — sizing uses stale score |
| `OQ` / `EQ` | T (scan, `pair_scanner.py:819-869`) | T (READY gate only) | — | ❌ Never re-checked; never used after gate |
| `consensus_*` (direction/agreement) | T | T+N planner `_advisor_agreement` indirectly; ranking | scan→entry | ❌ stale (direction itself re-derived only via DE structure, not consensus) |
| Structure (D1/H4/H1) for DE | — | T+N (`_build_entry_context` re-runs `structure.analyze`, `main_loop.py:3262-3277`) | ~0 | ✅ **Fresh** |
| Momentum (M1) for DE | — | T+N (`_build_entry_context:3280-3290`) | ~0 | ✅ Fresh |
| Spread | T+N fetch (`:1432`) | T+N gates + OQ-equivalent never | ~0 | ✅ fresh at entry, but OQ's spread component (from T) is stale and unused |
| Correlation/exposure | T+N | T+N (`_scan_and_enter:1012`, `risk_engine.py:202`) | ~0 | ✅ Fresh (checked twice) |
| Drawdown mode / dd-from-peak | T+N | T+N (`risk_engine.py:150-152, 510`) | ~0 | ✅ Fresh |
| Portfolio heat | T+N | T+N batch (`:960`) + per-account (`:1377`) | ~0 | ✅ Fresh (but NOT passed to planner) |
| EV estimate | T (scan) and T+N (RiskEngine recomputes) | both | — | ⚠️ RiskEngine recomputes; scanner's is stale but feeds OQ at T |
| Management re-score | each mgmt cycle | same cycle | ~0 | ✅ Fresh (`scan_pair` re-run `:3051`) |

**Conclusion:** The dangerous staleness is concentrated in **`score` (sizing input)** and **`OQ/EQ` (never re-validated)**. Everything the Decision/Risk layers *re-derive themselves* is fresh — but they re-derive a *different* subset (structure/momentum/correlation/drawdown), so the scanner's environment-quality intelligence (spread cost, R:R magnitude, volatility regime, entry geometry) is never reconciled at execution time.

---

# 5. AUTHORITY MAP

Ordered hard gauntlet a trade must survive (live entry). “Fail behavior” = what happens if the check’s code throws.

| # | Component (cite) | Veto? | Size? | Set SL/TP? | Override upstream? | Fail behavior |
|---|---|---|---|---|---|---|
| B0 | DrawdownGuard.can_trade (`:685`) | ✅ (pauses loop) | — | — | — | fail-closed (pause) |
| B1 | PRSM freeze DEFENSIVE/REDUCING (`:947`) | ✅ batch | — | — | — | n/a |
| B2 | Portfolio-heat block ≥1.8% (`:960`) | ✅ batch | — | — | — | n/a |
| B3 | Ranking (`:977`) | — | — (orders) | — | — | — |
| B4 | dedupe / BE cooldown (`:989,995`) | ✅ | — | — | — | skip |
| B5 | CorrelationEngine #1 (`:1012`) | ✅ | — | — | — | continue(skip pair) |
| B6 | margin / max_trades (`:1017,1023`) | ✅ | — | — | — | skip / break |
| 9-14 | registry/data/balance/acct-halt/heat (`:1310-1383`) | ✅ | — | — | — | **fail-closed** |
| 15 | EntryEngine.calculate_entry (`:1396`) | ✅ | base lots | ✅ SL/TP/entry | — | fail-closed (rejection→shadow) |
| 17 | SituationEngine.assess_entry (`:1456`) | — | — | — | — | (in DE try) fail-closed |
| 18 | DecisionEngine.decide_entry (`:1457`) | ✅ SKIP | ✅ `size_multiplier` | — | — | **fail-closed** (skip entry, `:1587`) |
| 19 | RiskGovernor.review_entry (`:1460`) | ✅ | (zeros size on veto) | — | ✅ overrides DE | fail-closed |
| 20 | TradePlanner.plan_trade (`:1508`) | ✅ SKIP/WAIT | ✅ via risk_pct→conv_mult | ✅ SL/TP strategy | ✅ overwrites DE size (`:1528`) | planner err → keep DE sizing (fail-open-ish) |
| 21 | PortfolioGovernor.check (inside planner `:174`; fallback `:1541`) | ✅ | — | — | — | fail-closed by default (`config.fail_closed=True`) |
| 22 | EntryValidator (`:1606`) | ✅ | — | — | — | fail-closed |
| 23 | spread gate >3× (`:1621`) | ✅ | — | — | — | fail-closed |
| 24 | regime threshold (`:1636`) | ✅ (veto mode) | ✅ ×0.7 | — | — | **fail-open** (skip gate, `:1679`) |
| 25 | weekend buffer (`:1682`) | ✅ | — | — | — | fail-closed |
| 26 | **RiskEngine.assess** (`:1689`) | ✅ | ✅ **binding ceiling** + risk% | — | — | fail-closed (reject) |
| 27 | EV gate (`:1715`) | ✅ (rare) | — | — | — | **fail-open** (`pair_mult=1.0`, gate can't fire `:1727`) |
| 28 | losing-pattern gate (`:1743`) | ✅ | — | — | — | **fail-open** (skip, `:1760`) |
| 29 | ML get_trade_adjustments / should_trade (`:1764`) | ✅ | ✅ `position_size_multiplier` | — | — | **fail-open** (`lots=signal.lots`, `:1801`) |
| 30 | sizing ceiling + sub-min-lot (`:1784-1799`) | ✅ (reject) | ✅ clamp | — | — | reject below min lot |
| 31-33 | sidedness / breaker / idempotency (`:1820-1869`) | ✅ | — | — | — | fail-closed |
| 34 | execute_entry (`:1976`) | — | — | — | — | breaker.record_failure |

**Final authority on capital deployment = `RiskEngine.assess()`** (sole sizing ceiling + final veto). Everything after only shrinks or rejects.

**Management authority order:** `RiskGovernor.review` (catastrophic override / SL protection) ≥ `DecisionEngine.decide_management` (HOLD/CLOSE/...) ≥ `TradeManager` mechanical; `thesis-secure` overrides only would-be HOLD; EMERGENCY/REDUCING risk-directed exits outrank adaptive (`portfolio_risk_state.py:757-766`).

---

# 6. INFORMATION-LOSS TABLE

Every place a rich signal is reduced to a scalar or context is dropped.

| Where (cite) | What is lost | Why it matters |
|---|---|---|
| `pair_scanner.py:982-987` (sort) & ranker `:106` | confluence `score` dropped from `scan_all` sort; OQ/EQ dropped from ranker | qualification and prioritization use different, non-overlapping signals |
| `pair_scanner.py:902-936` → entry | `opportunity_quality`, `entry_quality` are on `PairScanResult` but **no consumer reads them after the gate** | all the OQ/EQ intelligence (spread cost, R:R magnitude, volatility regime, entry geometry, stop quality) evaporates at execution |
| `EntryContext` (`context.py:106`) | has **no `oq_score`/`eq_score` fields**; `confluences` list passed but unused by DE | DE/planner cannot co-gate or shape on quality |
| `decision/engine.py:663-695` | full `SituationAssessment` (tf_alignment, momentum, structure_integrity, urgency, read_confidence) → single `conviction`/`size_multiplier` | planner & risk see one number; the *reasons* (which dimension is strong/weak) are gone |
| `_build_plan_context` (`main_loop.py:3404-3455`) | `de_*` are proxies of `sa`; raw `sa.momentum` not passed as its own field; **portfolio heat not passed** | planner can’t separately weight momentum vs structure, and can’t scale size for live heat |
| `main_loop.py:1527-1528` | `plan.risk_pct` → bounded ratio `conviction_mult` (0.3–2.0); also **overwrites** DE `size_multiplier` | planner’s carefully computed risk% is reduced to a multiply and can erase DE’s sizing |
| `RiskEngine.assess` args (`:1689-1705`) | `plan.risk_pct`, `EntryDecision.conviction`, OQ/EQ, `sa` dims not passed | RiskEngine recomputes risk% from drawdown+**stale score** only |
| `risk_engine.py:251-259` | uses **stale** `score` for the sizing dial | size can reflect confluence that no longer holds at T+N |
| `optimizer.py:215-218` | `score_threshold_adjustment`, `tp_multiplier`, `sl_buffer_adjustment` computed, never read | learner effort wasted; intended adaptation never applied |
| `pair_scanner.py:291,324,...` | consensus `.get(k, 3.0/2.0/1.5)` fallbacks dead (config always supplies `1.0`) | apparent module weighting is illusory; all votes weight 1.0 |
| RL path (`pair_scanner.py:698-777`, `rl/bridge.py`) | `rl_action/confidence/expected_r` always neutral (no checkpoint) | the only true MTF-fusion + policy signal contributes nothing |

---

# 7. PROPOSED INTEGRATION POINTS

Concrete, implementable changes. Ordered by ROI. Each references the exact site.

### 7.1 Re-validate OQ/EQ at entry time (close the stale-quality gap)
**Where:** `_execute_entry_inner` after the fresh fetch (around `main_loop.py:1432-1456`, where M5/spread are already fresh).
**Change:** recompute `compute_opportunity_quality(...)`/`compute_entry_quality(...)` (`brain/setup_quality.py:73,261`) using the entry-time M5/spread/ATR already in scope, and reject (or down-size) if either dropped below `opportunity_quality_min`/`entry_quality_min` (`config.py:343-344`). Reuses inputs already gathered; one function call each. This makes the gate that *qualified* the trade also *confirm* it at execution.

### 7.2 Pass OQ/EQ through the whole chain
**Where:** add `oq_score: float` / `eq_score: float` to `EntryContext` (`decision/context.py:106`) and to `TradePlanContext` (`planning/models.py:25`); populate in `_build_entry_context` (`main_loop.py:3201`) and `_build_plan_context` (`main_loop.py:3404`) from `result.opportunity_quality`/`result.entry_quality`.
**Use them:**
- `DecisionEngine.decide_entry` (`engine.py:553`): add a SKIP penalty when `oq_score < threshold` and an ENTER contribution when high — co-gate, not just structure.
- `TradePlanner._sl_plan` (`trade_planner.py:334`): widen SL when `eq_score` low (entry less precise); `_tp_plan` (`:365`): trim TP ambition when `oq_score` marginal.

### 7.3 Make the score co-gate READY (optional, design decision)
**Where:** `pair_scanner.py:881`.
**Change:**
```python
elif oq_score >= ld_cfg.opportunity_quality_min \
     and eq_score >= ld_cfg.entry_quality_min \
     and score   >= profile.min_entry_score:     # add confluence co-requirement
    status = "READY"
```
This restores the 123-point intelligence as a gate (pattern conviction) alongside OQ (conditions) and EQ (geometry) — three independent axes, all required.

### 7.4 Refresh the sizing score (or pass conviction instead of stale score)
**Where:** `RiskEngine.assess` call (`main_loop.py:1689-1705`) and `_scale_risk_by_score` (`risk_engine.py:494`).
**Options:**
- (a) Recompute the score at entry from the fresh fetch and pass that, OR
- (b) Replace the stale-score sizing dial with the **fresh** `EntryDecision.conviction` (already computed at `:1457` on fresh structure). Pass `conviction` into `assess` and scale risk by it. This removes the staleness entirely and unifies sizing on one fresh signal.

### 7.5 Give the Planner full portfolio context (finish the integration point it was built for)
**Where:** `_build_plan_context` (`main_loop.py:3404-3455`).
**Change:** populate the already-existing-but-often-default fields with live values and add heat:
- `correlated_exposure` ← `CorrelationEngine.calculate_exposure(...)` even when computing pre-open (today it’s `0.0` when book empty, `:3333`).
- `current_drawdown_pct` ← guard status (already wired but `0.0` on error, `:3347`) — make the except fail to a *conservative* value, not `0.0`.
- add `portfolio_heat_pct` (from PRSM `_last_heat_pct`, already read for `EntryContext` at `:3237`) and have `_size_plan` (`trade_planner.py:412`) scale down near `portfolio_heat_block_pct` (1.8%).

### 7.6 Flip the fail-open gates to fail-safe
**Where:** `main_loop.py` EV-gate pair_mult (`:1727`), losing-pattern (`:1760`), ML adjustments (`:1801`), regime threshold (`:1679`); scanner EV (`pair_scanner.py:695`), OQ/EQ (`:871`).
**Change:** on exception, **skip the trade** (or apply the most conservative multiplier), not “allow / 1.0”. A swallowed exception must never silently upgrade a gate to “yes”. Mirror the `PortfolioGovernor.fail_closed` pattern (`governor/portfolio_governor.py:85-107`).

### 7.7 Consolidate sizing authority (single source of truth)
**Where:** final sizing block (`main_loop.py:1780-1792`).
**Change:** collapse `adjustments.size × density × vol × exec × conviction_mult` into one explicit, logged `final_multiplier` computed in a single helper, with each contributing factor recorded, then a single `min(signal.lots, assessment.lots)` clamp. Today a regression in any one multiplier compounds silently; one helper makes the product auditable and bounded.

### 7.8 Pass `sa` dimensions, not just conviction, to the Planner
**Where:** `_build_plan_context` (`:3404`) already sets `de_structure_score=sa.structure_integrity`, `de_momentum_score=sa.momentum` — **but the planner never reads them** (`trade_planner.py` only reads `de_tf_alignment`/`de_confidence`). Wire `_size_plan`/`_tp_plan` to use `de_momentum_score`/`de_structure_score` so the planner reasons over the dimensions, not a single blended number.

### 7.9 Extend the counterfactual tuner to the high-authority gates
**Where:** `GateTuner.TUNABLE` (`adaptive/gate_tuner.py:33-42`) currently only `ev_gate` + `entry_engine`.
**Change:** add shadow-resolved tuning for the *real* bottlenecks — `opportunity_quality_min`/`entry_quality_min` (`config.py:343-344`), consensus `min_agreement` (`config.py:276`), and `max_correlated_trades` (correlation cap). Requires tagging each shadow with its rejecting gate (already partially present via `_persist_shadow_contract`, `main_loop.py:4308`). This lets the system learn whether its tightest gates help or hurt.

### 7.10 Make RL status unambiguous
**Where:** `RLBridge` load (`scanner/pair_scanner.py:208-214`) + dashboard.
**Change:** either ship a checkpoint or surface `rl.enabled=False` prominently so operators don’t mistake the dormant path for active. Until then, `rl_*` fields should be excluded from `_advisor_agreement` (`trade_planner.py:269-277`) to avoid a neutral vote diluting real signals.

---

## APPENDIX — Synchronization summary (the core problem in one view)

```
                SCANNER (T)            DECISION/PLANNER (T+N)        RISK (T+N)
                -----------            ----------------------        ----------
direction       consensus.decide       re-derived (DE structure)     —
structure       +20 pts (T)            FRESH re-analyze (3262)        —  (uses stale score)
momentum        penalties (T)          FRESH re-analyze (3280)        —
OQ/EQ           gate (881)             ✂ never read                   ✂ never read
score           computed (T)           minor nudges                   sizing dial (STALE)
correlation     —                      planner corr (0 if empty)      FRESH (×2)
drawdown/heat   —                      planner dd (0 on err)          FRESH
```

Three independent reads of the same market, at different instants, that do not reconcile. The integration target is a single coherent context object — carrying **fresh** structure/momentum **and** re-validated OQ/EQ **and** live portfolio state — consumed by Decision, Planner, and Risk alike, with one auditable sizing chain and fail-safe (not fail-open) gates.

*End of map. All citations reference the repository at the analysed commit; no files were modified.*
