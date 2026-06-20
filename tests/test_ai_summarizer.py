"""Tests for ``src/ai/summarizer.py`` — daily Markdown summary renderer.

This module has no AI client dependency — pure programmatic rendering. Tests
cover:

- ``_pangu`` CJK ↔ ASCII spacing (e.g. ``中文English`` → ``中文 English``)
- ``DailySummarizer.generate_summary`` — header, TOC, items, empty fallback
- ``DailySummarizer.generate_webhook_overview`` — compact multi-message view
- ``DailySummarizer.generate_webhook_item`` — single-item message
- ``_format_item`` — title link, source line, background, references, tags,
  AI-summary fallback when no detailed summary is present
- ``_generate_empty_summary`` — placeholder body
- Language handling (en / zh) — Chinese-specific behaviour falls back to en
"""

from datetime import UTC, datetime

from src.ai.summarizer import DailySummarizer, _pangu
from src.models import ContentItem, SourceType


def _make_item(
    item_id: str = "rss:t:1",
    *,
    title: str = "English title",
    ai_score: float | None = 8.0,
    ai_reason: str = "Good",
    ai_summary: str = "A summary",
    ai_tags: list[str] | None = None,
    author: str | None = "alice",
    metadata: dict | None = None,
    source_type: SourceType = SourceType.RSS,
    published_at: datetime | None = None,
) -> ContentItem:
    """Build a ContentItem with AI fields set on the instance directly.

    ContentItem is a Pydantic model — AI metadata (score/reason/summary/tags)
    is mutated by the analyzer after construction. Mirroring that here keeps
    tests decoupled from the model's frozen schema.
    """
    item = ContentItem(
        id=item_id,
        source_type=source_type,
        title=title,
        url=f"https://example.com/{item_id}",
        author=author,
        content=None,
        published_at=published_at or datetime(2026, 1, 4, 12, 30, tzinfo=UTC),
        metadata=metadata or {},
    )
    item.ai_score = ai_score
    item.ai_reason = ai_reason
    item.ai_summary = ai_summary
    item.ai_tags = ai_tags if ai_tags is not None else []
    return item


# ---------------------------------------------------------------------------
# _pangu
# ---------------------------------------------------------------------------


def test_pangu_inserts_space_between_cjk_and_ascii_letters():
    assert _pangu("中文abc") == "中文 abc"
    assert _pangu("abc中文") == "abc 中文"


def test_pangu_inserts_space_between_cjk_and_digits():
    assert _pangu("中文123") == "中文 123"
    assert _pangu("123中文") == "123 中文"


def test_pangu_inserts_space_between_cjk_characters_low_and_high_ranges():
    # Random CJK codepoint from extension B (U+3400…) — confirms both sub-ranges.
    assert _pangu("hello\u3400") == "hello \u3400"
    assert _pangu("\u4e00hello") == "\u4e00 hello"


def test_pangu_no_change_when_already_spaced():
    assert _pangu("中文 abc") == "中文 abc"


def test_pangu_no_change_when_pure_ascii_or_pure_cjk():
    assert _pangu("hello world") == "hello world"
    assert _pangu("中文测试") == "中文测试"


# ---------------------------------------------------------------------------
# generate_summary
# ---------------------------------------------------------------------------


def test_generate_summary_returns_empty_for_no_items():
    summarizer = DailySummarizer()
    out = asyncio_run(summarizer.generate_summary([], "2026-01-04", total_fetched=0))
    assert "Horizon Daily - 2026-01-04" in out
    assert "0 items" in out or "Analyzed 0" in out


def test_generate_summary_formats_header_and_toc():
    summarizer = DailySummarizer()
    items = [
        _make_item("rss:a", title="First", ai_score=9.0),
        _make_item("rss:b", title="Second", ai_score=7.0),
    ]
    out = asyncio_run(summarizer.generate_summary(items, "2026-01-04", total_fetched=42, language="en"))

    assert "# Horizon Daily - 2026-01-04" in out
    assert "From 42 items, 2 important" in out
    # TOC entries — both items get anchor links.
    assert "#item-1" in out
    assert "#item-2" in out
    assert "9.0/10" in out
    assert "7.0/10" in out


def test_generate_summary_applies_pangu_to_zh_titles():
    summarizer = DailySummarizer()
    item = _make_item("rss:zh", title="测试abc", ai_score=8.0)
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1, language="zh"))

    # Pangu spacing applies between CJK cluster and ASCII suffix only —
    # "测试abc" → "测试 abc" (no space inserted between 测 and 试).
    assert "测试 abc" in out


def test_generate_summary_uses_metadata_title_override():
    """``metadata['title_zh']`` overrides item.title for Chinese rendering."""
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:over",
        title="Default title",
        ai_score=7.0,
        metadata={"title_zh": "重写中文标题"},
    )
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1, language="zh"))

    assert "重写中文标题" in out


def test_generate_summary_unknown_language_falls_back_to_en():
    summarizer = DailySummarizer()
    item = _make_item("rss:a", title="Title", ai_score=8.0)
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1, language="xx"))

    # English header used as fallback.
    assert "# Horizon Daily" in out


def test_generate_summary_strips_brackets_in_toc_title():
    """Markdown link syntax can't have ``[`` or ``]`` in display text → strip."""
    summarizer = DailySummarizer()
    item = _make_item("rss:bracket", title="Title [draft]", ai_score=8.0)
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1, language="en"))

    # ']' '[' should be escaped to '(' ')'.
    assert "[Title (draft)]" in out


# ---------------------------------------------------------------------------
# generate_webhook_overview
# ---------------------------------------------------------------------------


def test_generate_webhook_overview_returns_empty_for_no_items():
    summarizer = DailySummarizer()
    out = summarizer.generate_webhook_overview([], "2026-01-04", total_fetched=0)
    assert "Horizon Daily" in out


