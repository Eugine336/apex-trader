"""Characterization tests for the Phase K god-file decomposition.

These pin the behaviour of the clusters lifted out of ``event_driven_bootstrap.py``
into named modules — the tick-source threads (`tick/tick_sources.py`), the
execution lifecycle loops (`execution/lifecycle_loops.py`), and the within-cycle
candidate selector (`scanner/cycle_selection.py`). Each module is loaded directly
(bypassing heavy package __init__s) and its moved code paths are actually
*executed* with stubs, so a missed runtime import would surface as a real error
rather than slipping past ``py_compile``.
"""

import importlib.util
import sys
import types
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# ── Stub loguru (absent offline) ────────────────────────────────────────────
if "loguru" not in sys.modules:
    _lg = types.ModuleType("loguru")
    _logger = types.SimpleNamespace(**{
        m: (lambda *a, **k: None)
        for m in ("debug", "info", "warning", "error", "critical", "success",
                  "trace", "exception")
    })
    _logger.bind = lambda *a, **k: _logger
    _logger.opt = lambda *a, **k: _logger
    _lg.logger = _logger
    sys.modules["loguru"] = _lg


def _load(mod_name, rel_path, *, prime=None):
    """Load a module directly from its file, optionally priming sys.modules.

    ``prime`` maps a module path to a stub module inserted into sys.modules first,
    so the module-under-test's ``from X import Y`` resolves against light stubs
    instead of dragging the heavy real package.
    """
    for name, stub in (prime or {}).items():
        sys.modules.setdefault(name, stub)
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── tick/tick_sources.py ────────────────────────────────────────────────────
# Prime a light `tick` package exposing a Tick dataclass-like stub so the module
# imports without the real tick package.
_tick_stub = types.ModuleType("tick")


class _Tick:
    def __init__(self, symbol, bid, ask, timestamp, source):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.timestamp = timestamp
        self.source = source


_tick_stub.Tick = _Tick
tick_sources = _load("_tick_sources_uut", "tick/tick_sources.py",
                     prime={"tick": _tick_stub})


class _Px:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


class _PM:
    def __init__(self, px):
        self._px = px
        self.calls = 0

    def get_price(self, sym):
        self.calls += 1
        return self._px


class _Router:
    def __init__(self):
        self.ticks = []

    def on_tick(self, tick):
        self.ticks.append(tick)


def _run_poller(cls, px):
    pm = _PM(px)
    router = _Router()
    src = cls(pm, router, ["EURUSD"], poll_interval=0.0)
    src.start()
    # Let the daemon poll a few cycles, then stop.
    deadline = time.time() + 1.0
    while not router.ticks and time.time() < deadline:
        time.sleep(0.01)
    src.stop()
    return router


def test_mt5_poller_routes_ticks_from_prices():
    router = _run_poller(tick_sources.MT5TickPoller, _Px(1.2345, 1.2347))
    assert router.ticks, "poller should route at least one tick"
    assert router.ticks[0].symbol == "EURUSD"
    assert router.ticks[0].source == "mt5"


def test_deriv_adapter_routes_ticks_from_prices():
    router = _run_poller(tick_sources.DerivTickAdapter, _Px(101.0, 101.2))
    assert router.ticks and router.ticks[0].source == "deriv"


def test_poller_no_symbols_is_inert():
    src = tick_sources.MT5TickPoller(_PM(_Px(1.0, 1.0)), _Router(), [], poll_interval=0.0)
    src.start()                      # empty symbol list → never starts a thread
    assert src._running is False
    src.stop()


# ── scanner/cycle_selection.py ──────────────────────────────────────────────
cycle_selection = _load("_cycle_selection_uut", "scanner/cycle_selection.py")


class _Cand:
    def __init__(self, direction, score, ev=0.0):
        self.direction = direction
        self.score = score
        self.ev_estimate = ev


class _Decision:
    def __init__(self, cand):
        self.candidate = cand


def _item(direction, score, ev=0.0):
    return (_Decision(_Cand(direction, score, ev)), {"score": score})


def test_select_empty_returns_empty():
    assert cycle_selection.select_cycle_candidates([]) == ([], [], "")


