"""
APEX TRADER — Domain Event Type Constants
Typed string constants for structured domain events emitted to the EventStore.
Prevents stringly-typed drift across modules.
"""

# ── Decision events ──────────────────────────────────────────────────────────
DECISION_REJECT = "DECISION_REJECT"

# ── Pipeline awareness (Decision Trace) ──────────────────────────────────────
# One event per completed scan→entry pipeline pass: the full chain of stage
# verdicts (ranker, correlation, margin, entry/decision/governor/planner), any
# downstream challenges, and the terminal outcome. Powers the dashboard's
# pipeline-funnel / rejection-breakdown / challenge-feed panels.
DECISION_TRACE = "DECISION_TRACE"

# ── Scanner-stage events ────────────────────────────────────────────────────
SETUP_SKIPPED = "SETUP_SKIPPED"

# ── Order lifecycle events ───────────────────────────────────────────────────
ORDER_SENT = "ORDER_SENT"
ORDER_FILLED = "ORDER_FILLED"

# ── Trade lifecycle (Phase 3+) ───────────────────────────────────────────────
TRADE_OPEN = "TRADE_OPEN"
TRADE_CLOSE = "TRADE_CLOSE"

# ── Shadow resolver (Phase 4) ───────────────────────────────────────────────
SHADOW_CONTRACT_CREATED = "SHADOW_CONTRACT_CREATED"
SHADOW_RESOLVED = "SHADOW_RESOLVED"

# ── Re-entry (event-driven re-arm after breakeven stop) ─────────────────────
# Emitted when ReEntryManager confirms a stopped-at-breakeven setup is still
# structurally valid and the entry path is re-armed (BE cooldown cleared).
RE_ENTRY_ARMED = "RE_ENTRY_ARMED"

# ── Safety events ──────────────────────────────────────────────────────────
BALANCE_UNAVAILABLE = "BALANCE_UNAVAILABLE"
PERSISTENCE_DEGRADED = "PERSISTENCE_DEGRADED"
CYCLE_FAILED = "CYCLE_FAILED"
TRADING_LOOP_HALTED = "TRADING_LOOP_HALTED"

# ── Backfill (Phase 6) ─────────────────────────────────────────────────────
TRADE_CLOSE_DERIVED = "TRADE_CLOSE_DERIVED"

# ── Orchestrator (round table — graded sizing) ──────────────────────────────
# One event per entry attempt the orchestrator graded: the collected evidence
# proposal and the resulting bounded size multiplier (or physics veto). Powers
# the dashboard's Orchestrator panel.
ORCHESTRATOR_PROPOSAL = "ORCHESTRATOR_PROPOSAL"

# ── Outcome feedback (post-trade learning loop) ─────────────────────────────
# One event per closed trade linking the realised R back to the modules /
# opportunity that drove the entry, so per-module accuracy can be measured.
OUTCOME_FEEDBACK = "OUTCOME_FEEDBACK"

# ── Position health (orchestrator live-management round table) ───────────────
# One event per open-position management evaluation: the re-evaluated evidence,
# the per-dimension health multipliers, the overall health score, the graded
# management action and how it changed since entry. Powers the dashboard's
# Position Health panel (health-over-time, dimension breakdown, action log).
POSITION_HEALTH = "POSITION_HEALTH"

