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
    assert fresh[0].polarity == 0.0        # Part XXV — non-directional reading
    assert fresh[0].source_module == "brain.thesis_engine"


def test_consolidator_short_is_non_directional():
    thesis = SimpleNamespace(get_status=lambda: {
        "theses": {"EURUSD": {
            "short": {"confidence": 0.7},
            "effective": {"dominant": "SHORT", "long_ev": -0.1, "short_ev": 0.5, "flat_ev": 0.0},
        }}
    })
    ms = EvidenceConsolidator(ctx=SimpleNamespace(thesis_engine=thesis)).build("EURUSD")
    assert ms.fresh_evidence()[0].polarity == 0.0


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
    assert fresh[0].source_module == "structure" and fresh[0].polarity == 0.0
    assert fresh[0].measurements.get("bos") is True


def test_consolidator_vote_source_is_fault_safe():
    cons = EvidenceConsolidator(ctx=None)
    def _boom(_sym):
        raise RuntimeError("store down")
    cons.set_vote_source(_boom)
    ms = cons.build("XAUUSD")  # must not raise
    # Article XXXIV — a failed source no longer degrades silently: instead of
    # producing no evidence, it surfaces a zero-confidence integrity Evidence so
    # the Brain can tell "no observations" apart from "observations unavailable".
    integrity = [e for e in ms.evidence if e.source_module == "consolidator.integrity"]
    assert any("MODULE OBSERVATIONS UNAVAILABLE" in e.observation for e in integrity)
    assert all(e.confidence == 0.0 and e.uncertainty == 1.0 for e in integrity)


def test_loop_set_vote_source_delegates_to_consolidator():
    seen = {}
    consolidator = EvidenceConsolidator(ctx=None)
    loop = CognitionLoop(_StubBrain(), consolidator, lambda: ["X"], interval_seconds=1.0)
    fn = lambda sym: []
    loop.set_vote_source(fn)
    assert consolidator._vote_source is fn


def test_consolidator_set_developing_source_surfaces_bias():
    cons = EvidenceConsolidator(ctx=None)
    assert cons.build("XAUUSD").consolidation()["evidence_fresh"] == 0
    cons.set_developing_source(lambda sym: {
        "direction": "LONG", "confidence": 0.7, "conflict_score": 0.1,
        "long_probability": 0.7, "short_probability": 0.3,
    })
    fresh = cons.build("XAUUSD").fresh_evidence()
    assert len(fresh) == 1
    assert fresh[0].source_module == "world_model.developing"
    assert fresh[0].polarity == 0.0
    assert fresh[0].measurements.get("developing") is True


def test_consolidator_developing_source_is_fault_safe():
    cons = EvidenceConsolidator(ctx=None)
    def _boom(_sym):
        raise RuntimeError("store down")
    cons.set_developing_source(_boom)
    assert cons.build("XAUUSD").consolidation()["evidence_fresh"] == 0  # must not raise


def test_loop_set_developing_source_delegates_to_consolidator():
    consolidator = EvidenceConsolidator(ctx=None)
    loop = CognitionLoop(_StubBrain(), consolidator, lambda: ["X"], interval_seconds=1.0)
    fn = lambda sym: {}
    loop.set_developing_source(fn)
    assert consolidator._developing_source is fn


def test_consolidator_structural_interaction_source_surfaces_evidence():
    # Violation #2 — inter-candle structural interactions ride into the state.
    cons = EvidenceConsolidator(ctx=None)
    assert cons.build("XAUUSD").consolidation()["evidence_fresh"] == 0
    ev = Evidence(
        source_module="market.structural_interaction",
        domain=EvidenceDomain.LIQUIDITY, symbol="XAUUSD",
        observation="XAUUSD M15 sell_side liquidity level sweep @ 1900.0",
        confidence=0.62, uncertainty=0.38, polarity=0.0,
        measurements={"interaction": "level_sweep", "level_kind": "liquidity"},
    )
    cons.set_structural_interaction_source(lambda sym: [ev] if sym == "XAUUSD" else [])
    fresh = cons.build("XAUUSD").fresh_evidence()
    assert any(e.source_module == "market.structural_interaction" for e in fresh)
    got = next(e for e in fresh if e.source_module == "market.structural_interaction")
    assert got.polarity == 0.0
    assert got.measurements.get("interaction") == "level_sweep"


