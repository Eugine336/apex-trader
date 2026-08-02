"""Tests for Phase 3 Feature A — News calendar pre-planner (NewsPlanner).

Exercises the news-calendar pre-planning machinery in full isolation (no broker,
no feed, no threads) via the planner's injected callables: event detection +
range-based OCO staging, the auto-size-down risk multiplier, the OCO
cancel-on-fill state machine (PENDING → ONE_FILLED), the post-event cooldown
reap (→ DONE), idempotency, and the enable/disable + cancel_all paths.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from brain.news_planner import (
    NewsPlanner,
    STATE_PENDING,
    STATE_ONE_FILLED,
    STATE_DONE,
    ORDER_FILLED,
    ORDER_CANCELLED,
    event_key,
)
from entry.models import EntryConfig


# ── Fakes ────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, order_id: str, success: bool = True, error: str = ""):
        self.success = success
        self.order_id = order_id
        self.error = error


def _event(minutes_ahead: float, now: datetime, title: str = "CPI", currency: str = "USD"):
    return SimpleNamespace(
        title=title,
        currency=currency,
        impact="HIGH",
        time_utc=now + timedelta(minutes=minutes_ahead),
    )


class _Harness:
    """Wires a NewsPlanner to recording fakes, driven by an EntryConfig."""

    def __init__(self, *, enabled=True, pip=0.01, buffer_pips=200.0,
                 risk_mult=0.5, pre_stage=5, lookback=10, cooldown=300.0,
                 range_low=1990.0, range_high=2009.0, symbols=("XAUUSD",)):
        cfg = EntryConfig()
        cfg.news_pre_planning_enabled = enabled
        cfg.news_pre_stage_minutes = pre_stage
        cfg.news_range_lookback_bars = lookback
        cfg.news_breakout_buffer_pips = buffer_pips
        cfg.news_risk_multiplier = risk_mult
        cfg.news_post_event_cooldown_s = cooldown

        self.placed: list = []
        self.cancelled: list = []
        self.filled: set = set()
        self.size_calls: list = []
        self._order_seq = 0
        self._events: list = []
        self._candles = [
            {"high": range_high, "low": range_low} for _ in range(lookback + 5)
        ]

        def place(symbol, kind, price, lots, sl, tp, comment, idem):
            self._order_seq += 1
            oid = f"o{self._order_seq}"
            self.placed.append(SimpleNamespace(
                symbol=symbol, kind=kind, price=price, lots=lots,
                sl=sl, tp=tp, comment=comment, idem=idem, order_id=oid,
            ))
            return _Result(oid)

        def cancel(symbol, order_id):
            self.cancelled.append(order_id)
            return True

        def size(symbol, direction, entry, sl, risk_mult):
            self.size_calls.append(risk_mult)
            return round(0.10 * risk_mult, 4)

        self.planner = NewsPlanner(
            config=cfg,
            get_symbols=lambda: list(symbols),
            get_events=lambda s, m, n: [e for e in self._events
                                        if 0 <= (e.time_utc - n).total_seconds() / 60.0 <= m],
            get_candles=lambda s, b: self._candles,
            place_pending_order=place,
            cancel_pending_order=cancel,
            is_filled=lambda s, oid: oid in self.filled,
            pip_size_lookup=lambda s: pip,
            size_lookup=size,
            profile_lookup=lambda s: None,  # config drives every knob
        )

    def set_events(self, events):
        self._events = list(events)

    def by_kind(self, kind):
        return [o for o in self.placed if o.kind == kind]


# ── Event detection + staging ─────────────────────────────────────────────


def test_stages_oco_pair_when_event_in_window():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.set_events([_event(3, now)])  # 3 min ahead, within 5-min pre-stage window
    h.planner.update(now)

    assert len(h.placed) == 2
    kinds = {o.kind for o in h.placed}
    assert kinds == {"BUY_STOP", "SELL_STOP"}
    plans = list(h.planner.active_plans.values())
    assert len(plans) == 1
    assert plans[0].state == STATE_PENDING


def test_breakout_prices_straddle_range_with_buffer():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    # pip 0.01 × 200-pip buffer = 2.00 offset either side of the range.
    h = _Harness(pip=0.01, buffer_pips=200.0, range_low=1990.0, range_high=2009.0)
    h.set_events([_event(2, now)])
    h.planner.update(now)

    buy = h.by_kind("BUY_STOP")[0]
    sell = h.by_kind("SELL_STOP")[0]
    assert buy.price == pytest.approx(2009.0 + 2.0)   # range_high + buffer
    assert sell.price == pytest.approx(1990.0 - 2.0)  # range_low - buffer
    # SLs sit on the opposite range edge (± buffer).
    assert buy.sl == pytest.approx(1990.0 - 2.0)
    assert sell.sl == pytest.approx(2009.0 + 2.0)


def test_no_staging_when_disabled():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness(enabled=False)
    h.set_events([_event(3, now)])
    h.planner.update(now)
    assert h.placed == []
    assert h.planner.active_plans == {}


def test_no_staging_when_event_outside_window():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness(pre_stage=5)
    h.set_events([_event(30, now)])  # 30 min ahead — outside 5-min window
    h.planner.update(now)
    assert h.placed == []


def test_no_staging_without_events():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.planner.update(now)
    assert h.placed == []


def test_staging_is_idempotent_across_updates():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.set_events([_event(4, now)])
    h.planner.update(now)
    h.planner.update(now + timedelta(seconds=61))  # same event still in window
    assert len(h.placed) == 2  # not re-staged


# ── Risk sizing (auto size-down) ───────────────────────────────────────────


def test_risk_multiplier_passed_to_sizing():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness(risk_mult=0.5)
    h.set_events([_event(3, now)])
    h.planner.update(now)
    # Both legs sized with the news risk multiplier (auto size-down).
    assert h.size_calls == [0.5, 0.5]
    for o in h.placed:
        assert o.lots == pytest.approx(0.05)  # 0.10 × 0.5


def test_zero_lots_skips_that_leg():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness(risk_mult=0.0)  # size() returns 0 → no legs placed
    h.set_events([_event(3, now)])
    h.planner.update(now)
    assert h.placed == []
    assert h.planner.stats["stage_failures"] == 1


# ── OCO state machine ──────────────────────────────────────────────────────


def test_oco_cancels_opposite_on_fill():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.set_events([_event(3, now)])
    h.planner.update(now)

    buy = h.by_kind("BUY_STOP")[0]
    sell = h.by_kind("SELL_STOP")[0]
    h.filled.add(buy.order_id)  # BUY_STOP breakout fills
    h.planner.update(now + timedelta(minutes=1))

    assert sell.order_id in h.cancelled
    assert buy.order_id not in h.cancelled  # the filled leg is NOT cancelled
    plan = next(iter(h.planner.active_plans.values()))
    assert plan.state == STATE_ONE_FILLED
    assert h.planner.stats["oco_cancels"] == 1
    assert h.planner.stats["filled"] == 1


def test_filled_leg_marked_and_opposite_cancelled_status():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.set_events([_event(3, now)])
    h.planner.update(now)
    buy = h.by_kind("BUY_STOP")[0]
    h.filled.add(buy.order_id)
    h.planner.update(now + timedelta(minutes=1))

    plan = next(iter(h.planner.active_plans.values()))
    statuses = {o.order_kind: o.status for o in plan.orders}
    assert statuses["BUY_STOP"] == ORDER_FILLED
    assert statuses["SELL_STOP"] == ORDER_CANCELLED


# ── Post-event cooldown ────────────────────────────────────────────────────


def test_cooldown_cancels_unfilled_and_marks_done():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness(cooldown=300.0)
    ev_time_min = 3
    h.set_events([_event(ev_time_min, now)])
    h.planner.update(now)  # stage PENDING

    # Advance well past the event + cooldown; neither leg filled.
    later = now + timedelta(minutes=ev_time_min) + timedelta(seconds=301)
    h.planner.update(later)

    plan = next(iter(h.planner.active_plans.values()))
    assert plan.state == STATE_DONE
    assert len(h.cancelled) == 2  # both unfilled legs cancelled
    assert h.planner.stats["cooldown_cancels"] == 2


def test_cooldown_does_not_cancel_before_window_elapses():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness(cooldown=300.0)
    h.set_events([_event(3, now)])
    h.planner.update(now)
    # 1 minute after the event — still inside the cooldown band.
    h.planner.update(now + timedelta(minutes=4))
    plan = next(iter(h.planner.active_plans.values()))
    assert plan.state == STATE_PENDING
    assert h.cancelled == []


# ── Enable/disable + cancel_all ────────────────────────────────────────────


def test_cancel_all_cancels_resting_orders():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.set_events([_event(3, now)])
    h.planner.update(now)
    h.planner.cancel_all()
    assert len(h.cancelled) == 2
    plan = next(iter(h.planner.active_plans.values()))
    assert plan.state == STATE_DONE


def test_disabling_mid_session_reaps_resting_orders():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    h = _Harness()
    h.set_events([_event(3, now)])
    h.planner.update(now)
    # Toggle the feature OFF, then run monitoring — resting orders are reaped.
    h.planner._config.news_pre_planning_enabled = False
    h.planner.update(now + timedelta(minutes=1))
    plan = next(iter(h.planner.active_plans.values()))
    assert plan.state == STATE_DONE
    assert len(h.cancelled) == 2


# ── event_key identity ─────────────────────────────────────────────────────


def test_event_key_is_stable_and_distinct():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    ev = _event(3, now, title="CPI")
    k1 = event_key("XAUUSD", ev)
    k2 = event_key("XAUUSD", ev)
    assert k1 == k2
    other = _event(3, now, title="NFP")
    assert event_key("XAUUSD", other) != k1
