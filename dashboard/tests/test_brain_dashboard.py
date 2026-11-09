"""Dashboard Brain tests for the current Single-Reasoner architecture.

Directional Vote and Opportunity ranker panels were retired. The dashboard
must expose empty, correctly-shaped compatibility responses when no current
world-model evidence is attached; it must not import the retired subsystem.
"""

from dashboard.state_brain import BrainMixin


class IdleBrainState(BrainMixin):
    @property
    def is_live(self) -> bool:
        return False


def test_idle_module_observations_are_empty():
    payload = IdleBrainState().get_module_votes()

    assert payload == {
        "pairs": [],
        "modules": [],
        "pair_count": 0,
        "source": "idle",
    }


def test_idle_ranker_is_empty_without_reviving_retired_ranker():
    payload = IdleBrainState().get_ranker()

    assert payload["pairs"] == []
    assert payload["pair_count"] == 0
    assert payload["total_opportunities"] == 0
    assert payload["execute"] is False
    assert payload["override_count"] == 0
    assert payload["horizon_distribution"] == {"SCALP": 0, "SWING": 0}
    assert payload["direction_distribution"] == {"LONG": 0, "SHORT": 0}
    assert payload["avg_expected_value"] == 0.0
    assert payload["source"] == "idle"
