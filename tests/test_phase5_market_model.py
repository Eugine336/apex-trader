"""Phase 5 — Management developing-structure advisory + Market Model panel.

Part A (management advisory): the developing (forming-bar) WorldModel is an
ADVISORY secondary signal in the management path. It can escalate a HOLD/OBSERVE
verdict to a strictly-tightening protective stop on a reversal warning, but it
must never:

* act at all when no developing store is wired (backwards-compatible no-op),
* override a confirmed CLOSE,
* loosen an existing stop or open a position.

Part B (dashboard): ``LiveState.get_market_model`` serves confirmed vs
developing structure per symbol with an agreement / divergence flag.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from brain.structure_engine import StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from decision.actions import Action, ManagementDecision
from execution.intent_aggregator import IntentAggregator
from tick import TickStore


# ── shared fakes ─────────────────────────────────────────────────────


def _sa(trend, conf, event=StructureEvent.NONE):
    return SimpleNamespace(
        trend=trend, confidence=conf, last_event=event,
        swing_high=None, swing_low=None,
    )


class _DevWM:
    def __init__(self, struct: dict, bias: dict | None = None):
        self._struct = struct
        self._bias = bias or {}

    def structure_by_tf(self) -> dict:
        return dict(self._struct)

    def bias_dict(self) -> dict:
        return dict(self._bias)


class _DevStore:
    def __init__(self, mapping: dict | None = None):
        self._m = mapping or {}

    def get(self, sym):
        return self._m.get(sym)


def _pm():
    pm = SimpleNamespace()
    pm.get_all_open_positions = lambda: []
    pm.fetch_market_data = lambda *a, **k: {}
    pm.get_symbol_spec = lambda s: {"pip_size": 0.0001}
    return pm


def _evaluator(developing=None, config=None):
    from event_driven_bootstrap import PositionEvaluator

    return PositionEvaluator(
        _pm(), TickStore(), WorldModelStore(), IntentAggregator(),
        config=config, developing_world_model_store=developing,
    )


# ── Part A: constructor backwards-compat ─────────────────────────────


class TestEvaluatorDevelopingStore:
    def test_default_constructor_has_no_developing_store(self):
        from event_driven_bootstrap import PositionEvaluator

        ev = PositionEvaluator(
            _pm(), TickStore(), WorldModelStore(), IntentAggregator(),
        )
        assert ev._developing_wm_store is None

    def test_developing_store_param_is_wired(self):
        store = _DevStore()
        ev = _evaluator(developing=store)
        assert ev._developing_wm_store is store


# ── Part A: pure SL-tightening helper ────────────────────────────────


class TestTighteningBreakevenSL:
    def _call(self, *a):
        from event_driven_bootstrap import PositionEvaluator

        return PositionEvaluator._tightening_breakeven_sl(*a)

    def test_buy_tightens_to_breakeven(self):
        # entry 1.1000, price 1.1020 (in profit), sl below breakeven → raise sl.
        assert self._call("BUY", 1.1000, 1.1020, 1.0950) == 1.1000

    def test_buy_does_not_loosen(self):
        # sl already above breakeven → moving to breakeven would loosen → None.
        assert self._call("BUY", 1.1000, 1.1020, 1.1010) is None

    def test_buy_not_in_profit_is_none(self):
        # price below entry → breakeven sits above price → invalid long stop.
        assert self._call("BUY", 1.1000, 1.0990, 1.0950) is None

    def test_sell_tightens_to_breakeven(self):
        assert self._call("SELL", 1.1000, 1.0980, 1.1050) == 1.1000

    def test_sell_does_not_loosen(self):
        assert self._call("SELL", 1.1000, 1.0980, 1.0990) is None


# ── Part A: developing reversal detection ────────────────────────────


class TestDevelopingReversalTfs:
    def _call(self, struct, norm_dir, min_conf=0.6):
        from event_driven_bootstrap import PositionEvaluator

        return PositionEvaluator._developing_reversal_tfs(struct, norm_dir, min_conf)

    def test_opposing_trend_above_confidence_warns(self):
        struct = {"H1": _sa(Trend.BEARISH, 0.8)}
        assert self._call(struct, "BUY") == ["H1"]

    def test_opposing_trend_below_confidence_silent(self):
        struct = {"H1": _sa(Trend.BEARISH, 0.4)}
        assert self._call(struct, "BUY") == []

    def test_reversal_event_bypasses_confidence(self):
        # Low-confidence RANGING trend, but a confirmed CHoCH against the BUY.
        struct = {"H1": _sa(Trend.RANGING, 0.1, StructureEvent.CHOCH_BEARISH)}
        assert self._call(struct, "BUY") == ["H1"]

    def test_agreeing_structure_is_silent(self):
        struct = {"H1": _sa(Trend.BULLISH, 0.9), "M5": _sa(Trend.BULLISH, 0.9)}
        assert self._call(struct, "BUY") == []

    def test_sell_opposed_by_bullish(self):
        struct = {"M5": _sa(Trend.BULLISH, 0.7)}
        assert self._call(struct, "SELL") == ["M5"]


# ── Part A: advisory escalation end-to-end ───────────────────────────


class TestApplyDevelopingAdvisory:
    def _apply(self, ev, de, **kw):
        defaults = dict(
            symbol="EURUSD", norm_dir="BUY", entry_price=1.1000,
            price=1.1020, current_sl=1.0950, pnl_pips=20.0, trade_ctx=None,
        )
        defaults.update(kw)
        return ev._apply_developing_advisory(
            de, defaults["symbol"], defaults["norm_dir"],
            defaults["entry_price"], defaults["price"],
            defaults["current_sl"], defaults["pnl_pips"], defaults["trade_ctx"],
        )

    def test_noop_without_developing_store(self):
        ev = _evaluator(developing=None)
        de = ManagementDecision(action=Action.HOLD, reason="hold")
        out = self._apply(ev, de)
        assert out.action == Action.HOLD

    def test_noop_when_advisory_disabled(self):
        cfg = SimpleNamespace(
            developing_analysis=SimpleNamespace(
                management_advisory_enabled=False,
                management_advisory_min_confidence=0.6,
            )
        )
        store = _DevStore({"EURUSD": _DevWM({"H1": _sa(Trend.BEARISH, 0.9)})})
        ev = _evaluator(developing=store, config=cfg)
        de = ManagementDecision(action=Action.HOLD, reason="hold")
        out = self._apply(ev, de)
        assert out.action == Action.HOLD

    def test_escalates_hold_to_protective_stop_on_reversal(self):
        store = _DevStore({"EURUSD": _DevWM({"H1": _sa(Trend.BEARISH, 0.85)})})
        ev = _evaluator(developing=store)
        de = ManagementDecision(action=Action.HOLD, reason="hold")
        out = self._apply(ev, de)  # BUY in profit, sl below breakeven
        assert out.action == Action.SET_PROTECTIVE_STOP
        assert out.new_sl == 1.1000  # breakeven, strictly tightening
        assert any("developing advisory" in e for e in out.evidence)

    def test_never_overrides_close(self):
        store = _DevStore({"EURUSD": _DevWM({"H1": _sa(Trend.BEARISH, 0.95)})})
        ev = _evaluator(developing=store)
        de = ManagementDecision(action=Action.CLOSE, reason="thesis dead")
        out = self._apply(ev, de)
        assert out.action == Action.CLOSE

    def test_does_not_open_or_loosen_when_not_in_profit(self):
        store = _DevStore({"EURUSD": _DevWM({"H1": _sa(Trend.BEARISH, 0.9)})})
        ev = _evaluator(developing=store)
        de = ManagementDecision(action=Action.HOLD, reason="hold")
        # price below entry → no safe tighten → verdict stays HOLD.
        out = self._apply(ev, de, price=1.0990)
        assert out.action == Action.HOLD
        assert any("no safe tighten" in e for e in out.evidence)

    def test_no_change_when_developing_agrees(self):
        store = _DevStore({"EURUSD": _DevWM({"H1": _sa(Trend.BULLISH, 0.9)})})
        ev = _evaluator(developing=store)
        de = ManagementDecision(action=Action.HOLD, reason="hold")
        out = self._apply(ev, de)
        assert out.action == Action.HOLD


# ── Part B: Market Model dashboard panel ─────────────────────────────


def _first_registry_symbol() -> str:
    from config import INSTRUMENT_REGISTRY

    return next(iter(INSTRUMENT_REGISTRY))


def _wm(symbol, store, h1_trend, direction):
    return build_world_model(
        symbol=symbol,
        version=store.next_version(),
        timestamp=datetime.now(timezone.utc),
        structure={"H1": _sa(h1_trend, 0.8)},
        bias={
            "direction": direction,
            "long_probability": 0.7 if direction == "LONG" else 0.3,
            "short_probability": 0.3 if direction == "LONG" else 0.7,
            "conflict_score": 0.2,
            "confidence": 0.6,
            "strength": "MODERATE",
        },
    )


def _live_state(confirmed_store, developing_store):
    from dashboard.state import LiveState

    state = LiveState()
    ed = SimpleNamespace(
        _wm_store=confirmed_store,
        _developing_wm_store=developing_store,
        is_running=True,
    )
    state.set_event_driven_system(ed)
    return state


class TestMarketModelPanel:
    def test_developing_disabled_when_store_missing(self):
        sym = _first_registry_symbol()
        conf = WorldModelStore()
        conf.publish(_wm(sym, conf, Trend.BULLISH, "LONG"))
        # ed_system has no _developing_wm_store attribute.
        from dashboard.state import LiveState

        state = LiveState()
        state.set_event_driven_system(
            SimpleNamespace(_wm_store=conf, is_running=True)
        )
        out = state.get_market_model()
        assert out["developing_enabled"] is False
        assert out["confirmed_count"] >= 1
        row = next(r for r in out["symbols"] if r["symbol"] == sym.upper())
        assert row["confirmed"]["bias_direction"] == "LONG"
        assert row["developing"] == {}

    def test_agreement_when_both_align(self):
        sym = _first_registry_symbol()
        conf = WorldModelStore()
        dev = WorldModelStore()
        conf.publish(_wm(sym, conf, Trend.BULLISH, "LONG"))
        dev.publish(_wm(sym, dev, Trend.BULLISH, "LONG"))
        out = _live_state(conf, dev).get_market_model()
        row = next(r for r in out["symbols"] if r["symbol"] == sym.upper())
        assert row["agreement"] == "agree"
        assert row["diverging_timeframes"] == []
        assert out["diverging_count"] == 0

    def test_divergence_flagged_when_developing_opposes(self):
        sym = _first_registry_symbol()
        conf = WorldModelStore()
        dev = WorldModelStore()
        conf.publish(_wm(sym, conf, Trend.BULLISH, "LONG"))
        dev.publish(_wm(sym, dev, Trend.BEARISH, "SHORT"))
        out = _live_state(conf, dev).get_market_model()
        row = next(r for r in out["symbols"] if r["symbol"] == sym.upper())
        assert row["agreement"] == "diverge"
        assert "H1" in row["diverging_timeframes"]
        assert out["diverging_count"] >= 1
        # Diverging symbols sort first for operator visibility.
        assert out["symbols"][0]["agreement"] == "diverge"

    def test_empty_when_no_engine(self):
        from dashboard.state import LiveState

        out = LiveState().get_market_model()
        assert out["symbols"] == []
        assert out["confirmed_count"] == 0
        assert out["developing_enabled"] is False
