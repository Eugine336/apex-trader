"""Tests for the operational-intelligence author (Part IX, Article 9)."""

from cognition.operations import (
    CAP_GITHUB_CREATE_ISSUE,
    CAP_OPERATOR_NOTIFY,
    CAP_REPORT_PUBLISH,
    OperationsAuthor,
)


def _author(**kw):
    params = dict(enabled=True, loss_streak_threshold=3,
                  report_period_seconds=1_000_000.0, cooldown_seconds=0.0)
    params.update(kw)
    return OperationsAuthor(**params)


def test_disabled_author_emits_nothing():
    author = OperationsAuthor(enabled=False)
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG",
                                    verdict="deserved_loss")
    assert author.tick(now=2_000_000.0) == []


def test_repeated_deserved_loss_authors_github_issue():
    author = _author()
    for _ in range(3):
        author.observe_campaign_outcome(symbol="EURUSD", direction="LONG",
                                        verdict="deserved_loss")
    out = author.tick(now=0.0)   # report not due at t=0 (last_report seeds to 0)
    issues = [i for i in out if i.intent == CAP_GITHUB_CREATE_ISSUE]
    assert len(issues) == 1
    assert "title" in issues[0].params and "body" in issues[0].params


def test_loss_streak_resets_on_non_loss():
    author = _author()
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG", verdict="deserved_loss")
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG", verdict="deserved_loss")
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG", verdict="validated")
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG", verdict="deserved_loss")
    out = author.tick(now=0.0)
    assert [i for i in out if i.intent == CAP_GITHUB_CREATE_ISSUE] == []


def test_validated_win_authors_operator_notify():
    author = _author()
    author.observe_campaign_outcome(symbol="GBPUSD", direction="SHORT",
                                    verdict="validated", reasoning_quality=0.8,
                                    realized_pnl=25.0)
    out = author.tick(now=0.0)
    notifs = [i for i in out if i.intent == CAP_OPERATOR_NOTIFY]
    assert len(notifs) == 1
    assert "message" in notifs[0].params


def test_periodic_report_emitted_when_due():
    author = _author(report_period_seconds=100.0)
    out = author.tick(now=1_000.0, memory_status={"completed_rows": 7})
    reports = [i for i in out if i.intent == CAP_REPORT_PUBLISH]
    assert len(reports) == 1
    assert "summary" in reports[0].params


def test_cooldown_suppresses_duplicate_issue():
    author = _author(cooldown_seconds=10_000.0)
    for _ in range(3):
        author.observe_campaign_outcome(symbol="EURUSD", direction="LONG", verdict="deserved_loss")
    first = author.tick(now=0.0)
    assert len([i for i in first if i.intent == CAP_GITHUB_CREATE_ISSUE]) == 1
    # Re-trigger within cooldown → suppressed
    for _ in range(3):
        author.observe_campaign_outcome(symbol="EURUSD", direction="LONG", verdict="deserved_loss")
    second = author.tick(now=1.0)
    assert [i for i in second if i.intent == CAP_GITHUB_CREATE_ISSUE] == []
    assert author.get_status()["suppressed"] >= 1


def test_author_is_fail_safe_on_garbage():
    author = _author()
    author.observe_campaign_outcome(symbol=None, direction=None, verdict=None)  # no raise
    assert isinstance(author.tick(now=0.0), list)


def test_intent_is_duck_compatible_with_planner():
    # An OperationalIntent must expose the fields the Action Planner reads.
    author = _author()
    author.observe_campaign_outcome(symbol="EURUSD", direction="LONG",
                                    verdict="validated", reasoning_quality=0.9)
    intent = author.tick(now=0.0)[0]
    for field in ("intent", "objective", "params", "confidence", "priority",
                  "source", "evidence_ref"):
        assert hasattr(intent, field)
