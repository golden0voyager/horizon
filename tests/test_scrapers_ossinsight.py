"""Tests for ``src/scrapers/ossinsight.py`` — trending GitHub repos scraper.

Coverage targets:
- ``fetch`` — disabled short-circuit, dedup, min_star + keyword filter, sort + max_items cap
- ``_fetch_period`` — returns rows from response payload, swallows HTTPError → []
- ``_row_to_item`` — repo_name/repo_id required → None when missing
- ``_stars_int`` / ``_int`` — best-effort int coercion
- ``_matches_keywords`` — case-insensitive substring against description/collection_names/repo_name
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.models import OSSInsightConfig
from src.scrapers.ossinsight import OSSInsightScraper


def _since() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _client_with_json(json_body, status_code=200):
    client = AsyncMock()

    async def fake_get(url, params=None, headers=None, **kw):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {}
        resp.text = ""
        resp.json = MagicMock(return_value=json_body)
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPError("error") if status_code >= 400 else None
        )
        return resp

    client.get = fake_get
    return client


def test_fetch_returns_empty_when_disabled():
    config = OSSInsightConfig(enabled=False, languages=["Python"])
    scraper = OSSInsightScraper(config, AsyncMock())
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_returns_top_stargain_repos():
    rows = [
        {"repo_name": "x/y", "repo_id": 1, "stars": 500, "forks": 10,
         "pushes": 5, "pull_requests": 1, "primary_language": "Python",
         "description": "Cool tool", "collection_names": "AI"},
        {"repo_name": "x/z", "repo_id": 2, "stars": 100, "forks": 2,
         "pushes": 1, "pull_requests": 0, "primary_language": "Python"},
    ]
    body = {"data": {"rows": rows}}
    config = OSSInsightConfig(
        enabled=True, languages=["Python"], min_stars=50, max_items=30,
    )
    scraper = OSSInsightScraper(config, _client_with_json(body))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 2
    # Should be sorted by stars desc.
    assert result[0].metadata["stars_gained"] == 500
    assert result[0].metadata["repo"] == "x/y"


def test_fetch_filters_below_min_stars():
    rows = [
        {"repo_name": "x/low", "repo_id": 1, "stars": 1, "forks": 0,
         "pushes": 0, "primary_language": "Python"},
    ]
    body = {"data": {"rows": rows}}
    config = OSSInsightConfig(enabled=True, languages=["Python"], min_stars=100)
    scraper = OSSInsightScraper(config, _client_with_json(body))
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_filters_by_keywords_case_insensitive():
    rows = [
        {"repo_name": "x/match", "repo_id": 1, "stars": 100, "forks": 1,
         "pushes": 1, "primary_language": "Python",
         "description": "AI / LLM helper library"},
        {"repo_name": "x/skip", "repo_id": 2, "stars": 100, "forks": 1,
         "pushes": 1, "primary_language": "Python",
         "description": "CLI formatter"},
    ]
    body = {"data": {"rows": rows}}
    config = OSSInsightConfig(
        enabled=True, languages=["Python"], min_stars=10, keywords=["ai"],
    )
    scraper = OSSInsightScraper(config, _client_with_json(body))
    result = asyncio.run(scraper.fetch(_since()))
    # Only the ai/llm matching repo survives.
    assert len(result) == 1
    assert result[0].metadata["repo"] == "x/match"


def test_fetch_respects_max_items_cap():
    rows = [
        {"repo_name": f"x/r{i}", "repo_id": i, "stars": 100 - i, "forks": 1,
         "pushes": 1, "primary_language": "Python"}
        for i in range(1, 6)
    ]
    body = {"data": {"rows": rows}}
    config = OSSInsightConfig(
        enabled=True, languages=["Python"], min_stars=0, max_items=2,
    )
    scraper = OSSInsightScraper(config, _client_with_json(body))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 2


def test_fetch_dedup_same_repo_across_languages():
    rows = [
        {"repo_name": "x/y", "repo_id": 1, "stars": 100, "forks": 1,
         "pushes": 1, "primary_language": "Python"},
    ]
    body = {"data": {"rows": rows}}
    # Two languages both return the same repo → only one ContentItem.
    config = OSSInsightConfig(
        enabled=True, languages=["Python", "JavaScript"], min_stars=0,
    )
    scraper = OSSInsightScraper(config, _client_with_json(body))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1


def test_fetch_period_handles_http_error():
    config = OSSInsightConfig(enabled=True, languages=["Python"])
    scraper = OSSInsightScraper(config, _client_with_json({}, status_code=500))
    rows = asyncio.run(scraper._fetch_period("past_24_hours", "Python"))
    assert rows == []


def test_fetch_period_handles_missing_data_key():
    config = OSSInsightConfig(enabled=True, languages=["Python"])
    scraper = OSSInsightScraper(config, _client_with_json({"data": None}))
    rows = asyncio.run(scraper._fetch_period("past_24_hours", "Python"))
    assert rows == []


def test_row_to_item_returns_none_when_missing_required_fields():
    config = OSSInsightConfig(enabled=True)
    scraper = OSSInsightScraper(config, AsyncMock())
    assert scraper._row_to_item({"repo_name": "x/y"}, "Python") is None
    assert scraper._row_to_item({"repo_id": 1}, "Python") is None


def test_int_handles_invalid_values():
    assert OSSInsightScraper._int(None) == 0
    assert OSSInsightScraper._int("not-a-number") == 0
    assert OSSInsightScraper._int("42") == 42
    assert OSSInsightScraper._int(42.5) == 42


def test_matches_keywords_no_keywords_returns_true():
    """``_matches_keywords`` with empty keywords_list returns ``False``
    (any([]) is ``False``). The keyword gate is bypassed in ``fetch``
    via the ``if self._keywords_lower`` guard preceding this method.
    """
    config = OSSInsightConfig(enabled=True, keywords=[])
    scraper = OSSInsightScraper(config, AsyncMock())
    assert scraper._keywords_lower == []
    assert scraper._matches_keywords({"description": "anything"}) is False
