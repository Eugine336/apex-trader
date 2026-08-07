"""Tests for the persistent institutional memory (Phase H — Constitution Part VII)."""

from types import SimpleNamespace

from cognition.contracts import Evidence, EvidenceDomain, MarketState
from cognition.memory import (
    CampaignMemoryStore,
    fingerprint_from_market_state,
    fingerprint_similarity,
)


def _ms(symbol="EURUSD", *evidence) -> MarketState:
    ms = MarketState(symbol=symbol)
    for e in evidence:
        ms.add(e)
    return ms


def _ev(domain, polarity, confidence=0.8, symbol="EURUSD"):
    return Evidence(source_module="m", domain=domain, symbol=symbol,
                    polarity=polarity, confidence=confidence)


# ── Fingerprint ────────────────────────────────────────────────────────────────

def test_fingerprint_empty_state_is_neutral():
    fp = fingerprint_from_market_state(_ms())
    assert fp["domains"] == {}
    assert fp["polarity"] == 0.0
    assert "direction" not in fp  # Part XXV — no directional field


def test_fingerprint_domains_and_confidence():
    fp = fingerprint_from_market_state(_ms(
        "EURUSD",
        _ev(EvidenceDomain.MOMENTUM, 0.8, confidence=0.8),
        _ev(EvidenceDomain.STRUCTURE, 0.6, confidence=0.6),
    ))
    # Part XXV — non-directional: domains carry confidence magnitude, no lean.
    assert fp["polarity"] == 0.0
    assert set(fp["domains"]) == {"momentum", "structure"}
    assert fp["domains"]["momentum"] == 0.8 and fp["domains"]["structure"] == 0.6


def test_fingerprint_similarity_identical_is_one():
    fp = fingerprint_from_market_state(_ms(
        "EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8), _ev(EvidenceDomain.VOLUME, 0.4)))
    assert fingerprint_similarity(fp, fp) == 1.0


def test_fingerprint_similarity_same_domains_is_high():
    # Two states active in the same domain are analogous situations — the former
    # directional "opposite" distinction is gone (Part XXV).
    a = fingerprint_from_market_state(_ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8)))
    b = fingerprint_from_market_state(_ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, -0.8)))
    assert fingerprint_similarity(a, b) == 1.0


def test_fingerprint_similarity_disjoint_is_zero():
    a = fingerprint_from_market_state(_ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8)))
    b = fingerprint_from_market_state(_ms("EURUSD", _ev(EvidenceDomain.VOLUME, 0.8)))
    assert fingerprint_similarity(a, b) == 0.0


# ── Store: open / close / retrieval ──────────────────────────────────────────

def _store():
    return CampaignMemoryStore(db_path=":memory:")


def _closed_campaign(symbol="EURUSD", direction="LONG", won=True, pnl=12.0,
                     verdict="validated", rq=0.8):
    return SimpleNamespace(to_dict=lambda: {
        "campaign_id": "c1", "symbol": symbol, "direction": direction,
        "state": "completed", "ended_reason": "tp", "realized_pnl": pnl,
        "postmortem": {"outcome_won": won, "verdict": verdict, "reasoning_quality": rq},
    })


def test_record_open_then_close_stitches_by_symbol_direction():
    store = _store()
    fp = fingerprint_from_market_state(_ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8)))
    store.record_open(symbol="EURUSD", direction="LONG", fingerprint=fp, campaign_id="c1")
    assert store.count() == 1
    assert store.count(closed_only=True) == 0
    store.record_close(_closed_campaign())
    assert store.count() == 1                    # updated in place, not a new row
    assert store.count(closed_only=True) == 1
    store.close()


def test_close_without_open_inserts_outcome_only_row():
    store = _store()
    store.record_close(_closed_campaign())
    st = store.get_status()
    assert st["closes_recorded"] == 1
    assert store.count(closed_only=True) == 1
    store.close()


def test_find_analogues_returns_similar_completed_campaign():
    store = _store()
    fp = fingerprint_from_market_state(_ms(
        "EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8), _ev(EvidenceDomain.STRUCTURE, 0.6)))
    store.record_open(symbol="EURUSD", direction="LONG", fingerprint=fp, campaign_id="c1")
    store.record_close(_closed_campaign(won=True))
    # A near-identical current state should retrieve the past campaign.
    current = _ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.7), _ev(EvidenceDomain.STRUCTURE, 0.5))
    analogues = store.find_analogues(current, min_similarity=0.5)
    assert len(analogues) == 1
    assert analogues[0]["outcome_won"] is True
    assert analogues[0]["similarity"] >= 0.5
    store.close()


def test_find_analogues_excludes_open_only_records():
    store = _store()
    fp = fingerprint_from_market_state(_ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8)))
    store.record_open(symbol="EURUSD", direction="LONG", fingerprint=fp)
    current = _ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8))
    assert store.find_analogues(current) == []   # not yet closed → no outcome to learn from
    store.close()


def test_find_analogues_empty_current_state_returns_empty():
    store = _store()
    store.record_open(symbol="EURUSD", direction="LONG",
                      fingerprint=fingerprint_from_market_state(
                          _ms("EURUSD", _ev(EvidenceDomain.MOMENTUM, 0.8))))
    store.record_close(_closed_campaign())
    assert store.find_analogues(_ms("EURUSD")) == []   # no domains in current state
    store.close()


def test_store_is_fail_safe_on_garbage_close():
    store = _store()
    store.record_close(object())                 # no to_dict / not a mapping
    assert store.count() == 0
    store.close()
