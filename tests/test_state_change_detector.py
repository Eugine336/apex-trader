"""Tests for tick.state_change_detector — TickStateChangeDetector.

The detector maintains a tiny per-symbol running read of the market and wakes
the Brain (via the wired ``on_change`` sink) the instant something meaningful
happens, rate-limited per symbol. These tests isolate each trigger by setting
the other triggers' thresholds impossibly high, drive the core decision through
``_observe`` for precise reason assertions, and exercise the public ``on_tick``
path for sink/thread/fail-safe behaviour.
"""

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from tick.models import Tick
from tick.state_change_detector import TickStateChangeDetector

_BIG = 1e9  # a mult that makes a trigger effectively impossible (isolation)


def _cfg(**over) -> SimpleNamespace:
    base = dict(
        enabled=True,
        warmup_ticks=5,
        vol_alpha=0.05,
        spread_alpha=0.05,
        velocity_alpha=0.5,
        price_move_atr_fraction=6.0,
        spread_spike_mult=2.0,
        velocity_spike_mult=3.0,
        reversal_atr_fraction=3.0,
        min_interval_seconds=0.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mk_tick(symbol="XAUUSD", mid=100.0, spread=0.02, ts=1_700_000_000.0,
             source="mt5") -> Tick:
    half = spread / 2.0
    return Tick(
        symbol=symbol, bid=mid - half, ask=mid + half,
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc), source=source,
    )


def _warmup(det, symbol="XAUUSD", center=100.0, amp=0.01, n=6, spread=0.02,
            start_ep=1000.0, step=1.0):
    """Feed ``n`` tight-oscillation ticks so the estimates stabilise. Returns
    the next epoch. The first tick sits exactly at ``center`` so the move
    baseline (``ref_mid``) equals ``center``."""
    ep = start_ep
    det._observe(symbol, center, spread, ep)
    ep += step
    for i in range(1, n):
        mid = center + (amp if i % 2 == 1 else -amp)
        det._observe(symbol, mid, spread, ep)
        ep += step
    return ep


class TestNoFalseTriggers:
    def test_first_tick_seeds_only(self):
        det = TickStateChangeDetector(config=_cfg())
        _sym, mag, reasons = det._observe("XAUUSD", 100.0, 0.02, 1000.0)
        assert reasons == []
        assert mag == 0.0
        assert det.stats()["ticks_seen"] == 1
        assert det.stats()["symbols_tracked"] == 1

    def test_no_trigger_during_warmup(self):
        det = TickStateChangeDetector(config=_cfg(warmup_ticks=10))
        # A large jump on the 2nd tick must NOT trigger — warmup not satisfied.
        det._observe("XAUUSD", 100.0, 0.02, 1000.0)
        _sym, _mag, reasons = det._observe("XAUUSD", 200.0, 0.02, 1001.0)
        assert reasons == []
        assert det.stats()["changes_detected"] == 0

    def test_quiet_market_no_trigger(self):
        det = TickStateChangeDetector(config=_cfg())
        ep = _warmup(det)
        # A move within the oscillation band does not trigger anything.
        _sym, _mag, reasons = det._observe("XAUUSD", 100.005, 0.02, ep)
        assert reasons == []