def test_select_locks_to_best_direction():
    items = [_item("LONG", 0.9), _item("SHORT", 0.8), _item("LONG", 0.5)]
    survivors, dropped, winning = cycle_selection.select_cycle_candidates(items)
    assert winning == "LONG"
    assert len(survivors) == 2 and all(
        s[0].candidate.direction == "LONG" for s in survivors)
    assert len(dropped) == 1 and dropped[0][0].candidate.direction == "SHORT"


def test_select_ranks_best_first_with_ev_tiebreak():
    items = [_item("LONG", 0.7, ev=0.1), _item("LONG", 0.7, ev=0.9)]
    survivors, _, _ = cycle_selection.select_cycle_candidates(items)
    assert survivors[0][0].candidate.ev_estimate == 0.9   # higher EV breaks the tie


# ── execution/lifecycle_loops.py ────────────────────────────────────────────
# Prime light stubs for the runtime imports so the module loads without the
# heavy execution/persistence packages.
_intents_stub = types.ModuleType("execution.intents")


class _IntentType:
    OPEN = 5
    CLOSE = 40


_intents_stub.Intent = object
_intents_stub.IntentType = _IntentType
_es_stub = types.ModuleType("persistence.event_store")
_es_stub.get_event_store = lambda *a, **k: None
_de_stub = types.ModuleType("persistence.domain_events")
_de_stub.TRADE_MODIFIED = "TRADE_MODIFIED"
_exec_pkg = sys.modules.setdefault("execution", types.ModuleType("execution"))
_pers_pkg = sys.modules.setdefault("persistence", types.ModuleType("persistence"))

lifecycle_loops = _load(
    "_lifecycle_loops_uut", "execution/lifecycle_loops.py",
    prime={
        "execution.intents": _intents_stub,
        "persistence.event_store": _es_stub,
        "persistence.domain_events": _de_stub,
    },
)


class _Aggregator:
    def __init__(self):
        self.flushed = 0

    def flush(self):
        self.flushed += 1
        return []                     # no intents → loop stays on the quiet path

    def submit(self, intents):
        pass


def test_flush_loop_start_stop_and_flushes():
    agg = _Aggregator()
    loop = lifecycle_loops.FlushLoop(agg, executor=object(), platform_manager=object(),
                                     interval=0.0)
    loop.start()
    deadline = time.time() + 1.0
    while agg.flushed == 0 and time.time() < deadline:
        time.sleep(0.01)
    loop.stop()
    assert agg.flushed >= 1


def test_flush_loop_build_position_map_reads_broker_fields():
    class _PMpos:
        def get_all_open_positions(self):
            return [types.SimpleNamespace(order_id="T1", symbol="EURUSD",
                                          direction="LONG", sl=1.1, platform="mt5",
                                          lots=0.5)]
    loop = lifecycle_loops.FlushLoop(_Aggregator(), object(), _PMpos(), interval=0.0)
    pos_map = loop._build_position_map()
    assert pos_map["T1"]["symbol"] == "EURUSD"
    assert pos_map["T1"]["remaining_lots"] == 0.5


def test_flush_loop_build_position_map_fault_safe():
    class _PMbad:
        def get_all_open_positions(self):
            raise RuntimeError("broker down")
    loop = lifecycle_loops.FlushLoop(_Aggregator(), object(), _PMbad(), interval=0.0)
    assert loop._build_position_map() == {}


def test_tick_eval_loop_runs_evaluator_and_profiles():
    class _Eval:
        def __init__(self):
            self.evals = 0
            self.eval_count = 0
        def evaluate_all(self, scheduler=None):
            self.evals += 1
            self.eval_count += 1
        def shutdown(self):
            pass
    ev = _Eval()
    loop = lifecycle_loops.TickEvalLoop(ev, interval=0.0)
    loop.start()
    deadline = time.time() + 1.0
    while ev.evals == 0 and time.time() < deadline:
        time.sleep(0.01)
    loop.stop()
    assert ev.evals >= 1
    prof = loop.get_profile()
    assert prof["samples"] >= 1
