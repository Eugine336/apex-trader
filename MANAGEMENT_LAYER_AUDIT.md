# APEX TRADER — POST-ENTRY MANAGEMENT LAYER AUDIT

**Purpose:** A complete, end-to-end forensic map of everything that happens *after* a
trade is opened — every management component, every data handoff, every staleness
gap, every fail-open path, and every signal that is available but ignored. This is the
companion to `PIPELINE_INTEGRATION_MAP.md` (which covered the pre-entry pipeline).

**Method:** Static reconstruction of the live call chain from
`platforms/main_loop.py` (`_run_supervised_cycle` → `_update_positions` /
`_analyse_open_trades`) cross-checked against `management/`, `decision/`,
`platforms/trading_loop/*mixin.py`, and `risk/`. Every claim cites `file:line`.
**No trading logic was changed** — only this audit document was added.

> **One-line orientation:** The post-entry layer is **two parallel management brains
> that re-derive the market independently and do not share conclusions** — a *mechanical*
> `TradeManager` (TP/SL/BE/trail/stall on the tick) and a *strategic* `DecisionEngine`
> (HOLD/CLOSE/TIGHTEN/BE on the scan cycle) — wrapped by a thick stack of risk/heat/news/
> session exit checks. Just like the entry pipeline, **`OQ`/`EQ` are never read after the
> READY gate**, the strategic brain **fails open** (its `try/except` silently no-ops to the
> mechanical brain), and several "active management" features (`invalidation`,
> `conviction-collapse`, `htf-candle-close`, `dynamic-sl-tighten`) are **dead in the default
> config** because they live in the legacy `else` branch that only runs when the
> DecisionEngine is disabled. Re-entry is **logging-only** — it detects opportunities and
> places no order.

---

## TABLE OF CONTENTS
1. Component Map
2. Signal Flow Table
3. Management Tick Sequence
4. Handoff Table
5. Failure Mode Catalog
6. Gap Analysis
7. Mid-Pipeline Rejection Analysis (DE/Planner hard-reject vs shadow contracts)
8. Proposed Fix Plan (P0–P9)

---

# 1. COMPONENT MAP

Every component that touches an open trade, what it does, and where it lives.

