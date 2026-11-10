"""Guard the operator environment template against undocumented runtime inputs."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_TEMPLATE = ROOT / ".env.example"
READ_PATTERNS = (
    re.compile(r"\bos\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\bos\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\bos\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"),
    re.compile(r"\bgetenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    # Typed environment helpers hide the underlying os.getenv call.
    re.compile(r"\b(?:_llm_env_bool|_env_bool|_env_int|_env|env_bool)\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
)
SKIP_PARTS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "tests", "frontend", "build", "dist",
}


def test_every_runtime_environment_read_is_documented_in_env_example():
    declared = {
        match.group(1)
        for match in re.finditer(
            r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", ENV_TEMPLATE.read_text(encoding="utf-8"), re.M
        )
    }
    reads: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in READ_PATTERNS:
            for match in pattern.finditer(source):
                reads.setdefault(match.group(1), []).append(
                    str(path.relative_to(ROOT))
                )
        # Config dataclasses use a tuple of (environment-name, attribute)
        # pairs followed by os.getenv(env_name); capture those literal names too.
        for mapping in re.finditer(
            r"for\s+env_name\s*,\s*attr\s+in\s*\(([\s\S]*?)\)\s*:",
            source,
        ):
            for name in re.findall(r"['\"]([A-Z][A-Z0-9_]*)['\"]", mapping.group(1)):
                reads.setdefault(name, []).append(str(path.relative_to(ROOT)))

    missing = sorted(set(reads) - declared)
    assert not missing, (
        "Runtime environment variables are missing from .env.example: "
        + ", ".join(f"{name} ({', '.join(sorted(set(reads[name])))})" for name in missing)
    )


def test_template_does_not_advertise_unconsumed_legacy_controls():
    template = ENV_TEMPLATE.read_text(encoding="utf-8")
    removed = {
        "COGNITION_ORIGINATION_STOP_FRACTION",
        "COGNITION_ORIGINATION_REWARD_MULTIPLE",
        "COGNITION_ORIGINATION_MIN_RR",
        "APEX_OBS_CACHE",
        "APEX_OBS_CACHE_DIR",
    }
    active = {
        match.group(1)
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=", template, re.M)
    }
    assert not (removed & active)


def test_every_allowlisted_operator_field_is_documented():
    from config_env import ENV_CONFIG_FIELDS

    declared = {
        match.group(1)
        for match in re.finditer(
            r"^([A-Z][A-Z0-9_]*)=", ENV_TEMPLATE.read_text(encoding="utf-8"), re.M
        )
    }
    missing = sorted(
        f"APEX_{section}_{field}".upper()
        for section, names in ENV_CONFIG_FIELDS.items()
        for field in names
        if f"APEX_{section}_{field}".upper() not in declared
    )
    assert not missing, "Allowlisted settings missing from .env.example: " + ", ".join(missing)


def test_active_template_keys_have_a_runtime_or_external_consumer():
    from config_env import ENV_CONFIG_FIELDS

    active = {
        match.group(1)
        for match in re.finditer(
            r"^([A-Z][A-Z0-9_]*)=", ENV_TEMPLATE.read_text(encoding="utf-8"), re.M
        )
    }
    external_consumers = {
        # Standard Datadog agent/tracer variables consumed by the external
        # Datadog runtime rather than by Apex application code.
        "DD_API_KEY",
        "DD_AGENT_HOST",
        "DD_TRACE_AGENT_PORT",
        "DD_ENV",
        "DD_SERVICE",
        "DD_LOGS_INJECTION",
    }
    suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".yml", ".yaml", ".toml", ".sh", ".ps1"}
    source_parts = {".git", ".venv", "venv", "__pycache__", "node_modules", "tests", "docs", "build", "dist"}
    source = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        relative = path.relative_to(ROOT)
        if source_parts.intersection(relative.parts):
            continue
        try:
            source.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    repository_source = "\\n".join(source)
    dynamic_consumers = {
        f"APEX_{section}_{field}".upper()
        for section, names in ENV_CONFIG_FIELDS.items()
        for field in names
    }
    missing = sorted(
        key for key in active
        if key not in external_consumers
        and key not in dynamic_consumers
        and key not in repository_source
    )
    assert not missing, (
        "Active .env.example keys have no code/config consumer: "
        + ", ".join(missing)
    )
