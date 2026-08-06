"""Offline tests for Universal Advisory Council panel mode (Constitution Part XXIV)."""

from llm.reasoning_orchestrator import ReasoningEngine, ReasoningOrchestrator


class _FakeReasoner:
    """Duck-typed reasoner: .available + .reason(symbol, evidence, now)."""

    def __init__(self, direction="LONG", confidence=0.6, available=True, dead=False):
        self._d = direction
        self._c = confidence
        self.available = available
        self._dead = dead

    def reason(self, symbol, evidence, now=None):
        if self._dead:
            raise RuntimeError("advisor down")
        from types import SimpleNamespace
        return SimpleNamespace(direction=self._d, confidence=self._c, rationale="r")


def _council(n=5, panel=False, dead_idx=None):
    engines = []
    for i in range(n):
        dead = (dead_idx is not None and i == dead_idx)
        engines.append(ReasoningEngine(f"adv{i}", _FakeReasoner(dead=dead)))
    return ReasoningOrchestrator(engines, max_engines=3, panel=panel)


def test_panel_consults_all_available():
    # max_engines=0 ⇒ EVERY available advisor advises (uncapped panel).
    orch = _council(n=5)
    sel = orch.select(max_engines=0)
    assert len(sel) == 5                      # all five, not capped at 3


def test_none_keeps_default_cap():
    # Backward-compatible: None ⇒ the configured cap (3).
    orch = _council(n=5)
    assert len(orch.select(max_engines=None)) == 3


def test_explicit_cap_respected():
    orch = _council(n=5)
    assert len(orch.select(max_engines=2)) == 2


def test_panel_flag_from_status():
    assert _council(panel=True).get_status()["panel"] is True
    assert _council(panel=False).get_status()["panel"] is False


def test_dead_advisor_leaves_panel_others_still_advise():
    # A dead advisor yields no opinion and drops out; the rest still advise.
    orch = _council(n=4, dead_idx=1)
    result = orch.consult("XAUUSD", {"e": 1}, max_engines=0)
    assert len(result.consulted) == 4         # all four were asked (the panel)
    assert len(result.opinions) == 3          # the dead one simply left
    names = {o.engine for o in result.opinions}
    assert "adv1" not in names                # the one that died is absent


def test_build_sets_panel_from_config_mode():
    from types import SimpleNamespace
    from llm.reasoning_orchestrator import build_reasoning_orchestrator
    cfg = SimpleNamespace(
        provider="openai", model="gpt-4o-mini", api_key="sk", base_url="",
        extra_models=[], consult_mode="panel", consult_max_engines=3,
        timeout_seconds=20.0, max_tokens=512, temperature=0.2,
        min_interval_seconds=30.0, drive_decisions=False,
    )
    orch = build_reasoning_orchestrator(cfg)
    assert orch is not None and orch.panel is True
