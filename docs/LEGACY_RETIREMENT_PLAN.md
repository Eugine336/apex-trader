# Legacy Directional Stack — Retirement Plan

> Status: **Track opened (increment 1).** This plan governs the physical removal
> of the legacy directional-decision stack. It is the operational companion to
> Part XXV of `docs/APEX_Constitution.md` (Raw-Market Reasoning & Non-Collapsed
> Cognition). The cognition layer is already fully non-directional and the Brain
> is the sole decider under single-path; this track physically deletes the
> now-severed legacy decision code so `Vote.direction` and its consumers no
> longer exist anywhere.

## Why this is a multi-step track, not one deletion

An audit of the live tree shows the legacy directional-decision code is **deeply
entangled with still-live evidence/scanner/world-model code through shared
modules and types**, so naive file deletion breaks the running system:

- `brain/vote_evidence.py::Vote.direction` is read by **~209 non-test sites**
  across `brain/`, `adaptive/`, `management/`, `backtest/`, `entry/`, `rl/`,
  `execution/`, `decision/`, `planning/`. **Zero** are in `cognition/`.
- `entry/__init__.py` eagerly imports **every** entry module (orchestrator,
  gate, staging, tick detectors, flip trackers) — so any live import of a
  *shared type* (`entry.models.EntryConfig` / `EntryZone`, used by
  `brain/world_model.py`, `scanner/`, `brain/structure_context.py`, etc.)
  transitively loads the decision-only modules. None are physically dead.
- `event_driven_bootstrap.py` still **constructs and calls** `ZoneOrderStager`
  and `FlipSequenceTracker`, and imports `EntryOrchestrator` from the package.
- `brain/directional_consensus.py` (`form_thesis` / `decide` /
  `decide_opportunities`, and a `Vote`) is imported by `adaptive/`, `brain/`,
  and `event_driven_bootstrap.py`.

The live trading stack cannot be executed in the validation sandbox
(`numpy`/`pandas`/MetaTrader5 absent), so every deletion increment is validated
by (a) repo-wide grep proving no remaining reference and (b) `python -m
py_compile` syntax verification, never by a naive bulk delete.

## Ordered, dependency-safe demolition sequence

Each step is its own PR, grep + syntax verified, keeping the live system intact:

1. **Extract shared non-directional types** (`EntryConfig`, `EntryZone`,
   `GateResult`, `PendingEntry`) into a neutral module and make `entry/models.py`
   re-export for back-compat, so live evidence code depends on the neutral type,
   not the directional package.
2. **Slim `entry/__init__.py`** to stop eagerly importing decision-only modules;
   update the (gated) bootstrap imports to point at explicit submodules.
3. **Sever bootstrap wiring** — remove construction/subscription of
   `EntryOrchestrator`, `ZoneOrderStager`, `FlipSequenceTracker` and the
   `on_m1_close`/`on_world_model_update` legacy entry handlers.
4. **Delete decision-only `entry/` leaves** — `entry_orchestrator.py`,
   `entry_gate.py`, `zone_order_staging.py`, `tick_entry_detector.py`,
   `tick_delta_analyzer.py`, `flip_confirmer.py`, `flip_sequence_tracker.py`,
   `m1_confirmation.py` (keep `models.py`, `zone_watcher.py`, `m1_patterns.py`
   only for their live evidence use, de-directionalised).
5. **Retire `brain/directional_consensus.py`** decision functions (`form_thesis`,
   `decide`, `decide_opportunities`) and their now-dead callers in `adaptive/`,
   `brain/decision_core.py`, `event_driven_bootstrap.py`.
6. **Retire `decision/`, `rl/`, and legacy `backtest/` directional paths** that
   exist only to serve the removed decision funnel.
7. **Remove `Vote.direction`** (and the `signed` property / `VoteResult` /
   `DirectionDecision`) from `brain/vote_evidence.py` — last, once every consumer
   above is gone.

## Measurable gate

`cognition/legacy_audit.py::legacy_directional_inventory()` enumerates the
remaining legacy directional-decision surfaces (modules importing/calling
`form_thesis` / `decide` / `decide_opportunities`, constructing `VoteResult`, or
wiring the legacy entry pipeline). Every increment above must lower its
`surface_count`; `cognition/` must never appear in it. The Part XXV gate
`audit_nondirectional_cognition()` independently guarantees no directional
reading can re-enter cognition while this track proceeds.
