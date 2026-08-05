# APEX — Cognition Controlled-Rollout Runbook

> Constitution Parts XII (Observability) & XIII (Validation). Companion to
> `docs/APEX_Constitution_Implementation_Plan.md` (Phase L).

This runbook governs how the AI Cognitive Brain is promoted from observation to
sole decision authority **safely and reversibly**, per symbol group, with a
one-switch rollback. It is the discipline that lets the remaining Phase K work
(coupled-core decomposition + legacy-decider physical deletion) happen only
*after* the Brain-driven path is proven on the demo.

## The single control: `COGNITION_GATE_MODE`

The Brain's authority on the entry decision path is the `CognitionGate`, whose
mode is set by `COGNITION_GATE_MODE` (also `CognitionConfig.gate_mode`):

| mode | meaning | Brain authority | rollback |
| --- | --- | --- | --- |
| `off` | gate disabled | none — legacy path decides | n/a |
| `shadow` | Brain reasons, decisions recorded only | observe only (fail-open) | already safe |
| `veto` | Brain may **block** a legacy entry, never originate | one-way veto (fail-open) | → `shadow` |
| `authoritative` | Brain is the **sole** decider | full (fail-closed on staleness) | → `veto`/`shadow` |

Rollback is always a single env change + restart: set `COGNITION_GATE_MODE` back
one rung. No code change, no redeploy of logic. Origination
(`COGNITION_ORIGINATION_MODE`: `off`→`shadow`→`live`) follows the same ladder
independently.

## Promotion ladder (per symbol group)

Promote **one symbol group at a time** (e.g. majors → crosses → synthetics),
never the whole book at once.

1. **Shadow (observe).** `gate_mode=shadow`, `origination_mode=shadow`. Run for a
   sustained window. Watch the observability metrics (below). Exit criteria:
   `brain_available=true`, `brain_faults=0`, decisions flowing
   (`decisions_per_min > 0`), and `reasoning_quality_measurable=true`.
2. **Veto.** `gate_mode=veto`. The Brain can now block a legacy entry it does
   not back. Watch `would_veto_rate` vs `veto_rate` and confirm vetoes correlate
   with avoided losers (institutional memory / post-mortems). Exit criteria:
   `readiness_verdict(...) == ready` and no veto-driven regressions.
3. **Authoritative.** `gate_mode=authoritative`. The Brain is the sole decider;
   the gate fails **closed** on stale/absent decisions (no reasoning ⇒ no trade).
   Only enter after step 2's readiness holds for the group.

At each rung, the offline **validation harness must be green first**:
`ReplayHarness(brain).run(states, outcomes=...)` → `report.ready == True`, and the
live `readiness_verdict(governance.get_status()["cognition"])` → ready.

## What to watch (observability — Part XII)

Surfaced under governance `get_status()["cognition"]["observability"]`
(`CognitionObservability.metrics()`):

- `brain_available`, `brain_faults`, `gate_mode`, `authoritative`
- `decisions_total`, `decisions_per_min`, `campaigns_opened`, `observed`, `managed`
- `veto_rate`, `would_veto_rate`, `authorise_rate`, `gate_evaluations`
- `calibration_samples`, `reliability_gap`, `brier`,
  `reasoning_quality_score`, `reasoning_quality_measurable`
- `memory_completed_campaigns`, `orig_intended`, `orig_submitted`

`reasoning_quality_score` (= `1 − reliability_gap`, only once calibration is
statistically meaningful) is the headline "is the Brain trustworthy yet" signal.

## Rollback triggers (immediate step-down)

Step the group down one rung on any of: a fault storm (`brain_faults` rising),
`reliability_gap` degrading past the promotion threshold, an unexpected
`veto_rate` spike, or any deterministic-safety (Part X) alarm. Rollback is
`COGNITION_GATE_MODE` ↓ + restart; the legacy feasibility/mechanics path remains
intact beneath the Brain throughout the rollout, which is *why* the legacy
decision code is not physically deleted until a group has held `authoritative`
cleanly.

## Gate to the remaining Phase K deletion

The legacy-decider physical deletion and coupled-core decomposition are unlocked
only after a representative symbol group has run `authoritative` cleanly for a
sustained window with the harness green and observability healthy. Until then the
legacy path stays as the validated fallback (Constitution Part XV: no deletion
until superseded **and** validated).