def test_consolidator_structural_interaction_source_is_fault_safe():
    cons = EvidenceConsolidator(ctx=None)
    def _boom(_sym):
        raise RuntimeError("detector down")
    cons.set_structural_interaction_source(_boom)
    assert cons.build("XAUUSD").consolidation()["evidence_fresh"] == 0  # must not raise


def test_loop_set_structural_interaction_source_delegates_to_consolidator():
    consolidator = EvidenceConsolidator(ctx=None)
    loop = CognitionLoop(_StubBrain(), consolidator, lambda: ["X"], interval_seconds=1.0)
    fn = lambda sym: []
    loop.set_structural_interaction_source(fn)
    assert consolidator._structural_interaction_source is fn


class _StubKnowledge:
    def __init__(self):
        self.calls = 0

    def evidence_for(self, symbol, *, now=None):
        from cognition.contracts import Evidence, EvidenceDomain
        self.calls += 1
        return [Evidence(source_module="composio.knowledge",
                         domain=EvidenceDomain.MACRO, symbol=symbol,
                         observation="external context", confidence=0.4)]


def test_consolidator_surfaces_knowledge_evidence():
    ks = _StubKnowledge()
    cons = EvidenceConsolidator(ctx=None, knowledge=ks)
    fresh = cons.build("XAUUSD").fresh_evidence()
    assert ks.calls == 1
    assert any(e.source_module == "composio.knowledge" for e in fresh)


def test_consolidator_knowledge_is_fault_safe():
    class _Boom:
        def evidence_for(self, symbol, *, now=None):
            raise RuntimeError("composio down")
    cons = EvidenceConsolidator(ctx=None, knowledge=_Boom())
    assert cons.build("XAUUSD").consolidation()["evidence_fresh"] == 0  # must not raise


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


def test_loop_rotates_across_cycles_over_full_universe():
    # The periodic cycle must ROTATE, not always reason the first N symbols —
    # otherwise symbols past N are starved (the weekend "only 12 of 50" bug).
    brain = _StubBrain()
    syms = [f"S{i}" for i in range(5)]
    loop = CognitionLoop(brain, _StubConsolidator(), lambda: syms, max_symbols_per_cycle=2)
    loop.run_once()   # S0, S1
    loop.run_once()   # S2, S3
    loop.run_once()   # S4, S0 (wrap)
    assert brain.calls == ["S0", "S1", "S2", "S3", "S4", "S0"]
    assert set(brain.calls) == set(syms)   # whole universe covered within ceil(5/2) cycles


def test_loop_cap_ge_universe_covers_all_each_cycle():
    brain = _StubBrain()
    syms = ["A", "B", "C"]
    loop = CognitionLoop(brain, _StubConsolidator(), lambda: syms, max_symbols_per_cycle=10)
    loop.run_once()
    loop.run_once()
    assert brain.calls == ["A", "B", "C", "A", "B", "C"]
    assert loop._cycle_offset == 0


def test_loop_empty_universe_is_noop():
    loop = CognitionLoop(_StubBrain(), _StubConsolidator(), lambda: [], max_symbols_per_cycle=4)
    assert loop.run_once() == 0


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

from cognition.contracts import EvidenceDomain
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
    assert analogue[0].polarity == 0.0     # Part XXV — win/loss stats, no lean
    assert analogue[0].measurements["wins"] == 1
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


# ── Event-driven "breathing" (Part XII) ───────────────────────────────────────

class _ManualClock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def _event_loop(clock=None):
    return CognitionLoop(
        _StubBrain(), _StubConsolidator(), lambda: ["XAUUSD"],
        interval_seconds=30.0, event_driven=True,
        event_min_interval_seconds=8.0, event_confidence_delta=0.15,
        clock=clock or _ManualClock(),
    )


def test_event_driven_off_is_noop():
    loop = CognitionLoop(_StubBrain(), _StubConsolidator(), lambda: ["X"],
                         event_driven=False)
    assert loop.maybe_reason_on_change("X", 0.9) is False
    assert loop._drain_pending() == []