class TestTriggers:
    def test_price_move_triggers(self):
        det = TickStateChangeDetector(config=_cfg(
            spread_spike_mult=_BIG, velocity_spike_mult=_BIG,
            reversal_atr_fraction=_BIG,
        ))
        ep = _warmup(det)
        sym, mag, reasons = det._observe("XAUUSD", 100.20, 0.02, ep)
        assert "price_move" in reasons
        assert reasons == ["price_move"]  # isolated
        assert 0.0 < mag <= 1.0
        assert sym == "XAUUSD"

    def test_spread_spike_triggers(self):
        det = TickStateChangeDetector(config=_cfg(
            price_move_atr_fraction=_BIG, velocity_spike_mult=_BIG,
            reversal_atr_fraction=_BIG, spread_spike_mult=2.0,
        ))
        # Constant price so ONLY spread can trigger.
        ep = 1000.0
        for _ in range(6):
            det._observe("XAUUSD", 100.0, 0.02, ep)
            ep += 1.0
        _sym, mag, reasons = det._observe("XAUUSD", 100.0, 0.10, ep)
        assert reasons == ["spread_spike"]
        assert 0.0 < mag <= 1.0

    def test_velocity_spike_triggers(self):
        det = TickStateChangeDetector(config=_cfg(
            price_move_atr_fraction=_BIG, spread_spike_mult=_BIG,
            reversal_atr_fraction=_BIG, velocity_spike_mult=3.0,
            velocity_alpha=0.05,
        ))
        # Slow, steady creep to establish a small velocity baseline.
        ep = 1000.0
        mid = 100.0
        for _ in range(6):
            det._observe("XAUUSD", mid, 0.02, ep)
            mid += 0.001
            ep += 1.0
        # Same-ish move but in a tiny time slice → velocity spikes.
        _sym, mag, reasons = det._observe("XAUUSD", mid + 0.045, 0.02, ep + 0.01)
        assert reasons == ["velocity_spike"]
        assert 0.0 < mag <= 1.0

    def test_momentum_reversal_triggers(self):
        det = TickStateChangeDetector(config=_cfg(
            price_move_atr_fraction=_BIG, spread_spike_mult=_BIG,
            velocity_spike_mult=_BIG, reversal_atr_fraction=1.0,
            velocity_alpha=0.5,
        ))
        # Establish a clean uptrend (momentum sign +1).
        ep = 1000.0
        mid = 100.0
        for _ in range(6):
            det._observe("XAUUSD", mid, 0.02, ep)
            mid += 0.01
            ep += 1.0
        # A strong down move flips the smoothed momentum sign.
        _sym, mag, reasons = det._observe("XAUUSD", mid - 0.10, 0.02, ep)
        assert reasons == ["reversal"]
        assert 0.0 < mag <= 1.0

    def test_level_breach_triggers(self):
        det = TickStateChangeDetector(
            config=_cfg(
                price_move_atr_fraction=_BIG, spread_spike_mult=_BIG,
                velocity_spike_mult=_BIG, reversal_atr_fraction=_BIG,
            ),
            level_source=lambda s: [100.05],
        )
        ep = _warmup(det, center=100.0, amp=0.005)
        # Cross the structural level from below.
        _sym, mag, reasons = det._observe("XAUUSD", 100.06, 0.02, ep)
        assert reasons == ["level_breach"]
        assert 0.0 < mag <= 1.0

    def test_level_source_fault_is_swallowed(self):
        def _boom(_symbol):
            raise RuntimeError("boom")

        det = TickStateChangeDetector(
            config=_cfg(
                price_move_atr_fraction=_BIG, spread_spike_mult=_BIG,
                velocity_spike_mult=_BIG, reversal_atr_fraction=_BIG,
            ),
            level_source=_boom,
        )
        ep = _warmup(det)
        # No trigger and no raise even though the level source explodes.
        _sym, _mag, reasons = det._observe("XAUUSD", 100.06, 0.02, ep)
        assert reasons == []


class TestRateLimiting:
    def test_per_symbol_wake_floor(self):
        now = [100.0]
        det = TickStateChangeDetector(
            config=_cfg(
                spread_spike_mult=_BIG, velocity_spike_mult=_BIG,
                reversal_atr_fraction=_BIG, min_interval_seconds=8.0,
            ),
            clock=lambda: now[0],
        )
        ep = _warmup(det)
        # First meaningful move → wakes (last_wake starts at 0).
        _s, _m, r1 = det._observe("XAUUSD", 100.20, 0.02, ep)
        # Second move within the floor → detected but throttled.
        _s, _m, r2 = det._observe("XAUUSD", 100.45, 0.02, ep + 1)
        assert r1 == ["price_move"]
        assert r2 == []  # throttled
        # Advance beyond the floor → wakes again.
        now[0] = 109.0
        _s, _m, r3 = det._observe("XAUUSD", 100.75, 0.02, ep + 2)
        assert r3 == ["price_move"]
        stats = det.stats()
        assert stats["changes_detected"] == 3
        assert stats["wakes"] == 2
        assert stats["throttled"] == 1

    def test_symbols_are_independent(self):
        det = TickStateChangeDetector(config=_cfg(
            spread_spike_mult=_BIG, velocity_spike_mult=_BIG,
            reversal_atr_fraction=_BIG, min_interval_seconds=8.0,
        ))
        ep_a = _warmup(det, symbol="AAA")
        ep_b = _warmup(det, symbol="BBB")
        _s, _m, ra = det._observe("AAA", 100.20, 0.02, ep_a)
        _s, _m, rb = det._observe("BBB", 100.20, 0.02, ep_b)
        # Each symbol's first move wakes independently of the other.
        assert ra == ["price_move"]
        assert rb == ["price_move"]


