"""APEX TRADER — Live market snapshot for the Brain (Constitution Part XIX Art 2).

The Brain must observe reality like a trader looking at the MT5 chart — not a
bag of pre-digested module verdicts. This reconstructs a compact, information-
dense picture of the actual market for one symbol: recent OHLC candles across
several timeframes (the price action / shape), the live price and spread, and,
when managing, the Brain's own open position (live P&L in R). It becomes one
structured :class:`~cognition.contracts.Evidence` so the reasoner literally
sees the chart alongside the analytical reads.

Pure and stdlib-only (candles are passed in as plain rows, not a DataFrame) so
the reconstruction is fully offline-testable; the execution layer supplies the
raw price rows / tick / position.
"""

from __future__ import annotations

from typing import Any, Optional

from cognition.contracts import Evidence, EvidenceDomain


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _decimals_for(price: float) -> int:
    """A sensible rounding precision from price magnitude (compact output)."""
    p = abs(price)
    if p >= 100.0:
        return 2
    if p >= 1.0:
        return 4
    return 6


def _normalize_bars(bars: Any) -> "list[tuple[float, float, float, float]]":
    """Coerce mixed bar shapes → chronological list of (open, high, low, close)."""
    out: list[tuple[float, float, float, float]] = []
    for b in list(bars or []):
        try:
            if isinstance(b, dict):
                o = _f(b.get("open", b.get("o")))
                h = _f(b.get("high", b.get("h")))
                lo = _f(b.get("low", b.get("l")))
                c = _f(b.get("close", b.get("c")))
            else:
                seq = list(b)
                o, h, lo, c = _f(seq[0]), _f(seq[1]), _f(seq[2]), _f(seq[3])
            if o > 0 or h > 0 or c > 0:
                out.append((o, h, lo, c))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _trend(rows: "list[tuple[float, float, float, float]]") -> str:
    if len(rows) < 2:
        return "flat"
    first_close = rows[0][3]
    last_close = rows[-1][3]
    if first_close <= 0:
        return "flat"
    change = (last_close - first_close) / first_close
    # A move under ~0.05% of price over the window reads as flat/ranging.
    if change > 0.0005:
        return "up"
    if change < -0.0005:
        return "down"
    return "flat"


def build_price_snapshot(
    symbol: str,
    candles_by_tf: dict,
    *,
    tick: Any = None,
    position: Any = None,
    max_bars: int = 8,
) -> dict:
    """Reconstruct a compact multi-timeframe market picture for ``symbol``.

    ``candles_by_tf`` maps a timeframe label → chronological bars (each a
    ``(o,h,l,c)`` sequence or an ``{open,high,low,close}`` dict). Returns a
    JSON-friendly dict with, per timeframe: last close, window high/low/range,
    change %, trend and the last ``max_bars`` OHLC bars; plus current
    bid/ask/spread and, when supplied, the live position's direction/entry/
    current/profit_r/hold. Pure and fail-safe.
    """
    max_bars = max(1, int(max_bars))
    tfs_out: dict = {}
    for tf, bars in (candles_by_tf or {}).items():
        rows = _normalize_bars(bars)[-max_bars:]
        if not rows:
            continue
        last = rows[-1][3]
        dp = _decimals_for(last)
        highs = [r[1] for r in rows if r[1] > 0]
        lows = [r[2] for r in rows if r[2] > 0]
        hi = max(highs) if highs else last
        lo = min(lows) if lows else last
        first_ref = rows[0][0] or rows[0][3]
        change_pct = ((last - first_ref) / first_ref * 100.0) if first_ref else 0.0
        tfs_out[str(tf)] = {
            "last": round(last, dp),
            "high": round(hi, dp),
            "low": round(lo, dp),
            "range": round(hi - lo, dp),
            "change_pct": round(change_pct, 3),
            "trend": _trend(rows),
            "bars": [[round(o, dp), round(h, dp), round(low_, dp), round(c, dp)]
                     for (o, h, low_, c) in rows],
        }

    price: Optional[dict] = None
    if tick is not None:
        bid = _f(getattr(tick, "bid", 0.0))
        ask = _f(getattr(tick, "ask", 0.0))
        mid = _f(getattr(tick, "mid", 0.0)) or ((bid + ask) / 2.0 if bid and ask else 0.0)
        if bid or ask or mid:
            dp = _decimals_for(mid or bid or ask)
            price = {
                "bid": round(bid, dp), "ask": round(ask, dp),
                "mid": round(mid, dp), "spread": round(abs(ask - bid), dp),
            }

    pos: Optional[dict] = None
    if position is not None:
        direction = str(getattr(position, "direction", "") or "").upper()
        if direction:
            pr = getattr(position, "profit_r", None)
            pos = {
                "direction": direction,
                "profit_r": (round(_f(pr), 3) if pr is not None else None),
                "hold_seconds": round(_f(getattr(position, "hold_seconds", 0.0)), 1),
                "size": _f(getattr(position, "size", 0.0)),
            }

    return {"symbol": str(symbol or ""), "timeframes": tfs_out,
            "price": price, "position": pos}


def _net_trend_lean(tfs_out: dict) -> float:
    """Bounded directional lean (−1..+1) from multi-timeframe trend agreement."""
    if not tfs_out:
        return 0.0
    score = 0
    for v in tfs_out.values():
        t = v.get("trend")
        score += 1 if t == "up" else (-1 if t == "down" else 0)
    return max(-1.0, min(1.0, score / float(len(tfs_out))))


def snapshot_to_evidence(symbol: str, snapshot: dict) -> "list[Evidence]":
    """Turn a price snapshot into one multi-timeframe Evidence (the chart).

    Polarity is a *modest* multi-timeframe trend lean (price context, not a
    signal — the Brain still decides); the full snapshot rides in
    ``measurements`` so the reasoner sees the actual candles. Fail-safe: ``[]``
    on empty/any fault.
    """
    out: list[Evidence] = []
    try:
        snap = dict(snapshot or {})
        tfs = snap.get("timeframes") or {}
        if not tfs:
            return out
        lean = _net_trend_lean(tfs)
        agree = abs(lean)
        parts = []
        for tf, v in tfs.items():
            parts.append(f"{tf} {v.get('trend')} {v.get('change_pct')}%")
        price = snap.get("price") or {}
        spread_txt = f" spread {price.get('spread')}" if price else ""
        pos = snap.get("position") or {}
        pos_txt = ""
        if pos:
            pos_txt = f" | pos {pos.get('direction')} {pos.get('profit_r')}R"
        observation = (
            f"chart {symbol}: price {price.get('mid') if price else '?'}"
            f"{spread_txt} | " + ", ".join(parts) + pos_txt
        )[:300]
        out.append(Evidence(
            source_module="market.price_action",
            domain=EvidenceDomain.MULTI_TIMEFRAME, symbol=str(symbol or ""),
            observation=observation,
            confidence=round(0.4 + 0.5 * agree, 4),  # more agreement ⇒ clearer picture
            uncertainty=round(1.0 - agree, 4),
            polarity=round(lean * 0.4, 4),
            measurements=snap,
            relevance_horizon_seconds=90.0,
        ))
    except Exception:  # noqa: BLE001
        return out
    return out


__all__ = ["build_price_snapshot", "snapshot_to_evidence"]
