"""
APEX TRADER — Order Idempotency
Generates deterministic per-intent keys so the same order is never
submitted twice — even across retries, reconnects, or crash recovery.
"""

import hashlib
import re
from datetime import datetime, timezone


_KEY_WINDOW_SECONDS = 300

_MT5_COMMENT_MAX = 31

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_DELIM = "_"

_VALID_PREFIXES = ("APEX", "APND")

_LEGACY_PREFIXES = ("APEX", "APEX_PEND")


def _sanitize(value: str) -> str:
    """Strip non-alphanumeric chars (except . - _) to produce MT5-safe text."""
    return _SAFE_RE.sub("", value)


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


def build_order_comment(
    prefix: str,
    idem_key: str,
    score: int | float | None = None,
    session: str | None = None,
) -> str:
    """Build an MT5-safe order comment that fits within 31 chars.

    Layout: ``<PREFIX>-<idem_key>-<score>-<session>``

    The idem_key occupies a fixed position (field 1) and is NEVER
    truncated.  Score and session are sanitized and trimmed from the
    right if the total would exceed 31 chars.

    Prefixes:
        APEX  — market / generic orders
        APND  — pending limit / stop orders
    """
    pfx = _sanitize(prefix)[:4] or "APEX"
    key = _sanitize(idem_key)[:12]

    fixed = f"{pfx}{_DELIM}{key}"
    budget = _MT5_COMMENT_MAX - len(fixed)

    tail = ""
    if score is not None and budget > 1:
        s = _sanitize(str(int(score)))
        candidate = f"{_DELIM}{s}"
        if len(candidate) <= budget:
            tail += candidate
            budget -= len(candidate)
        else:
            tail += candidate[:budget]
            budget = 0

    if session and budget > 1:
        s = _sanitize(str(session))
        candidate = f"{_DELIM}{s}"
        if len(candidate) <= budget:
            tail += candidate
        else:
            tail += candidate[:budget]

    result = f"{fixed}{tail}"
    return result[:_MT5_COMMENT_MAX]


def extract_idempotency_key(comment: str) -> str | None:
    """Parse an APEX order comment and return the embedded idem key.

    Current format: ``<PREFIX>-<idem_key>-...``
       where PREFIX is APEX or APND, idem_key is at index 1.

    Legacy format (pipe-delimited): ``<PREFIX>|<idem_key>|...``
       or ``<PREFIX>|<score>|<session>|<idem_key>``
       where PREFIX is APEX/APEX_PEND, idem_key is at index 1 or 3.
    """
    for delim in (_DELIM, "|"):
        parts = comment.split(delim)
        if len(parts) < 2:
            continue
        prefix = parts[0]
        if prefix in _VALID_PREFIXES and len(parts) >= 2 and len(parts[1]) == 12:
            return parts[1]
        if prefix in _LEGACY_PREFIXES and len(parts) >= 4:
            return parts[3]
    return None
