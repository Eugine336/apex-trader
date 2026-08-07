"""APEX TRADER — Governance Division verdict.

The Governance Division (Department ⑧) is the only layer allowed to authorise a
change before it affects live behaviour.  Every authorisation request — a
Learning recommendation, a module promotion, a containment order — resolves to
one of three verdicts:

* ``AUTHORIZED`` — the change is approved and may take effect.
* ``REJECTED``   — the change is denied (fail-closed: this is also the verdict
  for an unknown request or an evaluation that could not be completed).
* ``DEFERRED``   — the change is not yet eligible (e.g. a promotion candidate
  that has not earned its next lifecycle stage).  ``DEFERRED`` is **not** an
  approval: like ``REJECTED`` it means "do not apply now", but it carries the
  distinct meaning "ask again once more evidence exists" rather than "denied".

Only ``AUTHORIZED`` is an approval (``approved`` is True); both ``REJECTED`` and
``DEFERRED`` withhold authorisation.
"""

from __future__ import annotations

from enum import Enum


class GovernanceVerdict(str, Enum):
    """The outcome of a Governance authorisation request (str-valued for
    trivial JSON / logging)."""

    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"

    @property
    def approved(self) -> bool:
        """True only for ``AUTHORIZED`` — the single verdict that permits a
        change to take effect.  ``REJECTED`` and ``DEFERRED`` both withhold."""
        return self is GovernanceVerdict.AUTHORIZED

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


__all__ = ["GovernanceVerdict"]