def test_event_first_read_triggers():
    loop = _event_loop()
    assert loop.maybe_reason_on_change("XAUUSD", 0.6) is True
    assert "XAUUSD" in loop._drain_pending()


def test_event_no_trigger_when_small_magnitude_delta():
    clk = _ManualClock()
    loop = _event_loop(clk)
    assert loop.maybe_reason_on_change("XAUUSD", 0.60) is True
    clk.t += 100.0  # clear the floor
    # sub-delta magnitude move ⇒ no trigger (non-directional)
    assert loop.maybe_reason_on_change("XAUUSD", 0.70) is False


def test_event_triggers_on_bare_nudge():
    clk = _ManualClock()
    loop = _event_loop(clk)
    assert loop.maybe_reason_on_change("XAUUSD", 0.6) is True
    clk.t += 100.0
    # a bare nudge (no magnitude) always wakes the Brain (throttled only)
    assert loop.maybe_reason_on_change("XAUUSD") is True


def test_event_triggers_on_magnitude_jump():
    clk = _ManualClock()
    loop = _event_loop(clk)
    assert loop.maybe_reason_on_change("XAUUSD", 0.50) is True
    clk.t += 100.0
    assert loop.maybe_reason_on_change("XAUUSD", 0.70) is True  # +0.20 >= 0.15


def test_event_per_symbol_floor_throttles():
    clk = _ManualClock()
    loop = _event_loop(clk)
    assert loop.maybe_reason_on_change("XAUUSD", 0.6) is True
    # within the 8s floor a fresh material change is throttled, not queued
    clk.t += 3.0
    assert loop.maybe_reason_on_change("XAUUSD", 0.9) is False
    assert loop.get_status()["event_throttled"] == 1
    # once the floor elapses it fires again
    clk.t += 6.0
    assert loop.maybe_reason_on_change("XAUUSD", 0.6) is True


def test_reason_symbol_now_reasons_once():
    brain = _StubBrain()
    loop = CognitionLoop(brain, _StubConsolidator(), lambda: ["XAUUSD"],
                         event_driven=True)
    made = loop.reason_symbol_now("XAUUSD")
    assert made == 1
    assert brain.calls == ["XAUUSD"]
    assert loop.get_status()["event_reasons"] == 1


def test_event_trigger_is_fault_safe():
    loop = _event_loop()
    # non-numeric magnitude must not raise (treated as a bare nudge)
    assert loop.maybe_reason_on_change("XAUUSD", None) is True


# ── Brain management → sink (Part VI) ─────────────────────────────────────────

class _ManageBrain:
    """Brain stub whose manage() emits a fixed management decision."""

    available = True

    def __init__(self, decision_type):
        self._dt = decision_type
        self.managed = 0

    def reason(self, market_state, now=None):
        return SimpleNamespace(
            decision=SimpleNamespace(symbol=market_state.symbol, authorises_action=False),
            direction="LONG")

    def manage(self, position, market_state, now=None):
        self.managed += 1
        dec = SimpleNamespace(decision_type=self._dt, symbol=position.symbol,
                              confidence=0.8, thesis="mgmt", decision_id="d1")
        return SimpleNamespace(decision=dec, direction=position.direction)


def _mgmt_pos(symbol="XAUUSD", direction="LONG"):
    return SimpleNamespace(symbol=symbol, direction=direction)


def test_management_shadow_records_but_does_not_call_sink():
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.EXIT), _StubConsolidator(), lambda: [],
        position_source=lambda: [_mgmt_pos()], management_mode="shadow",
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    st = loop.get_status()
    assert st["manage_intended"] == 1
    assert st["manage_submitted"] == 0
    assert calls == []   # shadow never calls the sink


def test_management_live_calls_sink():
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.EXIT), _StubConsolidator(), lambda: [],
        position_source=lambda: [_mgmt_pos()], management_mode="live",
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    st = loop.get_status()
    assert st["manage_submitted"] == 1
    assert len(calls) == 1 and calls[0].kind == "close"


def test_management_hold_is_noop():
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.HOLD), _StubConsolidator(), lambda: [],
        position_source=lambda: [_mgmt_pos()], management_mode="live",
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    assert loop.get_status()["manage_intended"] == 0
    assert calls == []


