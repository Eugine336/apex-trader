from cognition.memory import CampaignMemoryStore


def _store():
    store = CampaignMemoryStore(db_path=":memory:")
    store.record_open(
        symbol="EURUSD",
        direction="LONG",
        campaign_id="campaign-1",
        fingerprint={"regime": "range"},
        spec={"thesis": "range rotation"},
        now_ms=1_000,
    )
    return store


def test_milestone_progress_confirms_only_after_broker_size_reduces():
    store = _store()
    assert store.record_milestone_pending(
        "campaign-1", "range_rotation:M5:1:1.105",
        size_before=0.30, fraction=1 / 3, now_ms=2_000,
    )

    pending = store.get_management_progress(
        "campaign-1", current_size=0.30, now_ms=3_000,
    )
    assert pending["completed_milestones"] == []
    assert pending["pending_milestone"]["milestone_id"] == "range_rotation:M5:1:1.105"

    confirmed = store.get_management_progress(
        "campaign-1", current_size=0.20, now_ms=4_000,
    )
    assert confirmed["completed_milestones"] == ["range_rotation:M5:1:1.105"]
    assert confirmed["pending_milestone"] is None

    store.close()


def test_pending_milestone_expires_if_broker_does_not_reduce_position():
    store = _store()
    store.record_milestone_pending(
        "campaign-1", "crt:H1:1:1.082",
        size_before=0.20, fraction=0.5, now_ms=2_000,
    )

    progress = store.get_management_progress(
        "campaign-1", current_size=0.20, now_ms=63_000,
        pending_timeout_ms=60_000,
    )
    assert progress["completed_milestones"] == []
    assert progress["pending_milestone"] is None
    store.close()


def test_completed_milestone_cannot_be_requeued():
    store = _store()
    store.record_milestone_pending(
        "campaign-1", "crt:H1:1:1.082",
        size_before=0.20, fraction=0.5, now_ms=2_000,
    )
    store.get_management_progress("campaign-1", current_size=0.10, now_ms=3_000)
    assert store.record_milestone_pending(
        "campaign-1", "crt:H1:1:1.082",
        size_before=0.10, fraction=0.5, now_ms=4_000,
    )
    progress = store.get_management_progress("campaign-1", current_size=0.10, now_ms=5_000)
    assert progress["completed_milestones"] == ["crt:H1:1:1.082"]
    assert progress["pending_milestone"] is None
    store.close()
