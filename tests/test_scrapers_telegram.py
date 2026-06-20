"""Tests for ``src/scrapers/telegram.py`` — Telegram public channel HTML scraper.

Coverage targets:
- ``fetch`` — disabled short-circuit, channel async fan-out
- ``_fetch_channel`` — rate-limit retry, plain failure → empty list
- ``_parse_channel_html`` — BeautifulSoup-based message extraction
- ``_parse_message`` — data_post, time[datetime], text content, first external link
- ``_make_title`` — CJK punctuation boundary, fallback hard-truncate
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from src.models import TelegramChannelConfig, TelegramConfig
from src.scrapers.telegram import TelegramScraper


def _since() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _client_with_text(text, status_code=200, headers=None):
    client = AsyncMock()

    async def fake_get(url, params=None, headers=None, **kw):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = headers or {}
        resp.text = text
        resp.raise_for_status = MagicMock(return_value=None)
        return resp

    client.get = fake_get
    return client


def test_fetch_returns_empty_when_disabled():
    config = TelegramConfig(enabled=False, channels=[])
    scraper = TelegramScraper(config, AsyncMock())
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_parse_message_extracts_first_external_link():
    """When the message contains external links, the FIRST one becomes the URL.
    The message's own t.me URL is the fallback.
    """
    cfg = TelegramChannelConfig(channel="tech", fetch_limit=10)
    config = TelegramConfig(channels=[cfg])
    html = """
    <html><body>
      <div class="tgme_widget_message" data-post="tech/12345">
        <time datetime="2026-01-02T10:00:00+00:00"></time>
        <div class="tgme_widget_message_text">
          Check out <a href="https://example.com/article">this article</a> on linkedin <a href="https://linkedin.com/post">post</a>
        </div>
      </div>
    </body></html>
    """
    scraper = TelegramScraper(config, _client_with_text(html))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    # First external (non-t.me) link wins → "https://example.com/article".
    assert str(result[0].url) == "https://example.com/article"


def test_parse_message_falls_back_to_tme_url():
    """When no external link, the channel message URL is the fallback."""
    cfg = TelegramChannelConfig(channel="tech", fetch_limit=10)
    config = TelegramConfig(channels=[cfg])
    html = """
    <html><body>
      <div class="tgme_widget_message" data-post="tech/99">
        <time datetime="2026-01-02T10:00:00+00:00"></time>
        <div class="tgme_widget_message_text">
          Just some plain text.
        </div>
      </div>
    </body></html>
    """
    scraper = TelegramScraper(config, _client_with_text(html))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    assert str(result[0].url) == "https://t.me/tech/99"


def test_parse_message_skips_missing_post_id():
    cfg = TelegramChannelConfig(channel="tech", fetch_limit=10)
    config = TelegramConfig(channels=[cfg])
    html = """<html><body>
      <div class="tgme_widget_message">
        <time datetime="2026-01-02T10:00:00+00:00"></time>
        <div class="tgme_widget_message_text">x</div>
      </div>
    </body></html>"""
    scraper = TelegramScraper(config, _client_with_text(html))
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_make_title_short_under_eighty_chars():
    text = "Short sticky headline"
    title = TelegramScraper._make_title(text)
    assert title == text


def test_make_title_breaks_at_chinese_punctuation():
    text = "这是一段超过八十个字符的中文标题。" + "补" * 70
    title = TelegramScraper._make_title(text)
    # Should end at the first 。 boundary ≤ 80 chars.
    assert title.endswith("。")
    assert len(title) <= 80


def test_make_title_hard_truncates_when_no_punctuation():
    text = "a" * 100
    title = TelegramScraper._make_title(text)
    assert len(title) == 80


def test_fetch_returns_empty_after_failed_request():
    cfg = TelegramChannelConfig(channel="tech", fetch_limit=10)
    config = TelegramConfig(channels=[cfg])

    client = AsyncMock()

    async def fake_get(url, **kw):
        resp = MagicMock()
        resp.status_code = 500
        resp.headers = {}
        resp.text = ""
        resp.raise_for_status = MagicMock(side_effect=Exception("server error"))
        return resp

    client.get = fake_get
    scraper = TelegramScraper(config, client)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []
