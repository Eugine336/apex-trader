"""
Test for #1 — regime score-threshold (defensive-only entry-bar bump).

`_regime_score_bump` must:
  * return 0 until the regime is confident (≥ min_sample),
  * only ever RAISE the bar (a lower learned threshold never loosens),
  * be clamped to a small ceiling.
"""

from platforms.main_loop import _regime_score_bump

_MIN = 30


def test_zero_until_confident():
    # Wants a higher bar (90) but not enough samples → no change.
    assert _regime_score_bump(90, sample_size=10, min_sample=_MIN) == 0


def test_raises_bar_when_confident_and_stricter():
    assert _regime_score_bump(90, sample_size=50, min_sample=_MIN) == 5  # 90-85


def test_never_loosens():
    # Confident but a LOWER learned threshold must not drop the bar.
    assert _regime_score_bump(82, sample_size=50, min_sample=_MIN) == 0
    assert _regime_score_bump(85, sample_size=50, min_sample=_MIN) == 0


def test_clamped_to_ceiling():
    assert _regime_score_bump(200, sample_size=50, min_sample=_MIN) == 8
