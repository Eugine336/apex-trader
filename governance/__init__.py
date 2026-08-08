"""APEX TRADER — Governance Division.

Department 8 of the organizational architecture: the authorisation + containment
authority over the Learning Division.  Learning recommends; Governance
authorises.  No learner output mutates the live path until it passes through
:class:`governance.division.GovernanceDivision`.  See that module for the full
contract.
"""

from governance.division import GovernanceDivision
from governance.models import (
    AuthorizationRecord,
    PromotionRecord,
    PromotionStage,
    ToxicPairRecord,
)
from governance.verdict import GovernanceVerdict

__all__ = [
    "GovernanceDivision",
    "GovernanceVerdict",
    "PromotionStage",
    "AuthorizationRecord",
    "PromotionRecord",
    "ToxicPairRecord",
]
