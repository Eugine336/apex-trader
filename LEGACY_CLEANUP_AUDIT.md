# Legacy Cleanup — Reachability Audit

Status: **read-only audit** (no code removed). Produced to de-risk the eventual
retirement of the pre–event-driven (legacy) trading stack.

This maps every legacy module to whether it is reachable from the **production
entry point**, and enumerates the couplings that still keep it alive. It is the
prerequisite for any deletion: nothing here should be removed until the
blockers in §6 are cleared.

---

## 1. Verdict

The legacy **core is dormant in production** — `main.py` runs only
`EventDrivenSystem`. But it is **not dead code**: it is still load-bearing for
**backtesting/training**, the **test suite** (~62 files), a **smoke test**, and
package exports, and the dashboard still carries dead legacy-mode branches.

Recommendation: **do not big-bang delete.** Remove in stages, smallest blast
radius first, and only after the two hard blockers in §6 are resolved.

---

## 2. Production entry point (the reachability root)

`main.py` has two run modes; **both instantiate only `EventDrivenSystem`**:

- Dashboard mode (verified `main.py`): `state.attach(None, platform_manager, …)`
  then `EventDrivenSystem(...).start()` + `state.set_event_driven_system(ed)`.
  The dashboard's legacy `trading_loop` is passed as **`None`**, so
  `is_live` is always `False` in production and every legacy-mode dashboard
  branch is a dead path.
- Headless mode (verified `main.py`): `EventDrivenSystem(...).run_forever()`.

`TradingLoop` is **never** instantiated by `main.py`. The
`from scanner import PairScanner, …` line in `main.py` is a phase-load log
("Phase 2 — Scanner loaded"), not an instantiation.

---

## 3. Legacy-core modules (NOT production-reachable) — ~13.5K lines

| Module | Lines | Role |
|---|---:|---|
| `platforms/main_loop.py` (`TradingLoop`, `ManagedPosition`) | 8,553 | Legacy synchronous trading loop |
| `platforms/trading_loop/exit_checks_mixin.py` | 857 | TradingLoop mixin |
| `platforms/trading_loop/risk_heat_mixin.py` | 856 | TradingLoop mixin |
| `platforms/trading_loop/recovery_mixin.py` | 626 | TradingLoop mixin (broker reconciliation) |
| `platforms/trading_loop/shadow_live_mixin.py` | 222 | TradingLoop mixin |
| `platforms/trading_loop/positions.py` | 199 | TradingLoop helper |
| `platforms/trading_loop/__init__.py` | 10 | mixin package |
| `scanner/pair_scanner.py` (`PairScanner`, `PairScanResult`, `ScanReport`) | 1,994 | Legacy per-pair scan |
| `scanner/pair_ranker.py` (`PairRanker`) | 127 | Legacy ranker |
| `scanner/scan_scheduler.py` (`ScanScheduler`) | 82 | Legacy timer-based scan loop |
| **Total** | **~13,526** | |

`management/` (`TradeManager`, partial-close, trailing, re-entry) is **not used
by the ED runtime** (`event_driven_bootstrap.py` registers `("trade_manager",
None)` and never imports it; ED management lives in `execution/`). It is used by
the legacy loop + smoke test + tests, so it is **legacy-coupled** (verify before
removal — see §5 caveat).

---

## 4. What still depends on the legacy core (the couplings)

1. **Backtest / training (HARD dependency).**
   - `brain/backtest_engine.py` instantiates `PairScanner(config=…)` (L307) and
     `EntryEngine(...)` (L318).
   - `backtest/loop_adapter.py` is built to replay `platforms.main_loop.TradingLoop`
     against historical data.
   - ⇒ `PairScanner` and `TradingLoop` cannot be removed until backtest is
     ported to the ED/WorldModel pipeline or the legacy is formally scoped as
     "backtest-only".

2. **Test suite (~62 files)** reference `TradingLoop` / `PairScanner` /
   `last_report` / `_trading_loop` (e.g. `tests/test_atomic_write_and_balance.py`
   builds a `TradingLoop`).

3. **Dashboard live-mode fallbacks (~18 files)** carry `_trading_loop` / `is_live`
   branches. These are **dead in production** (`trading_loop` is always `None`)
   but still present in the code.

4. **`scripts/smoke_test.py`** instantiates `TradingLoop` (and asserts on its
   `entry_engine`).

5. **Package exports**: `platforms/__init__.py` lazily exports
   `TradingLoop`/`ManagedPosition`; `scanner/__init__.py` exports
   `PairScanner`/`PairRanker`/`ScanScheduler` alongside the ED `CandleCloseHandler`.

---

## 5. SHARED infrastructure — DO NOT remove

These are used by the ED runtime (directly or via `SystemContext`) and must
survive any legacy cleanup:

- **`trigger/EntryEngine` — SHARED (gotcha).** ED uses
  `ctx.entry_engine.calculate_stop_loss(...)` / `calculate_targets(...)` for
  ATR-based SL/TP (verified `event_driven_bootstrap.py` L2389/L2409);
  `core/system_context.py` L342 wires `ctx.entry_engine`. Do **not** treat the
  `trigger/` package as legacy-only.
- **`brain/*`** analysis modules — incl. `directional_consensus` +
  `opportunity_ranker` (now also feed the ED votes/ranker) and the FVG/OB/
  structure/liquidity/volume/wyckoff/inducement detectors used by
  `CandleCloseHandler`.
- **`core/system_context.py`** and every subsystem it wires (risk, adaptive,
  ml, governor, orchestrator, outcome feedback, …) — shared by ED and legacy.
- **`execution/`**, **`entry/`**, **`tick/`**, **`persistence/`**, **`risk/`**,
  **`adaptive/`** — the ED planes.

> Caveat to verify before deletion: confirm which of `trigger/`
> (`EntryPatternDetector`, `EntryValidator`) and `management/` (`TradeManager`,
> `PartialCloseCalculator`, `StructureTrailingStop`, `ReEntryManager`) are
> legacy/backtest-only vs. reached via `SystemContext`. `EntryEngine` is
> confirmed shared; the siblings were not individually traced in this pass.

---

## 6. Blockers (must clear before any removal)

1. **The `test` CI job is red.** Removing ~13.5K lines that ~62 tests depend on
   without a green suite to prove nothing broke is not safely validatable. Fix
   the `test` job first.
2. **Backtest depends on the legacy execution model.** Decide its future:
   (a) port `backtest_engine`/`loop_adapter` to drive the ED/WorldModel
   pipeline, or (b) formally scope `TradingLoop`+`PairScanner` as backtest-only
   and isolate them from the live tree.

---

## 7. Phased removal plan (after blockers clear)

- **Stage 1 — Dashboard dead-path removal (low risk, no backtest impact).**
  Delete the `_trading_loop` / `is_live` legacy-mode branches (~18 dashboard
  files); ED is the sole live source. Self-contained.
- **Stage 2 — Resolve backtest (§6.2).** Port to ED or isolate legacy as
  backtest-only.
- **Stage 3 — Retire the legacy core.** Remove `TradingLoop` + `trading_loop/`
  mixins + `pair_scanner`/`pair_ranker`/`scan_scheduler` (and `management/` if
  confirmed legacy-only), update `platforms/__init__.py` + `scanner/__init__.py`
  exports, retire `scripts/smoke_test.py`'s legacy path, and remove/rewrite the
  ~62 legacy tests.

Approximate reclaimable: **~13.5K lines** (core) plus `management/` and the
legacy-only tests, once Stage 2 frees the backtest dependency.
