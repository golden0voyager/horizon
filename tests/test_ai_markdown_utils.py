"""Tests for ``src/ai/markdown_utils.py`` — HTML → Markdown cleanup helpers.

Covers:
- ``_strip_html_tags`` (removes tags, decodes entities, strips whitespace)
- ``_escape_markdown_text`` (escapes markdown special chars + collapses whitespace)
- ``_is_safe_markdown_link_url`` (control chars, unsafe chars, scheme allowlist, paren balance)
- ``_convert_details_to_markdown`` (HTML <details> → Markdown sections, with link fallback)
- ``clean_app_summary_markdown`` (public entrypoint)
"""

from src.ai.markdown_utils import (
    _convert_details_to_markdown,
    _escape_markdown_text,
    _is_safe_markdown_link_url,
    _strip_html_tags,
    clean_app_summary_markdown,
)

# ---------------------------------------------------------------------------
# _strip_html_tags
# ---------------------------------------------------------------------------


def test_strip_html_tags_removes_simple_tags():
    assert _strip_html_tags("<b>hello</b>") == "hello"


def test_strip_html_tags_decodes_html_entities():
    assert _strip_html_tags("&lt;tag&gt; &amp; &quot;quoted&quot;") == '<tag> & "quoted"'


def test_strip_html_tags_handles_multiple_tags_and_whitespace():
    assert _strip_html_tags("  <div><p>x</p></div>  ") == "x"


# ---------------------------------------------------------------------------
# _escape_markdown_text
# ---------------------------------------------------------------------------


def test_escape_markdown_text_escapes_special_chars():
    # All characters in `_MARKDOWN_SPECIAL_RE` should be backslash-escaped
    escaped = _escape_markdown_text("a*b_c[d]e<f>g(h)i!j|k{l}m+n#o")
    # Each special char becomes \<char>
    assert r"\*" in escaped
    assert r"\_" in escaped
    assert r"\[" in escaped
    assert r"\]" in escaped
    assert r"\(" in escaped
    assert r"\)" in escaped


def test_escape_markdown_text_collapses_whitespace():
    assert _escape_markdown_text("a   b\n\n\tc") == "a b c"


def test_escape_markdown_text_strips_leading_trailing_whitespace():
    assert _escape_markdown_text("   hello   ") == "hello"


# ---------------------------------------------------------------------------
# _is_safe_markdown_link_url
# ---------------------------------------------------------------------------


def test_is_safe_markdown_link_url_accepts_https_with_netloc():
    assert _is_safe_markdown_link_url("https://example.com/path") is True


def test_is_safe_markdown_link_url_accepts_mailto():
    assert _is_safe_markdown_link_url("mailto:user@example.com") is True


def test_is_safe_markdown_link_url_rejects_control_chars():
    assert _is_safe_markdown_link_url("https://example.com/\x07") is False


def test_is_safe_markdown_link_url_rejects_whitespace_chars():
    assert _is_safe_markdown_link_url("http://example.com/path with space") is False


def test_is_safe_markdown_link_url_rejects_unbalanced_parens():
    assert _is_safe_markdown_link_url("https://example.com/(oops") is False


def test_is_safe_markdown_link_url_rejects_unknown_scheme():
    assert _is_safe_markdown_link_url("javascript:alert(1)") is False
    assert _is_safe_markdown_link_url("file:///etc/passwd") is False


def test_is_safe_markdown_link_url_rejects_http_without_netloc():
    assert _is_safe_markdown_link_url("http:///path") is False


# ---------------------------------------------------------------------------
# _convert_details_to_markdown
# ---------------------------------------------------------------------------


def test_convert_details_to_markdown_typical_references_block():
    html = """
    <details><summary>References</summary>
      <ul>
        <li><a href="https://example.com/a">Title A</a></li>
        <li><a href="https://example.com/b">Title B</a></li>
      </ul>
    </details>
    """
    out = _convert_details_to_markdown(html)
    assert "**References**" in out
    assert "[Title A](https://example.com/a)" in out
    assert "[Title B](https://example.com/b)" in out


def test_convert_details_to_markdown_uses_default_summary_when_summary_tag_empty():
    """An empty ``<summary></summary>`` triggers the default ``"References"`` fallback in ``_replace`` because ``strip_html_tags("") | ""`` is falsy."""
    html = "<details><summary></summary><p>orphan body</p></details>"
    out = _convert_details_to_markdown(html)
    assert "**References**" in out
    assert "orphan body" in out


def test_convert_details_to_markdown_falls_back_to_plain_label_for_unsafe_url():
    html = '<details><summary>S</summary><ul><li><a href="javascript:alert(1)">bad</a></li></ul></details>'
    out = _convert_details_to_markdown(html)
    assert "[bad]" not in out  # unsafe URL → no link syntax
    assert "- bad" in out  # falls back to plain bullet


def test_convert_details_to_markdown_handles_text_items_without_links():
    html = "<details><summary>Misc</summary><ul><li>just text</li></ul></details>"
    out = _convert_details_to_markdown(html)
    assert "**Misc**" in out
    assert "- just text" in out


def test_convert_details_to_markdown_empty_body():
    html = "<details><summary>X</summary></details>"
    out = _convert_details_to_markdown(html)
    assert "**X**" in out


# ---------------------------------------------------------------------------
# clean_app_summary_markdown
# ---------------------------------------------------------------------------


def test_clean_app_summary_markdown_strips_anchor_id_tags_and_converts_details():
    html = """
    <a id="user-content-anchor1"></a>
    <p>some prose</p>
    <details><summary>References</summary>
      <ul><li><a href="https://example.com/x">Title X</a></li></ul>
    </details>
    """
    out = clean_app_summary_markdown(html)
    assert "user-content-anchor1" not in out
    assert "**[References]**" in out or "**References**" in out
    assert "[Title X](https://example.com/x)" in out
