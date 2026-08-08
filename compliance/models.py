"""APEX TRADER — Compliance Division input models.

Small immutable carriers describing *what* is being permitted, the current
*book* (open positions), and the *account* it would land on.  Keeping these
as explicit inputs (rather than reaching into global state) makes the
Compliance Division pure and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ComplianceCandidate:
    """The prospective trade being evaluated for permission."""

    symbol: str
    direction: str


@dataclass
class ComplianceBook:
    """The current open book the candidate would be added to."""

    open_positions: list[Any] = field(default_factory=list)


@dataclass
class ComplianceAccount:
    """The account the candidate would be sized against.

    ``account_key`` is the risk-silo key (``broker:account_id``) used by the
    per-account daily-loss / heat silos.  ``balance`` is the account balance
    in account currency (0.0 is a valid post-margin-call state, not "missing").
    """

    account_key: str = ""
    balance: float = 0.0