| # | Component | File:line | Role | Authority |
|---|---|---|---|---|
| C1 | `TradeManager.update` (mechanical) | `management/trade_manager.py:301` | TP1 partial → BE → trail → TP3 → TP2 → structure-exit → stall, on every tick | ENFORCE (mechanical exits) |
| C2 | `StructureTrailingStop.calculate_trail` | `management/trailing_stop.py:34` | M5 swing-based trailing SL; only ever moves in profit direction | ADVISE (returns new SL) |
| C3 | `PartialCloseCalculator` | `management/partial_close.py:13` | Lot math for partials + BE level | COMPUTE |
| C4 | `ReEntryManager.check_re_entry` | `management/re_entry.py:46` | After a BE stop, detect whether the setup re-formed | **LOGGING-ONLY (no executor)** |
| C5 | `SituationEngine.assess_open_trade` | `decision/situation.py:75` | Re-derive `tf_alignment / momentum / structure_integrity / profit_state / urgency` from fresh frames | ADVISE |
| C6 | `DecisionEngine.decide_management` | `decision/engine.py:159` | Weighted scoring → HOLD / CLOSE / TIGHTEN_SL / MOVE_TO_BREAKEVEN | DECIDE (strategic) |
| C7 | `DecisionEngine._maybe_thesis_secure` | `decision/engine.py:382` | "Is the reason to hold still valid?" — banks profit / severe-collapse market close | DECIDE (overrides would-be HOLD only) |
| C8 | `RiskGovernor.review` | `decision/governor.py:27` | Catastrophic HOLD→CLOSE override; never-worsen-SL; veto SCALE_IN at heat | OVERRIDE (strategic) |
| C9 | `_analyse_open_trades` | `platforms/main_loop.py:3072` | Per-cycle re-scan of every open instrument; dispatches strategic engine or legacy checks | ORCHESTRATE |
| C10 | `_run_decision_engine` | `platforms/main_loop.py:3233` | build ctx → assess → decide → governor → execute | ORCHESTRATE |
| C11 | `_build_trade_context` | `platforms/main_loop.py:3600` | Assemble `TradeContext` (fresh structure, P&L, score history) | BUILD |
| C12 | `_execute_management_decision` | `platforms/main_loop.py:3732` | Map a `ManagementDecision` → broker close/modify | ENFORCE |
| C13 | `_update_positions` | `platforms/main_loop.py:2431` | Tick loop: broker reconcile → sync P&L/SL → `TradeManager.update` → exits/partials | ENFORCE |
| C14 | `_check_news_exit` | `trading_loop/exit_checks_mixin.py:292` | Per-trade news handling: winners→BE, flat/loss→close | VETO/PROTECT (unconditional) |
| C15 | `_check_session_close` | `exit_checks_mixin.py:514` | Index exchange-close exit; FX dead-zone→BE | VETO/PROTECT (unconditional) |
| C16 | `_check_spread_deterioration` | `exit_checks_mixin.py:611` | Spread ≥ N×typical → SL to BE | PROTECT (unconditional) |
| C17 | `_apply_absolute_profit_protection` | `exit_checks_mixin.py:393` | Absolute $/pip floor → SL to BE (covers adopted/orphan trades) | PROTECT (unconditional, in tick) |
| C18 | `_check_opportunity_cost_exit` | `exit_checks_mixin.py:677` | Close a flat position when a better candidate was blocked | VETO (runs regardless of DE) |
| C19 | `_check_invalidation` | `exit_checks_mixin.py:20` | Opposing scan / low score → close | **LEGACY-ONLY (dead when DE on)** |
| C20 | `_check_conviction_collapse` | `exit_checks_mixin.py:93` | N declining score cycles → close | **LEGACY-ONLY (dead when DE on)** |
| C21 | `_check_htf_candle_close` | `exit_checks_mixin.py:144` | Opposing H1 close → close | **LEGACY-ONLY (dead when DE on)** |
| C22 | `_apply_dynamic_sl_tightening` | `exit_checks_mixin.py:224` | Beyond-BE profit → tighten SL | **LEGACY-ONLY (dead when DE on)** |
| C23 | `_check_portfolio_heat` + state machine | `trading_loop/risk_heat_mixin.py:29` | Live heat → DEFENSIVE/REDUCING/EMERGENCY → BE/trim/close | ENFORCE (portfolio) |
| C24 | `_margin_guardian_check` / `_emergency_flatten_all` | `risk_heat_mixin.py:656,673` | Margin floor → flatten all + FREEZE | ENFORCE (survival) |
| C25 | `_flatten_account` | `risk_heat_mixin.py:712` | Per-account daily-loss flatten | ENFORCE (survival) |
| C26 | `_reconcile_positions` | `trading_loop/recovery_mixin.py:227` | Periodic broker↔managed reconciliation heartbeat | RECONCILE |
| C27 | `_advance_shadows` | `trading_loop/shadow_live_mixin.py` | Paper-manage rejected setups on the live feed via a shadow `TradeManager` | LEARN (isolated) |
| C28 | `_record_closed_trade` | `platforms/main_loop.py:4168` | Book P&L into drawdown/risk/account/governor/ml; journal; arm cooldowns | FEEDBACK |
| C29 | `_check_scale_in` / `_check_scale_in_on_scan` | `main_loop.py:4030,3928` | Add to a winning position on fresh scan | ADD-RISK |

---

# 2. SIGNAL FLOW TABLE

For each management component: what fresh data feeds it, whether it re-queries the
scanner/DE/risk, whether it sees the book, and whether it sees the original thesis.

