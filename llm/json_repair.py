"""APEX TRADER — Lenient JSON recovery for LLM replies.

Frontier models are instructed to answer with strict JSON, but in practice they
wrap it in ```` ```json ```` fences, prepend a sentence of reasoning, or — worst
of all — get **truncated** mid-object when they hit the token cap, leaving a
missing closing brace. A strict ``json.loads`` throws all of that away, and for
the Cognitive Brain that means silently discarding its own reasoning (defaulting
to FLAT). A missing brace must never cost the Brain its decision.

``repair_json`` recovers a JSON value from such messy text through escalating,
purely-deterministic layers:

1. **Fence strip** — remove ```` ``` ```` / ```` ```json ```` wrappers (even a
   dangling opening fence with no close, the truncated case).
2. **Strict** — ``json.loads`` on the cleaned text.
3. **Balanced scan** — find the first balanced ``{...}`` / ``[...]`` and parse it
   (tolerates leading/trailing prose).
4. **Structural repair** — from the first ``{``/``[`` to end: close an unterminated
   string, drop a dangling trailing comma / incomplete ``"key":``, and append the
   closing brackets needed to balance the structure, then parse.

Pure standard library; never raises — returns ``None`` when nothing parseable
can be recovered.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE_OPEN = re.compile(r"^\s*```[a-zA-Z0-9_-]*[ \t]*\r?\n?")
_FENCE_CLOSE = re.compile(r"\r?\n?\s*```\s*$")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_DANGLING_KEY = re.compile(r",?\s*\"[^\"]*\"\s*:\s*$")
_DANGLING_COLON = re.compile(r":\s*$")


def _try_load(s: str) -> Optional[Any]:
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


def strip_fences(text: str) -> str:
    """Remove Markdown code fences, including a lone/truncated opening fence."""
    s = str(text or "").strip()
    s = _FENCE_OPEN.sub("", s)
    s = _FENCE_CLOSE.sub("", s)
    return s.strip()


def _first_fragment(s: str) -> str:
    """Substring from the first ``{`` or ``[`` to the end (or '' if neither)."""
    idx = [i for i in (s.find("{"), s.find("[")) if i != -1]
    return s[min(idx):] if idx else ""


def _scan_balanced(s: str) -> Optional[Any]:
    """Return the first balanced {...} / [...] object that parses, else None.

    Respects string literals and escapes so braces inside strings don't confuse
    the depth counter.
    """
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(s)):
                c = s[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        obj = _try_load(s[start:i + 1])
                        if obj is not None:
                            return obj
                        break  # malformed — try the next opener
            start = s.find(opener, start + 1)
    return None


def _repair_fragment(frag: str) -> str:
    """Balance an unterminated JSON fragment so it can parse (best-effort)."""
    stack: list = []
    in_str = False
    esc = False
    for c in frag:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if stack:
                stack.pop()
    out = frag
    if in_str:                       # close an unterminated string literal
        out += '"'
    # Drop a dangling trailing comma / incomplete "key": with no value yet.
    prev = None
    while prev != out:
        prev = out
        out = out.rstrip()
        out = _DANGLING_KEY.sub("", out)
        out = _TRAILING_COMMA.sub(r"\1", out)
        if _DANGLING_COLON.search(out):
            out = _DANGLING_KEY.sub("", out)
            out = out.rstrip().rstrip(",")
        out = out.rstrip().rstrip(",")
    # Append the closers needed to balance the still-open structures.
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def repair_json(text: str) -> Optional[Any]:
    """Best-effort parse of a possibly-fenced/truncated/prose-wrapped JSON reply.

    Returns the parsed value (usually a dict), or ``None`` if unrecoverable.
    """
    if not text:
        return None
    s = strip_fences(text)
    if not s:
        return None
    obj = _try_load(s)                         # 1) strict on cleaned text
    if obj is not None:
        return obj
    obj = _scan_balanced(s)                     # 2) first balanced object/array
    if obj is not None:
        return obj
    frag = _first_fragment(s)                   # 3) structural repair
    if frag:
        obj = _try_load(_repair_fragment(frag))
        if obj is not None:
            return obj
    return None


__all__ = ["repair_json", "strip_fences"]
