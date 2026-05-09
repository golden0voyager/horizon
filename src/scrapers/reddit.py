"""Reddit scraper via RSS (no OAuth required)."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

import feedparser
import httpx

from ..models import ContentItem, RedditConfig, RedditSubredditConfig, RedditUserConfig, SourceType
from .base import BaseScraper

logger = logging.getLogger(__name__)

REDDIT_BASE = "https://www.reddit.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)


class RedditScraper(BaseScraper):
    """Scraper for Reddit posts via RSS feeds."""

    def __init__(self, config: RedditConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        self.reddit_config = config

    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not self.config.get("enabled", True):
            return []

        tasks = []
        for sub_cfg in self.reddit_config.subreddits:
            if sub_cfg.enabled:
                tasks.append(self._fetch_subreddit(sub_cfg, since))
        for user_cfg in self.reddit_config.users:
            if user_cfg.enabled:
                tasks.append(self._fetch_user(user_cfg, since))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: List[ContentItem] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Error fetching Reddit source: %s", result)
            elif isinstance(result, list):
                items.extend(result)
        return items

    async def _fetch_subreddit(self, cfg: RedditSubredditConfig, since: datetime) -> List[ContentItem]:
        url = f"{REDDIT_BASE}/r/{cfg.subreddit}/{cfg.sort}.rss"
        params: dict = {}
        if cfg.sort in ("top", "controversial"):
            params["t"] = cfg.time_filter

        entries = await self._fetch_rss(url, params)
        return self._process_entries(entries, since, "subreddit", cfg.subreddit, cfg.min_score)

    async def _fetch_user(self, cfg: RedditUserConfig, since: datetime) -> List[ContentItem]:
        url = f"{REDDIT_BASE}/user/{cfg.username}/submitted.rss"
        params: dict = {}
        if cfg.sort in ("top", "controversial"):
            params["t"] = cfg.time_filter

        entries = await self._fetch_rss(url, params)
        return self._process_entries(entries, since, "user", cfg.username, min_score=0)

    async def _fetch_rss(self, url: str, params: dict) -> List[dict]:
        headers = {
            "User-Agent": self.reddit_config.user_agent or USER_AGENT,
            "Accept": "application/rss+xml,application/xml,*/*",
        }
        try:
            response = await self.client.get(url, params=params, headers=headers, follow_redirects=True)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                logger.warning("Reddit RSS rate limited, retrying after %ds", retry_after)
                await asyncio.sleep(retry_after)
                response = await self.client.get(url, params=params, headers=headers, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Reddit RSS request failed for %s: %s", url, exc)
            return []

        try:
            parsed = feedparser.parse(response.text)
        except Exception as exc:
            logger.warning("Failed to parse Reddit RSS from %s: %s", url, exc)
            return []

        return parsed.entries

    def _process_entries(
        self,
        entries: List[dict],
        since: datetime,
        subtype: str,
        source_name: str,
        min_score: int,
    ) -> List[ContentItem]:
        items: List[ContentItem] = []
        for entry in entries:
            item = self._parse_entry(entry, since, subtype, source_name, min_score)
            if item:
                items.append(item)
        return items

    def _parse_entry(
        self,
        entry: dict,
        since: datetime,
        subtype: str,
        source_name: str,
        min_score: int,
    ) -> Optional[ContentItem]:
        try:
            # Parse published time
            published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            if published_parsed:
                published_at = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            else:
                # Fallback: try to parse published string
                published_at = self._parse_date(entry.get("published", ""))
                if not published_at:
                    return None

            if published_at < since:
                return None

            title = entry.get("title", "")
            if not title:
                return None

            link = entry.get("link", "")
            if not link:
                return None

            # Reddit RSS puts score in title like "Title (123 upvotes)"
            # Try to extract score from title or content
            score = self._extract_score(entry)
            if score < min_score:
                return None

            author = entry.get("author", "unknown")
            # author often comes as "/u/username" — strip prefix
            if isinstance(author, str) and author.startswith("/u/"):
                author = author[3:]

            content = self._extract_content(entry)
            post_id = entry.get("id", link)
            # Normalize ID
            if "reddit.com" in post_id:
                post_id = link.rstrip("/").split("/")[-1]
            if "_" in str(post_id):
                post_id = str(post_id).split("_")[-1]

            return ContentItem(
                id=self._generate_id("reddit", subtype, post_id),
                source_type=SourceType.REDDIT,
                title=title,
                url=link,
                content=content,
                author=author,
                published_at=published_at,
                metadata={
                    "score": score,
                    "subreddit": source_name if subtype == "subreddit" else "",
                    "source": source_name,
                    "subtype": subtype,
                },
            )
        except Exception as exc:
            logger.debug("Failed to parse Reddit RSS entry: %s", exc)
            return None

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """Try common RSS date formats."""
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(date_str)
        except Exception:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_score(entry: dict) -> int:
        """Try to extract upvote score from Reddit RSS entry."""
        # Reddit sometimes includes score in content or title
        content = entry.get("summary", "")
        # Look for patterns like "123 upvotes" or "score: 123"
        import re
        match = re.search(r"(\d+)\s+upvotes?", content, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 0

    @staticmethod
    def _extract_content(entry: dict) -> str:
        """Extract description/content from RSS entry."""
        content = entry.get("summary", "")
        if not content:
            content = entry.get("description", "")
        # Reddit RSS often wraps content in HTML, strip tags for plain text
        if content:
            import html
            # Simple tag stripping
            import re
            content = re.sub(r"<[^>]+>", " ", content)
            content = html.unescape(content)
            content = " ".join(content.split())
        return content