| Component | Fresh market data? | Re-queries Scanner? | Re-queries DE? | Sees other positions / heat? | Sees original thesis (OB/FVG/score)? | Sees OQ/EQ? |
|---|---|---|---|---|---|---|
| C1 `TradeManager.update` | M5 only (and only when at-BE or stalled >30min, `main_loop.py:2614`) | No | No | No | Partially — `score`, `confluences` carried on `ManagedTrade`, but **only `score` is used** (and only by DE-mgmt, not the manager) | **No** |
| C2 trailing | M5 (`df_m5`) | No | No | No | No | No |
| C5 `assess_open_trade` | Fresh D1/H4/H1/M1 structure (re-analysed in `_build_trade_context`) | Indirect — uses `scan_result.score` trajectory | n/a (feeds DE) | heat carried on ctx (`main_loop.py:3724`) | `score_history` only | **No — TradeContext has no oq/eq fields** |
| C6 `decide_management` | via `sa` | Uses `scan_score`/`scan_direction` for opposing-scan boost (≥65) | self | `portfolio_heat_pct` (governor only) | `scan_score` (opposing boost), `score_history` (conviction-collapse term) | **No** |
| C8 governor | via `sa`/`ctx` | No | No | `portfolio_heat_pct` | No | No |
| C14 news exit | News calendar | No | No | iterates all positions | No | No |
| C15 session close | Clock | No | No | iterates all positions | No | No |
| C16 spread deterioration | Live tick spread | No | No | iterates all positions | No | No |
| C17 absolute profit protect | Live tick price + broker P&L | No | No | No | No | No |
| C19–C22 legacy checks | Fresh scan (`scan_result`) | **Yes** (re-scores) | No | No | `score` | **No** |
| C23 portfolio heat | Live SL/lots/balance + correlation | No | No | **Yes — whole book + correlation engine** | `score` (weakest-ranking) | No |
| C26 reconcile | Broker snapshot | No | No | Yes (counts) | No | No |

**Punchline (identical to the entry-pipeline finding):**
`OQ`/`EQ` are computed once at scan time, gate `READY`, and are then **never read again** —
not by the mechanical manager, not by the strategic engine, not by any exit check. If
spread widens or volatility collapses after entry (which is exactly what `OQ` measures),
no management component notices via `OQ`. The only post-entry spread guard is the coarse
`spread ≥ N× typical` check (C16), and the only volatility input is the trade's own
fresh structure read.

---

# 3. MANAGEMENT TICK SEQUENCE

Exact order of operations every cycle, from `_run_supervised_cycle`
(`platforms/main_loop.py:574`). When `drawdown.can_trade()` is false the loop only runs
the starred (★) steps (`main_loop.py:688-693`).

