"""Session 26 — continuous (sub-candle) thesis re-evaluation (Gap 2).

The APEX vision says "if the probability changes, APEX changes" and "every
thesis is continuously challenged by new evidence". Before this session the
ThesisEngine was only re-fed on candle close (the confirmed ``world_model_update``
EventBus). This session makes the DEVELOPING WorldModel store — which the
sub-candle analysis loop republishes between candle closes — drive an immediate
thesis re-evaluation, so a probability shift updates the thesis the instant it
lands, not on the next bar close.

Coverage:

* ``WorldModelStore.set_on_publish`` — change notification fires on a real
  publish, NOT on a stale one, and a callback fault never breaks a publish.
* ``EventDrivenSystem._on_developing_update`` — a developing-store publish
  re-evaluates the thesis with the confirmed vote panel + the developing store's
  fresher probabilities, and the thesis actually changes when evidence changes.
* The candle-close feed path is unchanged (default trigger still works).
* Per-symbol debounce prevents a burst of developing publishes from
  re-evaluating the same symbol repeatedly.
* The master switch (``continuous_reeval_enabled=False``) disables the path.
* The change logging emits a ``[thesis-update]`` line on a should_act flip.
* End-to-end: registering the callback on the developing store and publishing
  drives the re-evaluation through the real store mechanism.

The bare-shell ``EventDrivenSystem.__new__`` pattern (no ``__init__``) mirrors
the existing thesis / multi-opportunity wiring tests.
"""

import threading
from types import SimpleNamespace

from brain.thesis_engine import ThesisEngine
from brain.world_model import WorldModelStore, build_world_model


# ── Fixtures / helpers ───────────────────────────────────────────────────────


def _vote(module: str, direction: str, confidence: float, weight: float):
    return SimpleNamespace(
        module=module, direction=direction, confidence=confidence, weight=weight,
    )


def _wm(symbol: str, store: WorldModelStore, *, long_p: float, short_p: float,
        votes=None, direction: str = ""):
    """Build + return a WorldModel carrying a probabilistic bias and votes."""
    return build_world_model(
        symbol=symbol,
        version=store.next_version(),
        bias={
            "direction": direction,
            "long_probability": long_p,
            "short_probability": short_p,
        },
        votes=list(votes or []),
    )


def _shell(*, thesis_engine, continuous_enabled=True, min_interval=0.0,
           wm_store=None, dev_store=None):
    """An EventDrivenSystem shell exposing only what the continuous path reads."""
    from event_driven_bootstrap import EventDrivenSystem

    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    sys._ctx = SimpleNamespace(thesis_engine=thesis_engine)
    sys._config = SimpleNamespace(
        thesis=SimpleNamespace(
            gate_enabled=True,
            continuous_reeval_enabled=continuous_enabled,
            continuous_reeval_min_interval_seconds=min_interval,
        ),
        risk=SimpleNamespace(tp1_rr=1.5),
    )
    sys._wm_store = wm_store if wm_store is not None else WorldModelStore()
    sys._developing_wm_store = dev_store if dev_store is not None else WorldModelStore()
    sys._developing_reeval_at = {}
    sys._developing_reeval_lock = threading.Lock()
    return sys


# ── WorldModelStore change notification ──────────────────────────────────────


