"""Tests for ``src/scrapers/openbb.py`` — OpenBB SDK-backed news scraper.

Coverage targets:
- ``__init__`` — ``_try_import_obb`` resolves to ``obb`` when import works, ``None`` when missing
- ``fetch`` — no-op when ``_obb`` is None, no-op when watchlists disabled, dedup by URL across watchlists
- ``_fetch_watchlist`` — calls ``obb.news.company`` with concatenated symbol string
- ``_raw_to_item`` — drops records missing URL/title/date, falls back to watchlist symbols
- ``_coerce_datetime`` — datetime / string / None paths, tz-naive → UTC
- ``_coerce_url`` — None / falsy / ``str(value).strip()``
- ``_parse_symbols`` — str / list / tuple / set, dedup + uppercase
- ``_derive_native_id`` — stable pairing ``ts::url``
"""

import asyncio
from datetime import UTC, datetime, timezone, timedelta
from types import SimpleNamespace

from src.models import OpenBBConfig, OpenBBWatchlist
from src.scrapers.openbb import OpenBBScraper


def _since() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _make_result(url, title, date, body="", author=None, symbols="AAPL"):
    return SimpleNamespace(url=url, title=title, date=date, body=body,
                           author=author, symbols=symbols, excerpt=None)


def test_fetch_returns_empty_when_obb_is_none(monkeypatch):
    """When openbb SDK is not installed, _obb is None and fetch returns []."""
    monkeypatch.setattr("src.scrapers.openbb.OpenBBScraper._try_import_obb",
                        staticmethod(lambda: None))
    config = OpenBBConfig(
        enabled=True,
        watchlists=[OpenBBWatchlist(name="W", symbols=["AAPL"])],
    )
    scraper = OpenBBScraper(config, http_client=None)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_returns_empty_when_config_disabled(monkeypatch):
    """when ``enabled=False`` even with obb installed, fetch is no-op."""
    fake_obb = SimpleNamespace()
    monkeypatch.setattr("src.scrapers.openbb.OpenBBScraper._try_import_obb",
                        staticmethod(lambda: fake_obb))
    config = OpenBBConfig(
        enabled=False,  # explicitly disabled
        watchlists=[OpenBBWatchlist(name="W", symbols=["AAPL"])],
    )
    scraper = OpenBBScraper(config, http_client=None)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_dedups_urls_across_watchlists(monkeypatch):
    """Same URL appears in two watchlists → only one ContentItem."""
    fake_obb = SimpleNamespace()

    def fake_news_company(symbol, limit, provider):
        return SimpleNamespace(results=[
            _make_result("https://news/1", "Apple Q4", datetime(2026, 1, 2, tzinfo=UTC)),
        ])

    fake_obb.news = SimpleNamespace(company=fake_news_company)
    monkeypatch.setattr("src.scrapers.openbb.OpenBBScraper._try_import_obb",
                        staticmethod(lambda: fake_obb))

    config = OpenBBConfig(
        enabled=True,
        watchlists=[
            OpenBBWatchlist(name="W1", symbols=["AAPL"]),
            OpenBBWatchlist(name="W2", symbols=["AAPL"]),  # duplicate
        ],
    )
    scraper = OpenBBScraper(config, http_client=None)
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    assert result[0].metadata["watchlist"] == "W1"


def test_raw_to_item_drops_records_missing_url():
    """``url=None`` or empty → item dropped."""
    config = OpenBBConfig(enabled=True, watchlists=[OpenBBWatchlist(name="W", symbols=[])])
    scraper = OpenBBScraper(config, http_client=None)
    raw = _make_result(None, "Title", datetime(2026, 1, 2, tzinfo=UTC))
    item = scraper._raw_to_item(raw, OpenBBWatchlist(name="W", symbols=["A"]),
                                 datetime(2026, 1, 1, tzinfo=UTC))
    assert item is None


def test_raw_to_item_drops_records_missing_title():
    config = OpenBBConfig(enabled=True, watchlists=[OpenBBWatchlist(name="W", symbols=[])])
    scraper = OpenBBScraper(config, http_client=None)
    raw = _make_result("https://news/1", "  ", datetime(2026, 1, 2, tzinfo=UTC))
    item = scraper._raw_to_item(raw, OpenBBWatchlist(name="W", symbols=["A"]),
                                 datetime(2026, 1, 1, tzinfo=UTC))
    assert item is None


def test_raw_to_item_drops_records_older_than_since():
    config = OpenBBConfig(enabled=True, watchlists=[OpenBBWatchlist(name="W", symbols=[])])
    scraper = OpenBBScraper(config, http_client=None)
    raw = _make_result("https://news/1", "Old", datetime(2025, 12, 31, tzinfo=UTC))
    item = scraper._raw_to_item(raw, OpenBBWatchlist(name="W", symbols=["A"]),
                                 datetime(2026, 1, 1, tzinfo=UTC))
    assert item is None


