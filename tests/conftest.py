from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_horizon_ai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ``HORIZON_AI_*`` env vars before every test.

    Several tests mutate ``os.environ`` directly without the ``monkeypatch``
    fixture, leaking values like ``HORIZON_AI_PROVIDER`` to the rest of the
    suite. ``src.models.AIConfig.model_validator(mode="before")`` reads these
    keys to override the explicit JSON config, so a leaked provider flips
    downstream assertions in ``test_minimax_client.py`` and
    ``test_storage.py`` when they run later as part of the full suite (yet pass
    in isolation). Resetting these keys per-test keeps every test deterministic.
    """
    for key in list(os.environ.keys()):
        if key.startswith("HORIZON_AI_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fast_twitter_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass 25s real-time waits in ``TwitterScraper.fetch()`` retry path.

    Production scraper fans out ``asyncio.sleep(20)`` between retry attempts
    plus ``random.uniform(2, 4)`` jitter for human-browsing simulation. Layer 2
    tests ground through the same paths but cannot wait 25 wall-clock seconds
    on CI. Patches are pinned to ``src.scrapers.twitter`` to avoid affecting
    randomization elsewhere in the suite.
    """

    async def _noop_sleep(_delay: float = 0, *_args: object) -> None:
        return None

    monkeypatch.setattr("src.scrapers.twitter.asyncio.sleep", _noop_sleep)
    monkeypatch.setattr("src.scrapers.twitter.random.uniform", lambda _a, _b: 0)
    monkeypatch.setattr("src.scrapers.twitter.random.randint", lambda _a, _b: 0)
