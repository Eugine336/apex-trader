# APEX — Cognition Runtime and Production Acceptance

> Current contract: the CognitiveBrain is the sole market decision authority.
> The historical CognitionGate/ManagementGate shadow-veto-authoritative ladder
> has been retired. This runbook describes the active runtime, not that legacy
> routing model.

## Active runtime controls

- `COGNITION_ENABLED`: constructs and runs the native Brain/loop subsystem.
- `COGNITION_ORIGINATION_MODE`: `off`, `shadow`, or `live` for new Brain-authored campaigns.
- `COGNITION_MANAGEMENT_MODE`: controls Brain-authored management actions; do not disable risk-reducing broker mechanics.
- `TRADING_ENABLED=false`: global kill switch for new exposure. Existing-position management, reconciliation and risk-reducing closes remain enabled.

There is no `COGNITION_GATE_MODE`, `CognitionGate`, or `ManagementGate`. No legacy
entry route is permitted to originate exposure as a fallback. A missing, stale,
invalid or incomplete Brain decision must not be converted into permission to
trade. The shared execution, portfolio-risk, broker-geometry and owner-limit
checks remain mandatory after the Brain decision.

## Production promotion requirements

1. Confirm the same timestamped market observations reach the Brain and that
   forming candles remain provisional while completed-bar evidence is explicit.
2. Verify provider outage and native-only operation separately. Advisor failure
   must not be confused with a market no-opportunity verdict.
3. Verify the Brain emits a structured opportunity set, challenges competing
   hypotheses, and authorizes only a qualified opportunity with verified net EV.
4. Trace every exposure-increasing path through the canonical Brain campaign
   contract, shared execution authority, whole-book risk and broker preflight.
5. Verify open-position management, pending-order lifecycle, reconciliation and
   risk-reducing exits independently. A failed origination path must not disable
   position protection.
6. Run Python 3.11/3.12, lint, constitutional audit, replay/backtest, fault
   injection and broker-demo checks. CI does not replace owner-connected MT5
   acceptance or holdout performance evidence.

## Cognition observability

`CognitionObservability.metrics()` reports decision throughput, campaigns opened,
observed/managed counts, Brain availability/faults, calibration sample count,
reliability gap, Brier score, reasoning-quality measurability, campaign-memory
counts and origination intent/submission counts. It no longer reports gate modes,
veto rates or authorization rates because those retired gates no longer exist.

## Safe rollback

Set `TRADING_ENABLED=false` to stop new exposure while preserving existing-position
management and risk-reducing closes. Do not restore the retired shadow/veto gate
or legacy entry route as a rollback mechanism. Any production rollback that changes
management or execution behavior requires a separate safety review and explicit
owner approval.

## Non-negotiable acceptance

- No broker orders during engineering validation.
- No weakening of portfolio, broker, owner-limit or execution safety controls.
- Unknown evidence remains unknown; no inferred probability or fabricated geometry.
- A green CI run is necessary but not sufficient for live deployment.