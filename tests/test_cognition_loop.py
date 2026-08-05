"""Tests for the cognition loop, evidence consolidator, and Brain→action bridge."""

from types import SimpleNamespace

from cognition.contracts import DecisionType, Evidence, MarketState
from cognition.loop import BrainActionBridge, CognitionLoop, EvidenceConsolidator


# ── EvidenceConsolidator ──────────────────────────────────────────────────────

def test_consolidator_empty_without_ctx():
    ms = EvidenceConsolidator(ctx=None).build("EURUSD")
    assert isinstance(ms, MarketState)
    assert ms.consolidation()["evidence_fresh"] == 0


def test_consolidator_reads_thesis_status():
    thesis = SimpleNamespace(get_status=lambda: {
        "theses": {"EURUSD": {
            "long": {"confidence": 0.8}, "short": {"confidence": 0.1},
            "flat": {"confidence": 0.2},
            "effective": {"dominant": "LONG", "long_ev": 0.6, "short_ev": -0.2, "flat_ev": 0.0},
        }}
    })
    ctx = SimpleNamespace(thesis_engine=thesis)
    ms = EvidenceConsolidator(ctx=ctx).build("EURUSD")
    fresh = ms.fresh_evidence()
    assert len(fresh) == 1
    assert fresh[0].polarity > 0            # LONG dominant ⇒ positive lean
    assert fresh[0].source_module == "brain.thesis_engine"


def test_consolidator_short_is_negative_polarity():
    thesis = SimpleNamespace(get_status=lambda: {
        "theses": {"EURUSD": {
            "short": {"confidence": 0.7},
            "effective": {"dominant": "SHORT", "long_ev": -0.1, "short_ev": 0.5, "flat_ev": 0.0},
        }}
    })
    ms = EvidenceConsolidator(ctx=SimpleNamespace(thesis_engine=thesis)).build("EURUSD")
    assert ms.fresh_evidence()[0].polarity < 0


def test_consolidator_set_vote_source_surfaces_live_votes():
    # With no thesis engine (single-path), the vote source is the Brain's only
    # window onto the live market — set_vote_source must wire it end-to-end.
    votes = [SimpleNamespace(module="structure", direction="LONG", confidence=0.8,
                             weight=1.0, timeframe="H1", evidence={"bos": True})]
    cons = EvidenceConsolidator(ctx=None)
    assert cons.build("XAUUSD").consolidation()["evidence_fresh"] == 0
    cons.set_vote_source(lambda sym: votes if sym == "XAUUSD" else [])
    ms = cons.build("XAUUSD")
    fresh = ms.fresh_evidence()
    assert len(fresh) == 1
    assert fresh[0].source_module == "structure" and fresh[0].polarity > 0
    assert fresh[0].measurements.get("bos") is True


def test_consolidator_vote_source_is_fault_safe():
    cons = EvidenceConsolidator(ctx=None)
    def _boom(_sym):
        raise RuntimeError("store down")
    cons.set_vote_source(_boom)
    ms = cons.build("XAUUSD")  # must not raise
    assert ms.consolidation()["evidence_fresh"] == 0


def test_loop_set_vote_source_delegates_to_consolidator():
    seen = {}
    consolidator = EvidenceConsolidator(ctx=None)
    loop = CognitionLoop(_StubBrain(), consolidator, lambda: ["X"], interval_seconds=1.0)
    fn = lambda sym: []
    loop.set_vote_source(fn)
    assert consolidator._vote_source is fn


# ── CognitionLoop ─────────────────────────────────────────────────────────────

class _StubBrain:
    def __init__(self):
        self.calls = []

    def reason(self, market_state, now=None):
        self.calls.append(market_state.symbol)
        return SimpleNamespace(
            decision=SimpleNamespace(symbol=market_state.symbol, authorises_action=True),
            direction="LONG",
        )


class _StubConsolidator:
    def build(self, symbol, now=None):
        return MarketState(symbol=symbol)


class _StubBridge:
    def __init__(self):
        self.seen = []

    def on_decision(self, output):
        self.seen.append(output)


def test_loop_run_once_drives_brain_per_symbol():
    brain = _StubBrain()
    bridge = _StubBridge()
    loop = CognitionLoop(brain, _StubConsolidator(), lambda: ["EURUSD", "GBPUSD"],
                         action_bridge=bridge)
    made = loop.run_once()
    assert made == 2
    assert brain.calls == ["EURUSD", "GBPUSD"]
    assert len(bridge.seen) == 2


def test_loop_symbols_provider_fault_is_fail_safe():
    def _boom():
        raise RuntimeError("down")

    loop = CognitionLoop(_StubBrain(), _StubConsolidator(), _boom)
    assert loop.run_once() == 0


