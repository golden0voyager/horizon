"""Phase 6 unit tests for ``src.setup.tag_aliases``.

Covers the explicit ``TAG_ALIASES`` map (sanity check on shape), the read-only
``get_tag_aliases`` helper, and ``resolve_tag_alias`` canonical lookup.
"""

from __future__ import annotations

from src.setup.tag_aliases import TAG_ALIASES, get_tag_aliases, resolve_tag_alias


def test_tag_aliases_map_is_non_empty() -> None:
    assert len(TAG_ALIASES) > 50  # sanity: substantial coverage.


def test_tag_aliases_keys_are_lowercase_canonical_tags() -> None:
    for key in TAG_ALIASES:
        assert key == key.lower()


def test_get_tag_aliases_returns_aliases_for_known_tag() -> None:
    aliases = get_tag_aliases("ai")
    assert "人工智能" in aliases
    assert "artificial-intelligence" in aliases


def test_get_tag_aliases_returns_empty_for_unknown_tag() -> None:
    assert get_tag_aliases("this-tag-does-not-exist") == []


def test_get_tag_aliases_known_tag_with_empty_alias_list() -> None:
    # "maker" is in the alias map with an explicit empty list.
    assert get_tag_aliases("maker") == []


def test_resolve_tag_alias_returns_canonical_for_main_tag() -> None:
    assert resolve_tag_alias("ai") == "ai"
    assert resolve_tag_alias("AI") == "ai"


def test_resolve_tag_alias_canonicalizes_chinese_alias() -> None:
    out = resolve_tag_alias("人工智能")
    assert out == "ai"


def test_resolve_tag_alias_canonicalizes_hyphenated_alias() -> None:
    out = resolve_tag_alias("artificial-intelligence")
    assert out == "ai"


def test_resolve_tag_alias_returns_lowercased_input_when_unknown() -> None:
    out = resolve_tag_alias("NotARealTag")
    assert out == "notarealtag"


def test_resolve_tag_alias_strips_whitespace_before_lookup() -> None:
    out = resolve_tag_alias("  kubernetes  ")
    assert out == "k8s"


def test_resolve_tag_alias_alias_kubernetes_to_k8s() -> None:
    assert resolve_tag_alias("kubernetes") == "k8s"


def test_resolve_tag_alias_llm_aliases() -> None:
    assert resolve_tag_alias("LLM") == "llm"
    assert resolve_tag_alias("大语言模型") == "llm"
    assert resolve_tag_alias("large-language-model") == "llm"


def test_resolve_tag_alias_rust_variants() -> None:
    assert resolve_tag_alias("Rust") == "rust"
    assert resolve_tag_alias("rust语言") == "rust"
    assert resolve_tag_alias("rust编程") == "rust"


def test_resolve_tag_alias_python_variants() -> None:
    assert resolve_tag_alias("Python") == "python"
    assert resolve_tag_alias("python编程") == "python"
    assert resolve_tag_alias("py") == "python"


def test_resolve_tag_alias_javascript_variants() -> None:
    assert resolve_tag_alias("JS") == "javascript"
    assert resolve_tag_alias("JavaScript") == "javascript"
    assert resolve_tag_alias("js") == "javascript"
