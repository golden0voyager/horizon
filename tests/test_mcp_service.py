"""Phase 5 unit tests for ``src.mcp.service.HorizonPipelineService``.

Coverage focus (the integration heavy-paths ``fetch_items`` / ``score_items`` /
``enrich_items`` / ``generate_summary`` / ``run_pipeline`` are deferred to a
separate integration smoke suite — they require a fully working runtime + AI
client + storage):

- ``list_runs`` aggregates ``run_store.list_runs`` + has_stage per stage.
- ``get_run_meta`` raises ``HZ_RUN_NOT_FOUND`` when load_meta raises
  FileNotFoundError.
- ``get_run_stage`` validation: max_items <= 0 → ``HZ_INVALID_INPUT``;
  invalid stage → ``HZ_INVALID_STAGE``; missing → ``HZ_STAGE_NOT_FOUND``.
- ``get_run_summary`` raises ``HZ_SUMMARY_NOT_FOUND`` for missing summaries.
- ``_score_distribution`` pure-math bucketing.
- ``_pick_summary_stage`` selects first stage with has_stage True; raises
  ``HZ_STAGE_NOT_FOUND`` when none.
- ``_total_fetched`` returns ``len(load_items('raw'))`` or fallback.
- ``send_webhook`` short-circuits when webhook disabled; otherwise calls
  ``notifier.notify`` with computed variables.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mcp.errors import HorizonMcpError
from src.mcp.run_store import RunStore
from src.mcp.service import HorizonPipelineService, _default_runs_root

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path: Path) -> HorizonPipelineService:
    return HorizonPipelineService(runs_root=tmp_path / "runs")


# ---------------------------------------------------------------------------
# list_runs / get_run_meta / get_run_stage / get_run_summary
# ---------------------------------------------------------------------------


def test_list_runs_empty(service: HorizonPipelineService) -> None:
    assert service.list_runs(limit=10) == {"count": 0, "items": []}


def test_list_runs_with_stages_present(
    service: HorizonPipelineService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = service.run_store.create_run("r1")
    service.run_store.save_items(rid, "raw", [{"id": "1"}])
    monkeypatch.setattr(
        service.run_store, "list_runs",
        lambda limit=20: [
            {"run_id": rid, "created_at": "x", "updated_at": "y", "meta": {}}
        ],
    )
    out = service.list_runs(limit=10)
    assert out["count"] == 1
    assert out["items"][0]["stages"]["raw"] is True
    assert out["items"][0]["stages"]["scored"] is False


def test_get_run_meta_raises_hz_run_not_found(service: HorizonPipelineService) -> None:
    with pytest.raises(HorizonMcpError) as einfo:
        service.get_run_meta("not-exist")
    assert einfo.value.code == "HZ_RUN_NOT_FOUND"


def test_get_run_stage_invalid_max_items_raises_hz_invalid_input(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("r1")
    service.run_store.save_items(rid, "raw", [{"id": "1"}])
    with pytest.raises(HorizonMcpError, match="HZ_INVALID_INPUT"):
        service.get_run_stage(rid, "raw", max_items=0)


def test_get_run_stage_invalid_stage_raises_hz_invalid_stage(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("r1")
    service.run_store.save_items(rid, "raw", [{"id": "1"}])
    with pytest.raises(HorizonMcpError, match="HZ_INVALID_STAGE"):
        service.get_run_stage(rid, "raw-scored")


def test_get_run_stage_missing_raises_hz_stage_not_found(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("r1")
    with pytest.raises(HorizonMcpError, match="HZ_STAGE_NOT_FOUND"):
        service.get_run_stage(rid, "raw")


def test_get_run_stage_truncates_when_more_than_max(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("r1")
    items = [{"id": str(i)} for i in range(20)]
    service.run_store.save_items(rid, "raw", items)

    out = service.get_run_stage(rid, "raw", max_items=5)
    assert out["count"] == 20
    assert out["truncated"] is True
    assert len(out["items"]) == 5


def test_get_run_stage_returns_full_when_within_max(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("r1")
    items = [{"id": str(i)} for i in range(3)]
    service.run_store.save_items(rid, "raw", items)

    out = service.get_run_stage(rid, "raw", max_items=10)
    assert out["count"] == 3
    assert out["truncated"] is False
    assert len(out["items"]) == 3


def test_get_run_summary_raises_hz_summary_not_found(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("r1")
    with pytest.raises(HorizonMcpError, match="HZ_SUMMARY_NOT_FOUND"):
        service.get_run_summary(rid, "zh")


def test_get_run_summary_returns_markdown(service: HorizonPipelineService) -> None:
    rid = service.run_store.create_run("r1")
    service.run_store.save_summary(rid, "zh", "# \u4e2d\u6587\u65e5\u62a5")
    out = service.get_run_summary(rid, "zh")
    assert out["summary"] == "# \u4e2d\u6587\u65e5\u62a5"


# ---------------------------------------------------------------------------
# _score_distribution (pure math)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([], {"0-2": 0, "3-4": 0, "5-6": 0, "7-8": 0, "9-10": 0}),
        ([1.0], {"0-2": 1, "3-4": 0, "5-6": 0, "7-8": 0, "9-10": 0}),
        ([3.5], {"0-2": 0, "3-4": 1, "5-6": 0, "7-8": 0, "9-10": 0}),
        ([5.0], {"0-2": 0, "3-4": 0, "5-6": 1, "7-8": 0, "9-10": 0}),
        ([7.5], {"0-2": 0, "3-4": 0, "5-6": 0, "7-8": 1, "9-10": 0}),
        ([9.5], {"0-2": 0, "3-4": 0, "5-6": 0, "7-8": 0, "9-10": 1}),
        (
            [0, 3, 4.99, 5, 6.99, 7, 8.99, 9, 10],
            {"0-2": 1, "3-4": 2, "5-6": 2, "7-8": 2, "9-10": 2},
        ),
    ],
)
def test_score_distribution_buckets(
    scores: list[float], expected: dict[str, int]
) -> None:
    items = [MagicMock(ai_score=s if s else None) for s in scores]
    out = HorizonPipelineService._score_distribution(items)
    assert out == expected


# ---------------------------------------------------------------------------
# _pick_summary_stage
# ---------------------------------------------------------------------------


def test_pick_summary_stage_prefers_enriched(service: HorizonPipelineService) -> None:
    rid = service.run_store.create_run("r1")
    for stage in ("raw", "scored", "filtered", "enriched"):
        service.run_store.save_items(rid, stage, [])
    assert service._pick_summary_stage(rid) == "enriched"


def test_pick_summary_stage_falls_back_to_scored(service: HorizonPipelineService) -> None:
    rid = service.run_store.create_run("r1")
    service.run_store.save_items(rid, "scored", [])
    assert service._pick_summary_stage(rid) == "scored"


def test_pick_summary_stage_falls_back_to_raw(service: HorizonPipelineService) -> None:
    rid = service.run_store.create_run("r2")
    service.run_store.save_items(rid, "raw", [])
    assert service._pick_summary_stage(rid) == "raw"


def test_pick_summary_stage_no_stages_raises_hz_stage_not_found(
    service: HorizonPipelineService,
) -> None:
    rid = service.run_store.create_run("empty")
    with pytest.raises(HorizonMcpError, match="HZ_STAGE_NOT_FOUND"):
        service._pick_summary_stage(rid)


# ---------------------------------------------------------------------------
# _total_fetched
# ---------------------------------------------------------------------------


def test_total_fetched_uses_raw_when_present(service: HorizonPipelineService) -> None:
    rid = service.run_store.create_run("r1")
    service.run_store.save_items(rid, "raw", [{"x": 1}, {"x": 2}, {"x": 3}])
    assert service._total_fetched(rid, fallback=99) == 3


def test_total_fetched_falls_back_when_missing(service: HorizonPipelineService) -> None:
    rid = service.run_store.create_run("r1")
    assert service._total_fetched(rid, fallback=42) == 42


# ---------------------------------------------------------------------------
# send_webhook — short-circuit + happy path
# ---------------------------------------------------------------------------


def _stub_context(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    """Replace ``_build_context`` to return a deterministic (context, [], [])."""

    fake_ctx = MagicMock()
    fake_ctx.config = cfg
    monkeypatch.setattr(
        HorizonPipelineService,
        "_build_context",
        lambda self, **_kw: (fake_ctx, [], []),
    )


def test_send_webhook_short_circuit_when_webhook_none(
    service: HorizonPipelineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = MagicMock()
    cfg.webhook = None
    _stub_context(monkeypatch, cfg)
    out = asyncio.run(
        service.send_webhook(
            date="2026-01-15", language="zh",
            important_items=2, all_items=10,
            result="success", summary="hello",
        )
    )
    assert out["sent"] is False
    assert "not enabled" in out["reason"]


def test_send_webhook_short_circuit_when_disabled(
    service: HorizonPipelineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = MagicMock()
    cfg.webhook = MagicMock(enabled=False)
    _stub_context(monkeypatch, cfg)
    out = asyncio.run(service.send_webhook(date="2026-01-15", summary="hello"))
    assert out["sent"] is False


def test_send_webhook_happy_calls_notifier(
    service: HorizonPipelineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = MagicMock()
    cfg.webhook = MagicMock(enabled=True)
    _stub_context(monkeypatch, cfg)

    fake_notifier = MagicMock()
    fake_notifier.notify = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "src.mcp.service.WebhookNotifier", lambda _cfg: fake_notifier,
    )

    out = asyncio.run(
        service.send_webhook(
            date="2026-01-15", language="en",
            important_items=3, all_items=12,
            result="success", summary="hello-world",
        )
    )
    assert out["sent"] is True
    fake_notifier.notify.assert_awaited_once()
    vars_sent = fake_notifier.notify.await_args.args[0]
    assert vars_sent["date"] == "2026-01-15"
    assert vars_sent["language"] == "en"
    assert vars_sent["important_items"] == 3
    assert vars_sent["all_items"] == 12
    assert vars_sent["result"] == "success"
    assert vars_sent["summary"] == "hello-world"
    assert "timestamp" in vars_sent


def test_send_webhook_redacts_summary_in_response_variables(
    service: HorizonPipelineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = MagicMock()
    cfg.webhook = MagicMock(enabled=True)
    _stub_context(monkeypatch, cfg)

    fake_notifier = MagicMock()
    fake_notifier.notify = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "src.mcp.service.WebhookNotifier", lambda _cfg: fake_notifier,
    )

    out = asyncio.run(service.send_webhook(summary="abcdefghij", date="2026-01-15"))
    # Variables payload redacts ``summary`` to "<N chars>".
    rendered = str(out["variables"])
    assert "<10 chars>" in rendered


# ---------------------------------------------------------------------------
# run_store property lazy init
# ---------------------------------------------------------------------------


def test_run_store_property_lazy_creates_store(tmp_path: Path) -> None:
    svc = HorizonPipelineService(runs_root=tmp_path / "lazy")
    rs1 = svc.run_store
    assert isinstance(rs1, RunStore)


def test_default_runs_root_returns_path_under_repo() -> None:
    p = _default_runs_root()
    assert p.name == "mcp-runs"
    assert p.parent.name == "data"