def test_management_off_skips_entirely():
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.EXIT), _StubConsolidator(), lambda: [],
        position_source=lambda: [_mgmt_pos()], management_mode="off",
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    assert loop.get_status()["manage_intended"] == 0
    assert calls == []


def test_management_live_without_sink_degrades_to_shadow():
    loop = CognitionLoop(
        _ManageBrain(DecisionType.REVERSE), _StubConsolidator(), lambda: [],
        position_source=lambda: [_mgmt_pos()], management_mode="live",
    )
    loop.run_once()  # no sink wired
    st = loop.get_status()
    assert st["manage_intended"] == 1 and st["manage_submitted"] == 0


# ── Part XVIII Art 13 — event-driven (not clock-gated) management ─────────────

def test_event_reason_manages_symbol_immediately():
    """A symbol re-reasoned on an event wake must manage its open campaign
    right then — not wait for the periodic cycle (Part XVIII Art 13)."""
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.EXIT), _StubConsolidator(), lambda: ["XAUUSD"],
        interval_seconds=30.0, event_driven=True, event_min_interval_seconds=8.0,
        position_source=lambda: [_mgmt_pos("XAUUSD")], management_mode="live",
        clock=_ManualClock(),
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.reason_symbol_now("XAUUSD")
    st = loop.get_status()
    assert st["manage_submitted"] == 1
    assert len(calls) == 1 and calls[0].kind == "close"


def test_event_management_respects_per_symbol_floor():
    """Back-to-back events within the floor must not re-manage the same
    campaign; once the floor clears, management resumes."""
    calls = []
    clk = _ManualClock()
    loop = CognitionLoop(
        _ManageBrain(DecisionType.EXIT), _StubConsolidator(), lambda: ["XAUUSD"],
        interval_seconds=30.0, event_driven=True, event_min_interval_seconds=8.0,
        position_source=lambda: [_mgmt_pos("XAUUSD")], management_mode="live",
        clock=clk,
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.reason_symbol_now("XAUUSD")
    loop.reason_symbol_now("XAUUSD")  # within the 8s floor → throttled
    assert len(calls) == 1
    clk.t += 10.0                      # clear the floor
    loop.reason_symbol_now("XAUUSD")
    assert len(calls) == 2


def test_periodic_backstop_manages_unreasoned_symbol():
    """A campaign whose symbol is NOT in this cycle's reasoning slice is still
    managed by the periodic backstop (Art 16 — every campaign, every cycle)."""
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.EXIT), _StubConsolidator(), lambda: ["OTHER"],
        position_source=lambda: [_mgmt_pos("XAUUSD")], management_mode="live",
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    assert len(calls) == 1 and calls[0].kind == "close"


# ── Part XVIII Art 1/8/11/12 — campaign registry driven by the Brain ─────────

class _FakeRegistry:
    """Records the loop's campaign-lifecycle calls for assertions."""

    def __init__(self):
        self.theses = []
        self.legs = []
        self.managed = []

    def observe_thesis(self, symbol, direction, should_act, ev_over_flat=0.0,
                       confidence=0.0, now=None):
        self.theses.append((symbol, direction, should_act, ev_over_flat, confidence))

    def record_leg(self, symbol, direction, kind, *, size=0.0, price=0.0,
                   pnl=0.0, ticket="", now=None):
        self.legs.append((symbol, direction, kind, size))

    def record_management(self, symbol, direction, objective, *, confidence=0.0,
                          ev_over_flat=0.0, uncertainty=0.0, now=None):
        self.managed.append((symbol, direction, objective, confidence))

    def portfolio_assessment(self, now=None):
        return {"campaign_count": 0, "correlated_clusters": [],
                "concentration": 0.0, "concentration_warning": False}

    def live_campaigns(self):
        return []


def test_origination_live_births_campaign():
    reg = _FakeRegistry()
    loop = CognitionLoop(
        _OpenBrain(direction="LONG"), _StubConsolidator(), lambda: ["EURUSD"],
        origination_mode="live", campaign_registry=reg,
    )
    loop.set_origination_sink(lambda intent: None)
    loop.run_once()
    assert ("EURUSD", "LONG", True) == reg.theses[0][:3]
    assert ("EURUSD", "LONG", "open") == reg.legs[0][:3]


