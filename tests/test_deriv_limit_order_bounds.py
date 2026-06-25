"""Tests for the Deriv multiplier-fit + limit_order clamping fix.

A Deriv multiplier contract liquidates at a loss equal to the stake, so the
``limit_order.stop_loss`` dollar value (stake × multiplier × sl_pct) can never
exceed the stake. A too-high multiplier against a wide SL produced a stop_loss
larger than the stake → Deriv rejected the order with
'Input validation failed: parameters'. These tests cover the two pure helpers
that prevent it: snapping the multiplier down so the SL fits, and clamping the
limit_order dollar values as a last-resort safety net.
"""

from platforms.deriv.deriv_connector import (
    DerivConnector,
    _SL_STAKE_SAFETY,
    _FALLBACK_ACCEPTED_MULTIPLIERS,
)
from platforms.base_connector import TickData


_ACCEPTED = [80, 200, 400, 600, 800, 1000, 2000, 4000]


class TestFitMultiplierForSl:
    def test_wide_sl_snaps_multiplier_down(self):
        # The live-log case: 1000× against a 0.339% SL would lose 3.4× the stake.
        fitted = DerivConnector._fit_multiplier_for_sl(1000, 0.00339, _ACCEPTED)
        assert fitted <= 1000
        # Loss-at-SL must stay within the stake (with the commission buffer).
        assert fitted * 0.00339 <= _SL_STAKE_SAFETY + 1e-9
        assert fitted == 200

    def test_tight_sl_keeps_multiplier(self):
        fitted = DerivConnector._fit_multiplier_for_sl(1000, 0.0001, _ACCEPTED)
        assert fitted == 1000

    def test_never_exceeds_requested_multiplier(self):
        fitted = DerivConnector._fit_multiplier_for_sl(200, 0.0001, _ACCEPTED)
        assert fitted <= 200

    def test_falls_back_to_smallest_when_nothing_fits(self):
        # An extreme SL no accepted multiplier can honour → smallest, minimising
        # the overshoot (the dollar clamp then caps the stop).
        fitted = DerivConnector._fit_multiplier_for_sl(1000, 0.5, _ACCEPTED)
        assert fitted == min(_ACCEPTED)

    def test_zero_sl_pct_is_inert(self):
        assert DerivConnector._fit_multiplier_for_sl(1000, 0.0, _ACCEPTED) == 1000


class TestLimitOrderDollars:
    def test_stop_loss_clamped_within_stake(self):
        # 0.339% SL at 1000× on a $27.99 stake would compute a $94 stop_loss.
        sl, tp = DerivConnector._limit_order_dollars(0.00339, 0.005, 27.99, 1000)
        assert sl <= round(27.99 * _SL_STAKE_SAFETY, 2)
        assert sl <= 27.99
        assert tp > 0

    def test_within_bounds_passes_through(self):
        # After the multiplier is fitted to 200× the stop fits without clamping.
        sl, tp = DerivConnector._limit_order_dollars(0.00339, 0.005, 27.99, 200)
        expected = round(0.00339 * 27.99 * 200, 2)
        assert abs(sl - expected) < 0.01
        assert sl < 27.99

    def test_minimum_positive_floor(self):
        sl, tp = DerivConnector._limit_order_dollars(0.0, 0.0, 10.0, 100)
        assert sl >= 0.01
        assert tp >= 0.01

    def test_values_are_rounded_to_cents(self):
        sl, tp = DerivConnector._limit_order_dollars(0.0012345, 0.0098765, 13.37, 200)
        assert sl == round(sl, 2)
        assert tp == round(tp, 2)


def _make_connector():
    """Build a DerivConnector without running __init__ (no event-loop thread)."""
    conn = object.__new__(DerivConnector)
    conn._discovered_multipliers = {}
    conn._positions = {}
    conn._require_connection = lambda: None
    conn.symbol_map = lambda s: s
    conn.get_price = lambda s: TickData(bid=7956.9, ask=7956.9, spread=0.0, time=0.0)
    conn._accepted_multipliers = lambda m: (_ACCEPTED, 1000)
    return conn