```
_run_supervised_cycle (every loop)
  ├─ watchdog / health checks
  ├─ session_status, news_status
  ├─ drawdown.can_trade()? ─── NO ──► ★_check_pending_orders
  │                                   ★_check_weekend_protection
  │                                   ★_update_positions            (mgmt still runs)
  │
  └─ YES:
       ├─ (maybe) _scan_and_enter                                   [entry path]
       ├─ _check_pending_orders
       ├─ _check_weekend_protection
       ├─ _update_positions ───────────────────────────────────────┐ TICK MGMT (C13)
       │     1. broker snapshot (source of truth)                   │
       │     2. margin guardian (C24)                               │
       │     3. detect broker-side closes → _record_closed_trade    │
       │     4. for each still-open position:                       │
       │          a. sync broker P&L / lots / SL (NAKED alarm)      │
       │          b. fetch tick (fail → skip this position)         │
       │          c. (maybe) fetch M5 for stall/structure           │
       │          d. TradeManager.update (C1) → TP1/BE/trail/TP2/    │
       │             structure/stall                                │
       │          e. act on TERMINAL status:                        │
       │             - stall/structure  → DEFER if DE verdict=HOLD   │
       │               or pnl_r>0.3, else broker close              │
       │             - simulated SL/TP2 → reset (broker owns these)  │
       │          f. TP1 partial close / Deriv close+reopen          │
       │          g. _apply_absolute_profit_protection (C17)        │
       ├─ _check_scale_in (C29)                                     ┘
       ├─ if managed_positions:
       │     ├─ _check_news_exit          (C14)
       │     ├─ _check_session_close      (C15)
       │     ├─ _check_portfolio_heat     (C23) → DEFENSIVE/REDUCING/EMERGENCY
       │     ├─ _check_spread_deterioration (C16)
       │     └─ _analyse_open_trades      (C9)  STRATEGIC MGMT
       │           per position (wrapped in try/except → silent hold):
       │             - re-scan instrument (fresh score, in-trade pressure)
       │             - track score history
       │             - if _decision_enabled (DEFAULT):
       │                   _run_decision_engine (C10):
       │                     build ctx → assess (C5) → decide (C6/C7)
       │                     → governor (C8) → execute (C12)
       │               else (LEGACY, DE off):
       │                   _check_invalidation (C19)
       │                   _check_conviction_collapse (C20)
       │                   _check_htf_candle_close (C21)
       │                   _apply_dynamic_sl_tightening (C22)
       │             - _check_opportunity_cost_exit (C18)  [always]
       │             - _check_scale_in_on_scan (C29)       [always]
       ├─ _advance_shadows (C27)                            [isolated learning]
       └─ periodic reconcile heartbeat (C26)
```

**Two cadences:**
- **Tick** (`_update_positions`): runs every loop; mechanical manager + protective BE
  modifies + broker reconcile. Cheap, frequent.
- **Scan cycle** (`_analyse_open_trades`): runs only when fresh full-timeframe data is
  available (`main_loop.py:736-742`); strategic engine + re-score. Richer, less frequent.

---

# 4. HANDOFF TABLE

| From → To | Data passed | Available but dropped | Impact |
|---|---|---|---|
| Planner → open trade | `plan_be_trigger_r`, `plan_trail_activation_r`, `plan_trail_strategy`, `plan_partial_ratio` carried onto `EntrySignal`→`ManagedTrade` (`trade_manager.py:264-269`) | OQ/EQ, raw `sa.*` dims, conviction, regime shaping rationale | Manager honours per-trade BE/trail/partial overrides, but the *reasoning* is gone — it cannot re-judge them mid-trade |
| Scanner re-score → DE-mgmt | `scan_result.score` (adjusted by in-trade pressure), `direction`, `confluences`, `score_history` | `opportunity_quality`, `entry_quality`, `consensus_*` (never recomputed for management) | Management's "is the thesis intact?" runs on a re-derived structure read + score trajectory, never on OQ/EQ |
| `SituationEngine` → DE | full `SituationAssessment` object | — | (full) |
| DE → `_execute_management_decision` | `ManagementDecision{action, new_sl, reason, confidence}` | the `sa` dims behind the verdict | Executor acts on one verdict; the dimensional reasoning is only logged |
| DE verdict → tick manager | `_last_decision_action[oid]` + timestamp (`main_loop.py:3269`) | full decision | Tick stall/structure exit **defers** to a fresh HOLD/SCALE_IN/BE/TIGHTEN verdict (`main_loop.py:2666`); a verdict older than 600 s (`_decision_verdict_max_age_seconds`) stops deferring |
| TradeManager → loop | `status` (TERMINAL), `close_reason`, `remaining_size_lots` | — | Loop interprets reason strings (`"Stall"`, `"Structure"`, `"TP2"`, `"Stop loss"`) to decide defer vs close vs reset |
| close → feedback (C28) | `pnl_dollars/pips/pct` → `drawdown.register_trade_result`, `risk_engine.record_trade_result`, `_account_risk`, `_governor.update_daily_pnl`, `ml.register_new_trade` | per-trade situation dims, which exit fired (only as a reason string) | Learners retrain on aggregates only; *why* a trade was managed a certain way is not a learning feature |
| severe-thesis close → shadow | persists a shadow from the **exit** price (`main_loop.py:3754`, `_audit_severe_thesis_close`) | — | Severe-decay hard-closes are counterfactually validated; ordinary management exits are **not** |

