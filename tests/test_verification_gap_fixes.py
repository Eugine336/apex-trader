"""APEX TRADER — verification-gap fixes (3 gaps from the description audit).

Covers the three fixes:

  1. Execution cost in the EV gate — the spread erodes the reward leg, so the
     EV formula now subtracts the spread cost (pips × pip_size) before the R:R.
  2. currency_strength graded penalty — a single high-authority module no longer
     binary-vetoes an otherwise-unanimous multi-TF thesis; in "penalty" mode it
     applies a confidence-scaled conviction haircut instead.
  3. Event-driven retrain — regime shifts / drawdown escalation / loss streaks
     can force an out-of-band retrain instead of waiting for the 50-trade /
     7-day periodic cycle.

Dependency-light: pure gate / consensus math + the optimizer's trigger logic.
"""

from datetime import datetime, timedelta, timezone

import pytest

from brain.directional_consensus import Vote, decide, form_thesis
from config import ConsensusConfig
from entry.entry_gate import EntryGate


# ── Fix 1: execution cost in the EV formula ──────────────────────────────────


class TestEvSpreadCost:
    def test_spread_cost_conversion(self):
        gate = EntryGate()
        # EURUSD pip_size = 0.0001 → 1 pip of spread = 0.0001 in price units.
        assert gate._spread_cost_price("EURUSD", 1.0) == pytest.approx(0.0001)
        assert gate._spread_cost_price("EURUSD", 2.5) == pytest.approx(0.00025)

    def test_spread_cost_degrades_gracefully(self):
        gate = EntryGate()
        # Non-positive / non-finite / unknown instrument → 0.0 (no penalty),
        # never raises.
        assert gate._spread_cost_price("EURUSD", 0.0) == 0.0
        assert gate._spread_cost_price("EURUSD", -1.0) == 0.0
        assert gate._spread_cost_price("EURUSD", float("nan")) == 0.0
        assert gate._spread_cost_price("NOPE_NOT_A_SYMBOL", 1.0) == 0.0

    def test_spread_lowers_ev_vs_no_spread(self):
        gate = EntryGate()
        # Same setup, spread 0 vs spread 1 pip — the spread case has a lower R:R.
        r_no = gate._check_ev("EURUSD", "LONG", 1.10000, 1.09900, 1.10120, 0.6, 0.4, 0.0)
        r_sp = gate._check_ev("EURUSD", "LONG", 1.10000, 1.09900, 1.10120, 0.6, 0.4, 1.0)

        def _rr(reason: str) -> float:
            # reason contains "rr=<x>"
            token = [t for t in reason.split() if t.startswith("rr=")][0]
            return float(token.split("=")[1])

        assert _rr(r_sp.reason) < _rr(r_no.reason)

    def test_spread_flips_borderline_trade_to_reject(self):
        gate = EntryGate()
        # risk = 0.00100, reward = 0.00120 → rr 1.2, EV = 0.6*1.2 - 0.4 = 0.32R
        # (>= 0.30 min) passes with no spread.
        r_no = gate._check_ev("EURUSD", "LONG", 1.10000, 1.09900, 1.10120, 0.6, 0.4, 0.0)
        assert r_no.passed is True
        # 1 pip spread → net reward 0.00110 → rr 1.1 → EV 0.26R < 0.30 → reject.
        r_sp = gate._check_ev("EURUSD", "LONG", 1.10000, 1.09900, 1.10120, 0.6, 0.4, 1.0)
        assert r_sp.passed is False

    def test_zero_spread_is_unchanged_behaviour(self):
        gate = EntryGate()
        # Default spread arg (0.0) reproduces the pre-fix gross-reward EV.
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.6, 0.4)
        assert r.passed is True
        assert "spread_cost=0.00000" in r.reason


# ── Fix 2: currency_strength graded penalty vs binary veto ───────────────────


_HA = ["currency_strength"]


def _opposed_panel():
    """Strong LONG panel with currency_strength opposing SHORT @ 0.7."""
    return [
        Vote("structure", "LONG", 0.9, 3.0),
        Vote("momentum", "LONG", 0.8, 1.0),
        Vote("volume", "LONG", 0.7, 1.0),
        Vote("currency_strength", "SHORT", 0.7, 2.0),
    ]


def _thesis(votes, **over):
    kw = dict(
        min_net_score=1.5,
        min_agreement=0.55,
        high_authority_modules=_HA,
        high_authority_oppose_confidence=0.6,
        min_contributors=2,
        conviction_threshold=0.62,
        net_scale=0.0,
    )
    kw.update(over)
    return form_thesis(votes, **kw)


