"""Tests for brain.world_model (Phase 1)."""

import threading

import pytest

from brain.world_model import WorldModelStore, build_world_model


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_fvg(kind="BULLISH", tf="M5"):
    """Minimal FairValueGap-like object for testing."""
    import pandas as pd
    from brain.fvg_detector import FairValueGap, FVGStatus

    return FairValueGap(
        kind=kind, top=1.10, bottom=1.09, midpoint=1.095,
        size_pips=10.0, strength="STRONG", status=FVGStatus.OPEN,
        candle_index=5, timestamp=pd.Timestamp.now(), timeframe=tf,
    )


def _stub_ob(kind="BULLISH", tf="H1"):
    """Minimal OrderBlock-like object for testing."""
    import pandas as pd
    from brain.order_block import OrderBlock, OBStatus

    return OrderBlock(
        kind=kind, top=1.10, bottom=1.09, midpoint=1.095,
        origin_index=3, strength="STRONG", status=OBStatus.FRESH,
        impulse_size=15.0, timestamp=pd.Timestamp.now(), timeframe=tf,
        breaker=False,
    )


def _stub_structure():
    """Minimal StructureAnalysis for testing."""
    from brain.structure_engine import StructureAnalysis, Trend, StructureEvent

    return StructureAnalysis(
        trend=Trend.BULLISH, last_event=StructureEvent.BOS_BULLISH,
        swing_high=1.12, swing_low=1.08, last_bos_level=1.11,
        last_choch_level=None, structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.8,
    )


def _stub_volume():
    """Minimal VolumeAnalysis for testing."""
    from brain.volume_analyzer import VolumeAnalysis

    return VolumeAnalysis(
        volume_ratio=1.5, has_spike=True,
        divergence_type="NONE", climax_detected=False,
        poc_level=1.095, confirmation_bias="BULLISH",
    )


# ---------------------------------------------------------------------------
# WorldModel construction and immutability
# ---------------------------------------------------------------------------


class TestWorldModel:

    def test_build_empty(self):
        wm = build_world_model(symbol="EURUSD", version=1)
        assert wm.symbol == "EURUSD"
        assert wm.version == 1
        assert wm.fvgs == ()
        assert wm.order_blocks == ()

    def test_build_with_fvgs(self):
        fvg_m5 = _stub_fvg("BULLISH", "M5")
        fvg_h1 = _stub_fvg("BEARISH", "H1")
        wm = build_world_model(
            symbol="EURUSD", version=2,
            fvgs={"M5": [fvg_m5], "H1": [fvg_h1]},
        )
        by_tf = wm.fvgs_by_tf()
        assert "M5" in by_tf
        assert "H1" in by_tf
        assert len(by_tf["M5"]) == 1
        assert by_tf["M5"][0].kind == "BULLISH"
        assert by_tf["H1"][0].kind == "BEARISH"

    def test_build_with_order_blocks(self):
        ob = _stub_ob("BEARISH", "H4")
        wm = build_world_model(
            symbol="GBPUSD", version=3,
            order_blocks={"H4": [ob]},
        )
        by_tf = wm.order_blocks_by_tf()
        assert "H4" in by_tf
        assert by_tf["H4"][0].kind == "BEARISH"

    def test_build_with_structure(self):
        sa = _stub_structure()
        wm = build_world_model(
            symbol="EURUSD", version=4,
            structure={"H1": sa, "H4": sa},
        )
        by_tf = wm.structure_by_tf()
        assert "H1" in by_tf
        assert "H4" in by_tf
        assert by_tf["H1"].confidence == 0.8

    def test_build_with_volume(self):
        va = _stub_volume()
        wm = build_world_model(
            symbol="EURUSD", version=5,
            volume={"M5": va, "H1": va},
        )
        by_tf = wm.volume_by_tf()
        assert len(by_tf) == 2
        assert by_tf["M5"].volume_ratio == 1.5

    def test_build_with_bias(self):
        bias = {"direction": "BULLISH", "h4_trend": "BULLISH", "strength": "STRONG"}
        wm = build_world_model(
            symbol="EURUSD", version=6,
            bias=bias,
        )
        bd = wm.bias_dict()
        assert bd["direction"] == "BULLISH"

    def test_frozen_immutability(self):
        wm = build_world_model(symbol="EURUSD", version=1)
        with pytest.raises(AttributeError):
            wm.symbol = "GBPUSD"  # type: ignore[misc]

    def test_all_fvgs_flat_list(self):
        fvg1 = _stub_fvg("BULLISH", "M5")
        fvg2 = _stub_fvg("BEARISH", "H1")
        wm = build_world_model(
            symbol="EURUSD", version=7,
            fvgs={"H1": [fvg2], "M5": [fvg1]},
        )
        flat = wm.all_fvgs()
        assert len(flat) == 2

    def test_all_order_blocks_flat_list(self):
        ob1 = _stub_ob("BULLISH", "H1")
        ob2 = _stub_ob("BEARISH", "H4")
        wm = build_world_model(
            symbol="EURUSD", version=8,
            order_blocks={"H4": [ob2], "H1": [ob1]},
        )
        flat = wm.all_order_blocks()
        assert len(flat) == 2