---

# 5. FAILURE MODE CATALOG

Severity: **HIGH** = can silently hold a losing trade or leave it unprotected;
**MED** = degrades management quality; **LOW** = cosmetic / self-healing.

| # | Failure path | File:line | Behaviour on failure | Open/Safe | Severity |
|---|---|---|---|---|---|
| F1 | Strategic engine raises | `main_loop.py:3273-3277` | `except → log "falling back to legacy"` but **no legacy call** — strategic layer silently no-ops; trade left to mechanical manager | **fail-open (hold)** | HIGH |
| F2 | Per-position analysis raises | `main_loop.py:3225-3229` | `except → warning`, position untouched this cycle | **fail-open (hold)** | HIGH |
| F3 | Tick fetch fails for a position | `main_loop.py:2600-2602` | `continue` — skip management of that position this tick (broker SL/TP still server-side) | fail-safe-ish (broker holds) | MED |
| F4 | Open-trade fresh data unavailable | `main_loop.py:739-742` | strategic analysis **skipped entirely** this cycle (only a warning) | fail-open (hold) | MED |
| F5 | Broker close FAILS on a discretionary exit | `main_loop.py:2696,2728` | logged ERROR, position **retained**, retried next cycle | fail-safe (retry) | MED |
| F6 | Broker reports SL=0 while we expect one | `main_loop.py:2575-2596` | CRITICAL "NAKED POSITION" alarm + re-assert SL; if re-assert fails, stays naked | fail-loud (correct) | — |
| F7 | Reconcile stale + broker **unreachable** | `risk_heat_mixin.py:218-235` | does **NOT** force-close; logs CRITICAL + retains for human review | fail-safe (correct) | — |
| F8 | Reconcile stale + broker **reachable** | `risk_heat_mixin.py:236-248` | escalates to EMERGENCY (force-close) — but first does one retry reconcile (`risk_heat_mixin.py:179-189`) | fail-safe (correct) | — |
| F9 | Spread-protection check raises | `exit_checks_mixin.py:670-675` | `except → warning`; position may be unprotected against spread this cycle | fail-open | MED |
| F10 | News-guard check raises | `exit_checks_mixin.py:307-309` | `except → return`; **all** news exits skipped this cycle | fail-open | MED |
| F11 | Absolute profit protection raises | `main_loop.py:2977-2981` | `except → warning`; profit not locked this tick | fail-open | LOW (retried next tick) |
| F12 | Scale-in governor check raises | (prior audit) `main_loop.py` scale-in path | `except → return True` (allow add) | **fail-open (adds risk)** | HIGH |
| F13 | Margin guardian / account flatten close fails | `risk_heat_mixin.py:687-704,735-749` | position retained + retried; risk FROZEN | fail-safe (correct) | — |
| F14 | `_update_positions` itself raises | `main_loop.py:744-747` | `except → log "Position update error"`, `closed_count=0`; **entire management pass skipped** this cycle | fail-open (hold) | HIGH |
| F15 | Re-entry detection raises | `main_loop.py:4871-4872` | `except → debug`; no re-entry (it does nothing anyway — see G2) | n/a | LOW |

**Net management failure posture:** Like the entry pipeline, the dominant systemic risk is
**fail-open**. When the strategic brain, the per-position analysis, the whole position-update
pass, or the news guard throws, the system **keeps holding** whatever is open and falls back
to broker-side SL/TP only. The hard *survival* paths (margin, account flatten, reconcile-while-
unreachable) are correctly fail-safe — that engineering is genuinely good. The discretionary/
intelligence paths are the ones that fail to "do nothing."

