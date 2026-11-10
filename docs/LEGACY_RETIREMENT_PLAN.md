# Legacy Directional Stack — Retirement Plan

> Status: **Incremental retirement; historical inventory reconciled 2026-11-10.**
> This plan governs removal of obsolete decision authorities and no-op runtime
> wiring. The canonical identity and Brain authority are defined by
> cognition/identity.py and docs/APEX_Constitution.md. A historical count
> must not be presented as a current call-site count.

## Current verified source-tree observations

The previous inventory recorded approximately 209 reads of
brain.vote_evidence.Vote.direction. That count is a historical baseline, not
the current tree: the current main directory inventory contains neither
brain/vote_evidence.py nor brain/directional_consensus.py; the rl/ package is
absent; and decision/ contains only __init__.py and actions.py. The old
209-consumer figure must not be used to imply those modules are still active.
Remaining historical documents and audit text that name those modules require
reconciliation separately.

Before this increment, the entry/ package contained shared models, M1
patterns, and the M1/M5 flip-sequence implementation. Source tracing found that
event_driven_bootstrap.py constructed and fed FlipSequenceTracker on M1 and M5
candle-close events, but no production caller read its is_confirmed() result.
Its flip_require_sequence profile setting therefore advertised a gate that did
not participate in a Brain decision. The tracker also depended on
M1CandleConfirmer, whose only production import was the tracker itself. The
remaining flip-confirmer, tick-delta and sequence settings in InstrumentProfile
also had no production readers. This was dead state/configuration and redundant
data-fetch work, not an active gate.

The unused CognitionGate and ManagementGate modules are also being retired in
PR #890. They were constructed and exposed through status, but no production
caller evaluated either gate. Their mode/freshness configuration and status
metrics are removed with the wiring; the canonical Brain loop and shared
execution/portfolio/broker/owner-limit checks remain authoritative.

## Ordered retirement record

1. **Extract shared non-directional types.** Completed historically:
   EntryConfig, EntryZone, GateResult and PendingEntry remain in the shared
   entry model surface; keep these data contracts where active callers require
   them.
2. **Slim entry/__init__.py.** Completed historically: package exports are
   limited to shared data models rather than eagerly importing decision modules.
3. **Sever bootstrap wiring.** The legacy orchestrator/stager wiring was
   previously retired. This increment removes the unconsumed M1/M5 sequence
   tracker construction, candle-close subscriptions, feed methods and all orphaned
   flip-confirmer/tick-delta/sequence profile switches. The M1/M5 state was never read by a production decision consumer,
   so no replacement gate is introduced. The canonical Brain and its
   opportunity/management contracts remain the decision path.
4. **Delete decision-only entry/ leaves.** Previously removed leaves remain
   retired. This increment also removes entry/flip_sequence_tracker.py,
   entry/m1_confirmation.py, and their dedicated tests. Shared entry/models.py
   and entry/m1_patterns.py are not removed merely because they live under the
   same package; their actual evidence/data consumers must be evaluated
   independently.
5. **Retire directional consensus.** The source files
   brain/directional_consensus.py and brain/vote_evidence.py are absent from
   the current main tree. Do not repeat historical caller counts or delete
   unrelated current analysis modules based on obsolete file names. Search for
   remaining executable references and keep any residual offline/replay
   consumers explicitly non-authoritative.
6. **Retire decision-only learning/backtest code selectively.** The former
   rl/ package is absent. The backtest/ package is a current general
   backtesting surface and must not be deleted wholesale. The remaining
   decision/actions.py module must be evaluated by its actual imports and
   runtime callers, not by its directory name.
7. **Remove residual vote contracts.** brain/vote_evidence.py is absent from
   the current tree. Treat any references in old documentation or audit
   snapshots as stale until confirmed against executable source.

## Regression and acceptance gates

- scripts/constitution_audit.py remains the executable static guard for
  retired entry and decision surfaces. This increment adds explicit existence
  guards for the retired M1/M5 tracker and confirmer so they cannot silently
  return.
- CI must compile the changed runtime modules, run focused entry/session tests,
  run the constitutional audit and full Python 3.11/3.12 matrix.
- A path is retired only when production imports, construction, event
  subscriptions, configuration switches and callers are gone. Test-only
  references are removed or converted to assertions that the retired path
  stays absent.
- No live broker orders, .env access, credential changes, runtime database
  mutation or execution-setting changes are part of this work.
- Passing CI validates repository behavior; it does not substitute for
  owner-connected broker acceptance or full production qualification.


8. **Retire the no-op breakeven re-entry subsystem.** The runtime had no
   consumer for the stored breakeven/last-close cooldowns, and ReEntryManager
   only emitted an observational event after M5/FVG/OB checks; it did not
   submit or authorize an order. Remove its SystemContext construction,
   close-handler evaluation, re-entry event, snapshot/trade-model flag,
   package export and source module. A closed campaign returns to the normal
   Brain observation cycle; no direction-locked re-entry shortcut or dormant
   cooldown state remains.


9. **Retire disconnected cross-instrument heuristics.** Call-site audit found no
   runtime consumers for the CrossInstrumentRanker, OpportunityQualitySizer or
   PositionDisplacer beyond SystemContext construction and dashboard status. The
   quality sizer also used reasoning confidence as a size multiplier. Remove the
   modules, tests, SystemContext wiring, dashboard/UI status, configuration fields,
   environment switches and operator-reference rows. Retain only the queue for
   already Brain-authorized intents and the observational proactive watchlist.
