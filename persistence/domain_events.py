"""
APEX TRADER — Domain Event Type Constants
Typed string constants for structured domain events emitted to the EventStore.
Prevents stringly-typed drift across modules.
"""

# ── Decision events ──────────────────────────────────────────────────────────
DECISION_REJECT = "DECISION_REJECT"

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

# ── Safety events ──────────────────────────────────────────────────────────
BALANCE_UNAVAILABLE = "BALANCE_UNAVAILABLE"

# ── Backfill (Phase 6) ─────────────────────────────────────────────────────
TRADE_CLOSE_DERIVED = "TRADE_CLOSE_DERIVED"
