"""Offline tests for robust LLM reply parsing (Constitution Part XXIII).

Reproduces the exact failures seen in production with Gemini — fenced JSON,
JSON truncated at the token cap (missing closing brace), prose-wrapped JSON —
and asserts the Brain now recovers its own decision instead of dropping it.
"""

from llm.json_repair import repair_json, strip_fences, strip_reasoning
from llm.reasoner import _extract_opinion_fields, _regex_opinion_fields


# ── json_repair.repair_json ───────────────────────────────────────────────────

def test_strict_json():
    assert repair_json('{"direction": "LONG", "confidence": 0.6}') == {
        "direction": "LONG", "confidence": 0.6}


def test_fenced_json():
    txt = '```json\n{"direction": "SHORT", "confidence": 0.7}\n```'
    assert repair_json(txt)["direction"] == "SHORT"
    assert strip_fences(txt).startswith("{")


def test_truncated_missing_brace():
    # The logged failure: fenced + cut off before the closing brace.
    txt = '```json\n{\n  "direction": "FLAT",\n  "confidence": 0'
    obj = repair_json(txt)
    assert obj == {"direction": "FLAT", "confidence": 0}


def test_truncated_one_line_missing_brace():
    txt = '```json\n{"direction": "LONG", "confidence": 0.61'
    obj = repair_json(txt)
    assert obj == {"direction": "LONG", "confidence": 0.61}


def test_truncated_open_string():
    txt = '{"direction": "LONG", "rationale": "structure votes lo'
    obj = repair_json(txt)
    assert obj["direction"] == "LONG"
    assert obj["rationale"].startswith("structure votes")


def test_trailing_comma():
    assert repair_json('{"direction": "SHORT", "confidence": 0.5,}')["direction"] == "SHORT"


def test_prose_prefixed_json():
    txt = 'Here is my read:\n{"direction": "LONG", "confidence": 0.8} — done.'
    assert repair_json(txt)["confidence"] == 0.8


def test_dangling_key_dropped():
    txt = '{"direction": "LONG", "confidence": 0.6, "rationale":'
    obj = repair_json(txt)
    assert obj["direction"] == "LONG" and obj["confidence"] == 0.6


def test_unrecoverable_returns_none():
    assert repair_json("") is None
    assert repair_json("just some prose, no structure at all") is None


# ── json_repair — <think>…</think> stripping (deepseek-r1 & kin) ───────────────

def test_strip_reasoning_removes_think_block():
    # The reasoning prose contains stray braces/quotes that would derail a scan.
    txt = ('<think>\nHmm, structure looks {messy} with "quotes" and }}} braces.\n'
           '</think>\n{"direction": "LONG", "confidence": 0.7}')
    assert strip_reasoning(txt).startswith("{")
    assert repair_json(txt) == {"direction": "LONG", "confidence": 0.7}


def test_think_block_then_fenced_json():
    txt = ('<think>reasoning here</think>\n'
           '```json\n{"direction": "SHORT", "confidence": 0.5}\n```')
    assert repair_json(txt) == {"direction": "SHORT", "confidence": 0.5}


def test_dangling_think_close_tag_only():
    # The opening <think> was truncated out of the stream; drop up to </think>.
    txt = 'blah reasoning prose </think> {"direction": "FLAT", "confidence": 0}'
    assert repair_json(txt) == {"direction": "FLAT", "confidence": 0}


def test_think_stripping_is_case_insensitive():
    txt = '<THINK>noise {x}</THINK>{"direction": "LONG", "confidence": 0.4}'
    assert repair_json(txt)["direction"] == "LONG"


def test_no_think_block_is_unchanged():
    # A reply with no reasoning block must be untouched.
    txt = '{"direction": "LONG", "confidence": 0.6}'
    assert strip_reasoning(txt) == txt
    assert repair_json(txt) == {"direction": "LONG", "confidence": 0.6}


def test_think_block_wrapping_opportunity_reply():
    txt = ('<think>weighing HTF vs LTF...</think>\n'
           '{"opportunities": [{"id": "opp_1", "direction": "SHORT", '
           '"quality": 0.6}], "preferred_opportunity": "opp_1", '
           '"market_is_untradeable": false}')
    obj = repair_json(txt)
    assert obj["preferred_opportunity"] == "opp_1"
    assert obj["opportunities"][0]["direction"] == "SHORT"


# ── reasoner._extract_opinion_fields (JSON layer → regex fallback) ─────────────

def test_extract_from_truncated_json():
    fields = _extract_opinion_fields('```json\n{"direction": "LONG", "confidence": 0.61')
    assert fields["direction"] == "LONG" and fields["confidence"] == 0.61


def test_extract_flat_zero_is_a_real_opinion():
    # Previously logged as "NO opinion parsed" — it IS a valid FLAT decision.
    fields = _extract_opinion_fields('```json\n{\n  "direction": "FLAT",\n  "confidence": 0')
    assert fields["direction"] == "FLAT"


def test_regex_fallback_when_json_unrecoverable():
    # No braces at all, but the fields are stated inline.
    fields = _extract_opinion_fields('direction: SHORT, confidence: 0.44 because ...')
    assert fields["direction"] == "SHORT"
    assert float(fields["confidence"]) == 0.44


def test_regex_maps_buy_sell_synonyms():
    assert _regex_opinion_fields('"direction": "BUY", "confidence": 0.5')["direction"] == "LONG"
    assert _regex_opinion_fields('"direction": "SELL", "confidence": 0.5')["direction"] == "SHORT"


def test_regex_missing_confidence_defaults_zero():
    fields = _regex_opinion_fields('{"direction": "LONG"}')
    assert fields["direction"] == "LONG" and fields["confidence"] == 0.0


def test_lone_token_fallback():
    fields = _extract_opinion_fields("After weighing the evidence I choose FLAT.")
    assert fields["direction"] == "FLAT"


def test_pure_prose_no_direction_returns_none():
    # No direction key and no unambiguous token ⇒ genuinely unparseable.
    assert _extract_opinion_fields(
        "Context: D1 is massively up (4019 to 4304, now consolidating)") is None


def test_ambiguous_tokens_return_none():
    # Both LONG and SHORT mentioned with no explicit field ⇒ don't guess.
    assert _extract_opinion_fields("could be LONG or SHORT, unclear") is None
