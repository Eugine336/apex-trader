"""Environment templates are shareable; runtime credentials are not."""

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"

_SECRET_KEY = re.compile(
    r"(?:PASSWORD|TOKEN|SECRET|(?:^|_)KEY$|API_KEY|LOGIN$|CLIENT_ID|ACCOUNT_ID|CREDENTIAL|VAULT)",
    re.IGNORECASE,
)


def test_secret_free_environment_template_exists():
    assert EXAMPLE.is_file()
    assert GITIGNORE.is_file()


def test_secret_like_environment_values_are_blank_in_example():
    for raw in EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip().upper()
        # Expiry durations are policy/configuration values, not bearer tokens.
        # Validate the numeric duration explicitly, then continue scanning all
        # actual credential-bearing fields with the strict blank-value rule.
        if normalized_key.endswith("_EXPIRES_IN"):
            assert value.strip().isdigit() and int(value.strip()) > 0, (
                f"{key.strip()} must contain a positive expiry duration"
            )
            continue
        if _SECRET_KEY.search(key.strip()):
            assert not value.strip(), f"{key.strip()} must not contain a committed credential"


def test_model_roster_has_no_inline_credentials():
    roster_lines = [
        line.partition("=")[2].strip()
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.startswith("LLM_EXTRA_MODELS=")
    ]
    assert len(roster_lines) == 1, "exactly one LLM_EXTRA_MODELS roster must be provided"
    roster = json.loads(roster_lines[0])
    assert isinstance(roster, list)
    sensitive_fields = {
        "api_key", "api-key", "token", "access_token", "secret", "password",
        "client_secret", "authorization", "bearer_token",
    }
    for spec in roster:
        assert isinstance(spec, dict), "provider roster entries must be objects"
        for key, value in spec.items():
            if key.strip().lower() in sensitive_fields:
                assert not str(value or "").strip(), (
                    f"provider {spec.get('name', spec.get('provider', 'unknown'))!r} "
                    f"contains inline credential field {key!r}"
                )


def test_gitignore_ignores_runtime_environment_but_allows_example():
    lines = {
        line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".env" in lines
    assert "*.env" in lines
    assert "!.env.example" in lines
