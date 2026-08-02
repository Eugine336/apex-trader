"""Tests for Phase 2 Feature B — pre-staged limit orders (ZoneOrderStager).

The stager is fully decoupled: every broker / gate / sizing dependency is an
injected callable, so these tests drive it in isolation with fakes. get_profile
is bypassed via profile_lookup=lambda s: None so the passed EntryConfig fully
controls the staging knobs.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from entry.models import EntryConfig, EntryZone, ZoneType
from entry.zone_order_staging import ZoneOrderStager, zone_key


def _ts() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime


class FakeResult:
    def __init__(self, success=True, order_id="T1", error=""):
        self.success = success
        self.order_id = order_id
        self.error = error


def _long_zone(top=1.0840, bottom=1.0835, inv=1.08325) -> EntryZone:
    return EntryZone(
        symbol="EURUSD", direction="LONG", zone_type=ZoneType.FVG_MIDPOINT,
        top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        invalidation_level=inv, conviction=80, created_at=_ts(),
        expires_at=_ts(), timeframe="M5",
    )


def _short_zone(top=1.0860, bottom=1.0855, inv=1.08625) -> EntryZone:
    return EntryZone(
        symbol="EURUSD", direction="SHORT", zone_type=ZoneType.FVG_MIDPOINT,
        top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        invalidation_level=inv, conviction=80, created_at=_ts(),
        expires_at=_ts(), timeframe="M5",
    )


def _make_stager(zones, *, config=None, gate=True, lots=0.5, place_success=True):
    active = list(zones)
    calls = {"placed": [], "cancelled": []}

    def place(symbol, kind, price, lots_, sl, tp, comment, idem):
        calls["placed"].append(
            {"symbol": symbol, "kind": kind, "price": price, "lots": lots_,
             "sl": sl, "tp": tp, "comment": comment, "idem": idem}
        )
        return FakeResult(success=place_success, order_id=f"T{len(calls['placed'])}")

    def cancel(symbol, order_id):
        calls["cancelled"].append((symbol, order_id))
        return True

    stager = ZoneOrderStager(
        config=config or EntryConfig(pre_staging_enabled=True),
        get_active_zones=lambda s: active,
        place_pending_order=place,
        cancel_pending_order=cancel,
        pip_size_lookup=lambda s: 0.0001,
        size_lookup=lambda s, d, e, sl: lots,
        derive_targets=lambda s, d, e, sl: (
            (e + 0.001, e + 0.002) if d == "LONG" else (e - 0.001, e - 0.002)
        ),
        gate_check=(lambda *a, **k: gate),
        profile_lookup=lambda s: None,
    )
    return stager, calls, active


class TestZoneStagingConfig:
    def test_entry_config_defaults(self):
        c = EntryConfig()
        assert c.pre_staging_enabled is False
        assert c.staging_proximity_pips == 5.0
        assert c.staging_cooldown_s == 60.0

    def test_gold_profile_opts_in(self):
        from brain.instrument_profile import get_profile
        assert get_profile("XAUUSD").pre_staging_enabled is True
        assert get_profile("EURUSD").pre_staging_enabled is False


class TestZoneStagingPlacement:
    def test_stages_buy_limit_for_long_zone(self):
        stager, calls, _ = _make_stager([_long_zone()])
        # mid 1.08435: within 5 pips above the boundary (1.0840), not yet touched.
        stager.on_tick(FakeTick("EURUSD", 1.08425, 1.08445, _ts()))
        assert len(calls["placed"]) == 1
        p = calls["placed"][0]
        assert p["kind"] == "BUY_LIMIT"
        assert p["price"] == 1.0840          # near boundary (zone top)
        assert p["sl"] == 1.08325            # zone invalidation
        assert p["lots"] == 0.5
        assert stager.stats["staged"] == 1
        assert len(stager.staged_orders) == 1

    def test_stages_sell_limit_for_short_zone(self):
        stager, calls, _ = _make_stager([_short_zone()])
        # mid 1.08515: within 5 pips below the boundary (1.0855), not yet touched.
        stager.on_tick(FakeTick("EURUSD", 1.08510, 1.08520, _ts()))
        assert len(calls["placed"]) == 1
        assert calls["placed"][0]["kind"] == "SELL_LIMIT"
        assert calls["placed"][0]["price"] == 1.0855

    def test_no_stage_when_price_far(self):
        stager, calls, _ = _make_stager([_long_zone()])
        stager.on_tick(FakeTick("EURUSD", 1.0900, 1.0901, _ts()))  # 60 pips away
        assert calls["placed"] == []

    def test_no_stage_when_already_touched(self):
        stager, calls, _ = _make_stager([_long_zone()])
        # Price already below the LONG boundary → touched, not "approaching".
        stager.on_tick(FakeTick("EURUSD", 1.08375, 1.08385, _ts()))
        assert calls["placed"] == []

    def test_idempotent_single_stage(self):
        stager, calls, _ = _make_stager([_long_zone()])
        tick = FakeTick("EURUSD", 1.08425, 1.08445, _ts())
        stager.on_tick(tick)
        stager.on_tick(tick)
        assert len(calls["placed"]) == 1  # same zone only staged once

    def test_gate_reject_blocks_stage(self):
        stager, calls, _ = _make_stager([_long_zone()], gate=False)
        stager.on_tick(FakeTick("EURUSD", 1.08425, 1.08445, _ts()))
        assert calls["placed"] == []
        assert stager.stats["gate_rejects"] == 1

    def test_disabled_no_stage(self):
        stager, calls, _ = _make_stager(
            [_long_zone()], config=EntryConfig(pre_staging_enabled=False),
        )
        stager.on_tick(FakeTick("EURUSD", 1.08425, 1.08445, _ts()))
        assert calls["placed"] == []

    def test_zero_size_no_stage(self):
        stager, calls, _ = _make_stager([_long_zone()], lots=0.0)
        stager.on_tick(FakeTick("EURUSD", 1.08425, 1.08445, _ts()))
        assert calls["placed"] == []


class TestZoneStagingLifecycle:
    def test_staleness_cancels_when_zone_gone(self):
        zone = _long_zone()
        stager, calls, active = _make_stager([zone])
        stager.on_tick(FakeTick("EURUSD", 1.08425, 1.08445, _ts()))
        assert len(stager.staged_orders) == 1

        # Zone expires / invalidated → leaves the active set.
        active.clear()
        stager.on_zone_update("EURUSD")
        assert len(calls["cancelled"]) == 1
        assert calls["cancelled"][0][0] == "EURUSD"
        assert stager.staged_orders == {}
        assert stager.stats["cancelled"] == 1

    def test_cooldown_prevents_immediate_restage(self):
        zone = _long_zone()
        stager, calls, active = _make_stager(
            [zone], config=EntryConfig(pre_staging_enabled=True, staging_cooldown_s=600.0),
        )
        tick = FakeTick("EURUSD", 1.08425, 1.08445, _ts())
        stager.on_tick(tick)
        assert len(calls["placed"]) == 1

        # Zone disappears then reappears within the cooldown window.
        active.clear()
        stager.on_zone_update("EURUSD")
        active.append(zone)
        stager.on_tick(tick)
        assert len(calls["placed"]) == 1  # no re-stage during cooldown
        assert stager.stats["cooldown_skips"] >= 1

    def test_cancel_all(self):
        stager, calls, _ = _make_stager([_long_zone()])
        stager.on_tick(FakeTick("EURUSD", 1.08425, 1.08445, _ts()))
        assert len(stager.staged_orders) == 1
        stager.cancel_all()
        assert stager.staged_orders == {}
        assert len(calls["cancelled"]) == 1


class TestZoneKey:
    def test_zone_key_stable(self):
        z1 = _long_zone()
        z2 = _long_zone()
        assert zone_key(z1) == zone_key(z2)
        assert zone_key(_short_zone()) != zone_key(_long_zone())