class TestPublicOnTickPath:
    def test_on_tick_invokes_sink_with_magnitude(self):
        seen = []
        det = TickStateChangeDetector(
            on_change=lambda sym, mag: seen.append((sym, mag)),
            config=_cfg(
                spread_spike_mult=_BIG, velocity_spike_mult=_BIG,
                reversal_atr_fraction=_BIG,
            ),
        )
        ep = 1000.0
        # Warmup via the public path (tight oscillation).
        det.on_tick(_mk_tick(mid=100.0, ts=ep))
        ep += 1
        for i in range(1, 6):
            det.on_tick(_mk_tick(mid=100.0 + (0.01 if i % 2 else -0.01), ts=ep))
            ep += 1
        det.on_tick(_mk_tick(mid=100.20, ts=ep))
        assert len(seen) == 1
        sym, mag = seen[0]
        assert sym == "XAUUSD"
        assert 0.0 < mag <= 1.0

    def test_on_change_none_is_safe(self):
        det = TickStateChangeDetector(on_change=None, config=_cfg(
            spread_spike_mult=_BIG, velocity_spike_mult=_BIG,
            reversal_atr_fraction=_BIG,
        ))
        ep = _warmup(det)
        det._observe("XAUUSD", 100.20, 0.02, ep)  # no sink → must not raise
        assert det.stats()["wakes"] == 1

    def test_sink_fault_does_not_raise(self):
        def _boom(_sym, _mag):
            raise ValueError("boom")

        det = TickStateChangeDetector(on_change=_boom, config=_cfg(
            spread_spike_mult=_BIG, velocity_spike_mult=_BIG,
            reversal_atr_fraction=_BIG,
        ))
        ep = 1000.0
        det.on_tick(_mk_tick(mid=100.0, ts=ep))
        ep += 1
        for i in range(1, 6):
            det.on_tick(_mk_tick(mid=100.0 + (0.01 if i % 2 else -0.01), ts=ep))
            ep += 1
        # The jump fires the (raising) sink — on_tick must swallow it.
        det.on_tick(_mk_tick(mid=100.20, ts=ep))
        assert det.stats()["wakes"] == 1

    def test_malformed_tick_is_ignored(self):
        det = TickStateChangeDetector(config=_cfg())
        det.on_tick(object())              # no attributes at all
        det.on_tick(_mk_tick(mid=0.0))     # non-positive mid
        assert det.stats()["ticks_seen"] == 0  # neither reached _observe

    def test_reset(self):
        det = TickStateChangeDetector(config=_cfg())
        _warmup(det, symbol="AAA")
        _warmup(det, symbol="BBB")
        assert det.stats()["symbols_tracked"] == 2
        det.reset("AAA")
        assert det.stats()["symbols_tracked"] == 1
        det.reset()
        assert det.stats()["symbols_tracked"] == 0


class TestThreadSafety:
    def test_concurrent_on_tick(self):
        det = TickStateChangeDetector(config=_cfg())
        errors = []

        def writer(sym, n):
            try:
                ep = 1000.0
                for i in range(n):
                    det.on_tick(_mk_tick(sym, mid=100.0 + i * 0.001, ts=ep))
                    ep += 0.05
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("EURUSD", 200)),
            threading.Thread(target=writer, args=("XAUUSD", 200)),
            threading.Thread(target=writer, args=("EURUSD", 200)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert det.stats()["ticks_seen"] == 600


class TestRealConfigIntegration:
    def test_appconfig_carries_detector_config(self):
        from config import AppConfig, TickStateChangeConfig

        cfg = AppConfig()
        assert isinstance(cfg.tick_state_change, TickStateChangeConfig)
        assert cfg.tick_state_change.enabled is True

    def test_detector_runs_with_real_config(self):
        from config import TickStateChangeConfig

        det = TickStateChangeDetector(config=TickStateChangeConfig(warmup_ticks=3))
        ep = _warmup(det, n=4)
        # A large jump wakes with the production defaults — just assert no fault.
        det._observe("XAUUSD", 105.0, 0.02, ep)
        assert det.stats()["ticks_seen"] >= 4