class TestCurrencyStrengthPenalty:
    def test_veto_mode_forces_neutral(self):
        # decide() in veto mode kills the direction on high-authority opposition.
        d = decide(
            _opposed_panel(),
            min_net_score=1.5,
            min_agreement=0.55,
            high_authority_modules=_HA,
            high_authority_oppose_confidence=0.6,
            min_contributors=2,
            currency_strength_penalty_mode="veto",
        )
        assert d.direction == "NEUTRAL"
        assert "currency_strength" in d.opposed_by

    def test_penalty_mode_holds_direction(self):
        # Penalty mode keeps the net direction; opposition is recorded, not a veto.
        d = decide(
            _opposed_panel(),
            min_net_score=1.5,
            min_agreement=0.55,
            high_authority_modules=_HA,
            high_authority_oppose_confidence=0.6,
            min_contributors=2,
            currency_strength_penalty_mode="penalty",
        )
        assert d.direction == "LONG"
        assert "currency_strength" in d.opposed_by

    def test_penalty_reduces_conviction_but_can_still_trigger(self):
        # Strong panel: penalty trims conviction but it stays above threshold.
        t = _thesis(_opposed_panel(), currency_strength_penalty_mode="penalty")
        assert t.direction == "LONG"
        # Compare against the same panel with NO opposing module.
        clean = [v for v in _opposed_panel() if v.module != "currency_strength"]
        t_clean = _thesis(clean, currency_strength_penalty_mode="penalty")
        assert t.conviction < t_clean.conviction

    def test_penalty_scales_with_confidence(self):
        low = _opposed_panel()[:-1] + [Vote("currency_strength", "SHORT", 0.6, 2.0)]
        high = _opposed_panel()[:-1] + [Vote("currency_strength", "SHORT", 1.0, 2.0)]
        t_low = _thesis(low, currency_strength_penalty_mode="penalty")
        t_high = _thesis(high, currency_strength_penalty_mode="penalty")
        # Higher opposing confidence ⇒ bigger conviction haircut ⇒ lower conviction.
        assert t_high.conviction < t_low.conviction

    def test_penalty_can_drop_marginal_below_threshold(self):
        # A marginal panel that just clears threshold loses the trigger once the
        # confidence-scaled penalty is applied. net = 1.8+0.8-1.0 = 1.6 (≥1.5),
        # agreement ≈ 0.72 — directional and above the floors.
        votes = [
            Vote("structure", "LONG", 0.9, 2.0),
            Vote("momentum", "LONG", 0.8, 1.0),
            Vote("currency_strength", "SHORT", 1.0, 1.0),
        ]
        t_pen = _thesis(
            votes, min_contributors=2,
            currency_strength_penalty_mode="penalty",
            currency_strength_penalty_amount=30.0,
        )
        # Same panel with no penalty amount applied keeps higher conviction.
        t_zero = _thesis(
            votes, min_contributors=2,
            currency_strength_penalty_mode="penalty",
            currency_strength_penalty_amount=0.0,
        )
        assert t_pen.direction == "LONG"
        assert t_pen.conviction < t_zero.conviction
        # The penalty pushes the marginal thesis below the conviction threshold.
        assert t_zero.trigger is True
        assert t_pen.trigger is False

    def test_veto_mode_thesis_neutral(self):
        t = _thesis(_opposed_panel(), currency_strength_penalty_mode="veto")
        assert t.direction == "NEUTRAL"
        assert t.trigger is False

    def test_config_defaults_to_penalty(self):
        cfg = ConsensusConfig()
        assert cfg.currency_strength_penalty_mode == "penalty"
        assert cfg.currency_strength_penalty_amount == 20.0

    def test_config_rejects_bad_mode(self):
        with pytest.raises(ValueError, match="currency_strength_penalty_mode"):
            ConsensusConfig(currency_strength_penalty_mode="nope")

    def test_config_rejects_negative_amount(self):
        with pytest.raises(ValueError, match="currency_strength_penalty_amount"):
            ConsensusConfig(currency_strength_penalty_amount=-5.0)


# ── Fix 3: event-driven retrain triggers ─────────────────────────────────────


def _optimizer():
    from adaptive.optimizer import AdaptiveOptimizer

    opt = AdaptiveOptimizer()
    # Anchor the periodic clock so should_retrain() doesn't fire on the
    # "never trained" / "stale" branches — isolating the event path.
    opt._last_train_time = datetime.now(timezone.utc)
    opt._trades_since_train = 0
    return opt


class TestEventDrivenRetrain:
    def test_trigger_arms_and_should_retrain_consumes_once(self):
        opt = _optimizer()
        assert opt.should_retrain() is False
        assert opt.trigger_event_retrain("manual") is True
        assert opt.should_retrain() is True   # consumes the flag
        assert opt.should_retrain() is False  # already consumed

    def test_cooldown_suppresses_back_to_back(self):
        opt = _optimizer()
        opt.event_retrain_cooldown_minutes = 60
        assert opt.trigger_event_retrain("first") is True
        # Second immediate trigger is within cooldown → suppressed.
        assert opt.trigger_event_retrain("second") is False

    def test_cooldown_expiry_allows_retrigger(self):
        opt = _optimizer()
        opt.event_retrain_cooldown_minutes = 60
        assert opt.trigger_event_retrain("first") is True
        # Pretend the last event retrain was 2 hours ago.
        opt._last_event_retrain_time = datetime.now(timezone.utc) - timedelta(hours=2)
        assert opt.trigger_event_retrain("later") is True

    def test_disabled_suppresses(self):
        opt = _optimizer()
        opt.event_retrain_enabled = False
        assert opt.trigger_event_retrain("manual") is False
        assert opt.should_retrain() is False

    def test_loss_streak_threshold(self):
        opt = _optimizer()
        opt.loss_streak_retrain_threshold = 3
        assert opt.notify_loss_streak(2) is False
        assert opt.notify_loss_streak(3) is True

    def test_drawdown_escalation_only_on_tightening(self):
        opt = _optimizer()
        assert opt.notify_drawdown_escalation("NORMAL", "CAUTION") is True
        # reset cooldown to isolate the de-escalation case
        opt._last_event_retrain_time = None
        opt._event_retrain_pending = False
        assert opt.notify_drawdown_escalation("RECOVERY", "NORMAL") is False

    def test_regime_change_only_on_difference(self):
        opt = _optimizer()
        assert opt.notify_regime_change("TRENDING", "TRENDING") is False
        assert opt.notify_regime_change("TRENDING", "RANGING") is True
