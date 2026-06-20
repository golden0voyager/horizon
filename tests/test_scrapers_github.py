"""Tests for ``src/scrapers/github.py`` — GitHub events + releases scraper.

Coverage targets:
- ``fetch`` orchestration (skips disabled, dispatches by ``source.type``)
- ``_fetch_user_events`` event-type filtering (PushEvent/CreateEvent/...)
- ``_fetch_repo_releases`` release date filter, prerelease flag
- ``_parse_event`` PushEvent/CreateEvent/ReleaseEvent/PublicEvent/WatchEvent titles
- ``_get_headers`` Bearer token passthrough when ``GITHUB_TOKEN`` is set
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.models import GitHubSourceConfig
from src.scrapers.github import GitHubScraper


def _client_with_json(json_body, status_code=200):
    """Build an httpx.AsyncClient whose ``.get`` returns a canned JSON body."""
    client = AsyncMock()

    async def fake_get(url, params=None, headers=None, **kw):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {}
        resp.text = ""
        resp.json = MagicMock(return_value=json_body)
        if status_code >= 400:
            resp.raise_for_status = MagicMock(side_effect=httpx.HTTPError("http error"))
        else:
            resp.raise_for_status = MagicMock(return_value=None)
        return resp

    client.get = fake_get
    return client


def _since() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_fetch_returns_empty_when_all_sources_disabled():
    config = [
        GitHubSourceConfig(type="user_events", username="octocat", enabled=False),
        GitHubSourceConfig(type="repo_releases", owner="x", repo="y", enabled=False),
    ]
    scraper = GitHubScraper(config, AsyncMock())
    result = asyncio.run(scraper.fetch(_since()))
    assert result == []


def test_fetch_user_events_filters_to_supported_types():
    config = [GitHubSourceConfig(type="user_events", username="octocat", enabled=True)]
    events = [
        {"id": "1", "type": "PushEvent", "created_at": "2026-01-02T00:00:00Z",
         "repo": {"name": "octocat/hello"}, "payload": {"commits": [{"message": "fix bug"}]}},
        {"id": "2", "type": "IssueCommentEvent", "created_at": "2026-01-02T00:00:00Z",
         "repo": {"name": "octocat/hello"}, "payload": {}},  # not in support list
        {"id": "3", "type": "WatchEvent", "created_at": "2026-01-02T00:00:00Z",
         "repo": {"name": "octocat/hello"}, "payload": {}},
    ]
    scraper = GitHubScraper(config, _client_with_json(events))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 2
    types = {item.metadata["event_type"] for item in result}
    assert types == {"PushEvent", "WatchEvent"}


def test_fetch_user_events_filters_by_date():
    config = [GitHubSourceConfig(type="user_events", username="octocat", enabled=True)]
    events = [
        {"id": "1", "type": "PushEvent", "created_at": "2025-12-31T00:00:00Z",
         "repo": {"name": "octocat/hello"}, "payload": {"commits": []}},
        {"id": "2", "type": "PushEvent", "created_at": "2026-01-02T00:00:00Z",
         "repo": {"name": "octocat/hello"}, "payload": {"commits": []}},
    ]
    scraper = GitHubScraper(config, _client_with_json(events))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 1
    assert result[0].metadata["event_type"] == "PushEvent"


def test_fetch_user_events_handles_http_error():
    config = [GitHubSourceConfig(type="user_events", username="missing", enabled=True)]
    scraper = GitHubScraper(config, _client_with_json({}, status_code=500))
    result = asyncio.run(scraper.fetch(_since()))
    # Production catches httpx.HTTPError → empty list.
    assert result == []


def test_fetch_repo_releases_includes_prerelease_flag():
    config = [GitHubSourceConfig(type="repo_releases", owner="x", repo="y", enabled=True)]
    releases = [
        {"id": 1, "tag_name": "v1.0", "html_url": "https://github.com/x/y/releases/tag/v1.0",
         "body": "first release", "author": {"login": "alice"}, "published_at": "2026-01-02T00:00:00Z",
         "prerelease": False},
        {"id": 2, "tag_name": "v1.1-beta", "html_url": "https://github.com/x/y/releases/tag/v1.1-beta",
         "body": "beta", "author": {"login": "bob"}, "published_at": "2026-01-03T00:00:00Z",
         "prerelease": True},
    ]
    scraper = GitHubScraper(config, _client_with_json(releases))
    result = asyncio.run(scraper.fetch(_since()))
    assert len(result) == 2
    pr_flags = [item.metadata["prerelease"] for item in result]
    assert False in pr_flags
    assert True in pr_flags


def test_get_headers_uses_token_when_env_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token-123")
    config: list = []
    scraper = GitHubScraper(config, AsyncMock())
    asyncio.run(scraper.fetch(_since()))  # returns [] because config is empty
    headers = scraper._get_headers()
    assert headers["Authorization"] == "token secret-token-123"


def test_get_headers_omits_authorization_when_token_absent(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config: list = []
    scraper = GitHubScraper(config, AsyncMock())
    headers = scraper._get_headers()
    assert "Authorization" not in headers
