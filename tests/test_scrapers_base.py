"""Tests for ``src/scrapers/base.py`` — abstract base for all source scrapers.

``BaseScraper`` is abstract (``abstractmethod fetch``) so we cannot instantiate
it directly. Tests exercise a minimal subclass that implements ``fetch``.

Covers:
- Construction stores ``config`` and optional ``http_client``.
- Subclass implementing ``fetch`` can be instantiated and used.
- The protected ``_generate_id`` helper produces ``{source}:{subtype}:{native_id}``.
"""

import httpx
import pytest

from src.scrapers.base import BaseScraper


class _ConcreteScraper(BaseScraper):
    async def fetch(self, since):
        return []


def test_basescraper_cannot_be_instantiated_directly():
    import datetime as dt

    with pytest.raises(TypeError):
        BaseScraper({}, http_client=None).fetch(dt.datetime.now(dt.UTC))  # type: ignore[abstract]


def test_concrete_subclass_stores_config_and_client():
    cfg = {"source_type": "github", "enabled": True}
    client = httpx.AsyncClient()
    scraper = _ConcreteScraper(cfg, client)
    assert scraper.config is cfg
    assert scraper.client is client
    # Mosaic cleanup: not closing httpx.AsyncClient (sync scope OK for test stub).


def test_concrete_subclass_works_with_no_http_client():
    """Browser-based scrapers don't need an async HTTP client."""
    scraper = _ConcreteScraper({"foo": "bar"})
    assert scraper.client is None


def test_generate_id_formats_three_part_string():
    scraper = _ConcreteScraper({})
    assert scraper._generate_id("github", "release", "v1.0.0") == "github:release:v1.0.0"
    assert scraper._generate_id("hackernews", "story", "42") == "hackernews:story:42"


def test_fetch_returns_empty_list_for_concrete_subclass():
    import asyncio
    import datetime as dt

    scraper = _ConcreteScraper({})
    result = asyncio.run(scraper.fetch(dt.datetime.now(dt.UTC)))
    assert result == []
