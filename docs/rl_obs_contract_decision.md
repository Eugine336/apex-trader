# RL Observation Contract — Train vs Production Dimension Mismatch

## Status
**Decision required.** The mechanical safety fixes (C5 checkpoint round-trip,
H6 loud dimension guard) are implemented. The architecture choice below
determines how to resolve the mismatch itself.

## Verified Facts (file:line at HEAD 8623d5d)

| Property | Training env | Production contract |
|----------|-------------|---------------------|
| Features per timestep | **12** (`rl/environment.py:59`) | **48** = 12 × 4 TFs (`rl/contracts.py:59`) |
| Context vector dim | **0** (`rl/trainer.py:162-165`, no `context_dim` arg) | **8** (`rl/contracts.py:90`) |
| Timeframes | **1** (single CSV) | **4** (M5/M15/H1/H4, `rl/contracts.py:47`) |
| Env class | `ApexTradingEnv` (`rl/environment.py`) | `ApexMultiTFTradingEnv` (`rl/mtf_environment.py`) |
| Checkpoint init script | `scripts/init_rl_checkpoint.py` uses n_features=**48**, context_dim=**8** | matches contract |

The trainer (`rl/trainer.py`) uses the legacy single-TF environment and
produces models with 12-feature input.  The production inference path
(`rl/shadow.py` → `rl/bridge.py`) expects the MTF contract (48 features
+ 8-dim context).  After the C5/H6 fixes, any attempt to load a
trainer-produced checkpoint in production will **fail loudly** with a clear
error instead of silently consuming mis-aligned features.

## Current RL Live Authority
**Zero.** `rl/bridge.py:130` — `if self.authority.stage < 2: return passthrough`.
Authority DB is Stage 1 (JOURNAL_ONLY) with 0 log rows.  The RL subsystem
is shadow-only, fails safe, and has no influence on real orders.

## Resolution Options

### Option A — Align Up (make training match production)
Rewrite `rl/trainer.py` to use `ApexMultiTFTradingEnv` instead of
`ApexTradingEnv`.  The trainer would then produce models with
n_features=48 and context_dim=8, matching the production contract.

**Pros:**
- Trained model sees the same observation the scanner feeds in production.
- Multi-timeframe cascade relationships (H4 trend + M5 confirmation) are
  learnable — the stated design goal.
- Symbol conditioning (instrument context vector) is available.

**Cons:**
- Requires multi-TF CSV data per instrument (data availability unverified
  for all instruments).
- Higher compute cost (4× input features, plus context).
- Training curriculum (Phase 3 in the RL plan) is not yet implemented.

**Effort:** Medium — the MTF env and obs builder exist; the gap is the
training loop and data pipeline.

### Option B — Align Down (make production match training)
Change the production inference path to build a 12-feature single-TF
observation (like the legacy env) and pass context_dim=0.

**Pros:**
- Immediate compatibility — existing or future trainer-produced
  checkpoints load without error.
- Simpler, faster inference.

**Cons:**
- Discards the entire MTF infrastructure (Phases 1-2 of the RL plan).
- Agent cannot learn cascade relationships — it sees only one timeframe.
- Symbol conditioning lost.
- Contradicts the stated design direction.

**Effort:** Low — change the obs builder call in `scanner/pair_scanner.py`
to pass single-TF frames.

### Option C — Freeze RL as Research-Only
Add an explicit "untrained — do not arm" guard.  RL remains in the
codebase for research/experimentation but is formally excluded from the
live trading path until Options A or B are resolved and a real trained
checkpoint exists.

**Pros:**
- Zero risk — RL is already inert; this makes the intent explicit.
- No code changes to production paths.
- Buys time to decide A vs B with evidence (e.g. data availability audit,
  training curriculum implementation).

**Cons:**
- No progress toward RL integration.
- The MTF infra and authority model remain untested against live data.

**Effort:** Minimal — one config flag or import guard.

## Recommendation
**Option A** is the architecturally correct path — it matches the stated
design goal and leverages the MTF infrastructure already built (Phases 0-2
of the RL plan).  However, it depends on the Phase 3 training curriculum
being implemented and multi-TF data being available across instruments.

In the interim, **Option C** is the honest default: the system already
behaves this way (RL is inert), and the H6 guard now makes the dimension
mismatch fail loud instead of silent.  Option C simply documents the
existing reality.

**Do NOT choose Option B** unless the MTF design direction is being
abandoned — it would discard significant completed work (Phases 0-2)
for a marginal near-term convenience.
