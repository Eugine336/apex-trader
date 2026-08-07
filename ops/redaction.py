"""Helpers for keeping personally-identifying / sensitive values out of logs.

Pure stdlib — no third-party imports — so it can be reused from any layer
(connectors, API, dashboard) and unit-tested in isolation.
"""

from __future__ import annotations

import re
from typing import Any

# Matches the broker account-id segment in REST paths such as
# ``/options/accounts/<account_id>/otp`` so it can be masked in logged URLs.
_ACCOUNTS_SEGMENT = re.compile(r"(/accounts/)([^/?#]+)")


def mask_account_id(account_id: Any, *, keep_last: int = 4) -> str:
    """Return a log-safe representation of a broker account identifier.

    Shows only the final ``keep_last`` characters, masking the rest with ``*``
    so account numbers never appear in full in log files. Short identifiers are
    fully masked to avoid leaking them via the visible tail.

    Examples::

        mask_account_id(52856215)  -> "****6215"
        mask_account_id("DOT92986948") -> "*******6948"
        mask_account_id("123") -> "***"
        mask_account_id(None) -> "unknown"
    """
    if account_id is None:
        return "unknown"
    text = str(account_id).strip()
    if not text:
        return "unknown"
    if keep_last <= 0 or len(text) <= keep_last:
        return "*" * len(text)
    return "*" * (len(text) - keep_last) + text[-keep_last:]


def redact_account_in_url(url: Any) -> str:
    """Mask the account-id path segment inside a broker REST URL.

    ``.../accounts/DOT92986948/otp`` becomes ``.../accounts/*******6948/otp``
    so account identifiers never leak through logged request URLs.
    """
    text = str(url)
    return _ACCOUNTS_SEGMENT.sub(
        lambda m: m.group(1) + mask_account_id(m.group(2)), text
    )

