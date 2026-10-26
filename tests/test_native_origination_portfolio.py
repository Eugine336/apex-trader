"""Offline tests for Brain origination's shared PortfolioDivision boundary.

All broker/account/portfolio dependencies are fakes. No connector is started,
no real order is sent, and no persistent store is opened.
"""
from __future__ import annotations

from types import SimpleNamespace

import event_driven_bootstrap as edb


class _FakePortfolioDivision:
    def __init__(self, *, approved=True, approved_size=0.03, sizing_mode="lots"):
        self.approved = approved
        self.approved_size = approved_size
        self.sizing_mode = sizing_mode
        self.calls = []

    def evaluate(self, candidate, book, account, factors):
        self.calls.append((candidate, list(book), account, factors))
        return SimpleNamespace(
            approved=self.approved,
            approved_size=self.approved_size if self.approved else 0.0,
            sizing_mode=self.sizing_mode,
            risk_pct=0.005,
            max_loss=12.5,
            reason="fixture approved" if self.approved else "fixture rejected",
            rationale=["offline fake"],
        )


class _FakeAccountRisk:
    daily_loss_cap_pct = 3.0

    def __init__(self, *, halted=False, flatten=False, daily_pnl=-25.0):
        self.halted = halted
        self.flatten = flatten
        self.pnl = daily_pnl

    def daily_loss_halted(self, account_key):
        return self.halted

    def flatten_breached(self, account_key):
        return self.flatten

    def combined_pnl(self, account_key):
        return self.pnl


class _FakePlatformManager:
    def __init__(self, *, book=None, book_error=None):
        self.book = [] if book is None else book
        self.book_error = book_error
        self.balance_calls = []
        self.broker_calls = []

    def get_platform_balance(self, symbol):
        self.balance_calls.append(symbol)
        return 10_000.0

    def get_all_open_positions(self):
        if self.book_error is not None:
            raise self.book_error
        return list(self.book)

    def get_broker_name(self, symbol):
        self.broker_calls.append(symbol)
        return "offline-demo"


class _FakeAggregator:
    def __init__(self):
        self.batches = []

    def submit(self, intents):
        self.batches.append(list(intents))


def _system(monkeypatch, *, approved=True, approved_size=0.03,
            sizing_mode="lots", halted=False, flatten=False,
            book=None, book_error=None):
    symbol = "EURUSD"
    internal = edb.resolve_to_internal(symbol)
    # Isolate the instrument lookup; no broker config or live context is read.
    monkeypatch.setattr(
        edb, "INSTRUMENT_REGISTRY",
        {internal: SimpleNamespace(pip_value_per_lot=10.0, pip_size=0.0001)},
    )
    monkeypatch.setattr(
        edb, "build_context_for_symbol",
        lambda _symbol: SimpleNamespace(uses_stake=(sizing_mode == "stake")),
    )
    portfolio = _FakePortfolioDivision(
        approved=approved, approved_size=approved_size, sizing_mode=sizing_mode,
    )
    risk = _FakeAccountRisk(halted=halted, flatten=flatten)
    pm = _FakePlatformManager(book=book, book_error=book_error)
    ctx = SimpleNamespace(
        portfolio=portfolio,
        account_risk=risk,
        account_key=lambda _symbol, _pm: "offline-demo:123",
    )
    system = edb.EventDrivenSystem.__new__(edb.EventDrivenSystem)
    system._ctx = ctx
    system._pm = pm
    system._config = SimpleNamespace(
        cognition=SimpleNamespace(
            origination_risk_fraction=0.01,
            commission_per_lot_round_trip=0.0,
            opportunity_min_net_ev=0.0,
            opportunity_cost_multiple=2.0,
        ),
    )
    system._tick_store = SimpleNamespace(
        get_latest=lambda _symbol: SimpleNamespace(
            bid=1.1000, ask=1.1002, mid=1.1001,
        ),
    )
    system._aggregator = _FakeAggregator()
    system._safe_pip_size = lambda _symbol: 0.0001
    system._effective_pip_value = lambda _symbol, _pip_size: 10.0
    system._opportunity_qualifies = lambda *_args, **_kwargs: True
    system._opportunity_qualifies_stake = lambda *_args, **_kwargs: True
    return system, portfolio, risk, pm