---

# 6. GAP ANALYSIS

Ranked by "silently costs money" severity.

### G1 — Strategic management fails open to mechanical-only (HIGH)
`_run_decision_engine`'s single `try/except` (`main_loop.py:3273`) logs *"falling back to
legacy"* but **never calls the legacy checks** — on any exception the strategic layer simply
returns and the trade is governed by the mechanical `TradeManager` alone (TP/SL/BE/trail/stall).
There is no alert, no metric, no degraded-mode flag. A recurring exception silently downgrades
every open trade to dumb mechanical management.

### G2 — Re-entry is logging-only / dead executor (HIGH for wasted capability)
`_check_re_entry` (`main_loop.py:4839`) calls `ReEntryManager.check_re_entry`, and when
`opp.eligible` is true it logs *"RE-ENTRY eligible"* and… **returns**. No order is placed,
no signal is built, no entry path is invoked (`main_loop.py:4865-4870`). The entire
`ReEntryManager`, the `re_entry_eligible` flag plumbed through `TradeManager`, and the BE-stop
detection exist to feed an executor that does not exist. This is the post-entry twin of the
"wired-but-inert RL" finding.

### G3 — `OQ`/`EQ` invisible to all management (HIGH)
`TradeContext` (`decision/context.py`) has **no `oq`/`eq` fields**, and nothing recomputes them
after the READY gate. The dimension that gated entry (market tradeability + entry geometry) is
never re-validated while the trade is live. If conditions that produced `OQ≥5` decay, only the
coarse `spread ≥ 3×` check (C16) can notice — and it ignores volatility, R:R, and stop geometry
entirely.

### G4 — Four "active management" checks are dead in the default config (MED-HIGH)
`_check_invalidation` (C19), `_check_conviction_collapse` (C20), `_check_htf_candle_close` (C21),
and `_apply_dynamic_sl_tightening` (C22) live in the `else` branch of `_analyse_open_trades`
(`main_loop.py:3183-3210`) that runs **only when `DecisionConfig.enabled` is False**. The default
is `True` (`config.py:756`). So `continuous_analysis_enabled`, `conviction_monitoring_enabled`,
`htf_reassessment_enabled`, and `dynamic_sl_tightening_enabled` (all default `True`,
`config.py:467,477,624,498`) describe behaviour that **never executes** in the shipped config.
The DecisionEngine subsumes some of it (conviction-collapse → `_thesis_deterioration_score`,
opposing-scan → close-score boost), but `dynamic_sl_tightening` has no DE equivalent beyond the
profit-lock stop — beyond-BE R-laddering is simply off.

### G5 — Two un-synced management brains (MED)
The mechanical `TradeManager` re-runs `StructureEngine` on M5 (`trade_manager.py:565,597`) and the
strategic `SituationEngine` re-runs `StructureEngine` on D1/H4/H1/M1 (`main_loop.py:3666-3683`) —
**independently, possibly on different snapshots, with no shared conclusion**. They are reconciled
only by the coarse "defer the tick exit if the last DE verdict was HOLD/working" guard
(`main_loop.py:2666-2684`), which itself expires after 600 s. A stall/structure exit can fire on
the mechanical side while the strategic side believes HOLD, or vice-versa.

### G6 — `planner:WAIT` rejections are not counterfactually tracked (MED)
Every entry hard-reject persists a shadow contract **except** the planner `WAIT` branch
(`main_loop.py:1580-1589`): it logs and `return False` with no `_persist_shadow_contract`.
WAIT-deferred setups therefore never enter the GateTuner's "would-have-won" learning — the
counterfactual gap noted in PR 5 has a sibling here.

### G7 — Stale-score sizing leaks into scale-in (MED)
Scale-in (C29) re-uses the position's stored `score` (`main_loop.py:3928`) and the prior audit's
fail-open governor (`except → return True`) to add risk. Adding to a winner is sized off the
*original* confluence score, never a fresh one.

