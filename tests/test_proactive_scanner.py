"""APEX TRADER — Proactive Opportunity Scanner tests (GAP 5).

Covers: disabled = never starts, watchlist build + min-EV filter + best-first
order, proximity gate, density-tracker feed, event publish, robustness to missing
WorldModel / candidates, and stats/get_watchlist.
"""

from types import SimpleNamespace

from brain.proactive_scanner import ProactiveOpportunityScanner


def _wm(*cands):
    return SimpleNamespace(candidates_list=lambda: list(cands))


def _cand(direction, ev):
    return SimpleNamespace(direction=direction, ev_estimate=ev)


def test_disabled_does_not_start():
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda s: None,
        enabled=False,
    )
    s.start()
    assert s.stats()["running"] is False


def test_watchlist_filters_and_orders():
    wms = {
        "EURUSD": _wm(_cand("LONG", 0.8)),
        "GBPJPY": _wm(_cand("SHORT", 1.5)),
        "XAUUSD": _wm(_cand("LONG", 0.2)),   # below min_ev
    }
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: list(wms.keys()),
        get_world_model=lambda sym: wms.get(sym),
        enabled=True,
        min_ev=0.5,
    )
    wl = s.scan_once()
    assert [e.symbol for e in wl] == ["GBPJPY", "EURUSD"]


def test_proximity_gate_filters():
    wms = {"EURUSD": _wm(_cand("LONG", 1.0))}
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda sym: wms.get(sym),
        enabled=True, min_ev=0.5,
        proximity_check=lambda sym: False,
    )
    assert s.scan_once() == []


def test_density_tracker_fed():
    fed = {}

    class _Density:
        def record_scan(self, ready):
            fed["ready"] = list(ready)

    wms = {"EURUSD": _wm(_cand("LONG", 0.9))}
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda sym: wms.get(sym),
        enabled=True, min_ev=0.5,
        density_tracker=_Density(),
    )
    s.scan_once()
    assert fed["ready"] == ["EURUSD"]


def test_event_published():
    events = []
    wms = {"EURUSD": _wm(_cand("LONG", 0.9))}
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda sym: wms.get(sym),
        enabled=True, min_ev=0.5,
        event_publish=lambda topic, payload: events.append((topic, payload)),
    )
    s.scan_once()
    assert events and events[0][0] == "proactive_watchlist"
    assert events[0][1][0]["symbol"] == "EURUSD"


def test_missing_world_model_skipped():
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD", "GBPJPY"],
        get_world_model=lambda sym: None,
        enabled=True, min_ev=0.5,
    )
    assert s.scan_once() == []


def test_no_candidates_skipped():
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda sym: _wm(),  # no candidates
        enabled=True, min_ev=0.5,
    )
    assert s.scan_once() == []


def test_symbols_provider_failure_safe():
    def _boom():
        raise RuntimeError("registry down")

    s = ProactiveOpportunityScanner(
        symbols_provider=_boom,
        get_world_model=lambda sym: None,
        enabled=True,
    )
    assert s.scan_once() == []


def test_get_watchlist_and_stats():
    wms = {"EURUSD": _wm(_cand("LONG", 0.9))}
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda sym: wms.get(sym),
        enabled=True, min_ev=0.5,
    )
    s.scan_once()
    wl = s.get_watchlist()
    assert wl[0]["symbol"] == "EURUSD"
    st = s.stats()
    assert st["enabled"] is True
    assert st["scans"] == 1
    assert st["watchlist_size"] == 1


def test_picks_best_candidate_per_symbol():
    wms = {"EURUSD": _wm(_cand("LONG", 0.6), _cand("SHORT", 1.1))}
    s = ProactiveOpportunityScanner(
        symbols_provider=lambda: ["EURUSD"],
        get_world_model=lambda sym: wms.get(sym),
        enabled=True, min_ev=0.5,
    )
    wl = s.scan_once()
    assert wl[0].direction == "SHORT"
    assert abs(wl[0].ev - 1.1) < 1e-9
