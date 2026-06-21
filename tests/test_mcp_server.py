"""Layer-1 unit tests for ``src.mcp.server`` internal helpers.

These tests cover the pure helper functions
(``_ok``, ``_err``, ``_record_metrics``, ``_metrics_snapshot``,
``_resource_result``) without booting the FastMCP transport. The
module-level ``METRICS`` dict is reset before each assertion so
tests are order-independent.

Style follows AGENTS.md §行为准则 (_类型安全_): every public test
signature is annotated with ``-> None`` and uses PEP 484 type hints.
Fixture names are prefixed ``hz_mcp_server_`` following the mcp-001
brief to avoid future conftest collision.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from src.mcp import server
from src.mcp.errors import HorizonMcpError


@pytest.fixture(name="hz_mcp_server_reset_metrics")
def _hz_mcp_server_reset_metrics_fixture() -> Generator[None, None, None]:
    """Snapshot + restore server.METRICS around each test."""

    snapshot: dict = dict(server.METRICS)
    try:
        server.METRICS["tool_calls_total"] = 0
        server.METRICS["tool_calls_success"] = 0
        server.METRICS["tool_calls_failed"] = 0
        server.METRICS["tool_calls_by_name"] = {}
        server.METRICS["tool_errors_by_code"] = {}
        server.METRICS["tool_last_duration_ms"] = {}
        server.METRICS["last_error"] = None
        yield
    finally:
        for key, value in snapshot.items():
            server.METRICS[key] = value


def test_hz_mcp_server_ok_includes_rounded_duration() -> None:
    payload = server._ok("demo", {"x": 1}, duration_ms=1.234)
    assert payload["ok"] is True
    assert payload["tool"] == "demo"
    assert payload["data"] == {"x": 1}
    assert payload["meta"]["duration_ms"] == 1.23


def test_hz_mcp_server_ok_omits_duration_when_absent() -> None:
    payload = server._ok("demo", {"x": 1})
    assert "duration_ms" not in payload["meta"]


def test_hz_mcp_server_err_uses_typed_error_payload() -> None:
    exc = HorizonMcpError(code="HZ_CUSTOM", message="boom", details={"k": 1})
    payload = server._err("tool", exc)
    assert payload["ok"] is False
    assert payload["tool"] == "tool"
    assert payload["error"]["code"] == "HZ_CUSTOM"
    assert payload["error"]["message"] == "boom"
    assert payload["error"]["details"] == {"k": 1}


def test_hz_mcp_server_err_untyped_maps_to_internal() -> None:
    payload = server._err("tool", ValueError("nope"))
    assert payload["error"]["code"] == "HZ_INTERNAL_ERROR"
    assert payload["error"]["message"] == "nope"
    assert payload["error"]["details"] is None


def test_hz_mcp_server_metric_recording_counts_branches(
    hz_mcp_server_reset_metrics: None,
) -> None:
    server._record_metrics("a", ok=True, duration_ms=10.123)
    server._record_metrics("b", ok=False, duration_ms=20.5, error_code="HZ_X")
    server._record_metrics("b", ok=False, duration_ms=30.25, error_code="HZ_X")

    assert server.METRICS["tool_calls_total"] == 3
    assert server.METRICS["tool_calls_success"] == 1
    assert server.METRICS["tool_calls_failed"] == 2
    assert server.METRICS["tool_calls_by_name"] == {"a": 1, "b": 2}
    assert server.METRICS["tool_errors_by_code"] == {"HZ_X": 2}
    assert server.METRICS["tool_last_duration_ms"] == {"a": 10.12, "b": 30.25}
    last_error = server.METRICS["last_error"]
    assert last_error is not None
    assert last_error["tool"] == "b"
    assert last_error["code"] == "HZ_X"


def test_hz_mcp_server_metric_recording_keeps_last_error_none_when_all_ok(
    hz_mcp_server_reset_metrics: None,
) -> None:
    server._record_metrics("a", ok=True, duration_ms=5.0)
    assert server.METRICS["last_error"] is None
    assert server.METRICS["tool_errors_by_code"] == {}


def test_hz_mcp_server_metrics_snapshot_reports_uptime_seconds() -> None:
    snap = server._metrics_snapshot()
    assert "uptime_seconds" in snap
    assert isinstance(snap["uptime_seconds"], float)
    assert snap["uptime_seconds"] >= 0.0
    assert snap["tool_calls_total"] == 0


def test_hz_mcp_server_resource_result_returns_data_on_success() -> None:
    out = server._resource_result("horizon://x", lambda: {"answer": 42})
    assert out == {"ok": True, "resource": "horizon://x", "data": {"answer": 42}}


def test_hz_mcp_server_resource_result_swallows_exception() -> None:
    def _boom() -> None:
        raise RuntimeError("kaboom")

    out = server._resource_result("horizon://x", _boom)
    assert out["ok"] is False
    # _err preserves the resource name under ``tool`` so the MCP surface
    # can still surface which URI failed.
    assert out["tool"] == "horizon://x"
    assert out["error"]["code"] == "HZ_INTERNAL_ERROR"
    assert out["error"]["message"] == "kaboom"


def test_hz_mcp_server_resource_result_swallows_typed_error() -> None:
    def _typed_exc() -> None:
        raise HorizonMcpError(code="HZ_LOOKUP_FAILED", message="missing")

    out = server._resource_result("horizon://x", _typed_exc)
    assert out["ok"] is False
    assert out["error"]["code"] == "HZ_LOOKUP_FAILED"


def test_hz_mcp_server_run_tool_success(
    hz_mcp_server_reset_metrics: None,
) -> None:
    import asyncio

    async def _ok_runner() -> dict:
        return {"result": 42}

    payload = asyncio.run(server._run_tool("test_tool", _ok_runner))
    assert payload["ok"] is True
    assert payload["tool"] == "test_tool"
    assert payload["data"] == {"result": 42}
    assert "duration_ms" in payload["meta"]
    assert server.METRICS["tool_calls_total"] == 1
    assert server.METRICS["tool_calls_success"] == 1


def test_hz_mcp_server_run_tool_failure(
    hz_mcp_server_reset_metrics: None,
) -> None:
    import asyncio

    async def _fail_runner() -> dict:
        raise RuntimeError("boom")

    payload = asyncio.run(server._run_tool("test_tool", _fail_runner))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "HZ_INTERNAL_ERROR"
    assert payload["error"]["message"] == "boom"
    assert server.METRICS["tool_calls_total"] == 1
    assert server.METRICS["tool_calls_failed"] == 1


def test_hz_mcp_server_run_tool_typed_error(
    hz_mcp_server_reset_metrics: None,
) -> None:
    import asyncio

    async def _typed_runner() -> dict:
        raise HorizonMcpError(code="HZ_CUSTOM", message="typed fail")

    payload = asyncio.run(server._run_tool("test_tool", _typed_runner))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "HZ_CUSTOM"
    assert server.METRICS["tool_errors_by_code"]["HZ_CUSTOM"] == 1


def test_hz_mcp_server_err_includes_duration() -> None:
    payload = server._err("tool", ValueError("x"), duration_ms=5.5)
    assert payload["meta"]["duration_ms"] == 5.5


def test_hz_mcp_server_record_metrics_no_error_code() -> None:
    server._record_metrics("c", ok=False, duration_ms=1.0)
    assert server.METRICS["tool_calls_failed"] == 1
    assert server.METRICS["tool_errors_by_code"] == {}
    assert server.METRICS["last_error"] is None
