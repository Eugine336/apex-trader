"""Ensure each Vite frontend documents its build-time environment variables."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VITE_READ = re.compile(r"import\.meta\.env\.([A-Z][A-Z0-9_]*)")


def test_each_frontend_documents_its_vite_environment_variables():
    missing: list[str] = []
    for suffix in ("*.js", "*.jsx", "*.ts", "*.tsx"):
        for source_path in ROOT.rglob(suffix):
            relative = source_path.relative_to(ROOT)
            if any(part in {".git", "node_modules", "dist", "build"} for part in relative.parts):
                continue
            if "frontend" not in relative.parts:
                continue
            try:
                source = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            names = set(VITE_READ.findall(source))
            if not names:
                continue
            env_example = next(
                (parent / ".env.example" for parent in source_path.parents if (parent / ".env.example").is_file()),
                None,
            )
            if env_example is None:
                missing.append(f"{relative}: no adjacent .env.example")
                continue
            documented = set(
                re.findall(r"^\s*([A-Z][A-Z0-9_]*)=", env_example.read_text(encoding="utf-8"), re.M)
            )
            absent = sorted(names - documented)
            if absent:
                missing.append(f"{relative}: {', '.join(absent)}")
    assert not missing, "Undocumented Vite environment variables: " + "; ".join(missing)