def _origination(**overrides):
    values = {
        "symbol": "EURUSD",
        "direction": "LONG",
        "exposure": 0.5,
        "confidence": 0.8,
        "sl": 1.0990,
        "tp": 1.1026,
        "reason": "offline test",
        "campaign_id": "campaign-test",
        "decision_id": "decision-test",
        "opportunity_id": "opportunity-test",
        "identity_version": "apex-constitution-v1",
        "identity": "Apex is a continuously operating, market-aware trading intelligence.",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_portfolio_sizing_uses_brain_exposure_and_complete_account_context(monkeypatch):
    system, portfolio, risk, pm = _system(monkeypatch, approved_size=0.03)

    sized = system._size_brain_origination_with_portfolio(
        _origination(exposure=0.5), "EURUSD", "LONG", 1.0990,
    )

    assert sized is not None
    assert sized["sizing_mode"] == "lots"
    assert sized["lots"] == 0.03
    assert sized["stake_usd"] is None
    assert sized["entry_price"] == 1.1002
    assert len(portfolio.calls) == 1

    candidate, book, account, factors = portfolio.calls[0]
    assert candidate.symbol == "EURUSD"
    assert candidate.direction == "LONG"
    assert candidate.entry_price == 1.1002
    assert candidate.stop_loss == 1.0990
    assert book == []
    assert account.balance == 10_000.0
    assert account.account_key == "offline-demo:123"
    assert account.daily_pnl == -25.0
    assert account.daily_loss_cap_pct == 3.0
    assert factors.base_risk_pct == 0.005
    assert pm.balance_calls == ["EURUSD"]


def test_daily_loss_halt_blocks_portfolio_sizing(monkeypatch):
    system, portfolio, _risk, _pm = _system(monkeypatch, halted=True)

    sized = system._size_brain_origination_with_portfolio(
        _origination(), "EURUSD", "LONG", 1.0990,
    )

    assert sized is None
    assert portfolio.calls == []


def test_flatten_breach_blocks_portfolio_sizing(monkeypatch):
    system, portfolio, _risk, _pm = _system(monkeypatch, flatten=True)

    sized = system._size_brain_origination_with_portfolio(
        _origination(), "EURUSD", "LONG", 1.0990,
    )

    assert sized is None
    assert portfolio.calls == []


def test_unknown_broker_book_fails_closed_before_portfolio_evaluation(monkeypatch):
    system, portfolio, _risk, _pm = _system(
        monkeypatch, book_error=RuntimeError("book not confirmed"),
    )

    sized = system._size_brain_origination_with_portfolio(
        _origination(), "EURUSD", "LONG", 1.0990,
    )

    assert sized is None
    assert portfolio.calls == []


def test_portfolio_rejection_has_no_legacy_lot_size_fallback(monkeypatch):
    system, portfolio, _risk, _pm = _system(monkeypatch, approved=False)
    legacy_calls = []
    system._origination_lot_size = lambda *args, **kwargs: legacy_calls.append(args) or 1.0

    sink = system._make_origination_sink()
    assert sink(_origination()) is False

    assert len(portfolio.calls) == 1
    assert system._aggregator.batches == []
    assert legacy_calls == []


def test_approved_lots_reach_the_shared_intent_aggregator(monkeypatch):
    system, _portfolio, _risk, _pm = _system(monkeypatch, approved_size=0.03)

    sink = system._make_origination_sink()
    assert sink(_origination()) is True

    assert len(system._aggregator.batches) == 1
    intents = system._aggregator.batches[0]
    assert len(intents) == 1
    intent = intents[0]
    assert intent.symbol == "EURUSD"
    assert intent.direction == "LONG"
    assert intent.lots == 0.03
    assert intent.stake_usd is None
    assert intent.entry_price == 1.1002
    assert intent.new_sl == 1.0990 and intent.new_tp == 1.1026
    assert intent.campaign_id == "campaign-test"
    assert intent.decision_id == "decision-test"
    assert intent.opportunity_id == "opportunity-test"


def test_approved_stake_is_forwarded_as_stake_not_lots(monkeypatch):
    system, _portfolio, _risk, _pm = _system(
        monkeypatch, approved_size=2.5, sizing_mode="stake",
    )

    sink = system._make_origination_sink()
    assert sink(_origination()) is True

    assert len(system._aggregator.batches) == 1
    intent = system._aggregator.batches[0][0]
    assert intent.lots == 0.0
    assert intent.stake_usd == 2.5
    assert intent.entry_price == 1.1002
