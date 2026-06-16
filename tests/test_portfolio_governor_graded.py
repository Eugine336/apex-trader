"""Tests for the Portfolio Governor's graded analytical exposure (#24) and
non-forex correlation visibility (#31)."""

from governor import GovernorConfig, PortfolioGovernor


def _pos(symbol: str, direction: str) -> dict:
    return {"symbol": symbol, "direction": direction}


# ── #31 — non-forex exposure is no longer invisible ─────────────────────────


def test_index_signed_legs_group_by_region():
    gov = PortfolioGovernor(GovernorConfig())
    assert gov._signed_legs("US30", "BUY") == {"IDX:US": 1}
    assert gov._signed_legs("NAS100", "BUY") == {"IDX:US": 1}
    assert gov._signed_legs("GER40", "SELL") == {"IDX:EU": -1}


def test_synthetic_and_crypto_group_keys():
    gov = PortfolioGovernor(GovernorConfig())
    assert gov._group_key("V50_1S") == "VOL:50"
    assert gov._group_key("BOOM1000") == "VOL:BOOM"
    assert gov._group_key("CRASH500") == "VOL:CRASH"
    assert gov._group_key("BTCUSDT") == "CRY:BTC"


def test_unmapped_symbol_still_bounded_not_invisible():
    gov = PortfolioGovernor(GovernorConfig())
    legs = gov._signed_legs("WEIRDSYM", "BUY")
    assert legs  # never empty → can never stack invisibly


def test_correlated_indices_now_blocked_legacy():
    # Two US indices share the IDX:US group leg — previously invisible (empty
    # legs), now caught by the correlated-positions limit.
    gov = PortfolioGovernor(
        GovernorConfig(
            max_correlated_positions=1, max_currency_exposure=99, max_sector_exposure=99,
        )
    )
    book = [_pos("US30", "BUY")]
    v = gov.check("NAS100", "BUY", book, account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "correlated_positions"


# ── #24 — graded analytical exposure ────────────────────────────────────────


def test_graded_correlated_allows_with_dimmer():
    gov = PortfolioGovernor(
        GovernorConfig(
            graded_exposure=True,
            max_correlated_positions=1, max_currency_exposure=99, max_sector_exposure=99,
        )
    )
    book = [_pos("EURUSD", "BUY")]
    v = gov.check("GBPUSD", "BUY", book, account_balance=1000.0)
    # No longer a hard block — allowed but sized down.
    assert v.allowed
    assert v.risk_multiplier < 1.0
    assert "correlated_positions" in v.near_breaches


def test_graded_keeps_max_positions_hard():
    gov = PortfolioGovernor(GovernorConfig(graded_exposure=True, max_open_positions=2))
    book = [_pos("EURUSD", "BUY"), _pos("GBPJPY", "SELL")]
    v = gov.check("USDCHF", "SELL", book, account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "max_positions"


def test_graded_keeps_daily_cap_hard():
    gov = PortfolioGovernor(
        GovernorConfig(graded_exposure=True, daily_loss_cap_pct=3.0)
    )
    gov.set_reference_balance(1000.0)
    gov.update_daily_pnl(-35.0)
    v = gov.check("EURUSD", "BUY", [], account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "daily_loss_cap"


def test_graded_clear_book_full_size():
    gov = PortfolioGovernor(GovernorConfig(graded_exposure=True))
    v = gov.check("EURUSD", "BUY", [], account_balance=1000.0)
    assert v.allowed
    assert v.risk_multiplier == 1.0
    assert v.near_breaches == []


def test_graded_measures_all_limits_not_first():
    # Currency AND correlated both over — both must be reported.
    gov = PortfolioGovernor(
        GovernorConfig(
            graded_exposure=True,
            max_currency_exposure=1, max_correlated_positions=1, max_sector_exposure=99,
        )
    )
    book = [_pos("EURUSD", "BUY")]
    v = gov.check("EURGBP", "BUY", book, account_balance=1000.0)
    assert v.allowed
    assert "currency_exposure" in v.near_breaches
    assert v.risk_multiplier < 1.0


def test_verdict_to_dict_has_risk_fields():
    gov = PortfolioGovernor(GovernorConfig())
    v = gov.check("EURUSD", "BUY", [], account_balance=1000.0)
    d = v.to_dict()
    assert "risk_multiplier" in d
    assert "risk_score" in d
    assert "near_breaches" in d
