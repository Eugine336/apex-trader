"""Environment templates are shareable; runtime credentials are not."""

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
        if _SECRET_KEY.search(key.strip()):
            assert not value.strip(), f"{key.strip()} must not contain a committed credential"


def test_gitignore_ignores_runtime_environment_but_allows_example():
    lines = {
        line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".env" in lines
    assert "*.env" in lines
    assert "!.env.example" in lines