# ---------------------------------------------------------------------------
# WorldModelStore thread safety and versioning
# ---------------------------------------------------------------------------


class TestWorldModelStore:

    def test_publish_and_get(self):
        store = WorldModelStore()
        wm = build_world_model(symbol="EURUSD", version=store.next_version())
        store.publish(wm)
        assert store.get("EURUSD") is wm
        assert store.get("GBPUSD") is None

    def test_version_monotonic(self):
        store = WorldModelStore()
        v1 = store.next_version()
        v2 = store.next_version()
        v3 = store.next_version()
        assert v1 < v2 < v3

    def test_stale_publish_rejected(self):
        store = WorldModelStore()
        wm_new = build_world_model(symbol="EURUSD", version=10)
        wm_old = build_world_model(symbol="EURUSD", version=5)
        store.publish(wm_new)
        store.publish(wm_old)
        assert store.get("EURUSD").version == 10

    def test_snapshot_returns_copy(self):
        store = WorldModelStore()
        wm = build_world_model(symbol="EURUSD", version=1)
        store.publish(wm)
        snap = store.snapshot()
        assert "EURUSD" in snap
        snap["GBPUSD"] = wm
        assert store.get("GBPUSD") is None

    def test_symbols(self):
        store = WorldModelStore()
        store.publish(build_world_model(symbol="EURUSD", version=1))
        store.publish(build_world_model(symbol="GBPUSD", version=2))
        syms = store.symbols()
        assert set(syms) == {"EURUSD", "GBPUSD"}

    def test_clear(self):
        store = WorldModelStore()
        store.publish(build_world_model(symbol="EURUSD", version=1))
        store.clear()
        assert len(store) == 0

    def test_len(self):
        store = WorldModelStore()
        assert len(store) == 0
        store.publish(build_world_model(symbol="EURUSD", version=1))
        assert len(store) == 1

    def test_concurrent_publish_safety(self):
        """Multiple threads publishing simultaneously should not corrupt state."""
        store = WorldModelStore()
        errors = []

        def _publish(sym, n):
            try:
                for i in range(n):
                    wm = build_world_model(symbol=sym, version=store.next_version())
                    store.publish(wm)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_publish, args=(f"SYM{i}", 50))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store) == 8

    def test_concurrent_read_write_safety(self):
        """Reads and writes interleaved should not raise."""
        store = WorldModelStore()
        errors = []

        def _writer():
            try:
                for i in range(100):
                    store.publish(build_world_model(
                        symbol="EURUSD", version=store.next_version(),
                    ))
            except Exception as exc:
                errors.append(exc)

        def _reader():
            try:
                for _ in range(100):
                    store.get("EURUSD")
                    store.snapshot()
                    store.symbols()
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer),
            threading.Thread(target=_reader),
            threading.Thread(target=_reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
