"""
APEX TRADER — Order Idempotency
Generates deterministic per-intent keys so the same order is never
submitted twice — even across retries, reconnects, or crash recovery.
"""

import hashlib
from datetime import datetime, timezone


_KEY_WINDOW_SECONDS = 300


def generate_idempotency_key(
    symbol: str,
    direction: str,
    lots: float,
    signal_time: datetime | None = None,
) -> str:
    """Return a deterministic 12-char hex key for an order intent.

    The same (symbol, direction, lots) within a 5-minute window produces
    the **same** key, guaranteeing that retries of the same intent are
    deduplicated while distinct intents always get distinct keys.
    """
    ts = signal_time or datetime.now(timezone.utc)
    epoch = int(ts.timestamp())
    bucket = epoch // _KEY_WINDOW_SECONDS

    raw = f"{symbol}|{direction.upper()}|{lots:.4f}|{bucket}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return digest


def extract_idempotency_key(comment: str) -> str | None:
    """Parse an APEX order comment and return the embedded idem key, if any.

    Comment format: ``APEX|<score>|<session>|<idem_key>``
    Legacy format (no key): ``APEX|<score>|<session>``
    """
    parts = comment.split("|")
    if len(parts) >= 4 and parts[0] in ("APEX", "APEX_PEND"):
        return parts[3]
    return None
