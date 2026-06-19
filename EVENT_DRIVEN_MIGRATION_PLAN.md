# Full Event-Driven Migration Plan

Goal: make the pre–event-driven (legacy) stack **completely dead code** — zero
live, backtest, test, or export connections — so it can be deleted. This is the
executable blueprint referenced by `LEGACY_CLEANUP_AUDIT.md`. It is sequenced so
every stage is independently shippable and **validated**, never a blind big-bang.

> Status as of writing: the *live* ED path no longer imports the legacy core
> (dead imports removed in "Remove dead legacy scanner imports"). The remaining
> connections are **backtest**, **~62 tests**, **package exports**, and
> **dashboard fallbacks**.

---

## 0. End state (definition of done)

- `platforms/main_loop.py` (`TradingLoop`, `ManagedPosition`),
  `platforms/trading_loop/*`, `scanner/pair_scanner.py`,
  `scanner/pair_ranker.py`, `scanner/scan_scheduler.py`, and
  `backtest/loop_adapter.py` are **deleted**.
- `grep -rn "TradingLoop\|PairScanner\|PairRanker\|ScanScheduler\|main_loop\|loop_adapter" --include=*.py .`
  returns **nothing** outside historical comments (which are also scrubbed).
- The `test` and `lint` CI jobs are **green**.
- Live trading and backtest both decide through **one** shared ED decision core.

---

## 1. HARD PREREQUISITE — green `test` CI job

Nothing below ships until the `test (3.11/3.12)` job runs and passes. Rationale:
this migration deletes ~13.5K lines and changes backtest decision semantics; the
only way to prove the live trader and backtest still behave is the test suite.
Removing legacy with red CI = unvalidated change to a capital-at-risk system.

Action: obtain a failing `test` job log, fix the dependency-install / collection
failure, confirm green. (Tracked separately as item "1b".)

---

## 2. The shared ED decision core (new module)

Today the ED decision logic lives inside `scanner/candle_close_handler.py`
(`_run_modules` → `build_world_model` → `_compute_bias` → `_blend_concepts` →
`extract_entry_zones` → `_build_consensus`). It is already ~pure over candle
data; it just needs to be callable synchronously, without the EventBus / store /
thread pool.

**New file `brain/decision_core.py`:**

```
def analyze_window(
    symbol: str,
    candles_by_tf: dict[str, pd.DataFrame],   # {"M5":df, "M15":df, "H1":df, "H4":df, "D1":df}
    *,
    edge_weight=None,
    concept_weight=None,
) -> WorldModel:
    """Run the full ED brain pipeline over a fixed candle window and return a
    fully-populated WorldModel (fvgs/obs/structure/liquidity/volume/wyckoff/
    inducement + bias + concepts/regime + entry_zones + votes + candidates).
    No EventBus, no WorldModelStore, no threads — deterministic and reusable."""
```

Refactor `CandleCloseHandler` so `_run_modules` + the merge/bias/concepts/zone/
consensus body call into `analyze_window` (live path unchanged in behavior —
it just delegates). This guarantees **live and backtest share one decision
implementation** (the whole point of the migration).

**Backtest adapter contract** (what `brain/backtest_engine.py` consumes from the
legacy `PairScanResult`, verified at L744–L788): `.status` (`"READY"`),
`.direction` (`LONG`/`SHORT`), `.score` (int 0–100), `.regime` (str),
`.bias_strength` (`STRONG`/`MODERATE`/`CONFLICTED`/`NONE`). Provide a tiny
mapper `world_model_to_scan_view(wm) -> ScanView` deriving those from
`wm.bias_dict()` (direction/score/strength), `wm.regime_by_tf()`, and
`wm.entry_zones`/`wm.candidates`. `status="READY"` when bias is tradeable or a
conviction-qualified entry zone exists.

---

## 3. Staged execution

### Stage A — Extract the decision core (additive, zero behavior change)
- Add `brain/decision_core.py::analyze_window` (logic lifted from
  `candle_close_handler`).
- Re-point `CandleCloseHandler` to delegate to it.
- **Validate:** existing `tests/test_candle_close_handler.py` + WorldModel tests
  must stay green; live behavior identical.

### Stage B — Migrate backtest onto the core (the load-bearing change)
- `brain/backtest_engine.py`:
  - Replace `from scanner.pair_scanner import PairScanner` + `self.scanner =
    PairScanner(...)` (L306–L310) with the decision core; build
    `candles_by_tf` from the replay window and call `analyze_window`, then
    `world_model_to_scan_view`.
  - `scan_pair(...)` call site (L744) → core call; keep `EntryEngine`
    (`calculate_entry`) for levels (it is **shared**, not legacy).
  - Update `_require_decision_engine` (L340) accordingly.
