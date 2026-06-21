"""The verdict the Portfolio Division returns for a candidate trade."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Exposure verdict values.
EXPOSURE_FULL = "FULL"          # fits the book at full computed size
EXPOSURE_REDUCED = "REDUCED"    # fits, but size was haircut to respect a budget
EXPOSURE_REJECTED = "REJECTED"  # does not fit the book at any size


@dataclass
class PortfolioVerdict:
    """Result of ``PortfolioDivision.evaluate``.

    ``approved`` is the single source of truth for the caller: when ``False`` the
    trade must not be sent (``approved_size`` will be 0). ``rationale`` lists every
    factor and adjustment applied so a trade's size can be fully reconstructed.
    """

    approved: bool
    approved_size: float            # lots (MT5) or stake USD (Deriv)
    sizing_mode: str = "lots"       # "lots" | "stake"
    risk_pct: float = 0.0           # effective per-trade risk fraction
    max_loss: float = 0.0           # dollar max loss at the approved size
    exposure_verdict: str = EXPOSURE_FULL
    combined_mult: float = 1.0      # clamped product of all sizing factors
    reason: str = ""                # populated when not approved
    rationale: list[str] = field(default_factory=list)
    portfolio_impact: dict[str, Any] = field(default_factory=dict)

    @property
    def lots(self) -> float:
        return self.approved_size if self.sizing_mode != "stake" else 0.0

    @property
    def stake_usd(self) -> float:
        return self.approved_size if self.sizing_mode == "stake" else 0.0

    @classmethod
    def rejected(cls, reason: str, *, rationale: list[str] | None = None,
                 exposure_verdict: str = EXPOSURE_REJECTED) -> "PortfolioVerdict":
        return cls(
            approved=False,
            approved_size=0.0,
            reason=reason,
            exposure_verdict=exposure_verdict,
            rationale=rationale or [reason],
        )
