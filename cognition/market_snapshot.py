"""APEX TRADER — Live market snapshot for the Brain (Constitution Part XIX Art 2).

The Brain must observe reality like a trader looking at the MT5 chart — not a
bag of pre-digested module verdicts. This reconstructs a compact, information-
dense picture of the actual market for one symbol so the reasoner sees what a
real trader sees, end to end:

* recent OHLC candles across several timeframes (the price action / shape),
* the live price and spread,
* tick-level **microstructure** — velocity, drift, up/down tick balance,
  momentum and spread behaviour over the last N ticks (the tape),
* **order-book depth** — top-of-book and bid/ask volume imbalance when the
  feed provides Level-2 (the DOM),
* the **session** (Asian / London / New York / overlap) and whether it is a
  high-liquidity window,
* a **pullback read** — higher-timeframe trend vs the lower-timeframe move, so
  a dip within an uptrend (or a bounce within a downtrend) is legible, and,
* when managing, the Brain's own open position (live P&L in R).

It all becomes one structured :class:`~cognition.contracts.Evidence` so the
reasoner literally sees the chart, the tape and the book alongside the
analytical reads. The endgame is a real market; a demo feed that omits depth is
no reason not to reconstruct it — depth simply reads empty until the feed
supplies it, and every field is fail-open.

Pure and stdlib-only (candles/ticks/depth are passed in as plain rows, not
DataFrames) so the reconstruction is fully offline-testable; the execution
layer supplies the raw price rows / tick / ticks / depth / position.
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


# Timeframe → seconds, for ranking which reads are "context" vs "micro".
_TF_SECONDS = {
    "MN1": 2592000, "W1": 604800, "D1": 86400, "H12": 43200, "H8": 28800,
    "H6": 21600, "H4": 14400, "H3": 10800, "H2": 7200, "H1": 3600,
    "M30": 1800, "M20": 1200, "M15": 900, "M10": 600, "M5": 300,
    "M4": 240, "M3": 180, "M2": 120, "M1": 60, "S30": 30, "S10": 10,
}


def _tf_rank(label: str) -> int:
    return _TF_SECONDS.get(str(label or "").upper(), 0)


# Violation #3 — the tape is now ~30-40s deep (600 ticks), so microstructure
# reads the DEVELOPING story across several scales, not just the last instant.
# These trailing windows (seconds) are summarised individually so the Brain can
# tell a fresh 2s twitch apart from a 30s momentum transition.
_MICRO_WINDOWS: "tuple[tuple[str, float], ...]" = (
    ("2s", 2.0), ("10s", 10.0), ("30s", 30.0),
)


def _momentum_of(mids: "list[float]") -> "tuple[int, int, float]":
    """Up/down tick counts and a bounded momentum score for a mid series."""
    up = down = 0
    for a, b in zip(mids, mids[1:]):
        if b > a:
            up += 1
        elif b < a:
            down += 1
    moves = up + down
    momentum = (up - down) / float(moves) if moves else 0.0
    return up, down, momentum


def _segment_speed(seg: "list[tuple[float, float, float]]", *, have_time: bool) -> float:
    """Absolute price speed of a segment — price move per second (when
    timestamps are present) or per tick (index fallback). Direction-agnostic;
    it measures how FAST price is moving, not which way."""
    if len(seg) < 2:
        return 0.0
    move = abs(seg[-1][1] - seg[0][1])
    if have_time:
        span = seg[-1][0] - seg[0][0]
        return (move / span) if span > 0 else 0.0
    return move / float(len(seg) - 1)


def _segment_tick_rate(
    seg: "list[tuple[float, float, float]]", *, have_time: bool,
) -> Optional[float]:
    """Ticks/sec within a segment (only meaningful with real timestamps)."""
    if not have_time or len(seg) < 2:
        return None
    span = seg[-1][0] - seg[0][0]
    return (len(seg) / span) if span > 0 else None


def _enrich_microstructure(
    rows: "list[tuple[float, float, float]]",
    out: dict,
    *,
    have_time: bool,
    dp: int,
) -> None:
    """Augment the base microstructure dict with multi-scale, velocity-trend,
    spread-trend, tick-rate and exhaustion/absorption reads (Violation #3).

    Mutates ``out`` in place. Additive only — never removes or rewrites a base
    key, so every existing consumer keeps working. Fail-safe at the call site.
    """
    last_ep = rows[-1][0]

    # ── Multi-scale momentum / drift / velocity over trailing windows ──────
    if have_time:
        mom_w: dict = {}
        drift_w: dict = {}
        vel_w: dict = {}
        for label, w in _MICRO_WINDOWS:
            sub = [r for r in rows if r[0] >= last_ep - w]
            if len(sub) < 2:
                continue
            sub_mids = [r[1] for r in sub]
            _, _, m = _momentum_of(sub_mids)
            mom_w[label] = round(m, 3)
            drift_w[label] = round(sub_mids[-1] - sub_mids[0], dp)
            sub_span = sub[-1][0] - sub[0][0]
            denom = sub_span if sub_span > 0 else w
            vel_w[label] = round(len(sub) / denom, 2) if denom > 0 else None
        if mom_w:
            out["momentum_windows"] = mom_w
            out["drift_windows"] = drift_w
            out["velocity_windows"] = vel_w

    # ── Early vs late halves — velocity accel, spread & tick-rate trend ────
    n = len(rows)
    early: "list[tuple[float, float, float]]" = []
    late: "list[tuple[float, float, float]]" = []
    if have_time:
        mid_ep = (rows[0][0] + last_ep) / 2.0
        early = [r for r in rows if r[0] <= mid_ep]
        late = [r for r in rows if r[0] > mid_ep]
    if len(early) < 2 or len(late) < 2:
        # Index split fallback (also used when timestamps are absent).
        half = max(1, n // 2)
        early, late = rows[:half], rows[half:]

    early_speed = _segment_speed(early, have_time=have_time)
    late_speed = _segment_speed(late, have_time=have_time)
    accel = late_speed - early_speed
    out["velocity_accel"] = round(accel, dp + 2)
    if early_speed > 0:
        ratio = late_speed / early_speed
        if ratio >= 1.25:
            v_trend = "accelerating"
        elif ratio <= 0.8:
            v_trend = "decelerating"
        else:
            v_trend = "steady"
    else:
        v_trend = "accelerating" if late_speed > 0 else "steady"
    out["velocity_trend"] = v_trend

    # Spread trend — widening / narrowing over the window (a pattern, not a
    # single last-vs-mean snapshot).
    early_spreads = [r[2] for r in early if r[2] > 0]
    late_spreads = [r[2] for r in late if r[2] > 0]
    if early_spreads and late_spreads:
        e_sp = sum(early_spreads) / len(early_spreads)
        l_sp = sum(late_spreads) / len(late_spreads)
        if e_sp > 0:
            sp_ratio = l_sp / e_sp
            out["spread_trend_ratio"] = round(sp_ratio, 3)
            out["spread_trend"] = (
                "widening" if sp_ratio >= 1.2
                else "narrowing" if sp_ratio <= 0.83
                else "steady"
            )

    # Tick-rate change — a sudden burst or a sudden quiet vs earlier.
    early_rate = _segment_tick_rate(early, have_time=have_time)
    late_rate = _segment_tick_rate(late, have_time=have_time)
    if early_rate and late_rate and early_rate > 0:
        tr_ratio = late_rate / early_rate
        out["tick_rate_ratio"] = round(tr_ratio, 3)
        out["tick_rate_change"] = (
            "burst" if tr_ratio >= 1.5
            else "quiet" if tr_ratio <= 0.67
            else "steady"
        )

    # ── Exhaustion & absorption — the two classic tape transitions ─────────
    momentum = float(out.get("momentum", 0.0) or 0.0)
    rng = float(out.get("range", 0.0) or 0.0)
    spread_mean = float(out.get("spread_mean", 0.0) or 0.0)
    # Exhaustion: price is still making a directional move, but the speed of
    # that move is fading (decelerating) — a move running out of fuel.
    exhaustion = bool(abs(momentum) >= 0.3 and v_trend == "decelerating")
    out["exhaustion"] = exhaustion
    out["exhaustion_score"] = (
        round(min(1.0, abs(momentum) * (1.0 - (late_speed / early_speed)))
              if (exhaustion and early_speed > 0) else 0.0, 3)
    )
    # Absorption: lots of tick activity but price is pinned (tiny range vs the
    # typical spread and weak net momentum) — orders being absorbed at a level.
    contained = bool(spread_mean > 0 and rng <= spread_mean * 3.0)
    active = n >= 10
    absorption = bool(active and contained and abs(momentum) < 0.2)
    out["absorption"] = absorption
    out["absorption_score"] = (
        round(min(1.0, (1.0 - abs(momentum)) * (n / 60.0)) if absorption else 0.0, 3)
    )


def build_microstructure(ticks: Any, *, now_epoch: Optional[float] = None) -> dict:
    """Summarize the recent tape (tick stream) into compact microstructure.

    ``ticks`` is an iterable of tick-like objects exposing ``mid`` (or
    ``bid``/``ask``), ``spread`` and ``epoch`` (seconds). Returns velocity
    (ticks/sec), signed drift, up/down tick balance, a bounded momentum score,
    the price range and spread behaviour (last/mean/max + a widening flag).

    Violation #3 — with the deeper ~30-40s tape it additionally layers on
    multi-scale reads (all keys are additive; the base keys above are
    unchanged): ``momentum_windows`` / ``drift_windows`` / ``velocity_windows``
    (2s/10s/30s trailing summaries), ``velocity_accel`` + ``velocity_trend``
    (is the move speeding up or fading?), ``spread_trend`` (+ ratio),
    ``tick_rate_change`` (+ ratio, a sudden burst or quiet), and the two classic
    tape transitions ``exhaustion`` (directional move that is decelerating) and
    ``absorption`` (heavy tick activity but price pinned at a level), each with a
    bounded score. Pure and fail-safe: ``{}`` when fewer than two usable ticks
    are present.
    """
    try:
        rows: list[tuple[float, float, float]] = []
        for t in list(ticks or []):
            bid = _f(getattr(t, "bid", 0.0))
            ask = _f(getattr(t, "ask", 0.0))
            mid = _f(getattr(t, "mid", 0.0)) or (
                (bid + ask) / 2.0 if bid and ask else (bid or ask)
            )
            if mid <= 0:
                continue
            spread = _f(getattr(t, "spread", 0.0))
            if spread <= 0 and bid and ask:
                spread = abs(ask - bid)
            ep = _f(getattr(t, "epoch", 0.0))
            rows.append((ep, mid, spread))
        if len(rows) < 2:
            return {}
        # Order oldest → newest when timestamps are available.
        if all(r[0] > 0 for r in rows):
            rows.sort(key=lambda r: r[0])
        mids = [r[1] for r in rows]
        spreads = [r[2] for r in rows if r[2] > 0]
        first_mid, last_mid = mids[0], mids[-1]
        dp = _decimals_for(last_mid)
        up = down = 0
        for a, b in zip(mids, mids[1:]):
            if b > a:
                up += 1
            elif b < a:
                down += 1
        moves = up + down
        momentum = (up - down) / float(moves) if moves else 0.0
        drift = last_mid - first_mid
        drift_pct = (drift / first_mid * 100.0) if first_mid else 0.0
        rng = max(mids) - min(mids)
        first_ep, last_ep = rows[0][0], rows[-1][0]
        span = (last_ep - first_ep) if (first_ep > 0 and last_ep > first_ep) else 0.0
        if span <= 0 and now_epoch and first_ep > 0:
            span = _f(now_epoch) - first_ep
        velocity_tps = round(len(rows) / span, 2) if span > 0 else None
        spread_last = spreads[-1] if spreads else 0.0
        spread_mean = (sum(spreads) / len(spreads)) if spreads else 0.0
        spread_max = max(spreads) if spreads else 0.0
        widening = bool(spread_mean > 0 and spread_last > spread_mean * 1.2)
        out = {
            "ticks": len(rows),
            "window_seconds": round(span, 1) if span > 0 else None,
            "velocity_tps": velocity_tps,
            "drift": round(drift, dp),
            "drift_pct": round(drift_pct, 4),
            "up_ticks": up,
            "down_ticks": down,
            "momentum": round(momentum, 3),
            "range": round(rng, dp),
            "spread_last": round(spread_last, dp),
            "spread_mean": round(spread_mean, dp),
            "spread_max": round(spread_max, dp),
            "spread_widening": widening,
        }
        # Violation #3 — layer the multi-scale reads on top of the base dict in
        # a nested guard so any enrichment fault leaves the (backward-compatible)
        # base untouched.
        try:
            have_time = all(r[0] > 0 for r in rows)
            _enrich_microstructure(rows, out, have_time=have_time, dp=dp)
        except Exception:  # noqa: BLE001 — enrichment must never drop the base read
            pass
        return out
    except Exception:  # noqa: BLE001
        return {}


def summarize_depth(levels: Any) -> Optional[dict]:
    """Summarize Level-2 order-book depth into top-of-book + volume imbalance.

    ``levels`` is a list of ``{price, volume, side}`` where ``side`` is
    ``"bid"`` or ``"ask"``. Returns best bid/ask, aggregate bid/ask volume and
    a bounded imbalance (``+`` = bid-heavy / buy pressure). Returns ``None``
    when depth is empty — most demo/retail feeds do not publish a book, which
    is expected, not an error.
    """
    try:
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for lv in list(levels or []):
            if isinstance(lv, dict):
                price = _f(lv.get("price"))
                vol = _f(lv.get("volume"))
                side = str(lv.get("side", "") or "").lower()
            else:
                continue
            if price <= 0:
                continue
            if side.startswith("b"):
                bids.append((price, vol))
            elif side.startswith("a") or side.startswith("s"):
                asks.append((price, vol))
        if not bids and not asks:
            return None
        top_bid = max((p for p, _ in bids), default=None)
        top_ask = min((p for p, _ in asks), default=None)
        bid_vol = sum(v for _, v in bids)
        ask_vol = sum(v for _, v in asks)
        total = bid_vol + ask_vol
        imbalance = ((bid_vol - ask_vol) / total) if total > 0 else 0.0
        ref = top_bid or top_ask or 0.0
        dp = _decimals_for(ref)
        out: dict = {
            "levels": len(bids) + len(asks),
            "bid_vol": round(bid_vol, 4),
            "ask_vol": round(ask_vol, 4),
            "imbalance": round(imbalance, 3),
        }
        if top_bid is not None:
            out["top_bid"] = round(top_bid, dp)
        if top_ask is not None:
            out["top_ask"] = round(top_ask, dp)
        return out
    except Exception:  # noqa: BLE001
        return None


def session_context(epoch: Optional[float]) -> dict:
    """Classify the trading session from a UTC epoch (fail-safe ``{}``).

    Windows are approximate UTC: Asian < 07:00, London 07–12, London/NY overlap
    12–16 (deepest liquidity), New York 16–21, then a thin late-US tail. The
    ``high_liquidity`` flag flags the windows where spreads are typically
    tightest — useful context, never a hardcoded rule.
    """
    try:
        e = _f(epoch)
        if e <= 0:
            return {}
        h = int((e // 3600) % 24)
        if h < 7:
            session, liquid = "asian", False
        elif h < 12:
            session, liquid = "london", True
        elif h < 16:
            session, liquid = "london_ny_overlap", True
        elif h < 21:
            session, liquid = "newyork", True
        else:
            session, liquid = "late_us", False
        return {"utc_hour": h, "session": session, "high_liquidity": liquid}
    except Exception:  # noqa: BLE001
        return {}


def pullback_read(tfs_out: dict) -> dict:
    """Read the higher-timeframe trend against the lower-timeframe move.

    Compares the highest-ranked timeframe (the higher-timeframe context) with
    the lowest-ranked one (the current micro move) so a counter-move *within* a
    trend is explicit: a dip in an uptrend is a ``pullback_in_uptrend``, a
    bounce in a downtrend is ``bounce_in_downtrend``. Aligned moves read as
    ``impulse_up/down``; a flat context reads ``range``. Returns ``{}`` with
    fewer than two timeframes.

    Part XXV — this is raw structural context (which timeframe trends which way),
    never a precomputed trade direction: it reports each timeframe's observed
    ``up/down/flat`` trend and never a directional conclusion. The Brain alone
    turns this reality into a direction and decides whether it is worth acting on
    after costs.
    """
    try:
        ranked = sorted(
            ((lbl, v) for lbl, v in (tfs_out or {}).items() if _tf_rank(lbl) > 0),
            key=lambda kv: _tf_rank(kv[0]),
        )
        if len(ranked) < 2:
            return {}
        micro_lbl, micro_v = ranked[0]
        context_lbl, context_v = ranked[-1]
        context_trend = str(context_v.get("trend", "flat"))
        micro_trend = str(micro_v.get("trend", "flat"))
        if context_trend == "flat":
            read = "range"
        elif context_trend == "up":
            read = ("pullback_in_uptrend" if micro_trend == "down"
                    else "impulse_up" if micro_trend == "up" else "uptrend_pause")
        else:  # context down
            read = ("bounce_in_downtrend" if micro_trend == "up"
                    else "impulse_down" if micro_trend == "down" else "downtrend_pause")
        return {
            "context_tf": context_lbl, "context_trend": context_trend,
            "micro_tf": micro_lbl, "micro_trend": micro_trend,
            "read": read,
        }
    except Exception:  # noqa: BLE001
        return {}


def build_price_snapshot(
    symbol: str,
    candles_by_tf: dict,
    *,
    tick: Any = None,
    position: Any = None,
    ticks: Any = None,
    depth: Any = None,
    now_epoch: Optional[float] = None,
    max_bars: int = 8,
) -> dict:
    """Reconstruct a compact multi-timeframe market picture for ``symbol``.

    ``candles_by_tf`` maps a timeframe label → chronological bars (each a
    ``(o,h,l,c)`` sequence or an ``{open,high,low,close}`` dict). Returns a
    JSON-friendly dict with, per timeframe: last close, window high/low/range,
    change %, trend and the last ``max_bars`` OHLC bars; plus current
    bid/ask/spread, tick-level ``microstructure`` (from ``ticks``), order-book
    ``depth`` (from ``depth``), the ``session`` and a ``pullback`` read, and,
    when supplied, the live position's direction/entry/current/profit_r/hold.
    Pure and fail-safe — every optional block is omitted (``None``/absent) when
    its input is missing rather than raising.
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

    micro = build_microstructure(ticks, now_epoch=now_epoch) if ticks is not None else {}
    book = summarize_depth(depth) if depth is not None else None
    session = session_context(now_epoch) if now_epoch is not None else {}
    pullback = pullback_read(tfs_out)

    return {"symbol": str(symbol or ""), "timeframes": tfs_out,
            "price": price, "position": pos,
            "microstructure": micro or None, "depth": book,
            "session": session or None, "pullback": pullback or None}


def _trend_agreement(tfs_out: dict) -> float:
    """Direction-agnostic trend agreement in [0, 1] across the timeframes.

    Part XXV — measures only *how aligned* the observed timeframe trends are
    (the magnitude of agreement), never which way they lean. 0 = no net
    agreement (mixed/flat), 1 = every timeframe trends the same way. The Brain
    forms direction itself from the raw per-timeframe trends in the snapshot.
    """
    if not tfs_out:
        return 0.0
    score = 0
    for v in tfs_out.values():
        t = v.get("trend")
        score += 1 if t == "up" else (-1 if t == "down" else 0)
    return min(1.0, abs(score) / float(len(tfs_out)))


def snapshot_to_evidence(symbol: str, snapshot: dict) -> "list[Evidence]":
    """Turn a price snapshot into one multi-timeframe Evidence (the chart).

    Part XXV — the chart is raw market reality, so ``polarity`` is always 0 (no
    directional lean); ``confidence`` reflects only how *aligned* the observed
    timeframe trends are (a clearer picture, not a direction). The full snapshot
    rides in ``measurements`` so the reasoner sees the actual candles and forms
    direction itself. Fail-safe: ``[]`` on empty/any fault.
    """
    out: list[Evidence] = []
    try:
        snap = dict(snapshot or {})
        tfs = snap.get("timeframes") or {}
        if not tfs:
            return out
        agree = _trend_agreement(tfs)
        parts = []
        for tf, v in tfs.items():
            parts.append(f"{tf} {v.get('trend')} {v.get('change_pct')}%")
        price = snap.get("price") or {}
        spread_txt = f" spread {price.get('spread')}" if price else ""
        pos = snap.get("position") or {}
        pos_txt = ""
        if pos:
            pos_txt = f" | pos {pos.get('direction')} {pos.get('profit_r')}R"
        micro = snap.get("microstructure") or {}
        micro_txt = ""
        if micro:
            micro_txt = (
                f" | tape mom {micro.get('momentum')} drift {micro.get('drift_pct')}%"
                f" {micro.get('velocity_tps')}tps"
            )
            if micro.get("spread_widening"):
                micro_txt += " spread↑"
            v_trend = micro.get("velocity_trend")
            if v_trend in ("accelerating", "decelerating"):
                micro_txt += f" vel {v_trend}"
            if micro.get("exhaustion"):
                micro_txt += " exhaustion"
            if micro.get("absorption"):
                micro_txt += " absorption"
        book = snap.get("depth") or {}
        book_txt = f" | book imb {book.get('imbalance')}" if book else ""
        pull = snap.get("pullback") or {}
        pull_txt = f" | {pull.get('read')}" if pull else ""
        sess = snap.get("session") or {}
        sess_txt = f" | {sess.get('session')}" if sess else ""
        observation = (
            f"chart {symbol}: price {price.get('mid') if price else '?'}"
            f"{spread_txt} | " + ", ".join(parts)
            + pull_txt + micro_txt + book_txt + sess_txt + pos_txt
        )[:360]
        out.append(Evidence(
            source_module="market.price_action",
            domain=EvidenceDomain.MULTI_TIMEFRAME, symbol=str(symbol or ""),
            observation=observation,
            confidence=round(0.4 + 0.5 * agree, 4),  # more agreement ⇒ clearer picture
            uncertainty=round(1.0 - agree, 4),
            polarity=0.0,  # Part XXV — the chart is raw market reality, not a lean
            measurements=snap,
            relevance_horizon_seconds=90.0,
        ))
    except Exception:  # noqa: BLE001
        return out
    return out


__all__ = [
    "build_price_snapshot", "snapshot_to_evidence", "build_microstructure",
    "summarize_depth", "session_context", "pullback_read",
]
