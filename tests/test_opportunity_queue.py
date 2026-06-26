"""APEX TRADER — Global Opportunity Queue tests (GAP 1).

Covers: disabled = synchronous identity pass-through (same args, no thread, no
reorder), enabled buffering + best-EV-first dispatch on flush, cross-rank
stamping onto the decision dict, FIFO when no ranker, empty/stop flush, allocation
forwarding, decision parsing, thread-safe concurrent submit, and stats.
"""

import threading

from brain.opportunity_queue import GlobalOpportunityQueue, QueuedOpportunity


def _decision(symbol, direction="LONG", ev=0.0, conviction=0):
    return {
        "symbol": symbol,
        "direction": direction,
        "candidate_ev": ev,
        "conviction": conviction,
    }


def test_disabled_passthrough_synchronous():
    calls = []
    q = GlobalOpportunityQueue(
        dispatch=lambda d, a=None: calls.append((d["symbol"], a)),
        enabled=False,
    )
    q.submit(_decision("EURUSD"))
    # Dispatched immediately, in the caller's thread, no allocation arg.
    assert calls == [("EURUSD", None)]
    assert q.stats()["dispatched"] == 1


def test_disabled_forwards_allocation():
    calls = []
    q = GlobalOpportunityQueue(
        dispatch=lambda d, a=None: calls.append(a),
        enabled=False,
    )
    sentinel = object()
    q.submit(_decision("EURUSD"), sentinel)
    assert calls == [sentinel]


def test_enabled_dispatches_best_ev_first_on_flush():
    order = []

    class _Ranker:
        def rank(self, batch):
            for b in batch:
                b.adjusted_ev = b.ev
            return sorted(batch, key=lambda b: b.adjusted_ev, reverse=True)

    q = GlobalOpportunityQueue(
        dispatch=lambda d, a=None: order.append(d["symbol"]),
        enabled=True,
        window_ms=60000,
        ranker=_Ranker(),
    )
    q.submit(_decision("EURUSD", ev=0.5))
    q.submit(_decision("GBPJPY", ev=1.2))
    q.submit(_decision("XAUUSD", ev=0.8))
    assert order == []  # buffered, not yet dispatched
    n = q.flush()
    q.stop()
    assert n == 3
    assert order == ["GBPJPY", "XAUUSD", "EURUSD"]


def test_enabled_stamps_cross_rank():
    stamped = []

    class _Ranker:
        def rank(self, batch):
            return sorted(batch, key=lambda b: b.ev, reverse=True)

    q = GlobalOpportunityQueue(
        dispatch=lambda d, a=None: stamped.append(dict(d)),
        enabled=True, window_ms=60000, ranker=_Ranker(),
    )
    q.submit(_decision("A", ev=0.2))
    q.submit(_decision("B", ev=0.9))
    q.flush()
    q.stop()
    top = next(d for d in stamped if d["symbol"] == "B")
    assert top["cross_rank"] == 0
    assert top["cross_rank_total"] == 2
    assert "cross_adjusted_ev" in top


def test_no_ranker_preserves_fifo():
    order = []
    q = GlobalOpportunityQueue(
        dispatch=lambda d, a=None: order.append(d["symbol"]),
        enabled=True, window_ms=60000, ranker=None,
    )
    q.submit(_decision("A", ev=0.1))
    q.submit(_decision("B", ev=9.9))
    q.flush()
    q.stop()
    assert order == ["A", "B"]


def test_flush_empty_returns_zero():
    q = GlobalOpportunityQueue(dispatch=lambda d, a=None: None, enabled=True)
    assert q.flush() == 0


def test_stop_flushes_pending():
    order = []
    q = GlobalOpportunityQueue(
        dispatch=lambda d, a=None: order.append(d["symbol"]),
        enabled=True, window_ms=60000,
    )
    q.submit(_decision("A", ev=0.5))
    q.stop()
    assert order == ["A"]
    # After stop, submit is a passthrough.
    q.submit(_decision("B"))
    assert order == ["A", "B"]


def test_from_decision_conviction_fallback():
    qo = QueuedOpportunity.from_decision(_decision("EURUSD", conviction=80))
    assert qo.symbol == "EURUSD"
    assert abs(qo.confidence - 0.8) < 1e-9


def test_from_decision_prefers_candidate_score():
    d = _decision("EURUSD", conviction=80)
    d["candidate_score"] = 0.42
    qo = QueuedOpportunity.from_decision(d)
    assert abs(qo.confidence - 0.42) < 1e-9


def test_concurrent_submit_dispatches_all():
    order = []
    lock = threading.Lock()

    def _dispatch(d, a=None):
        with lock:
            order.append(d["symbol"])

    q = GlobalOpportunityQueue(dispatch=_dispatch, enabled=True, window_ms=60000)

    def _worker(i):
        q.submit(_decision(f"SYM{i}", ev=float(i)))

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    q.flush()
    q.stop()
    assert len(order) == 25
    assert q.stats()["submitted"] == 25


def test_stats_shape():
    q = GlobalOpportunityQueue(dispatch=lambda d, a=None: None, enabled=True, window_ms=500)
    st = q.stats()
    assert st["enabled"] is True
    assert st["window_ms"] == 500
    assert set(["submitted", "dispatched", "windows", "pending"]).issubset(st)