### G8 — Management exits don't feed learning by *cause* (LOW-MED)
`_record_closed_trade` books P&L into every risk tally and `ml.register_new_trade`, but the exit
*reason* survives only as a free-text string (`outcome`). The learners cannot distinguish
"stopped at BE", "thesis-secure", "stall", or "news exit" as features — so the system cannot learn
*which management behaviour* improves expectancy.

### Signals available but unused by management
- `OQ`/`EQ` (computed, gated, dropped) — G3.
- Raw `sa.*` dims (collapsed to a single verdict before the executor) — handoff table.
- `confluences` list carried onto `ManagedTrade` (`trade_manager.py:243`) — never read for exits.
- The full `TradePlan` (only `risk_pct`→sizing + 4 management overrides survive onto the trade).

### Signals needed but not available to management
- Re-validated `OQ`/`EQ` at management time.
- Per-trade correlation/heat *attribution* inside the mechanical manager (only the portfolio
  state machine sees the book; `TradeManager` manages each trade in isolation).

---

# 7. MID-PIPELINE REJECTION ANALYSIS

**Question:** Do the DecisionEngine or Planner have hard-reject paths between READY and
RiskEngine that silently drop setups without shadow contracts?

**Answer: Mostly NO — they are covered, with one exception (WAIT).** The EntryEngine produces a
full `signal` (with SL/TP) *before* DE and Planner run (`main_loop.py:~1474`), so by the time a
mid-pipeline gate rejects, real geometry exists and a shadow contract can be persisted:

| Mid-pipeline gate | File:line | Persists shadow? |
|---|---|---|
| `decision_engine:SKIP` | `main_loop.py:1532-1539` | ✅ yes (`decision_engine:<action>`) |
| Decision pipeline exception | `main_loop.py:1651-1666` | ✅ yes, fail-closed (`decision_engine:error`) |
| `planner:SKIP` | `main_loop.py:1573-1579` | ✅ yes (`planner:SKIP`) |
| **`planner:WAIT`** | `main_loop.py:1580-1589` | ❌ **NO — logs + returns, no shadow** |
| Planner-fallback governor veto | `main_loop.py:1614-1624` | ✅ yes (`governor:<blocked_by>`) |
| Planner-fallback governor error | `main_loop.py:1632-1649` | ✅ yes, fail-closed (`governor:error`) |
| validator / spread / regime / risk_engine / ev_gate / losing_pattern / ml / sidedness | `main_loop.py:1682,1692,1721,1784,1816,1839,1861,1926` | ✅ yes |

So the only mid-pipeline blind spot is **`planner:WAIT`** (G6). Every *hard reject* (SKIP/veto/
error) already feeds the counterfactual tuner with real-geometry shadows — the DE/Planner are not
a silent-drop blindspot the way the pre-PR5 scanner gates were. `WAIT` is a soft defer (re-evaluated
next scan), which is why it was likely excluded, but a setup that is perpetually WAIT-ed is
effectively rejected and should be tracked.

---

# 8. PROPOSED FIX PLAN (P0–P9)

Ordered by ROI (impact × inverse-effort). Each is a minimum, additive change; none requires a
redesign. File:line references are to the current commit.

### P0 — Strategic management must not fail open (HIGH)
**Where:** `_run_decision_engine` `except` at `main_loop.py:3273-3277`.
**Change:** On exception, actually fall back to the legacy protective checks (or at minimum
`_apply_absolute_profit_protection` + a loud degraded-mode warning/metric), instead of returning
silently. The log says "falling back to legacy" but no fallback runs — make the message true.

