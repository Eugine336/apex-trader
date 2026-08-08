from datetime import datetime, timezone

from brain.session_engine import SessionEngine


def test_session_engine_detects_london_new_york_overlap():
    engine = SessionEngine()
    monday_overlap = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)

    status = engine.get_status(monday_overlap)

    assert status.current_session.startswith("OVERLAP")
    assert status.is_tradeable is True
    assert status.liquidity == "HIGH"


def test_session_engine_blocks_weekends():
    engine = SessionEngine()
    saturday = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)

    status = engine.get_status(saturday)

    assert status.current_session == "WEEKEND"
    assert status.is_tradeable is False
    assert status.liquidity == "DEAD"


def test_session_score_is_high_during_best_overlap():
    engine = SessionEngine()
    monday_overlap = datetime(2026, 5, 25, 13, 30, tzinfo=timezone.utc)

    score = engine.get_session_score(monday_overlap)

    assert score == 10