def test_loop_caps_symbols():
    brain = _StubBrain()
    loop = CognitionLoop(brain, _StubConsolidator(),
                         lambda: [f"S{i}" for i in range(30)], max_symbols_per_cycle=4)
    assert loop.run_once() == 4
    assert len(brain.calls) == 4


def test_loop_start_stop_toggles():
    loop = CognitionLoop(_StubBrain(), _StubConsolidator(), lambda: ["X"], interval_seconds=1.0)
    loop.start()
    assert loop.running is True
    loop.stop()
    assert loop.running is False


# ── BrainActionBridge ─────────────────────────────────────────────────────────

class _StubOrchestrator:
    def __init__(self):
        self.submitted = []

    def submit(self, objective):
        self.submitted.append(objective)
        return SimpleNamespace(objective=objective)


def _brain_output(symbol="EURUSD", direction="LONG", authorises=True, confidence=0.8):
    decision = SimpleNamespace(
        symbol=symbol, authorises_action=authorises, confidence=confidence,
        thesis="t", decision_id="d1",
    )
    return SimpleNamespace(decision=decision, direction=direction)


def test_bridge_emits_on_campaign_open():
    orch = _StubOrchestrator()
    bridge = BrainActionBridge(orch, notify_enabled=True)
    bridge.on_decision(_brain_output(authorises=True))
    assert len(orch.submitted) == 1
    obj = orch.submitted[0]
    assert obj.capability == "operator.notify"
    assert obj.source == "ai_brain"


def test_bridge_silent_on_non_authorising_decision():
    orch = _StubOrchestrator()
    BrainActionBridge(orch).on_decision(_brain_output(authorises=False))
    assert orch.submitted == []


def test_bridge_noop_without_orchestrator():
    # Must not raise when there is no orchestrator.
    BrainActionBridge(None).on_decision(_brain_output())


def test_bridge_respects_disabled():
    orch = _StubOrchestrator()
    BrainActionBridge(orch, notify_enabled=False).on_decision(_brain_output())
    assert orch.submitted == []


# ── Origination (Phase G — Brain originates entries from its campaign spec) ────

from cognition.contracts import CampaignSpecification, DecisionPackage


class _OpenBrain:
    """Emits an OPEN_CAMPAIGN BrainOutput with a directional campaign per symbol."""

    def __init__(self, direction="LONG", exposure=0.5, confidence=0.8):
        self._dir = direction
        self._exp = exposure
        self._conf = confidence

    def reason(self, market_state, now=None):
        sym = market_state.symbol
        decision = DecisionPackage(
            symbol=sym, decision_type=DecisionType.OPEN_CAMPAIGN,
            thesis="breakout", confidence=self._conf,
        )
        campaign = CampaignSpecification(
            symbol=sym, direction=self._dir, desired_exposure=self._exp,
            confidence=self._conf, decision_id=decision.decision_id,
        )
        return SimpleNamespace(decision=decision, campaign=campaign, direction=self._dir)


def test_origination_shadow_records_intended_submits_nothing():
    loop = CognitionLoop(
        _OpenBrain(), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="shadow", balance_provider=lambda s: 10_000.0,
    )
    loop.run_once()
    st = loop.get_status()
    assert st["origination_mode"] == "shadow"
    assert st["orig_intended"] == 1
    assert st["orig_submitted"] == 0


def test_origination_live_calls_sink():
    submitted = []
    loop = CognitionLoop(
        _OpenBrain(), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="live", balance_provider=lambda s: 10_000.0,
    )
    loop.set_origination_sink(lambda intent: submitted.append(intent))
    loop.run_once()
    st = loop.get_status()
    assert st["orig_submitted"] == 1
    assert st["orig_intended"] == 0
    assert submitted and submitted[0].symbol == "EURUSD"
    assert abs(submitted[0].stake_usd - 50.0) < 1e-6


def test_origination_live_without_sink_degrades_to_shadow():
    loop = CognitionLoop(
        _OpenBrain(), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="live", balance_provider=lambda s: 10_000.0,
    )
    loop.run_once()
    assert loop.get_status()["orig_intended"] == 1


def test_origination_off_never_originates():
    loop = CognitionLoop(
        _OpenBrain(), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="off", balance_provider=lambda s: 10_000.0,
    )
    loop.run_once()
    st = loop.get_status()
    assert st["orig_intended"] == 0
    assert st["orig_submitted"] == 0


def test_origination_suppressed_for_already_open_book():
    pos = SimpleNamespace(symbol="EURUSD", direction="LONG")
    loop = CognitionLoop(
        _OpenBrain(direction="LONG"), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="shadow", balance_provider=lambda s: 10_000.0,
        position_source=lambda: [pos],
    )
    loop.run_once()
    assert loop.get_status()["orig_intended"] == 0


