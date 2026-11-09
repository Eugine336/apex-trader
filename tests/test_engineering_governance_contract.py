"""Repository guardrails for the mandatory Apex engineering contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_instructions_bind_constitution_and_delivery_contract():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/APEX_Constitution.md" in agents
    assert "docs/APEX_FULL_REDESIGN_ROADMAP.md" in agents
    assert "docs/APEX_ENGINEERING_DELIVERY_CONTRACT.md" in agents
    assert "The CognitiveBrain is the sole market decision authority" in agents
    assert "real repository `.env` is strictly off-limits" in agents
    assert "No live broker order placement" in agents


def test_delivery_contract_distinguishes_implementation_from_acceptance():
    contract = (
        ROOT / "docs" / "APEX_ENGINEERING_DELIVERY_CONTRACT.md"
    ).read_text(encoding="utf-8")
    for status in (
        "IMPLEMENTED",
        "INTEGRATED",
        "VALIDATED",
        "DATA-GATED",
        "PRODUCTION-ACCEPTED",
    ):
        assert status in contract
    assert "ten systems" in contract
    assert "untouched holdout" in contract
    assert "dashboard must not write to it" in contract


def test_env_template_documents_configuration_authority():
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DD_DASHBOARD_API_KEY=" in template
    assert "DD_DASHBOARD_TRUST_LOOPBACK=" in template
    assert "APEX_API_JWT_SECRET=" in template
    assert "runtime dashboard actions" in template.lower()
