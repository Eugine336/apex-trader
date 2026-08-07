#!/usr/bin/env python3
"""Standalone live test of every LLM provider that actually has a key configured.

Run this from a machine with real network access — the sandbox this was
written in can't reach any AI vendor endpoint.

It builds the *real* LLMClient (same class the bot uses at runtime) for
each configured provider, fires one real completion call, and reports
pass/fail with the actual HTTP status and a snippet of the reply (or error).

Usage:
    python scripts/test_llm_providers.py
    python scripts/test_llm_providers.py --only gemini,groq
    python scripts/test_llm_providers.py --verbose   # print full raw body on failure

Exits non-zero if any tested provider fails, so it's CI-friendly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        print(f"[warn] no .env found at {path}")
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _verbose_transport(verbose: bool):
    """Wraps the default transport to optionally print raw bodies on failure."""
    def transport(url: str, headers: dict, body: bytes, timeout: float):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return int(getattr(resp, "status", 200) or 200), text
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            if verbose:
                print(f"    [http {exc.code}] {detail[:500]}")
            return int(exc.code or 0), detail[:200]
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"    [transport error] {type(exc).__name__}: {exc}")
            return 0, f"{type(exc).__name__}: {exc}"
    return transport


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-test every configured LLM provider")
    parser.add_argument("--only", default="", help="Comma-separated subset of provider names to test")
    parser.add_argument("--verbose", action="store_true", help="Print raw response body on failure")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parent.parent / ".env"))
    ns = parser.parse_args()
    only = {s.strip().lower() for s in ns.only.split(",") if s.strip()} if ns.only else None

    _load_dotenv(Path(ns.env_file))
    from llm.client import LLMClient  # noqa: E402

    def env(name: str) -> str:
        return (os.getenv(name) or "").strip()

    # (label, provider, model, api_key, base_url) — mirrors LLM_EXTRA_MODELS / primary.
    candidates = [
        ("gemini (primary)", env("LLM_PROVIDER") or "gemini", env("LLM_MODEL") or "gemini-1.5-flash",
         env("LLM_API_KEY"), env("LLM_BASE_URL")),
        ("bluesminds", "bluesminds", "gpt-5-mini", env("BLUESMINDS_API_KEY"), "https://api.bluesminds.com/v1"),
        ("groq", "groq", "llama-3.3-70b-versatile", env("GROQ_API_KEY"), "https://api.groq.com/openai/v1"),
        ("openrouter", "openrouter", "meta-llama/llama-3.1-70b-instruct",
         env("OPENROUTER_API_KEY"), "https://openrouter.ai/api/v1"),
        ("nvidia-nim", "nvidia", "meta/llama-3.1-70b-instruct",
         env("NVIDIA_API_KEY"), "https://integrate.api.nvidia.com/v1"),
        ("openai", "openai", "gpt-4o-mini", env("OPENAI_API_KEY"), ""),
        ("anthropic", "anthropic", "claude-3-5-sonnet-latest", env("ANTHROPIC_API_KEY"), ""),
        ("cohere", "cohere", "command-r-plus", env("COHERE_API_KEY"), "https://api.cohere.ai/compatibility/v1"),
        ("xai/grok", "xai", "grok-2-latest", env("XAI_API_KEY"), "https://api.x.ai/v1"),
        ("together", "together", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
         env("TOGETHER_API_KEY"), "https://api.together.xyz/v1"),
        ("fireworks", "fireworks", "accounts/fireworks/models/llama-v3p1-70b-instruct",
         env("FIREWORKS_API_KEY"), "https://api.fireworks.ai/inference/v1"),
        ("deepinfra", "deepinfra", "meta-llama/Meta-Llama-3.1-70B-Instruct",
         env("DEEPINFRA_API_KEY"), "https://api.deepinfra.com/v1/openai"),
        ("cerebras", "cerebras", "llama-3.3-70b", env("CEREBRAS_API_KEY"), "https://api.cerebras.ai/v1"),
        ("huggingface", "huggingface", "meta-llama/Llama-3.1-8B-Instruct",
         env("HUGGINGFACE_API_KEY"), "https://router.huggingface.co/v1"),
    ]

    transport = _verbose_transport(ns.verbose)
    results = []
    tested_any = False

    for label, provider, model, api_key, base_url in candidates:
        if only is not None and label.lower() not in only and provider.lower() not in only:
            continue
        if not api_key:
            print(f"[skip] {label:20s} — no key configured (CONFIGURED, not AVAILABLE)")
            continue
        tested_any = True
        client = LLMClient(
            provider=provider, model=model, api_key=api_key, base_url=base_url,
            timeout_seconds=20.0, max_tokens=32, transport=transport,
        )
        if not client.usable:
            print(f"[FAIL] {label:20s} — client reports not usable "
                  f"(shape={client._shape}, requires_base_url={client._requires_base_url}, "
                  f"has_base_url={bool(client.base_url)})")
            results.append((label, False))
            continue
        print(f"[test] {label:20s} calling {client.describe()['base_url']} ...")
        reply = client.complete(
            "You are a connectivity test. Reply with exactly one short sentence.",
            "Say hello and name yourself in 5 words or fewer.",
        )
        ok = reply is not None and reply.strip() != ""
        results.append((label, ok))
        if ok:
            snippet = reply.strip().replace("\n", " ")[:100]
            print(f"[OK]   {label:20s} -> {snippet!r}")
        else:
            print(f"[FAIL] {label:20s} -> no usable reply (see logger warning above for HTTP status)")
        print()

    if not tested_any:
        print("No providers with a configured key matched the given filter.")
        return 0

    print("=== Summary ===")
    for label, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'}  {label}")

    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
