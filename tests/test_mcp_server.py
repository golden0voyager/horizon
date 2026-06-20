"""Phase 5 unit tests for ``src.mcp.server``.

Strategy: monkeypatch ``HorizonPipelineService`` to a MagicMock with AsyncMock
methods, then verify each tool wrapper threads kwargs correctly and every
`hz_*` resource returns the right ``horizon://`` URL verbatim.

Covers:
- ``_ok`` / ``_err`` payload structure (incl. duration rounding, HorrizonMcpError vs generic)
- ``_record_metrics`` happy + error paths
- ``_metrics_snapshot`` uptime math
- ``_run_tool`` happy + raises + records metrics
- ``_resource_result`` happy + raises (preserves resource field on error)
- All tool wrappers (hz_validate_config / hz_fetch_items / hz_score_items /
  hz_filter_items / hz_enrich_items / hz_generate_summary / hz_run_pipeline /
  hz_list_runs / hz_get_run_meta / hz_get_run_stage / hz_get_run_summary /
  hz_get_metrics / hz_send_webhook) thread kwargs correctly
- Resource handlers (r_server_info, r_metrics, r_runs, r_run_meta,
  r_run_items, r_run_summary, r_effective_config) — resource field name
  matches the URL
- ``main()`` calls ``mcp.run()``
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.mcp.server as server

# ---------------------------------------------------------------------------
# _ok / _err payload builders
# ---------------------------------------------------------------------------


def test_ok_payload_includes_tool_data_and_meta() -> None:
    payload = server._ok("hz_test", {"value": 42})
    assert payload["ok"] is True
    assert payload["tool"] == "hz_test"
    assert payload["data"] == {"value": 42}
    assert "meta" in payload
    assert "timestamp" in payload["meta"]
    assert "duration_ms" not in payload["meta"]


def test_ok_payload_includes_duration_when_provided() -> None:
    payload = server._ok("hz_test", {"x": 1}, duration_ms=12.34)
    assert payload["meta"]["duration_ms"] == 12.34


def test_ok_payload_rounds_duration_to_two_decimals() -> None:
    payload = server._ok("h", {}, duration_ms=12.3456789)
    assert payload["meta"]["duration_ms"] == 12.35


def test_err_payload_uses_horizon_mcp_error_code() -> None:
    from src.mcp.errors import HorizonMcpError

    exc = HorizonMcpError(code="HZ_X", message="bad", details={"k": 1})
    payload = server._err("hz_test", exc)
    assert payload["ok"] is False
    assert payload["tool"] == "hz_test"
    assert payload["error"]["code"] == "HZ_X"
    assert payload["error"]["message"] == "bad"
    assert payload["error"]["details"] == {"k": 1}


def test_err_payload_fallback_for_generic_exception() -> None:
    payload = server._err("h", RuntimeError("oops"))
    assert payload["error"]["code"] == "HZ_INTERNAL_ERROR"
    assert payload["error"]["message"] == "oops"
    assert payload["error"]["details"] is None


def test_err_payload_rounds_duration() -> None:
    payload = server._err("h", RuntimeError("x"), duration_ms=99.9999)
    assert payload["meta"]["duration_ms"] == 100.0


# ---------------------------------------------------------------------------
# _record_metrics
# ---------------------------------------------------------------------------


def _build_metrics_dict() -> dict[str, Any]:
    return {
        "started_at": datetime.now(UTC).isoformat(),
        "tool_calls_total": 0,
        "tool_calls_success": 0,
        "tool_calls_failed": 0,
        "tool_calls_by_name": {},
        "tool_errors_by_code": {},
        "tool_last_duration_ms": {},
        "last_error": None,
    }


def test_record_metrics_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "METRICS", _build_metrics_dict())
    server._record_metrics("hz_test", ok=True, duration_ms=42.0)

    m = server.METRICS
    assert m["tool_calls_total"] == 1
    assert m["tool_calls_success"] == 1
    assert m["tool_calls_failed"] == 0
    assert m["tool_calls_by_name"]["hz_test"] == 1
    assert m["tool_last_duration_ms"]["hz_test"] == 42.0
    assert m["last_error"] is None


def test_record_metrics_failure_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "METRICS", _build_metrics_dict())
    server._record_metrics("hz_test", ok=False, duration_ms=33.0, error_code="HZ_FAIL")

    m = server.METRICS
    assert m["tool_calls_failed"] == 1
    assert m["tool_errors_by_code"]["HZ_FAIL"] == 1
    assert m["last_error"]["code"] == "HZ_FAIL"
    assert m["last_error"]["tool"] == "hz_test"


def test_metrics_snapshot_includes_uptime(monkeypatch: pytest.MonkeyPatch) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    snap_input = _build_metrics_dict()
    snap_input["started_at"] = started
    monkeypatch.setattr(server, "METRICS", snap_input)

    snap = server._metrics_snapshot()
    assert "uptime_seconds" in snap
    assert snap["uptime_seconds"] >= 0
    assert snap["started_at"] == started


# ---------------------------------------------------------------------------
# _run_tool
# ---------------------------------------------------------------------------


def test_run_tool_calls_runner_and_returns_ok_when_clean() -> None:
    async def runner() -> dict[str, Any]:
        return {"items": [1, 2]}

    out = asyncio.run(server._run_tool("hz_test", runner))
    assert out["ok"] is True
    assert out["data"] == {"items": [1, 2]}


def test_run_tool_returns_err_when_runner_raises() -> None:
    from src.mcp.errors import HorizonMcpError

    async def runner() -> None:
        raise HorizonMcpError(code="HZ_EMPTY", message="no items")

    out = asyncio.run(server._run_tool("hz_test", runner))
    assert out["ok"] is False
    assert out["error"]["code"] == "HZ_EMPTY"


def test_run_tool_records_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "METRICS", _build_metrics_dict())

    async def runner() -> dict[str, Any]:
        return {}

    asyncio.run(server._run_tool("hz_t", runner))
    assert server.METRICS["tool_calls_success"] == 1
    assert server.METRICS["tool_calls_by_name"]["hz_t"] == 1


# ---------------------------------------------------------------------------
# _resource_result
# ---------------------------------------------------------------------------


def test_resource_result_happy() -> None:
    out = server._resource_result("horizon://x", lambda: {"k": 1})
    assert out["ok"] is True
    assert out["resource"] == "horizon://x"
    assert out["data"] == {"k": 1}


def test_resource_result_passes_through_err() -> None:
    from src.mcp.errors import HorizonMcpError

    def loader() -> None:
        raise HorizonMcpError(code="HZ_X", message="bad")

    out = server._resource_result("horizon://x", loader)
    assert out["ok"] is False
    assert out["error"]["code"] == "HZ_X"
    # When error fires, _err is reached; _err uses "tool" key (not "resource").
    assert out["ok"] is False
    assert "tool" in out
    assert out["tool"] == "horizon://x"


# ---------------------------------------------------------------------------
# hz_* tools — threaded kwargs
# ---------------------------------------------------------------------------


def _make_service_mock(method_names: list[str]) -> MagicMock:
    fake = MagicMock()
    for name in method_names:
        setattr(fake, name, AsyncMock(return_value={f"{name}": True}))
    return fake


def test_hz_validate_config_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["validate_config"])
    monkeypatch.setattr(server, "service", fake)
    out = asyncio.run(
        server.hz_validate_config(
            horizon_path="/x", config_path="/x/cfg.json",
            sources=["github"], check_env=False,
        )
    )
    fake.validate_config.assert_awaited_once_with(
        horizon_path="/x", config_path="/x/cfg.json",
        sources=["github"], check_env=False,
    )
    assert out["ok"] is True


def test_hz_fetch_items_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["fetch_items"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_fetch_items(
            hours=12, run_id="r1", horizon_path="/h", config_path="/c",
            sources=["github"],
        )
    )
    fake.fetch_items.assert_awaited_once_with(
        hours=12, run_id="r1", horizon_path="/h", config_path="/c",
        sources=["github"],
    )


def test_hz_score_items_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["score_items"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_score_items(
            run_id="r1", source_stage="raw",
            horizon_path="/h", config_path="/c",
        )
    )
    fake.score_items.assert_awaited_once_with(
        run_id="r1", source_stage="raw", horizon_path="/h", config_path="/c",
    )


def test_hz_filter_items_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["filter_items"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_filter_items(
            run_id="r1", threshold=8.0, source_stage="scored",
            topic_dedup=False, horizon_path="/h", config_path="/c",
        )
    )
    fake.filter_items.assert_awaited_once_with(
        run_id="r1", threshold=8.0, source_stage="scored",
        topic_dedup=False, horizon_path="/h", config_path="/c",
    )


def test_hz_enrich_items_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["enrich_items"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_enrich_items(
            run_id="r1", source_stage="filtered",
            horizon_path="/h", config_path="/c",
        )
    )
    fake.enrich_items.assert_awaited_once_with(
        run_id="r1", source_stage="filtered", horizon_path="/h", config_path="/c",
    )


def test_hz_generate_summary_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["generate_summary"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_generate_summary(
            run_id="r1", language="en", source_stage="enriched",
            horizon_path="/h", config_path="/c", save_to_horizon_data=False,
        )
    )
    fake.generate_summary.assert_awaited_once_with(
        run_id="r1", language="en", source_stage="enriched",
        horizon_path="/h", config_path="/c", save_to_horizon_data=False,
    )


def test_hz_run_pipeline_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["run_pipeline"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_run_pipeline(
            hours=6, languages=["zh"], threshold=6.0,
            horizon_path="/h", config_path="/c", sources=["github"],
            enrich=True, topic_dedup=False, save_to_horizon_data=False,
        )
    )
    fake.run_pipeline.assert_awaited_once_with(
        hours=6, languages=["zh"], threshold=6.0,
        horizon_path="/h", config_path="/c", sources=["github"],
        enrich=True, topic_dedup=False, save_to_horizon_data=False,
    )


def test_hz_list_runs_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.list_runs.return_value = {"runs": []}
    monkeypatch.setattr(server, "service", fake)

    out = server.hz_list_runs(limit=5)
    fake.list_runs.assert_called_once_with(limit=5)
    assert out["data"] == {"runs": []}


def test_hz_get_run_meta_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_run_meta.return_value = {"meta": {}}
    monkeypatch.setattr(server, "service", fake)

    out = server.hz_get_run_meta("r1")
    fake.get_run_meta.assert_called_once_with("r1")
    assert out["data"] == {"meta": {}}


def test_hz_get_run_stage_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_run_stage.return_value = {"items": []}
    monkeypatch.setattr(server, "service", fake)

    out = server.hz_get_run_stage("r1", "raw", max_items=10)
    fake.get_run_stage.assert_called_once_with(
        run_id="r1", stage="raw", max_items=10,
    )
    assert out["data"] == {"items": []}


def test_hz_get_run_summary_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_run_summary.return_value = {"summary": "x"}
    monkeypatch.setattr(server, "service", fake)

    out = server.hz_get_run_summary("r1", language="zh")
    fake.get_run_summary.assert_called_once_with(run_id="r1", language="zh")
    assert out["data"] == {"summary": "x"}


def test_hz_get_metrics_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(server, "service", fake)
    monkeypatch.setattr(server, "METRICS", _build_metrics_dict())

    out = server.hz_get_metrics()
    assert out["ok"] is True
    assert out["data"]["tool_calls_total"] >= 0


def test_hz_send_webhook_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_service_mock(["send_webhook"])
    monkeypatch.setattr(server, "service", fake)
    asyncio.run(
        server.hz_send_webhook(
            date="2026-01-15", language="zh",
            important_items=5, all_items=20,
            result="success", summary="hello",
            horizon_path="/h", config_path="/c",
        )
    )
    fake.send_webhook.assert_awaited_once_with(
        date="2026-01-15", language="zh",
        important_items=5, all_items=20,
        result="success", summary="hello",
        horizon_path="/h", config_path="/c",
    )


# ---------------------------------------------------------------------------
# Resource handlers
# ---------------------------------------------------------------------------


def test_r_server_info_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "service", MagicMock())
    out = server.r_server_info()
    assert out["name"] == "horizon-mcp"
    assert "started_at" in out
    assert "runs_root" in out


def test_r_metrics_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "service", MagicMock())
    monkeypatch.setattr(server, "METRICS", _build_metrics_dict())
    out = server.r_metrics()
    assert out["ok"] is True
    assert out["resource"] == "horizon://metrics"
    assert "uptime_seconds" in out["data"]


def test_r_runs_calls_service_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.list_runs.return_value = {"runs": []}
    monkeypatch.setattr(server, "service", fake)

    out = server.r_runs()
    fake.list_runs.assert_called_once_with(limit=30)
    assert out["ok"] is True


def test_r_run_meta_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_run_meta.return_value = {"meta": {}}
    monkeypatch.setattr(server, "service", fake)

    out = server.r_run_meta("r1")
    assert out["resource"] == "horizon://runs/r1/meta"
    fake.get_run_meta.assert_called_once_with("r1")


def test_r_run_items_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_run_stage.return_value = {"items": []}
    monkeypatch.setattr(server, "service", fake)

    out = server.r_run_items("r1", "raw")
    fake.get_run_stage.assert_called_once_with(
        run_id="r1", stage="raw", max_items=200,
    )
    assert out["resource"] == "horizon://runs/r1/items/raw"


def test_r_run_summary_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_run_summary.return_value = {"summary": "ok"}
    monkeypatch.setattr(server, "service", fake)

    out = server.r_run_summary("r1", "zh")
    fake.get_run_summary.assert_called_once_with(run_id="r1", language="zh")
    assert out["resource"] == "horizon://runs/r1/summary/zh"


def test_r_effective_config_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_effective_config.return_value = {"cfg": {}}
    monkeypatch.setattr(server, "service", fake)

    out = server.r_effective_config()
    fake.get_effective_config.assert_called_once_with()
    assert out["resource"] == "horizon://config/effective"


# ---------------------------------------------------------------------------
# main() entrypoint
# ---------------------------------------------------------------------------


def test_main_invokes_mcp_run(monkeypatch: pytest.MonkeyPatch) -> None:
    called = MagicMock()
    monkeypatch.setattr(server.mcp, "run", called)
    server.main()
    called.assert_called_once_with()