class TestStoreChangeNotification:
    def test_callback_fires_on_publish(self):
        seen = []
        s = WorldModelStore()
        s.set_on_publish(lambda sym: seen.append(sym))
        s.publish(build_world_model(symbol="EURUSD", version=s.next_version()))
        assert seen == ["EURUSD"]

    def test_callback_not_fired_on_stale_publish(self):
        seen = []
        s = WorldModelStore()
        s.publish(build_world_model(symbol="EURUSD", version=s.next_version()))
        s.set_on_publish(lambda sym: seen.append(sym))
        # version 0 < current version → stale, skipped, no callback.
        s.publish(build_world_model(symbol="EURUSD", version=0))
        assert seen == []

    def test_callback_fault_never_breaks_publish(self):
        s = WorldModelStore()
        s.set_on_publish(lambda sym: (_ for _ in ()).throw(RuntimeError("boom")))
        v = s.next_version()
        s.publish(build_world_model(symbol="EURUSD", version=v))
        # Despite the callback raising, the model is stored.
        assert s.get("EURUSD").version == v

    def test_callback_can_be_cleared(self):
        seen = []
        s = WorldModelStore()
        s.set_on_publish(lambda sym: seen.append(sym))
        s.set_on_publish(None)
        s.publish(build_world_model(symbol="EURUSD", version=s.next_version()))
        assert seen == []


# ── Developing-store change → thesis re-evaluation ───────────────────────────


class TestContinuousReeval:
    def test_developing_update_makes_thesis_actionable(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)

        # Confirmed WM: weak bias → thesis not actionable (candle-close feed).
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.1, short_p=0.1,
                votes=[_vote("structure", "LONG", 0.5, 1.0)]),
        )
        sys._feed_thesis_engine("EURUSD", list(sys._wm_store.get("EURUSD").votes))
        assert eng.should_act("EURUSD")[0] is False

        # Developing WM: stronger LONG probability lands between candle closes.
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.8, short_p=0.1),
        )
        sys._on_developing_update("EURUSD")

        act, direction, ev = eng.should_act("EURUSD")
        assert act is True
        assert direction == "LONG"
        assert ev > 0.1

    def test_thesis_values_change_with_evidence(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.3, short_p=0.3,
                votes=[_vote("structure", "LONG", 0.5, 1.0)]),
        )
        sys._feed_thesis_engine("EURUSD", list(sys._wm_store.get("EURUSD").votes))
        before = eng.get("EURUSD").long_thesis.ev

        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.9, short_p=0.05),
        )
        sys._on_developing_update("EURUSD")
        after = eng.get("EURUSD").long_thesis.ev
        assert after > before

    def test_developing_update_uses_confirmed_votes(self):
        # The developing store carries no votes; the module evidence base must
        # come from the confirmed store's vote panel.
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.1, short_p=0.1,
                votes=[_vote("structure", "LONG", 0.9, 2.0),
                       _vote("momentum", "LONG", 0.8, 1.0)]),
        )
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.8, short_p=0.1),
        )
        sys._on_developing_update("EURUSD")
        long_t = eng.get("EURUSD").long_thesis
        # Supporting modules were taken from the confirmed vote panel.
        assert set(long_t.supporting_modules) == {"structure", "momentum"}

    def test_candle_close_feed_unchanged(self):
        # Default trigger path (no kwargs) reads the confirmed store and builds
        # the thesis exactly as before.
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.8, short_p=0.1,
                votes=[_vote("structure", "LONG", 0.8, 2.0)]),
        )
        sys._feed_thesis_engine("EURUSD", list(sys._wm_store.get("EURUSD").votes))
        act, direction, _ = eng.should_act("EURUSD")
        assert act is True and direction == "LONG"


# ── Debounce ─────────────────────────────────────────────────────────────────


class TestDebounce:
    def test_debounce_suppresses_burst(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng, min_interval=100.0)

        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.5, short_p=0.5,
                votes=[_vote("structure", "LONG", 0.5, 1.0)]),
        )
        # First developing publish: strong LONG → actionable.
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.9, short_p=0.05),
        )
        sys._on_developing_update("EURUSD")
        assert eng.should_act("EURUSD")[0] is True

        # Second developing publish within the debounce window: weak bias that
        # WOULD flip the thesis to not-actionable — but the debounce suppresses
        # the re-eval, so the thesis is unchanged.
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.1, short_p=0.1),
        )
        sys._on_developing_update("EURUSD")
        assert eng.should_act("EURUSD")[0] is True  # still actionable — suppressed

    def test_zero_interval_never_debounces(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng, min_interval=0.0)
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.5, short_p=0.5,
                votes=[_vote("structure", "LONG", 0.5, 1.0)]),
        )
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.9, short_p=0.05),
        )
        sys._on_developing_update("EURUSD")
        assert eng.should_act("EURUSD")[0] is True
        # Immediately re-eval with a weak read — with no debounce it applies.
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.1, short_p=0.1),
        )
        sys._on_developing_update("EURUSD")
        assert eng.should_act("EURUSD")[0] is False