def test_generate_webhook_overview_renders_per_item_links():
    summarizer = DailySummarizer()
    items = [
        _make_item("rss:a", title="X", ai_score=8.0),
        _make_item("rss:b", title="Y", ai_score=9.0),
    ]
    out = summarizer.generate_webhook_overview(items, "2026-01-04", total_fetched=10, language="en")

    assert "Selected 2 important items from 10" in out
    assert "[X](https://example.com/rss:a)" in out
    assert "[Y](https://example.com/rss:b)" in out


def test_generate_webhook_overview_zh_uses_pangu():
    summarizer = DailySummarizer()
    item = _make_item("rss:zh", title="测试abc", ai_score=7.0)
    out = summarizer.generate_webhook_overview([item], "2026-01-04", total_fetched=1, language="zh")

    assert "测试 abc" in out
    assert "从 1 条内容中筛选出 1 条" in out


# ---------------------------------------------------------------------------
# generate_webhook_item
# ---------------------------------------------------------------------------


def test_generate_webhook_item_en_prefix():
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:a",
        title="Topic",
        ai_score=8.0,
        ai_summary="A summary",
        metadata={"title_en": "Topic"},
    )
    out = summarizer.generate_webhook_item(item, language="en", index=2, total=5)

    assert "Item 2/5" in out
    assert "Topic" in out


def test_generate_webhook_item_zh_prefix():
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:a",
        title="Header",
        ai_score=8.0,
        ai_summary="Some",
        metadata={"title_zh": "标题测试abc"},
    )
    out = summarizer.generate_webhook_item(item, language="zh", index=3, total=10)

    assert "第 3/10 条" in out
    # Pangu only adds space at CJK↔ASCII boundary; 标→测 stays adjacent.
    assert "标题测试 abc" in out


# ---------------------------------------------------------------------------
# _format_item — source line, background, references, tags
# ---------------------------------------------------------------------------


def test_format_item_source_line_strips_leading_zero_day():
    summarizer = DailySummarizer()
    pub = datetime(2026, 3, 7, 9, 5, tzinfo=UTC)
    item = _make_item("rss:s", title="x", published_at=pub, author="bob")
    out = asyncio_run(summarizer.generate_summary([item], "2026-03-07", total_fetched=1))

    # '%d' would give "07", but the code lstrip("0") → "7".
    # Format: "%b 7, %H:%M" → "Mar 7, 09:05"
    assert "bob" in out
    assert "Mar 7, 09:05" in out


def test_format_item_zh_date_format():
    summarizer = DailySummarizer()
    pub = datetime(2026, 3, 7, 9, 5, tzinfo=UTC)
    item = _make_item("rss:s", title="x", published_at=pub)
    out = asyncio_run(summarizer.generate_summary([item], "2026-03-07", total_fetched=1, language="zh"))

    # Chinese source date: "3月7日 09:05"
    assert "3月7日" in out
    assert "09:05" in out


def test_format_item_includes_background_when_present():
    summarizer = DailySummarizer()
    item = _make_item("rss:b", title="x", ai_score=7.0, metadata={"background_en": "Some context"})
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    assert "**Background**: Some context" in out


def test_format_item_includes_community_discussion():
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:d", title="x", ai_score=7.0,
        metadata={"community_discussion_en": "Divided opinions"},
    )
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    assert "**Discussion**: Divided opinions" in out


def test_format_item_includes_tags_as_hashtags():
    summarizer = DailySummarizer()
    item = _make_item("rss:t", title="x", ai_score=7.0, ai_tags=["python", "ai"])
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    # AI tags render as ``**Tags**: `#python`, `#ai` `` (backtick-quoted hashtags).
    assert "`#python`" in out
    assert "`#ai`" in out


def test_format_item_includes_references_when_sources_present():
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:r", title="x", ai_score=7.0,
        metadata={"sources": [{"url": "https://ref/a", "title": "Ref A"}]},
    )
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    # References render as HTML ``<details><summary>References</summary>``
    # rather than a bold header — Discord/Slack render it as a collapsible.
    assert "<summary>References</summary>" in out
    assert "https://ref/a" in out
    assert "Ref A" in out


def test_format_item_includes_discussion_url_when_distinct():
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:r", title="x", ai_score=7.0,
        metadata={"discussion_url": "https://news/thread/1"},
    )
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    assert "Discussion](https://news/thread/1)" in out


def test_format_item_uses_reddit_feed_name_in_source_line():
    summarizer = DailySummarizer()
    item = _make_item(
        "rss:r", title="x", ai_score=7.0,
        source_type=SourceType.REDDIT,
        metadata={"subreddit": "Python", "feed_name": "r/Python"},
    )
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    assert "r/Python" in out
    assert "Python" in out  # subreddit shows up before feed_name substitution


def test_format_item_uses_author_when_no_feed_name():
    summarizer = DailySummarizer()
    item = _make_item("rss:a", title="x", ai_score=7.0, author="")
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    # Falls back to "unknown" when author None/missing.
    assert "unknown" in out


def test_format_item_falls_back_to_ai_summary_when_no_detailed_summary():
    summarizer = DailySummarizer()
    item = _make_item("rss:f", title="x", ai_score=7.0, ai_summary="Fallback summary text")
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    assert "Fallback summary text" in out


def test_format_item_uses_placeholder_when_no_ai_score():
    summarizer = DailySummarizer()
    item = _make_item("rss:p", title="x", ai_score=None)
    out = asyncio_run(summarizer.generate_summary([item], "2026-01-04", total_fetched=1))

    assert "?/10" in out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):
    """Convenience: synchronously run an async coroutine for test setup."""
    import asyncio

    return asyncio.run(coro)