- `backtest/loop_adapter.py`: this only exists to replay `TradingLoop`. Delete it
  (the default `BacktestRunner` path never needed it) — confirm no importer
  remains.
- **Validate (critical):** run a fixed-dataset backtest before vs. after; assert
  results are sane and documented (they *will* differ — ED ≠ PairScanner — so
  capture the new baseline rather than expecting equality). Add a regression
  test pinning the new backtest output.

### Stage C — Remove dashboard legacy fallbacks
- 15 `dashboard/state_*.py` mixins + `state.py` + `state_helpers.py` carry
  `is_live` / `_trading_loop` branches. In production `trading_loop` is always
  `None`, so these are dead. For each: drop the legacy branch, keep the ED
  branch, simplify `is_live` away.
- `dashboard/state.py::attach(...)` — drop the `trading_loop` parameter / make it
  ED-only.
- **Validate:** dashboard tests (`tests/` + `dashboard/tests/`, ~12 `is_live`
  refs) updated to construct ED-backed state; all green.

### Stage D — Remove exports + scrub comments
- `scanner/__init__.py`: drop `PairScanner`, `PairScanResult`, `ScanReport`,
  `PairRanker`, `RankedSetup`, `ScanScheduler` from imports/`__all__`; keep
  `CandleCloseHandler`.
- `platforms/__init__.py`: drop `TradingLoop`, `ManagedPosition` from `__all__`
  and `_LAZY_IMPORTS`.
- Scrub historical "Mirrors TradingLoop…/main_loop…" comments in ~20 files
  (`event_driven_bootstrap.py`, `core/system_context.py`, `decision/engine.py`,
  `execution/*`, `ops/lifecycle.py`, …) — comment-only, no runtime effect.

### Stage E — Delete the legacy files + their tests, verify dead
- Delete: `platforms/main_loop.py`, `platforms/trading_loop/*`,
  `scanner/pair_scanner.py`, `scanner/pair_ranker.py`,
  `scanner/scan_scheduler.py`, `backtest/loop_adapter.py`.
- `scripts/smoke_test.py`: rewrite onto `EventDrivenSystem` (or delete).
- Remove/rewrite the legacy tests (see §4).
- **Final gate:** the §0 grep returns clean; `lint` + `test` green.

---

## 4. Test migration ledger (~62 files)

Categorize each `tests/` file that references the legacy:
- **Delete (pure-legacy):** e.g. `test_pair_scanner.py`,
  `test_m1_trading_loop_mixins.py`, `test_backtest_*` that assert PairScanner /
  TradingLoop internals.
- **Rewrite onto ED:** files testing behavior that still exists but via legacy
  entry points (e.g. management/exit/idempotency suites) — repoint at
  `EventDrivenSystem` / `decision_core` / `execution` equivalents.
- **Leave (incidental):** files that only import a legacy symbol for a fixture
  shared with ED — swap the import.

Produce the exact per-file disposition list at the start of Stage E (mechanical
once the core + backtest are migrated).

---

## 5. Per-connection ledger (authoritative checklist)

| Connection | File(s) | Stage | Action |
|---|---|---|---|
| Live ED imports | `main.py`, `event_driven_bootstrap.py` | done | removed |
| Decision core | `scanner/candle_close_handler.py` → `brain/decision_core.py` | A | extract + delegate |
| Backtest decision engine | `brain/backtest_engine.py` | B | PairScanner → core |
| TradingLoop replay | `backtest/loop_adapter.py` | B | delete |
| Dashboard fallbacks | 15 `dashboard/state_*.py` + `state.py`/`state_helpers.py` | C | drop legacy branch |
| Exports | `scanner/__init__.py`, `platforms/__init__.py` | D | drop legacy symbols |
| Comments | ~20 src files | D | scrub |
| Legacy core files | `main_loop.py`, `trading_loop/*`, `pair_scanner/ranker/scheduler` | E | delete |
| Smoke test | `scripts/smoke_test.py` | E | rewrite/delete |
| Tests | ~62 `tests/*` | E | delete/rewrite per §4 |

---

## 6. Validation gates (every stage)

1. `python -m py_compile` on all changed files.
2. `ruff check . --select E,F,W --ignore E501,E402,F401,E701,E731,E741` clean.
3. Full `pytest` green in CI (the prerequisite from §1).
4. Stage B additionally: documented before/after backtest baseline + a pinned
   regression test.

## 7. Rollback

Each stage is a separate PR. Because the legacy files are only **deleted** in
Stage E, Stages A–D are reversible by revert with no data/format loss; Stage E is
the point of no return and ships only after A–D are green on `main`.
