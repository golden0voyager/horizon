"""Layer-1 unit tests for ``src.mcp.service.HorizonPipelineService``.

Covers request/response shape of the service surface area that does
NOT require a fully-loaded Horizon runtime:

* ``list_runs`` aggregates per-run stage availability from the run store.
* ``get_run_meta`` / ``get_run_stage`` / ``get_run_summary`` translate
  missing-artifact ``FileNotFoundError`` into typed ``HorizonMcpError``s.
* ``get_run_stage`` rejects ``max_items <= 0`` as ``HZ_INVALID_INPUT``.
* ``get_effective_config`` filters sources and reports unknowns.
* ``_score_distribution`` bins scores across the 0-10 range.
* ``_pick_summary_stage`` walks stages from ``enriched`` down to ``raw``.

Pipeline-execution methods (``fetch_items``, ``score_items``, ...) and
``validate_config`` (which needs live env) are intentionally skipped
here — they belong to integration tests that bring up a real workspace.

Style follows AGENTS.md §行为准则 (_类型安全_): every public test is
annotated ``-> None`` and uses PEP 484 type hints. Fixture names are
prefixed ``hz_mcp_service_`` per the mcp-001 brief to keep the
namespace isolated from any future conftest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mcp.errors import HorizonMcpError
from src.mcp.service import HorizonPipelineService


@pytest.fixture(name="hz_mcp_service_root")
def hz_mcp_service_root_fixture(tmp_path: Path) -> HorizonPipelineService:
    """Return a service bound to a throwaway runs root, no real Horizon."""
    return HorizonPipelineService(runs_root=tmp_path / "mcp-runs")


def test_hz_mcp_service_list_runs_reports_stage_availability(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-list")
    hz_mcp_service_root.run_store.save_items(run_id, "raw", [{"id": "x"}])

    listing = hz_mcp_service_root.list_runs(limit=20)

    assert listing["count"] == 1
    entry = listing["items"][0]
    assert entry["run_id"] == "run-list"
    assert entry["stages"]["raw"] is True
    assert entry["stages"]["scored"] is False


def test_hz_mcp_service_list_runs_respects_limit(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    for idx in range(3):
        hz_mcp_service_root.run_store.create_run(f"run-list-{idx}")
    listing = hz_mcp_service_root.list_runs(limit=2)
    assert listing["count"] == 2


def test_hz_mcp_service_get_run_meta_returns_meta_dict(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    hz_mcp_service_root.run_store.create_run("run-meta")
    payload = hz_mcp_service_root.run_store.load_meta("run-meta")
    hz_mcp_service_root.run_store.update_meta("run-meta", {"extra": 42})

    out = hz_mcp_service_root.get_run_meta("run-meta")
    assert out["run_id"] == "run-meta"
    assert out["meta"]["extra"] == 42
    assert payload["run_id"] == "run-meta"


def test_hz_mcp_service_get_run_meta_missing_maps_to_error(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    with pytest.raises(HorizonMcpError) as exc_info:
        hz_mcp_service_root.get_run_meta("never-created")
    assert exc_info.value.code == "HZ_RUN_NOT_FOUND"
    assert exc_info.value.details == {"run_id": "never-created"}


def test_hz_mcp_service_get_run_stage_truncates(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-stage")
    items = [{"id": f"item-{idx}", "value": idx} for idx in range(5)]
    hz_mcp_service_root.run_store.save_items(run_id, "raw", items)

    out = hz_mcp_service_root.get_run_stage(run_id="run-stage", stage="raw", max_items=2)
    assert out["count"] == 5
    assert out["truncated"] is True
    assert len(out["items"]) == 2
    assert out["items"][0]["id"] == "item-0"


def test_hz_mcp_service_get_run_stage_rejects_zero_max_items(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    with pytest.raises(HorizonMcpError) as exc_info:
        hz_mcp_service_root.get_run_stage(
            run_id="anything",
            stage="raw",
            max_items=0,
        )
    assert exc_info.value.code == "HZ_INVALID_INPUT"


def test_hz_mcp_service_get_run_stage_missing_maps_to_error(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    hz_mcp_service_root.run_store.create_run("run-stage-missing")
    with pytest.raises(HorizonMcpError) as exc_info:
        hz_mcp_service_root.get_run_stage(run_id="run-stage-missing", stage="raw")
    assert exc_info.value.code == "HZ_STAGE_NOT_FOUND"


def test_hz_mcp_service_get_run_stage_unknown_stage_maps_to_invalid(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    hz_mcp_service_root.run_store.create_run("run-stage-bad-stage")
    with pytest.raises(HorizonMcpError) as exc_info:
        hz_mcp_service_root.get_run_stage(run_id="run-stage-bad-stage", stage="does-not-exist")
    assert exc_info.value.code == "HZ_INVALID_STAGE"


def test_hz_mcp_service_get_run_summary_returns_markdown(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-summary")
    hz_mcp_service_root.run_store.save_summary(run_id, "zh", "# title\nbody")

    out = hz_mcp_service_root.get_run_summary("run-summary", "zh")
    assert out["run_id"] == "run-summary"
    assert out["language"] == "zh"
    assert out["summary"].startswith("# title")


def test_hz_mcp_service_get_run_summary_missing_maps_to_error(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    hz_mcp_service_root.run_store.create_run("run-summary-missing")
    with pytest.raises(HorizonMcpError) as exc_info:
        hz_mcp_service_root.get_run_summary("run-summary-missing", "zh")
    assert exc_info.value.code == "HZ_SUMMARY_NOT_FOUND"


def test_hz_mcp_service_score_distribution_bins_each_bucket() -> None:
    items = [
        SimpleNamespace(ai_score=1.5),
        SimpleNamespace(ai_score=2.99),
        SimpleNamespace(ai_score=3.0),
        SimpleNamespace(ai_score=4.5),
        SimpleNamespace(ai_score=5.0),
        SimpleNamespace(ai_score=6.9),
        SimpleNamespace(ai_score=7.0),
        SimpleNamespace(ai_score=8.9),
        SimpleNamespace(ai_score=9.0),
        SimpleNamespace(ai_score=10.0),
        SimpleNamespace(ai_score=None),
        SimpleNamespace(ai_score=0.0),
    ]
    buckets = HorizonPipelineService._score_distribution(items)
    # Note: ``None`` ai_score becomes 0.0 via ``item.ai_score or 0.0``, joining
    # the 0-2 bucket together with the explicit 0.0/1.5/2.99 entries.
    assert buckets == {
        "0-2": 4,
        "3-4": 2,
        "5-6": 2,
        "7-8": 2,
        "9-10": 2,
    }


def test_hz_mcp_service_score_distribution_handles_empty() -> None:
    assert HorizonPipelineService._score_distribution([]) == {
        "0-2": 0,
        "3-4": 0,
        "5-6": 0,
        "7-8": 0,
        "9-10": 0,
    }


def test_hz_mcp_service_pick_summary_stage_prefers_enriched(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-stage-order")
    hz_mcp_service_root.run_store.save_items(run_id, "raw", [])
    hz_mcp_service_root.run_store.save_items(run_id, "scored", [])
    hz_mcp_service_root.run_store.save_items(run_id, "filtered", [])
    hz_mcp_service_root.run_store.save_items(run_id, "enriched", [])

    assert hz_mcp_service_root._pick_summary_stage("run-stage-order") == "enriched"


def test_hz_mcp_service_pick_summary_stage_walks_down_when_enriched_missing(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-stage-only-raw")
    hz_mcp_service_root.run_store.save_items(run_id, "raw", [])
    assert hz_mcp_service_root._pick_summary_stage("run-stage-only-raw") == "raw"


def test_hz_mcp_service_pick_summary_stage_raises_when_no_stages(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    hz_mcp_service_root.run_store.create_run("run-stage-empty")
    with pytest.raises(HorizonMcpError) as exc_info:
        hz_mcp_service_root._pick_summary_stage("run-stage-empty")
    assert exc_info.value.code == "HZ_STAGE_NOT_FOUND"


@dataclass(frozen=True)
class _FakeConfig:
    sources: object

    def model_dump(self, *, mode: str = "python") -> dict:
        # ``src/mcp/service.get_effective_config`` calls
        # ``ctx.config.model_dump(mode="json")``; we stub it to an empty
        # payload since the assertions under test only inspect the
        # wrapping keys (selected_sources, unknown_sources, horizon_path).
        return {}


def test_hz_mcp_service_get_effective_config_reports_unknown_sources(
    hz_mcp_service_root: HorizonPipelineService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    @dataclass
    class _ContextBundle:
        horizon_path: Path
        config_path: Path
        runtime: object
        config: object

    sentinel_context = _ContextBundle(
        horizon_path=tmp_path,
        config_path=tmp_path / "config.json",
        runtime=SimpleNamespace(),
        config=_FakeConfig(sources=SimpleNamespace()),
    )

    monkeypatch.setattr(
        hz_mcp_service_root,
        "_build_context",
        lambda horizon_path, config_path, sources: (
            sentinel_context,
            ["github"],
            ["facebook", "mastodon"],
        ),
    )

    out = hz_mcp_service_root.get_effective_config(sources=["github"])
    assert out["selected_sources"] == ["github"]
    assert out["unknown_sources"] == ["facebook", "mastodon"]
    assert out["horizon_path"] == str(tmp_path)


def test_hz_mcp_service_total_fetched_returns_raw_count(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-total")
    hz_mcp_service_root.run_store.save_items(run_id, "raw", [{"id": 1}, {"id": 2}, {"id": 3}])

    count = hz_mcp_service_root._total_fetched(run_id, fallback=0)
    assert count == 3


def test_hz_mcp_service_total_fetched_returns_fallback_on_error(
    hz_mcp_service_root: HorizonPipelineService,
) -> None:
    run_id = hz_mcp_service_root.run_store.create_run("run-no-raw")

    count = hz_mcp_service_root._total_fetched(run_id, fallback=99)
    assert count == 99


def test_hz_mcp_service_send_webhook_disabled(
    hz_mcp_service_root: HorizonPipelineService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    @dataclass
    class _Ctx:
        horizon_path: Path
        config_path: Path
        runtime: object
        config: object

    webhook_cfg = SimpleNamespace(enabled=False)
    cfg = SimpleNamespace(webhook=webhook_cfg)

    monkeypatch.setattr(
        hz_mcp_service_root,
        "_build_context",
        lambda horizon_path, config_path, sources: (
            _Ctx(horizon_path=tmp_path, config_path=tmp_path / "c.json", runtime=None, config=cfg),
            [],
            [],
        ),
    )

    import asyncio
    result = asyncio.run(hz_mcp_service_root.send_webhook(date="2026-01-01"))
    assert result["sent"] is False
    assert "not enabled" in result["reason"].lower()


def test_hz_mcp_service_send_webhook_no_config(
    hz_mcp_service_root: HorizonPipelineService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    @dataclass
    class _Ctx:
        horizon_path: Path
        config_path: Path
        runtime: object
        config: object

    cfg = SimpleNamespace(webhook=None)

    monkeypatch.setattr(
        hz_mcp_service_root,
        "_build_context",
        lambda horizon_path, config_path, sources: (
            _Ctx(horizon_path=tmp_path, config_path=tmp_path / "c.json", runtime=None, config=cfg),
            [],
            [],
        ),
    )

    import asyncio
    result = asyncio.run(hz_mcp_service_root.send_webhook(date="2026-01-01"))
    assert result["sent"] is False


def test_hz_mcp_service_send_webhook_sends(
    hz_mcp_service_root: HorizonPipelineService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    @dataclass
    class _Ctx:
        horizon_path: Path
        config_path: Path
        runtime: object
        config: object

    webhook_cfg = SimpleNamespace(enabled=True, url_env="WEBHOOK_URL")
    cfg = SimpleNamespace(webhook=webhook_cfg)

    monkeypatch.setattr(
        hz_mcp_service_root,
        "_build_context",
        lambda horizon_path, config_path, sources: (
            _Ctx(horizon_path=tmp_path, config_path=tmp_path / "c.json", runtime=None, config=cfg),
            [],
            [],
        ),
    )

    sent_vars = []

    class FakeNotifier:
        def __init__(self, config):
            pass

        async def notify(self, variables):
            sent_vars.append(variables)

    monkeypatch.setattr("src.mcp.service.WebhookNotifier", FakeNotifier)

    import asyncio
    result = asyncio.run(
        hz_mcp_service_root.send_webhook(
            date="2026-06-21",
            language="zh",
            important_items=5,
            all_items=20,
            result="success",
            summary="test summary",
        )
    )
    assert result["sent"] is True
    assert result["variables"]["date"] == "2026-06-21"
    assert result["variables"]["important_items"] == 5
    assert result["variables"]["summary"] == "<12 chars>"
    assert len(sent_vars) == 1
