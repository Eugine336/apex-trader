"""APEX TRADER — Structure & M1 micro-context readers (extracted from the bootstrap).

Phase K (Constitution Part XI — modular design): these helpers read a
WorldModel's ``structure_by_tf()`` layer and live M1 micro-structure, turning
them into the small, defensive reads the DecisionEngine entry/management context
consumes. They were defined inline in the 11k-line ``event_driven_bootstrap.py``;
lifting them into a named, unit-tested module is the second behaviour-preserving
decomposition slice. The bootstrap re-imports these names, so every call site
(and the existing ``from event_driven_bootstrap import _struct_swings`` /
``_micro_confirmation_from_event`` import paths used by the tests) resolves
identically — behaviour unchanged.

Pure standard library at import time (the ``_compute_m1_micro`` reader imports
its ``brain``/``entry`` collaborators lazily, inside the function, so this module
stays lightweight to import and is fully fail-safe: a missing/short feed returns
the safe defaults and never raises).
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger


def _struct_trend_conf(struct_by_tf: dict, tf: str) -> tuple[str, float]:
    """Read ``(trend, confidence)`` for a timeframe from a WorldModel's
    ``structure_by_tf()`` mapping of ``StructureAnalysis`` objects.

    Returns ``("UNKNOWN", 0.0)`` when the timeframe is absent.  Centralises
    the correct way to read the WorldModel's structure layer so consumers
    never treat it as a dict-of-dicts.
    """
    sa = struct_by_tf.get(tf)
    if sa is None:
        return "UNKNOWN", 0.0
    trend = sa.trend.value if hasattr(sa.trend, "value") else str(sa.trend)
    return trend, float(getattr(sa, "confidence", 0.0) or 0.0)


def _struct_event(struct_by_tf: dict, tf: str) -> str:
    """Read the last structural event (BOS/CHOCH) for a timeframe from a
    WorldModel's ``structure_by_tf()`` mapping of ``StructureAnalysis``.

    Returns ``"NONE"`` when the timeframe is absent or has no event.  The
    DecisionEngine's structure-integrity dimension keys off these BOS/CHOCH
    strings (``BOS_BEARISH``/``CHOCH_BULLISH``/…) to tell whether market
    structure has broken for or against an open position.
    """
    sa = struct_by_tf.get(tf)
    if sa is None:
        return "NONE"
    ev = getattr(sa, "last_event", None)
    if ev is None:
        return "NONE"
    return ev.value if hasattr(ev, "value") else str(ev)


def _struct_swings(struct_by_tf: dict, tf: str) -> tuple[Optional[float], Optional[float]]:
    """Read ``(swing_high, swing_low)`` for a timeframe from a WorldModel's
    ``structure_by_tf()`` mapping of ``StructureAnalysis`` objects.

    Returns ``(None, None)`` when the timeframe is absent.  Feeds the
    DecisionEngine's structure-based protective stop for adopted trades, whose
    candidate swing-level lists were always empty (and so the stop never placed
    a level) because neither management builder populated these.
    """
    sa = struct_by_tf.get(tf)
    if sa is None:
        return None, None
    return getattr(sa, "swing_high", None), getattr(sa, "swing_low", None)


def _compute_m1_micro(pm, symbol: str, norm_dir: str, pip_size: float) -> dict:
    """Live M1 momentum (aligned count + micro-structure event/trend) for one
    symbol/direction.

    Shared by the in-trade management micro-context AND the entry-side
    DecisionEngine context so BOTH planes read the SAME live M1 evidence
    instead of a static default (the entry plane previously pinned
    ``m1_aligned_count`` at 3, ``m1_event`` at ``""`` and ``m1_trend`` at
    ``"UNKNOWN"`` because the orchestrator decision dict never carried them).

    Reads go through the cached ``fetch_market_data`` (the M1@100 key the
    entry/analysis planes already warm), so no extra broker round-trip is
    added.  Returns the EntryContext/TradeContext safe defaults when data is
    unavailable, so a missing/short feed never changes behaviour or raises.
    """
    out: dict[str, Any] = {
        "m1_aligned_count": 0,
        "m1_event": "NONE",
        "m1_trend": "UNKNOWN",
        "m1_pattern": "",
    }
    is_long = norm_dir.upper() in ("BUY", "LONG")
    try:
        from brain.market_data_utils import drop_forming_bar
        from brain.structure_engine import StructureEngine
        from entry.m1_patterns import detect_m1_pattern

        m1_data = pm.fetch_market_data(symbol, ["M1"], 100)
        m1_df = m1_data.get("M1") if m1_data else None
        if m1_df is not None and len(m1_df) >= 5:
            closed = drop_forming_bar(m1_df)
            if closed is not None and len(closed) >= 5:
                last5 = closed.iloc[-5:]
                closes = last5["close"].values
                opens = last5["open"].values
                if is_long:
                    aligned = sum(1 for c, o in zip(closes, opens) if c > o)
                else:
                    aligned = sum(1 for c, o in zip(closes, opens) if c < o)
                out["m1_aligned_count"] = int(aligned)
                out["m1_pattern"] = detect_m1_pattern(closed, is_long)
                try:
                    engine = StructureEngine(swing_lookback=3, pip_size=pip_size)
                    analysis = engine.analyze(closed.iloc[-min(len(closed), 100):])
                    out["m1_event"] = analysis.last_event.value
                    out["m1_trend"] = analysis.trend.value
                except Exception as exc:
                    logger.warning(
                        "[m1-micro] M1 structure read failed for {}: {}",
                        symbol, exc,
                    )
    except Exception as exc:
        logger.debug("[m1-micro] M1 momentum read failed for {}: {}", symbol, exc)
    return out


def _micro_confirmation_from_event(
    m1_event: str, direction: str, m1_pattern: str = "",
) -> tuple[str, str]:
    """Derive ``(micro_confirmation, entry_mode)`` from live M1 evidence.

    An M1 BOS/CHoCH aligned with the trade direction is a market-confirmation
    trigger, so the DecisionEngine's MARKET fast-path becomes reachable instead
    of every entry defaulting to PENDING.  When no structural event confirms,
    an aligned M1 candle pattern (engulfing / pin bar from
    ``entry.m1_patterns.detect_m1_pattern``) also confirms the entry — so the
    ``micro_confirmation`` field and the MARKET path are no longer reachable
    only via BOS/CHoCH.  Returns ``("", "PENDING")`` when nothing confirms
    (unchanged behaviour).  This only affects the entry-action label (MARKET vs
    PENDING); both still enter, and the reversal-evidence terms read
    ``m1_event``/``micro_confirmation`` directly.
    """
    ev = str(m1_event or "").upper()
    is_long = direction.upper() in ("BUY", "LONG")
    aligned = ("BULLISH" in ev) if is_long else ("BEARISH" in ev)
    if ("BOS" in ev or "CHOCH" in ev) and aligned:
        return "choch_bos", "MARKET"
    pat = str(m1_pattern or "").strip().lower()
    if pat in ("engulfing", "pin_bar"):
        return pat, "MARKET"
    return "", "PENDING"


__all__ = [
    "_struct_trend_conf",
    "_struct_event",
    "_struct_swings",
    "_compute_m1_micro",
    "_micro_confirmation_from_event",
]
