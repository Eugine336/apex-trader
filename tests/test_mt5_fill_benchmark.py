"""Own-market-fill quote and deal lineage, using an isolated event database and fake MT5."""
from types import SimpleNamespace as NS

import pytest

from cognition.concepts.provider_data_readings import ProviderDataReadings
from data_sources import DataSourceRegistry, ObservationKind
from data_sources.mt5_execution import MT5ExecutionCostAdapter
from persistence import domain_events as DE
from persistence.event_store import EventStore
from platforms.mt5 import mt5_connector as mt5_module
from platforms.mt5.fill_benchmark import capture_fill_benchmark, matched_fill_epoch
from platforms.mt5.mt5_connector import MT5Connector


NOW = 1_800_000_000
ACCOUNT = NS(login=43756, server="Broker-A", currency="CHF")


def _receipt(side="BUY"):
    clock = NS(server="Broker-A", observed_monotonic=100, observed_gmt_epoch=NOW,
               uncertainty_seconds=0.1, offset_seconds=7200)
    raw_msc = (NOW + 7200) * 1000 + 750
    return capture_fill_benchmark(
        account=ACCOUNT, clock=clock, native_symbol="EURUSD.ecn", direction=side,
        tick=NS(time_msc=raw_msc, time=raw_msc // 1000, bid=1.1, ask=1.2),
        result=NS(order=87, deal=921), submitted_price=1.2 if side == "BUY" else 1.1,
        submit_monotonic=101, response_monotonic=102, max_tick_age_seconds=10,
    )


def _fixture(monkeypatch, *, side, raw_time, account=ACCOUNT):
    kind = 0 if side == "BUY" else 1
    deal = NS(ticket=921, order=87, magic=12, type=kind, entry=0, symbol="EURUSD.ecn",
              price=1.25 if side == "BUY" else 1.05, volume=0.15, commission=-0.4,
              time_msc=raw_time, time=raw_time // 1000)
    calls = []

    def history(*, ticket):
        calls.append(ticket)
        return (deal,)

    api = NS(account_info=lambda: account, history_deals_get=history,
             DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1, DEAL_ENTRY_IN=0)
    monkeypatch.setattr(mt5_module, "mt5", api)
    connector = MT5Connector.__new__(MT5Connector)
    connector._magic = 12
    connector._require_connection = lambda: None
    connector.symbol_map = lambda symbol: symbol + ".ecn"
    return NS(get_connector=lambda symbol: connector), deal, calls


def _emit(store, receipt, parent="87"):
    store.emit(DE.DECISION_TRADE_ENTRY_SNAPSHOT, "INFO", symbol="EURUSD", parent_id=parent,
               source_module="execution.decision_provenance", payload={"fill_benchmark": receipt},
               durable=True)


@pytest.mark.parametrize("side,offset,domain", [("BUY", 0, "utc"), ("SELL", 7200, "broker_server")])
def test_restart_reconciles_exact_deal_using_submit_quote(monkeypatch, tmp_path, side, offset, domain):
    raw_time = (NOW + offset + 1) * 1000 + 365
    manager, _, calls = _fixture(monkeypatch, side=side, raw_time=raw_time)
    path = str(tmp_path / "events.db")
    store = EventStore(path)
    receipt = _receipt(side)
    assert str(ACCOUNT.login) not in str(receipt)
    _emit(store, receipt)
    _emit(store, receipt)
    _emit(store, dict(receipt, quote_ask=12.0), parent="another-order")
    store.close()
    store = EventStore(path)
    try:
        registry = DataSourceRegistry()
        registry.register(MT5ExecutionCostAdapter(manager, lambda: store))
        bars = {"M5": [{"open": 1., "low": 1., "high": 2., "close": 1.1,
                        "timestamp_epoch": NOW - 300, "source_timestamp_verified": True}]}
        readings = ProviderDataReadings().read_with_context(
            "EURUSD", bars, {"analysis_epoch": NOW + 3, "data_source_registry": registry},
        )
        item = next(r for r in readings if r.concept == "market_impact")
        assert item.source_timestamp_epoch == pytest.approx(NOW + 1.365, rel=0, abs=0.000001)
        assert item.source_timestamp_verified is True
        row, = item.facts["executions"]
        assert row["fill_time_domain"] == domain
        assert row["raw_fill_time_msc"] == raw_time
        assert row["raw_quote_time_msc"] == receipt["quote_raw_time_msc"]
        assert row["benchmark"] == "actual_market_submit_quote"
        assert row["commission_account_currency"] == -0.4 and row["commission_currency"] == "CHF"
        group, = item.facts["observed_slippage_by_side_and_size"]
        assert group["side"] == side and group["executed_size"] == 0.15
        assert group["mean_signed_price_slippage"] == pytest.approx(0.05)
        assert calls == [87]
        assert registry.fetch(ObservationKind.EXECUTION_COST, canonical_instrument="EURUSD",
                              end_epoch=NOW + 1) == ()
    finally:
        store.close()


@pytest.mark.parametrize("change", [
    {"ticket": 888}, {"order": 555}, {"magic": 7}, {"type": 1}, {"entry": 1},
    {"symbol": "EURUSD.pro"}, {"volume": 0.0}, {"time": NOW + 7}, {"price": float("nan")},
    {"time_msc": (NOW + 96) * 1000, "time": NOW + 96},
])
def test_foreign_or_unresolved_deals_are_unknown(monkeypatch, change):
    manager, row, _ = _fixture(monkeypatch, side="BUY", raw_time=(NOW + 1) * 1000)
    for key, value in change.items():
        setattr(row, key, value)
    store = NS(recent_fill_benchmarks=lambda symbol: [_receipt()])
    adapter = MT5ExecutionCostAdapter(manager, lambda: store)
    assert not adapter.fetch(ObservationKind.EXECUTION_COST, canonical_instrument="EURUSD", end_epoch=NOW + 100)


def test_ambiguous_deal_clock_and_missing_benchmark_are_unverified():
    receipt = _receipt()
    assert matched_fill_epoch(dict(receipt, broker_offset_seconds=0.1), (NOW + 1) * 1000 + 300) is None
    assert capture_fill_benchmark(
        account=None, clock=None, tick=None, native_symbol="EURUSD", direction="BUY", result=None,
        submitted_price=1, submit_monotonic=0, response_monotonic=0, max_tick_age_seconds=1,
    ) is None


def test_conflicting_receipt_or_changed_account_never_uses_another_accounts_deal(monkeypatch):
    other = NS(login=88888, server=ACCOUNT.server, currency="CHF")
    manager, _, calls = _fixture(monkeypatch, side="BUY", raw_time=(NOW + 1) * 1000, account=other)
    receipt = _receipt()
    store = NS(recent_fill_benchmarks=lambda symbol: [receipt, dict(receipt, quote_ask=25)])
    adapter = MT5ExecutionCostAdapter(manager, lambda: store)
    kwargs = dict(canonical_instrument="EURUSD", end_epoch=NOW + 3)
    assert not adapter.fetch(ObservationKind.EXECUTION_COST, **kwargs)
    store.recent_fill_benchmarks = lambda symbol: [receipt]
    assert not adapter.fetch(ObservationKind.EXECUTION_COST, **kwargs)
    assert not calls


def test_market_requote_and_durable_open_callback_capture_the_final_request(monkeypatch, tmp_path):
    from collections import deque
    import time
    from event_driven_bootstrap import EventDrivenSystem
    from persistence import event_store as store_module
    from platforms.mt5.clock import MT5ServerClockState

    account = NS(login=3059, server="Broker-A", currency="CAD", margin_free=5000)
    anchor = time.monotonic()
    clock = MT5ServerClockState("Broker-A", 7200, NOW + 7200, NOW, 0.0, anchor, 0.1)
    raw = (NOW + 7200) * 1000
    quotes = deque([
        NS(bid=1.095, ask=1.096, time_msc=raw - 300, time=(raw - 300) // 1000),
        NS(bid=1.100, ask=1.101, time_msc=raw - 200, time=(raw - 200) // 1000),
    ])
    attempts = []

    def fake_submit(request):
        attempts.append(dict(request))
        return (NS(retcode=10004, comment="requote") if len(attempts) == 1
                else NS(retcode=10009, order=87, deal=921, volume=.15, price=1.102))

    fake = NS(
        symbol_info_tick=lambda _: quotes.popleft(), order_calc_margin=lambda *args: 10,
        account_info=lambda: account, order_send=fake_submit,
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1, ORDER_TIME_GTC=0,
        ORDER_FILLING_IOC=1, TRADE_RETCODE_DONE=10009,
    )
    monkeypatch.setattr(mt5_module, "mt5", fake)
    connector = MT5Connector()
    connector._connected = True
    connector.symbol_map = lambda symbol: symbol + ".ecn"
    connector._get_symbol_constraints = lambda _: dict(volume_step=.01, point=.00001)
    connector._effective_min_stop_distance = lambda *args: 0.0
    connector._read_server_clock_state = lambda **kwargs: clock
    result = connector.place_order("EURUSD", "BUY", .15, 1.09, 1.13)
    assert result.success
    assert [row["price"] for row in attempts] == [1.096, 1.101]
    assert result.fill_benchmark["quote_ask"] == 1.101
    assert result.fill_benchmark["quote_raw_time_msc"] == raw - 200

    store = EventStore(str(tmp_path / "callback.db"))
    monkeypatch.setattr(store_module, "get_event_store", lambda: store)
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._ctx = None
    system._entry_context = {}
    system._attach_open_campaign_ticket = lambda *args: None
    intent = NS(symbol="EURUSD", direction="BUY", timestamp=None,
                decision_id="dec-87", campaign_id="campaign", opportunity_id="opp")
    try:
        system._handle_open_result(intent, NS(success=True, broker_response=result))
        assert store.recent_fill_benchmarks("EURUSD") == [result.fill_benchmark]
    finally:
        store.close()