### P1 — Make the dead legacy checks reachable or delete them (HIGH clarity)
**Where:** `_analyse_open_trades` `if self._decision_enabled / else` at `main_loop.py:3170-3210`.
**Change:** Either (a) run the protective subset (`dynamic_sl_tightening`, `htf_candle_close`)
**in addition to** the DecisionEngine rather than only in the `else` branch, or (b) delete C19–C22
and their config flags so the system doesn't advertise management it never performs. Decide per
feature; `dynamic_sl_tightening` is the one with no DE equivalent and the clearest case for (a).

### P2 — Implement or remove re-entry (HIGH)
**Where:** `_check_re_entry` at `main_loop.py:4855-4870`.
**Change:** When `opp.eligible` and `_governor_allows_add`, actually build an `EntrySignal` for
`opp.new_entry_zone` and route it through the normal entry executor (with idempotency). If re-entry
is not wanted, delete `ReEntryManager`, the `re_entry_eligible` plumbing, and the BE-stop detection
so the capability isn't a phantom.

### P3 — Re-validate OQ/EQ during management (HIGH)
**Where:** add `oq`/`eq` to `TradeContext` (`decision/context.py`); populate in
`_build_trade_context` (`main_loop.py:3600`) from the fresh re-scan (`scan_result`), recomputing
`compute_opportunity_quality`/`compute_entry_quality` (`brain/setup_quality.py:73,261`) on the
entry-time frames.
**Use it:** in `decide_management`, add CLOSE/TIGHTEN pressure when live `OQ`/`EQ` collapse below
the entry thresholds — "the conditions that justified this trade are gone."

### P4 — Sync the two management brains (MED)
**Where:** mechanical `TradeManager.update` (`trade_manager.py:341`) structure-exit vs strategic
`SituationEngine`.
**Change:** Feed the strategic `structure_integrity`/`tf_alignment` (already computed each scan)
into the tick-level structure-exit decision instead of an independent M5 `StructureEngine.analyze`,
so the two brains can't reach opposite conclusions on the same trade.

### P5 — Persist a shadow contract on `planner:WAIT` (MED)
**Where:** `main_loop.py:1580-1589`.
**Change:** Call `_persist_shadow_contract(signal, rejecting_gate="planner:WAIT")` before
`return False`, mirroring `planner:SKIP`, so WAIT-deferred setups are counterfactually evaluated.

### P6 — Fail-safe the management exception paths (MED)
**Where:** F2 (`main_loop.py:3225`), F9 (`exit_checks_mixin.py:670`), F10
(`exit_checks_mixin.py:307`), F12 (scale-in governor), F14 (`main_loop.py:744`).
**Change:** Where an exception currently means "skip the protective check" or "allow the add",
emit a degraded-mode warning/metric, and for risk-*adding* paths (F12 scale-in) fail **closed**
(don't add) rather than `return True`.

### P7 — Pass the live exit cause into learning (MED)
**Where:** `_record_closed_trade` (`main_loop.py:4168`) → `ml.register_new_trade`.
**Change:** Tag each closed trade with a normalised `exit_cause` enum (TP2 / stall / structure /
thesis_secure / news / session / heat / BE-stop / broker) so learners can attribute outcomes to
management behaviour, not just entry features.

### P8 — Per-trade heat/correlation awareness in the mechanical manager (LOW-MED)
**Where:** `TradeManager.update` (`trade_manager.py:301`).
**Change:** Optionally pass current portfolio heat / correlated-exposure so a runner in a
concentrated, heating book trails tighter — today only the portfolio state machine (C23) reacts to
heat, and it acts on the *book*, not on the individual runner's trail width.

### P9 — Surface management mode + dead-feature status (LOW)
**Where:** dashboard / startup logs.
**Change:** Surface "strategic management: ACTIVE/DEGRADED", "re-entry: ACTIVE/INACTIVE", and the
legacy-checks-disabled state, so operators don't believe management features are running when they
are dead (G2/G4) or degraded (G1).

---

*End of audit. All citations reference the repository at the analysed commit; no trading logic was
modified — only this document was added.*