def test_management_records_campaign_health():
    reg = _FakeRegistry()
    loop = CognitionLoop(
        _ManageBrain(DecisionType.TIGHTEN_RISK), _StubConsolidator(), lambda: [],
        position_source=lambda: [_mgmt_pos("XAUUSD", "LONG")],
        management_mode="live", campaign_registry=reg,
    )
    loop.set_management_sink(lambda action, pos: None)
    loop.run_once()
    assert reg.managed
    sym, direction, objective, _conf = reg.managed[0]
    assert sym == "XAUUSD" and direction == "LONG"
    assert objective == "tighten_risk"


def test_closed_campaign_rearms_reasoning():
    """Art 8 — the instant a campaign's position leaves the book, reasoning is
    re-armed so the Brain immediately re-checks the opportunity."""
    book = {"n": 0}

    def _positions():
        # First backstop pass sees the position; the next sees it gone.
        book["n"] += 1
        return [_mgmt_pos("XAUUSD", "LONG")] if book["n"] == 1 else []

    reg = _FakeRegistry()
    loop = CognitionLoop(
        _ManageBrain(DecisionType.HOLD), _StubConsolidator(), lambda: [],
        position_source=_positions, management_mode="live",
        event_driven=True, campaign_registry=reg, clock=_ManualClock(),
    )
    loop._manage_open_positions()          # sees XAUUSD → known_open={XAUUSD}
    loop._manage_open_positions()          # XAUUSD gone → re-arm
    assert "XAUUSD" in loop._drain_pending()


def test_portfolio_evidence_reaches_market_state():
    """Art 11 — the consolidator surfaces correlated-book context for a symbol
    that shares a leg with a live cluster."""
    from brain.campaign import CampaignRegistry
    from cognition.loop import EvidenceConsolidator
    reg = CampaignRegistry(enabled=True)
    reg.record_management("EURUSD", "LONG", "hold", confidence=0.6)
    reg.record_management("GBPUSD", "LONG", "hold", confidence=0.6)
    cons = EvidenceConsolidator()
    cons.set_portfolio_source(reg.portfolio_assessment)
    ms = cons.build("AUDUSD")   # shares USD with the live cluster
    portfolio = [e for e in ms.evidence
                 if e.domain.value == "portfolio"]
    assert portfolio, "no portfolio-context evidence surfaced"
    assert any("USD" in e.observation for e in portfolio)


def _pos_r(symbol, direction="LONG", profit_r=0.0):
    return SimpleNamespace(symbol=symbol, direction=direction,
                           profit_r=profit_r, size=0.01)


def test_reallocation_trims_weakest_correlated_via_sink():
    """Art 11/XXVII — an over-concentrated correlated book trims its weakest
    campaign through the SAME management sink (partial_close), but ONLY once the
    Brain's reasoned verdict de-risks the candidate (SCALE_OUT here)."""
    from brain.campaign import CampaignRegistry
    reg = CampaignRegistry(enabled=True)
    positions = [
        _pos_r("EURUSD", "LONG", profit_r=2.0),
        _pos_r("GBPUSD", "LONG", profit_r=1.0),
        _pos_r("AUDUSD", "LONG", profit_r=-0.5),   # weakest USD-correlated campaign
    ]
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.SCALE_OUT), _StubConsolidator(), lambda: [],
        position_source=lambda: positions, management_mode="live",
        campaign_registry=reg, reallocation_enabled=True, clock=_ManualClock(),
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    trims = [a for a in calls if a.kind == "partial_close"]
    assert trims, "no reallocation trim was issued"
    assert any(a.symbol == "AUDUSD" for a in trims)
    assert loop.get_status()["reallocations"] >= 1