class TestPlaceOrderRetryPath:
    """Regression for the live ``name 'sl_pct' is not defined`` crash.

    When Deriv rejects the first attempt with 'Input validation failed:
    parameters', place_order retries and recomputes the limit_order dollar
    values via _limit_order_dollars(sl_pct, tp_pct, ...). Those fraction-of-price
    variables must be in scope on the retry path — previously only
    sl_pct_initial existed and the retry raised NameError before the order
    could be re-sent."""

    def test_retry_after_validation_error_does_not_raise(self):
        conn = _make_connector()
        calls = {"n": 0}

        def fake_send(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"error": {"message": "Input validation failed: parameters"}}
            return {"buy": {"contract_id": "12345"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert calls["n"] == 2  # first failed → retried
        assert result.success is True
        assert result.order_id == "12345"

    def test_retry_recomputes_limit_order_within_stake(self):
        conn = _make_connector()
        seen_payloads = []

        def fake_send(payload):
            seen_payloads.append(payload)
            if len(seen_payloads) == 1:
                return {"error": {"message": "Input validation failed: parameters"}}
            return {"buy": {"contract_id": "999"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert result.success is True
        # The retry drops limit_order (the validation error path disables it),
        # so the second payload must not carry SL/TP bounds.
        assert "limit_order" not in seen_payloads[1]["parameters"]


class TestErrorText:
    """_error_text must fold error.details into the searchable string so the
    multiplier/stake-cap regexes can match the real cause that Deriv hides in
    details behind a generic 'Input validation failed: parameters' message."""

    def test_flattens_message_and_details(self):
        txt = DerivConnector._error_text({
            "message": "Input validation failed: parameters",
            "details": {
                "multiplier": "Multiplier is not in acceptable range. Accepts 100,200,300,400,500.",
            },
        })
        assert "Input validation failed" in txt
        assert "Accepts 100,200,300,400,500" in txt

    def test_message_only(self):
        assert DerivConnector._error_text({"message": "Insufficient balance"}) == "Insufficient balance"

    def test_string_details(self):
        txt = DerivConnector._error_text({"message": "bad", "details": "extra reason"})
        assert "bad" in txt and "extra reason" in txt

    def test_empty_objects(self):
        assert DerivConnector._error_text({}) == "Unknown error"
        assert DerivConnector._error_text(None) == "Unknown error"


class TestErrorDetailsSelfCorrection:
    """Regression for the live V50_1S failure: Deriv returned the accepted
    multiplier list in error.details (not error.message). The retry loop now
    reads details, corrects the multiplier, and keeps limit_order instead of
    misattributing the generic envelope to SL/TP."""

    def test_details_multiplier_self_corrects_and_keeps_limit_order(self):
        conn = _make_connector()
        seen_payloads = []

        def fake_send(payload):
            seen_payloads.append(payload)
            if len(seen_payloads) == 1:
                return {"error": {
                    "message": "Input validation failed: parameters",
                    "details": {
                        "multiplier": "Multiplier is not in acceptable range. Accepts 100,200,300,400,500.",
                    },
                }}
            return {"buy": {"contract_id": "777"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=1000,
        )

        assert result.success is True
        assert result.order_id == "777"
        # Discovered list updated from details so future orders skip the guess.
        assert conn._discovered_multipliers["STPIDX"] == [100, 200, 300, 400, 500]
        # Multiplier was the real cause → limit_order must NOT be stripped.
        assert "limit_order" in seen_payloads[1]["parameters"]
        # The retried multiplier comes from the Deriv-supplied valid set.
        assert seen_payloads[1]["parameters"]["multiplier"] in (100, 200, 300, 400, 500)


class TestConservativeFallback:
    """The fallback multiplier set must not contain 80 (invalid for the 1s
    volatility indices that caused the live rejection) and must surface a
    warning so the silent discovery/config gap is visible."""

    def test_fallback_excludes_invalid_80(self):
        assert 80 not in _FALLBACK_ACCEPTED_MULTIPLIERS
        assert _FALLBACK_ACCEPTED_MULTIPLIERS == [100, 200, 300, 400, 500]

    def test_no_discovery_no_config_uses_fallback_and_warns(self):
        conn = object.__new__(DerivConnector)
        conn._discovered_multipliers = {}

        accepted, desired = conn._accepted_multipliers("UNKNOWN_SYNTH_XYZ")

        assert 80 not in accepted
        assert accepted == [100, 200, 300, 400, 500]
        # The fallback path records the symbol so the WARNING fires once.
        assert "UNKNOWN_SYNTH_XYZ" in conn._warned_fallback_symbols


# ═══════════════════════════════════════════════════════════════════════════════
# Symbol-property rejection → proposal→buy fallback
# ═══════════════════════════════════════════════════════════════════════════════

_SYMBOL_PROP_ERROR = {
    "error": {
        "message": "Input validation failed: parameters",
        "details": {"parameters": "Properties not allowed: symbol"},
    }
}


class TestSymbolPropertyDetection:
    """Regression for the live BOOM1000/V50_1S failure: Deriv rejects ``symbol``
    inside the buy ``parameters`` with 'Properties not allowed: symbol'. The
    detector must fire on that error and NOT on unrelated errors that merely
    mention the word symbol (e.g. an unsupported-symbol value error)."""

    def test_detects_properties_not_allowed_symbol(self):
        err = DerivConnector._error_text(_SYMBOL_PROP_ERROR["error"])
        assert "Properties not allowed: symbol" in err
        assert DerivConnector._is_symbol_property_error(err) is True

    def test_detects_additional_properties_phrasing(self):
        assert DerivConnector._is_symbol_property_error(
            "Input validation failed | Additional properties are not allowed: symbol"
        ) is True

    def test_ignores_generic_validation_error(self):
        assert DerivConnector._is_symbol_property_error(
            "Input validation failed: parameters"
        ) is False

    def test_ignores_multiplier_error(self):
        assert DerivConnector._is_symbol_property_error(
            "Multiplier is not in acceptable range. Accepts 100,200,300,400,500."
        ) is False

    def test_ignores_unsupported_symbol_value_error(self):
        # A value/permission error that names the symbol must NOT route to the
        # proposal fallback (which would not help and only waste a round-trip).
        assert DerivConnector._is_symbol_property_error(
            "Symbol R_100 is not offered for this account"
        ) is False

    def test_empty_is_false(self):
        assert DerivConnector._is_symbol_property_error("") is False
        assert DerivConnector._is_symbol_property_error(None) is False


class TestSymbolErrorProposalFallback:
    """The full live failure scenario: a multiplier-fitted multiplier contract
    whose buy-by-parameters request is rejected with 'Properties not allowed:
    symbol'. place_order must recover via proposal→buy where symbol lives in the
    proposal and the buy references the returned proposal id."""

    def test_symbol_error_recovers_via_proposal(self):
        conn = _make_connector()
        seen = []

        def fake_send(payload):
            seen.append(payload)
            if payload.get("proposal") == 1:
                return {"proposal": {"id": "PID-1", "ask_price": payload["amount"]}}
            if payload.get("buy") == 1:
                # buy-by-parameters → Deriv rejects the symbol property
                return dict(_SYMBOL_PROP_ERROR)
            # buy by proposal id
            return {"buy": {"contract_id": "C-123"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert result.success is True
        assert result.order_id == "C-123"

        proposals = [p for p in seen if p.get("proposal") == 1]
        buys_by_id = [p for p in seen if isinstance(p.get("buy"), str)]
        assert proposals, "a proposal request must be sent"
        assert buys_by_id, "a buy-by-id request must follow the proposal"

        # symbol lives in the proposal (contract definition) where Deriv accepts it
        assert proposals[0]["symbol"] == "STPIDX"
        # the buy-by-id request must NOT carry symbol or a parameters object
        assert "symbol" not in buys_by_id[0]
        assert "parameters" not in buys_by_id[0]
        assert buys_by_id[0]["buy"] == "PID-1"
        assert "price" in buys_by_id[0]

    def test_symbol_error_on_retry_path_recovers(self):
        # First buy-by-parameters fails with a generic validation error (triggers
        # the limit_order strip); the retry — still carrying symbol in parameters —
        # fails with the symbol-property error, which must route to the proposal
        # fallback rather than failing the order.
        conn = _make_connector()
        seen = []
        state = {"buy_params_calls": 0}

        def fake_send(payload):
            seen.append(payload)
            if payload.get("proposal") == 1:
                return {"proposal": {"id": "PID-2", "ask_price": payload["amount"]}}
            if payload.get("buy") == 1:
                state["buy_params_calls"] += 1
                if state["buy_params_calls"] == 1:
                    return {"error": {"message": "Input validation failed: parameters"}}
                return dict(_SYMBOL_PROP_ERROR)
            return {"buy": {"contract_id": "C-456"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert result.success is True
        assert result.order_id == "C-456"
        assert any(p.get("proposal") == 1 for p in seen)

    def test_proposal_carries_protective_limit_order(self):
        # Even when the buy-by-parameters retry already stripped limit_order, the
        # proposal fallback must re-attempt WITH the protective SL/TP so we never
        # open a naked Deriv position.
        conn = _make_connector()
        seen = []

        def fake_send(payload):
            seen.append(payload)
            if payload.get("proposal") == 1:
                return {"proposal": {"id": "PID-3", "ask_price": payload["amount"]}}
            if payload.get("buy") == 1:
                return dict(_SYMBOL_PROP_ERROR)
            return {"buy": {"contract_id": "C-789"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert result.success is True
        proposals = [p for p in seen if p.get("proposal") == 1]
        assert "limit_order" in proposals[0]
        assert proposals[0]["limit_order"]["stop_loss"] > 0
        assert proposals[0]["limit_order"]["take_profit"] > 0


class TestBuyViaProposal:
    """Direct unit tests of the proposal→buy helper, isolated from place_order's
    pre-fit so the self-correction branches can be exercised deterministically."""

    def _kwargs(self, **over):
        base = dict(
            mapped="STPIDX", contract_type="MULTDOWN", amount=27.99,
            multiplier=200, sl_pct=0.003, tp_pct=0.005, passthrough={},
            entry_price=7956.9, lots=0.01, symbol="STPIDX",
            direction="SHORT", sl=7980.67, tp=7920.99,
        )
        base.update(over)
        return base

    def test_happy_path_buys_by_id_without_symbol(self):
        conn = _make_connector()
        seen = []

        def fake_send(payload):
            seen.append(payload)
            if payload.get("proposal") == 1:
                return {"proposal": {"id": "PID", "ask_price": 27.99}}
            return {"buy": {"contract_id": "OK-1"}}

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs())

        assert result.success is True
        assert result.order_id == "OK-1"
        # Position is tracked for later modify/close.
        assert "OK-1" in conn._positions
        # The buy request references the proposal id and omits symbol/parameters.
        buy = [p for p in seen if isinstance(p.get("buy"), str)][0]
        assert "symbol" not in buy and "parameters" not in buy

    def test_symbol_property_error_on_proposal_recorrelates_and_succeeds(self):
        # A symbol-property error on a PROPOSAL is schema-impossible (symbol is a
        # valid proposal field), so it signals a mis-correlated/stale frame. The
        # helper must re-send the proposal (re-correlate) rather than fail.
        conn = _make_connector()
        seen = []
        state = {"proposal_calls": 0}

        def fake_send(payload):
            seen.append(payload)
            if payload.get("proposal") == 1:
                state["proposal_calls"] += 1
                if state["proposal_calls"] == 1:
                    return dict(_SYMBOL_PROP_ERROR)
                return {"proposal": {"id": "PID-RC", "ask_price": payload["amount"]}}
            return {"buy": {"contract_id": "OK-RC"}}

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs())

        assert result.success is True
        assert result.order_id == "OK-RC"
        # At least two proposals were sent (the first mis-correlated, the retry).
        assert state["proposal_calls"] >= 2

    def test_persistent_symbol_property_error_fails_fast(self):
        # If the symbol-property error never clears, the helper must stop after
        # the bounded re-correlation budget instead of looping the whole retry
        # budget, and surface the failure.
        conn = _make_connector()
        state = {"proposal_calls": 0}

        def fake_send(payload):
            if payload.get("proposal") == 1:
                state["proposal_calls"] += 1
                return dict(_SYMBOL_PROP_ERROR)
            return {"buy": {"contract_id": "SHOULD-NOT-HAPPEN"}}

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs())

        assert result.success is False
        assert "symbol" in (result.error or "").lower()
        # Bounded: initial send + 2 re-correlation retries (not the full budget).
        assert state["proposal_calls"] == 3

    def test_multiplier_self_correction_in_proposal(self):
        conn = _make_connector()
        seen = []

        def fake_send(payload):
            seen.append(payload)
            if payload.get("proposal") == 1:
                if payload["multiplier"] not in (100, 200, 300, 400, 500):
                    return {"error": {
                        "message": "Input validation failed: parameters",
                        "details": {
                            "multiplier": "Multiplier is not in acceptable range. Accepts 100,200,300,400,500.",
                        },
                    }}
                return {"proposal": {"id": "PID", "ask_price": payload["amount"]}}
            return {"buy": {"contract_id": "OK-2"}}

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs(multiplier=1000))

        assert result.success is True
        assert result.order_id == "OK-2"
        # Deriv's accepted list was learned from error.details.
        assert conn._discovered_multipliers["STPIDX"] == [100, 200, 300, 400, 500]
        final_proposal = [p for p in seen if p.get("proposal") == 1][-1]
        assert final_proposal["multiplier"] in (100, 200, 300, 400, 500)

    def test_proposal_failure_is_surfaced(self):
        conn = _make_connector()

        def fake_send(payload):
            return {"error": {"message": "Market is closed"}}

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs())

        assert result.success is False
        assert "Market is closed" in (result.error or "")

    def test_missing_proposal_id_fails_closed(self):
        conn = _make_connector()

        def fake_send(payload):
            if payload.get("proposal") == 1:
                return {"proposal": {"ask_price": 27.99}}  # no id
            return {"buy": {"contract_id": "SHOULD-NOT-HAPPEN"}}

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs())

        assert result.success is False
        assert "proposal id" in (result.error or "")

    def test_missing_contract_id_fails_closed(self):
        conn = _make_connector()

        def fake_send(payload):
            if payload.get("proposal") == 1:
                return {"proposal": {"id": "PID", "ask_price": 27.99}}
            return {"buy": {}}  # no contract_id

        conn._sync_send = fake_send
        result = conn._buy_via_proposal(**self._kwargs())

        assert result.success is False
        assert "contract_id" in (result.error or "")


