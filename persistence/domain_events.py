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