def test_raw_to_item_uses_watchlist_symbols_when_record_symbols_blank():
    config = OpenBBConfig(enabled=True, watchlists=[])
    scraper = OpenBBScraper(config, http_client=None)
    raw = _make_result("https://news/1", "T", datetime(2026, 1, 2, tzinfo=UTC), symbols="")
    item = scraper._raw_to_item(raw, OpenBBWatchlist(name="W", symbols=["AAPL", "GOOG"]),
                                 datetime(2026, 1, 1, tzinfo=UTC))
    assert item is not None
    assert item.metadata["symbols"] == ["AAPL", "GOOG"]


def test_coerce_datetime_from_iso_string():
    result = OpenBBScraper._coerce_datetime("2026-01-02T10:00:00Z")
    assert result == datetime(2026, 1, 2, 10, 0, tzinfo=UTC)


def test_coerce_datetime_from_naive_datetime_adds_utc():
    result = OpenBBScraper._coerce_datetime(datetime(2026, 1, 2, 10, 0))
    assert result.tzinfo == UTC


def test_coerce_datetime_returns_none_for_invalid_input():
    assert OpenBBScraper._coerce_datetime("not-a-date") is None
    assert OpenBBScraper._coerce_datetime(42) is None
    assert OpenBBScraper._coerce_datetime(None) is None


def test_parse_symbols_from_string_csv():
    assert OpenBBScraper._parse_symbols("aapl, GOOG, msft") == ["AAPL", "GOOG", "MSFT"]


def test_parse_symbols_dedup():
    assert OpenBBScraper._parse_symbols(["AAPL", "aapl", "AAPL"]) == ["AAPL"]


def test_parse_symbols_empty_when_unsupported_type():
    assert OpenBBScraper._parse_symbols(42) == []


def test_coerce_url_handles_bogus():
    assert OpenBBScraper._coerce_url(None) is None
    assert OpenBBScraper._coerce_url("  ") is None
    assert OpenBBScraper._coerce_url("http://x") == "http://x"


def test_ensure_utc_other_tz():
    """Non-UTC timezone is correctly converted to UTC."""
    other = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    result = OpenBBScraper._ensure_utc(other)
    assert result.tzinfo == UTC
    assert result.hour == 4


def test_fetch_skips_disabled_watchlist(monkeypatch):
    """Disabled watchlist within an enabled config is skipped."""
    fake_obb = SimpleNamespace()
    fake_obb.news = SimpleNamespace(company=lambda symbol, limit, provider: SimpleNamespace(results=[]))
    monkeypatch.setattr("src.scrapers.openbb.OpenBBScraper._try_import_obb",
                        staticmethod(lambda: fake_obb))
    config = OpenBBConfig(
        enabled=True,
        watchlists=[OpenBBWatchlist(name="off", symbols=["AAPL"], enabled=False)],
    )
    scraper = OpenBBScraper(config, http_client=None)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_skips_watchlist_with_no_symbols(monkeypatch):
    """Watchlist with empty symbols list is skipped."""
    fake_obb = SimpleNamespace()
    fake_obb.news = SimpleNamespace(company=lambda symbol, limit, provider: SimpleNamespace(results=[]))
    monkeypatch.setattr("src.scrapers.openbb.OpenBBScraper._try_import_obb",
                        staticmethod(lambda: fake_obb))
    config = OpenBBConfig(
        enabled=True,
        watchlists=[OpenBBWatchlist(name="empty", symbols=[])],
    )
    scraper = OpenBBScraper(config, http_client=None)
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_watchlist_exception_does_not_block_others(monkeypatch):
    """An exception in one watchlist doesn't prevent others from returning results."""
    fake_obb = SimpleNamespace()
    call_count = 0

    def fake_news_company(symbol, limit, provider):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("upstream 500")
        return SimpleNamespace(results=[
            SimpleNamespace(
                url="https://news/msft", title="MSFT up",
                date=datetime(2026, 1, 2, tzinfo=UTC), body="", author=None,
                symbols="MSFT", excerpt=None,
            )
        ])

    fake_obb.news = SimpleNamespace(company=fake_news_company)
    monkeypatch.setattr("src.scrapers.openbb.OpenBBScraper._try_import_obb",
                        staticmethod(lambda: fake_obb))

    config = OpenBBConfig(
        enabled=True,
        watchlists=[
            OpenBBWatchlist(name="boom", symbols=["AAPL"]),
            OpenBBWatchlist(name="ok", symbols=["MSFT"]),
        ],
    )
    scraper = OpenBBScraper(config, http_client=None)
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    assert result[0].metadata["watchlist"] == "ok"
