r"""Tests for ``src/ai/utils.py`` — the JSON response parser.

``parse_json_response`` has 5 fallback strategies:
1. Direct json.loads on the trimmed text.
2. ```json … ``` code block extraction.
3. ``` … ``` generic code block extraction.
4. Brace-matching find first balanced ``{ … }``.
5. Regex ``\{[\s\S]*\}`` last-resort extraction.

Plus a None fallback if every strategy fails.

Each strategy gets a unique test plus a "no strategy matches → None" test
and a "matches whitespace-wrapped inputs" test.
"""

from src.ai.utils import parse_json_response

# ---------------------------------------------------------------------------
# Strategy 1 — direct json.loads
# ---------------------------------------------------------------------------


def test_parse_json_response_direct_object():
    assert parse_json_response('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_parse_json_response_strips_whitespace_before_parsing():
    assert parse_json_response('   \n\t {"a":1}   ') == {"a": 1}


# ---------------------------------------------------------------------------
# Strategy 2 — ```json fenced block
# ---------------------------------------------------------------------------


def test_parse_json_response_json_fenced_block():
    text = "Sure, here:\n```json\n{\"k\": 42}\n```\nThanks!"
    assert parse_json_response(text) == {"k": 42}


def test_parse_json_response_json_fenced_block_invalid_json_falls_through():
    text = "```json\n{not valid json}\n```"
    # Strategy 2 fails, falls through strategies 3-5 also fail, returns None
    assert parse_json_response(text) is None


# ---------------------------------------------------------------------------
# Strategy 3 — generic ``` fenced block
# ---------------------------------------------------------------------------


def test_parse_json_response_generic_fenced_block():
    text = "Output:\n```\n{\"name\": \"alice\"}\n```\nEnd."
    assert parse_json_response(text) == {"name": "alice"}


# ---------------------------------------------------------------------------
# Strategy 4 — brace-matching
# ---------------------------------------------------------------------------


def test_parse_json_response_brace_matching_with_surrounding_text():
    """Strategy 4 finds the first balanced { … } when surrounding text exists."""
    text = "Here is the result: {\"x\": 1, \"y\": [1,2,3]} — done"
    assert parse_json_response(text) == {"x": 1, "y": [1, 2, 3]}


def test_parse_json_response_brace_matching_drops_trailing_comma():
    text = 'Some prose then {"a": 1,} — broken JSON inside'
    # First balanced block is `{"a": 1,}` which is invalid; strategy 4 falls through to regex then None
    assert parse_json_response(text) is None


# ---------------------------------------------------------------------------
# Strategy 5 — regex last resort
# ---------------------------------------------------------------------------


def test_parse_json_response_regex_strategy_falls_through_for_unbalanced_text():
    """When strategy 4's brace-matcher fails (unbalanced braces), strategy 5
    regex matches a span that is itself invalid JSON → returns ``None``."""
    text = r"prefix {not_json {but_nested}} suffix"
    assert parse_json_response(text) is None


def test_parse_json_response_regex_handles_multiline_object():
    text = "Header\n{\n  \"a\": 1,\n  \"b\": 2\n}\nFooter"
    assert parse_json_response(text) == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_parse_json_response_returns_none_when_no_object_present():
    assert parse_json_response("Just plain text with no JSON at all.") is None


def test_parse_json_response_returns_none_for_empty_string():
    assert parse_json_response("") is None


def test_parse_json_response_unbalanced_braces_returns_none():
    """Two ``{`` openings, one ``}`` closing — neither strategy 4 nor 5 yields valid JSON."""
    assert parse_json_response("a { b { c } }") is None


def test_parse_json_response_unclosed_brace_returns_none():
    """A leading ``{`` with no closing brace — strategy 5 regex match fails → ``None``."""
    assert parse_json_response("{not valid") is None
