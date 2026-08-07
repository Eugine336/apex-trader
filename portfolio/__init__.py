"""
APEX TRADER — Portfolio Division.

The Portfolio Division owns capital allocation across the whole book. It answers
a single question: *"Given what we already hold, what size is appropriate for
this candidate and does it fit our exposure budget?"*

It is deliberately distinct from its neighbours in the organisation:

* **Consensus** decides *whether* to trade (direction + conviction).
* **Compliance** decides whether a trade is *permitted* (binary safety vetoes).
* **Portfolio** (this division) decides *how big* and whether it *fits the book*.
* **Execution** is the only layer that talks to brokers.

Portfolio never opens/closes trades, never talks to a broker, and never forms a
market opinion. It consumes the sizing factors the rest of the organisation has
already produced and folds them — transparently, every factor logged — into one
concrete position size, while enforcing the daily-loss budget and exposure
ceilings that the legacy ``RiskEngine.assess()`` chain used to own.
"""

from portfolio.division import PortfolioDivision
from portfolio.models import PortfolioAccount, PortfolioCandidate, SizingFactors
from portfolio.verdict import PortfolioVerdict

__all__ = [
    "PortfolioDivision",
    "PortfolioVerdict",
    "PortfolioCandidate",
    "PortfolioAccount",
    "SizingFactors",
]
