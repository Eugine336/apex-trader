"""APEX TRADER — Broker-truth field readers (extracted from the bootstrap).

Phase K (Constitution Part XI — modular design): these defensive readers were
defined inline in the 11k-line ``event_driven_bootstrap.py``. They are pure,
self-contained, and shared across the sizing path, the scale-in path and the
portfolio heat monitor, so they are the natural first cluster to lift into a
named, unit-tested module. Behaviour is unchanged — the bootstrap re-imports
these names, so every existing call site (and the existing import path
``from event_driven_bootstrap import _broker_pip_value_from_spec``) resolves
identically.

Open positions returned by the platform layer are broker ``PositionInfo``
objects (fields: ``pnl``, ``lots``, ``open_price``, ``current_price``, ``sl``,
``tp``, ``swap``). Older call sites read legacy attribute names (``profit``,
``entry_price``, ``tp1``/``tp2``) that do not exist on ``PositionInfo`` and so
silently resolved to defaults. These helpers prefer the broker-reported field
and fall back to the legacy name so both real broker objects and any
legacy/test doubles resolve correctly.
"""

from __future__ import annotations

from loguru import logger


def _broker_pnl(pos) -> float:
    """Broker-reported P&L for an open position (falls back to legacy)."""
    v = getattr(pos, "pnl", None)
    if v is None:
        v = getattr(pos, "profit", None)
    if v is None:
        v = getattr(pos, "broker_pnl", None)
    return float(v) if v is not None else 0.0


def _broker_entry_price(pos, default: float = 0.0) -> float:
    """Broker open price for a position (falls back to legacy ``entry_price``)."""
    v = getattr(pos, "open_price", None)
    if not v:
        v = getattr(pos, "entry_price", None)
    return float(v) if v else float(default)


def _broker_tp(pos) -> float:
    """Broker take-profit for a position (single broker TP == tp1)."""
    v = getattr(pos, "tp", None)
    if not v:
        v = getattr(pos, "tp1", None)
    return float(v) if v else 0.0


def _broker_pip_value_from_spec(
    spec: dict, pip_size: float, fallback: float,
) -> float:
    """Broker-truth money-per-pip-per-lot derived from a symbol-spec dict.

    Computed as ``trade_tick_value * (pip_size / trade_tick_size)``. Returns
    *fallback* (the config-registry value) on any gap so sizing / heat / P&L
    never break.

    This is the single source of truth for the broker pip-value override. Every
    pip-value site — the sizing path, the scale-in path and the portfolio heat
    monitor — routes through it so the risk (heat) view and the sizing view of a
    position can never disagree on money-per-pip. A wrong registry pip_value
    (e.g. the 1.0 forex-scale placeholder on a sub-$10 crypto) diverging between
    those views is what produced phantom EMERGENCY force-closes.
    """
    try:
        tick_value = spec.get("trade_tick_value") if spec else None
        tick_size = spec.get("trade_tick_size") if spec else None
        if (
            tick_value and tick_size and tick_size > 0
            and pip_size and pip_size > 0
        ):
            pv = float(tick_value) * (float(pip_size) / float(tick_size))
            if pv > 0:
                return pv
    except Exception as exc:
        logger.debug("[symbol-spec] pip-value derive failed: {}", exc)
    return fallback


__all__ = [
    "_broker_pnl",
    "_broker_entry_price",
    "_broker_tp",
    "_broker_pip_value_from_spec",
]