# ── Master switch + fail-safe ────────────────────────────────────────────────


class TestGatesAndFailSafe:
    def test_disabled_switch_no_op(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng, continuous_enabled=False)
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.1, short_p=0.1,
                votes=[_vote("structure", "LONG", 0.5, 1.0)]),
        )
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.9, short_p=0.05),
        )
        sys._on_developing_update("EURUSD")
        # Continuous path disabled → no thesis was ever built.
        assert eng.get("EURUSD") is None

    def test_no_engine_no_op(self):
        sys = _shell(thesis_engine=None)
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.9, short_p=0.05),
        )
        # Must not raise.
        sys._on_developing_update("EURUSD")

    def test_blank_symbol_no_op(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)
        sys._on_developing_update("")
        assert eng.get("") is None


# ── Change logging ───────────────────────────────────────────────────────────


class TestChangeLogging:
    def test_should_act_flip_is_logged(self):
        # Intercept the module logger directly (mock ``logger.info``) rather than
        # capturing through loguru's global handler registry — the large legacy
        # suite mutates loguru's process-global state (remove/configure/disable),
        # so a real-sink capture is run-order-dependent. Patching the bound
        # module logger makes this assertion deterministic and isolation-proof.
        import event_driven_bootstrap as edb

        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)
        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.1, short_p=0.1,
                votes=[_vote("structure", "LONG", 0.5, 1.0)]),
        )
        sys._feed_thesis_engine("EURUSD", list(sys._wm_store.get("EURUSD").votes))

        infos: list[str] = []

        class _CapLogger:
            def info(self, msg, *args, **kwargs):
                # Mirror loguru's brace formatting so the rendered line matches
                # what operators actually see.
                try:
                    infos.append(str(msg).format(*args, **kwargs))
                except Exception:
                    infos.append(str(msg))

            def __getattr__(self, _name):
                # debug/warning/etc. — no-op for this assertion.
                return lambda *a, **k: None

        orig_logger = edb.logger
        edb.logger = _CapLogger()
        try:
            sys._developing_wm_store.publish(
                _wm("EURUSD", sys._developing_wm_store, long_p=0.9, short_p=0.05),
            )
            sys._on_developing_update("EURUSD")
        finally:
            edb.logger = orig_logger

        joined = "".join(infos)
        assert "[thesis-update]" in joined
        assert "developing" in joined
        # The before→after transition is surfaced (should_act flip).
        assert "False" in joined and "True" in joined


# ── End-to-end via the real store callback ───────────────────────────────────


class TestEndToEndWiring:
    def test_store_callback_drives_reeval(self):
        eng = ThesisEngine(min_ev_threshold=0.1, flat_ev=0.0)
        sys = _shell(thesis_engine=eng)
        # Wire the developing store exactly as the bootstrap does.
        sys._developing_wm_store.set_on_publish(sys._on_developing_update)

        sys._wm_store.publish(
            _wm("EURUSD", sys._wm_store, long_p=0.1, short_p=0.1,
                votes=[_vote("structure", "LONG", 0.7, 1.5)]),
        )
        # Publishing to the developing store should trigger the callback → re-eval.
        sys._developing_wm_store.publish(
            _wm("EURUSD", sys._developing_wm_store, long_p=0.85, short_p=0.05),
        )
        act, direction, _ = eng.should_act("EURUSD")
        assert act is True and direction == "LONG"
