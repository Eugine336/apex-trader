"""
APEX TRADER — Exit Attribution
Derives truthful exit reasons from broker deal data and TradeManager state.
Never fabricates a reason — when genuinely unavailable, records UNKNOWN with raw values.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


# MT5 DEAL_REASON enum values (from MQL5 documentation)
_MT5_DEAL_REASON_MAP = {
    0: "MANUAL",         # DEAL_REASON_CLIENT
    1: "MANUAL",         # DEAL_REASON_MOBILE
    2: "MANUAL",         # DEAL_REASON_WEB
    3: "ALGO",           # DEAL_REASON_EXPERT
    4: "SL",             # DEAL_REASON_SL
    5: "TP",             # DEAL_REASON_TP
    6: "STOP_OUT",       # DEAL_REASON_SO
    7: "ROLLOVER",       # DEAL_REASON_ROLLOVER
    8: "VMARGIN",        # DEAL_REASON_VMARGIN
    9: "SPLIT",          # DEAL_REASON_SPLIT
}


@dataclass
class ExitAttribution:
    exit_reason: str
    exit_reason_source: str  # "mt5_deal" | "trade_manager" | "unknown"
    raw_broker_reason: Optional[Any] = None
    raw_broker_comment: Optional[str] = None
    actual_fill_price: Optional[float] = None
    actual_fill_time: Optional[float] = None
    discrepancy: bool = False


def attribute_from_mt5_deals(deals) -> ExitAttribution:
    """Derive exit reason from MT5 history_deals_get result.

    Looks at the closing deal (entry == DEAL_ENTRY_OUT == 1) and maps
    its `reason` enum to a human-readable exit cause.
    """
    if deals is None or len(deals) == 0:
        return ExitAttribution(
            exit_reason="BROKER_CLOSED_UNKNOWN",
            exit_reason_source="unknown",
        )

    close_deal = None
    for d in deals:
        if getattr(d, "entry", None) == 1:  # DEAL_ENTRY_OUT
            close_deal = d
            break

    if close_deal is None:
        close_deal = deals[-1]

    raw_reason = getattr(close_deal, "reason", None)
    raw_comment = getattr(close_deal, "comment", None)
    fill_price = getattr(close_deal, "price", None)
    fill_time = getattr(close_deal, "time", None)

    mapped = _MT5_DEAL_REASON_MAP.get(raw_reason)
    if mapped is None:
        exit_reason = "BROKER_CLOSED_UNKNOWN"
    else:
        exit_reason = mapped

    return ExitAttribution(
        exit_reason=exit_reason,
        exit_reason_source="mt5_deal",
        raw_broker_reason=raw_reason,
        raw_broker_comment=str(raw_comment) if raw_comment is not None else None,
        actual_fill_price=float(fill_price) if fill_price is not None else None,
        actual_fill_time=float(fill_time) if fill_time is not None else None,
    )


def attribute_from_trade_manager(close_reason: Optional[str]) -> ExitAttribution:
    """Derive exit attribution from TradeManager's close_reason string."""
    if not close_reason:
        return ExitAttribution(
            exit_reason="CLOSED",
            exit_reason_source="trade_manager",
        )
    return ExitAttribution(
        exit_reason=close_reason,
        exit_reason_source="trade_manager",
    )


def attribute_unknown(platform: str) -> ExitAttribution:
    """Placeholder for platforms without exit-reason recovery (Deriv pending)."""
    # TODO: Deriv — call proposal_open_contract post-close to extract
    # the contract status ("sold"/"expired"/"won"/"lost") for true attribution.
    return ExitAttribution(
        exit_reason="BROKER_CLOSED_UNKNOWN",
        exit_reason_source="unknown",
        raw_broker_comment=f"platform={platform}, true-reason recovery pending",
    )


def reconcile(
    broker_attr: Optional[ExitAttribution],
    manager_attr: Optional[ExitAttribution],
) -> ExitAttribution:
    """Merge broker and manager attributions.

    Records both when available.  Flags a discrepancy if broker says SL/TP
    but manager says something else (or vice versa).
    """
    if broker_attr is None and manager_attr is None:
        return ExitAttribution(
            exit_reason="UNKNOWN",
            exit_reason_source="unknown",
        )

    if broker_attr is None:
        return manager_attr

    if manager_attr is None:
        return broker_attr

    primary = broker_attr
    sl_tp_set = {"SL", "TP"}
    broker_is_sl_tp = primary.exit_reason in sl_tp_set
    manager_is_sl_tp = manager_attr.exit_reason in ("Stop loss hit", "TP2 hit", "Stopped at breakeven")

    if broker_is_sl_tp != manager_is_sl_tp and manager_attr.exit_reason != "CLOSED":
        primary.discrepancy = True

    return ExitAttribution(
        exit_reason=primary.exit_reason,
        exit_reason_source=primary.exit_reason_source,
        raw_broker_reason=primary.raw_broker_reason,
        raw_broker_comment=primary.raw_broker_comment,
        actual_fill_price=primary.actual_fill_price,
        actual_fill_time=primary.actual_fill_time,
        discrepancy=primary.discrepancy,
    )
