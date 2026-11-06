from types import SimpleNamespace

from cognition.contracts import DecisionType
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION
from cognition.loop import CognitionLoop
from cognition.memory import CampaignMemoryStore


class _UnusedBrain:
    pass


class _UnusedConsolidator:
    pass


def _output():
    return SimpleNamespace(decision=SimpleNamespace(
        decision_type=DecisionType.SCALE_OUT,
        symbol="EURUSD",
        confidence=0.8,
        thesis="completed concept milestone",
        decision_id="decision-1",
        campaign_id="campaign-1",
        identity_version=IDENTITY_VERSION,
        identity=IDENTITY_STATEMENT,
        questions_answered={
            "native_management_baseline": {
                "management_plan": {
                    "kind": "milestone_partial",
                    "milestone_id": "market_structure:H1:1:1.105",
                    "milestone_price": 1.105,
                    "partial_fraction": 0.5,
                    "action_id": "campaign-1:milestone:market_structure:H1:1:1.105",
                },
            },
        },
    ))


def _position():
    return SimpleNamespace(
        symbol="EURUSD",
        direction="LONG",
        campaign_id="campaign-1",
        opportunity_id="opportunity-1",
        lots=0.2,
    )


def _store():
    store = CampaignMemoryStore(db_path=":memory:")
    store.record_open(
        symbol="EURUSD",
        direction="LONG",
        campaign_id="campaign-1",
        fingerprint={},
        spec={"thesis": "structural continuation"},
        now_ms=1_000,
    )
    return store


def _loop(store=None):
    loop = CognitionLoop(
        _UnusedBrain(), _UnusedConsolidator(), lambda: [],
        management_mode="live",
    )
    loop._memory = store
    return loop


def test_milestone_is_persisted_before_sink_and_duplicate_submission_is_blocked():
    store = _store()
    calls = []
    loop = _loop(store)
    loop.set_management_sink(lambda action, position: calls.append(action))
    output, position = _output(), _position()

    loop._realise_management(output, position)
    loop._realise_management(output, position)

    assert len(calls) == 1
    progress = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=10_000,
    )
    assert progress["pending_milestone"]["milestone_id"] == "market_structure:H1:1:1.105"
    assert loop.get_status()["manage_submitted"] == 1
    store.close()


def test_milestone_partial_is_suppressed_without_durable_campaign_memory():
    calls = []
    loop = _loop()
    loop.set_management_sink(lambda action, position: calls.append(action))

    loop._realise_management(_output(), _position())

    assert calls == []
    assert loop.get_status()["manage_submitted"] == 0
    assert loop.get_status()["manage_suppressed"] == 1


def test_explicit_sink_rejection_clears_write_ahead_marker_for_retry():
    store = _store()
    calls = []

    def sink(action, position):
        calls.append(action)
        return False if len(calls) == 1 else None

    loop = _loop(store)
    loop.set_management_sink(sink)
    loop._realise_management(_output(), _position())

    rejected = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=10_000,
    )
    assert rejected["pending_milestone"] is None

    loop._realise_management(_output(), _position())
    accepted = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=11_000,
    )
    assert len(calls) == 2
    assert accepted["pending_milestone"]["milestone_id"] == "market_structure:H1:1:1.105"
    store.close()