def test_reallocation_brain_veto_prevents_mechanical_trim():
    """Art XXVII — the registry proposes a trim but a confident Brain HOLD
    vetoes it: the reasoned portfolio view overrides the mechanical rule."""
    from brain.campaign import CampaignRegistry
    reg = CampaignRegistry(enabled=True)
    positions = [
        _pos_r("EURUSD", "LONG", profit_r=2.0),
        _pos_r("GBPUSD", "LONG", profit_r=1.0),
        _pos_r("AUDUSD", "LONG", profit_r=-0.5),
    ]
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.HOLD), _StubConsolidator(), lambda: [],
        position_source=lambda: positions, management_mode="live",
        campaign_registry=reg, reallocation_enabled=True, clock=_ManualClock(),
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    assert [a for a in calls if a.kind == "partial_close"] == []
    assert loop.get_status()["reallocations"] == 0


def test_reallocation_disabled_is_noop():
    from brain.campaign import CampaignRegistry
    reg = CampaignRegistry(enabled=True)
    positions = [
        _pos_r("EURUSD", "LONG", 2.0),
        _pos_r("GBPUSD", "LONG", 1.0),
        _pos_r("AUDUSD", "LONG", -0.5),
    ]
    calls = []
    loop = CognitionLoop(
        _ManageBrain(DecisionType.HOLD), _StubConsolidator(), lambda: [],
        position_source=lambda: positions, management_mode="live",
        campaign_registry=reg, reallocation_enabled=False, clock=_ManualClock(),
    )
    loop.set_management_sink(lambda action, pos: calls.append(action))
    loop.run_once()
    assert [a for a in calls if a.kind == "partial_close"] == []


# ── V11: influence weighting applied live when enabled ─────────────────────────

def test_consolidator_applies_influence_weights_when_enabled():
    """Part VIII / Art XXXI — a source past the significance floor earns a
    non-neutral weight that is applied to the live consolidation."""
    from cognition.influence import InfluenceLedger
    led = InfluenceLedger(min_samples=10, gain=1.0, max_weight=1.5)
    for _ in range(10):
        led.observe("mod.a", won=True)          # 100% win rate → weight 1.5
    cons = EvidenceConsolidator(ctx=None, influence=led, influence_enabled=True)
    ev = Evidence(source_module="mod.a", confidence=0.8, polarity=0.0, symbol="EURUSD")
    ms = cons.build("EURUSD", injected=[ev])
    assert ms.influence_weights.get("mod.a") == 1.5
    assert ms.consolidation()["influence_weighted"] is True


def test_consolidator_neutral_weight_below_min_samples():
    """A source without enough samples stays neutral (1.0) — the safety floor."""
    from cognition.influence import InfluenceLedger
    led = InfluenceLedger(min_samples=20)
    for _ in range(5):
        led.observe("mod.a", won=True)          # below significance floor
    cons = EvidenceConsolidator(ctx=None, influence=led, influence_enabled=True)
    ev = Evidence(source_module="mod.a", confidence=0.8, polarity=0.0, symbol="EURUSD")
    ms = cons.build("EURUSD", injected=[ev])
    assert ms.influence_weights.get("mod.a") == 1.0


def test_consolidator_ignores_influence_when_disabled():
    from cognition.influence import InfluenceLedger
    led = InfluenceLedger(min_samples=10, gain=1.0, max_weight=1.5)
    for _ in range(10):
        led.observe("mod.a", won=True)
    cons = EvidenceConsolidator(ctx=None, influence=led, influence_enabled=False)
    ev = Evidence(source_module="mod.a", confidence=0.8, polarity=0.0, symbol="EURUSD")
    ms = cons.build("EURUSD", injected=[ev])
    assert ms.consolidation()["influence_weighted"] is False


def test_consolidator_influence_enabled_by_default():
    """Art XXXI — applying learned weights is live by default (safe: the ledger's
    significance floor leaves under-sampled sources neutral)."""
    from cognition.influence import InfluenceLedger
    led = InfluenceLedger(min_samples=10, gain=1.0, max_weight=1.5)
    for _ in range(10):
        led.observe("mod.a", won=True)
    cons = EvidenceConsolidator(ctx=None, influence=led)   # no influence_enabled arg
    ev = Evidence(source_module="mod.a", confidence=0.8, polarity=0.0, symbol="EURUSD")
    ms = cons.build("EURUSD", injected=[ev])
    assert ms.influence_weights.get("mod.a") == 1.5