def test_origination_deduplicates_within_cycle():
    loop = CognitionLoop(
        _OpenBrain(direction="LONG"), _StubConsolidator(),
        lambda: ["EURUSD", "EURUSD"],
        origination_mode="shadow", balance_provider=lambda s: 10_000.0,
    )
    loop.run_once()
    assert loop.get_status()["orig_intended"] == 1


def test_origination_flat_decision_never_originates():
    class _FlatBrain:
        def reason(self, market_state, now=None):
            decision = DecisionPackage(
                symbol=market_state.symbol,
                decision_type=DecisionType.CONTINUE_OBSERVING,
            )
            return SimpleNamespace(decision=decision, campaign=None, direction="FLAT")

    loop = CognitionLoop(
        _FlatBrain(), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="shadow", balance_provider=lambda s: 10_000.0,
    )
    loop.run_once()
    assert loop.get_status()["orig_intended"] == 0


# ── Institutional memory (Phase H — Part VII) ─────────────────────────────────

from cognition.contracts import Evidence, EvidenceDomain
from cognition.memory import CampaignMemoryStore


class _EvidenceConsolidator:
    """Builds a MarketState with one directional momentum Evidence per symbol."""

    def build(self, symbol, now=None):
        ms = MarketState(symbol=symbol)
        ms.add(Evidence(source_module="m", domain=EvidenceDomain.MOMENTUM,
                        symbol=symbol, polarity=0.8, confidence=0.8))
        return ms


def test_loop_records_open_snapshot_to_memory():
    store = CampaignMemoryStore(db_path=":memory:")
    loop = CognitionLoop(
        _OpenBrain(), _EvidenceConsolidator(), lambda: ["EURUSD"],
        origination_mode="off", memory=store,
    )
    loop.run_once()
    st = loop.get_status()
    assert st["memory_enabled"] is True
    assert st["memory_opens"] == 1
    assert store.count() == 1        # one open snapshot recorded
    store.close()


def test_consolidator_injects_analogue_evidence_from_memory():
    from cognition.loop import EvidenceConsolidator
    from cognition.memory import fingerprint_from_market_state

    store = CampaignMemoryStore(db_path=":memory:")
    fp = fingerprint_from_market_state(MarketState(symbol="EURUSD"))
    # Seed a completed winning LONG campaign with a momentum-heavy fingerprint.
    ms_seed = MarketState(symbol="EURUSD")
    ms_seed.add(Evidence(source_module="m", domain=EvidenceDomain.MOMENTUM,
                         symbol="EURUSD", polarity=0.8, confidence=0.8))
    store.record_open(symbol="EURUSD", direction="LONG",
                      fingerprint=fingerprint_from_market_state(ms_seed))
    store.record_close(SimpleNamespace(to_dict=lambda: {
        "symbol": "EURUSD", "direction": "LONG", "state": "completed",
        "realized_pnl": 10.0, "ended_reason": "tp",
        "postmortem": {"outcome_won": True, "verdict": "validated", "reasoning_quality": 0.8},
    }))
    consolidator = EvidenceConsolidator(ctx=None, memory=store)
    ms = consolidator.build("EURUSD", injected=[Evidence(
        source_module="m", domain=EvidenceDomain.MOMENTUM, symbol="EURUSD",
        polarity=0.7, confidence=0.8)])
    analogue = [e for e in ms.evidence if e.domain == EvidenceDomain.HISTORICAL_ANALOGUE]
    assert len(analogue) == 1
    assert analogue[0].polarity > 0     # a won LONG analogue leans long
    store.close()


# ── Operations author → sink wiring (Phase I, Part IX Art 9/11) ───────────────

from cognition.operations import CAP_OPERATOR_NOTIFY, OperationsAuthor


def test_loop_drains_operations_author_to_sink():
    submitted = []
    author = OperationsAuthor(enabled=True, cooldown_seconds=0.0,
                              report_period_seconds=1e12)
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG",
                                    verdict="validated", reasoning_quality=0.9)
    loop = CognitionLoop(
        _OpenBrain(), _StubConsolidator(), lambda: [],
        operations_author=author, operations_sink=lambda i: submitted.append(i),
    )
    loop.run_once()
    assert len(submitted) == 1
    assert submitted[0].intent == CAP_OPERATOR_NOTIFY
    assert loop.get_status()["operations_enabled"] is True
    assert loop.get_status()["ops_submitted"] == 1


def test_loop_operations_inert_when_author_disabled():
    submitted = []
    author = OperationsAuthor(enabled=False)
    loop = CognitionLoop(
        _OpenBrain(), _StubConsolidator(), lambda: [],
        operations_author=author, operations_sink=lambda i: submitted.append(i),
    )
    loop.run_once()
    assert submitted == []
    assert loop.get_status()["ops_submitted"] == 0
