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
from datetime import UTC, datetime
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


def test_coerce_datetime_returns_none_for_invalid_string():
    assert OpenBBScraper._coerce_datetime("not-a-date") is None


def test_parse_symbols_from_string_csv():
    assert OpenBBScraper._parse_symbols("aapl, GOOG, msft") == ["AAPL", "GOOG", "MSFT"]


def test_parse_symbols_dedup():
    assert OpenBBScraper._parse_symbols(["AAPL", "aapl", "AAPL"]) == ["AAPL"]


def test_parse_symbols_empty_when_unsupported_type():
    assert OpenBBScraper._parse_symbols(42) == []
