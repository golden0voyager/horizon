"""Phase 5 unit tests for ``src.mcp.errors.HorizonMcpError``.

Covers structured-exception formatting (``__str__`` returns ``code: message``),
the ``code`` / ``message`` / ``details`` attributes, and Exception subclass
behavior (raise/catch).
"""

from __future__ import annotations

import pytest

from src.mcp.errors import HorizonMcpError


def test_horizon_mcp_error_is_exception_subclass() -> None:
    assert issubclass(HorizonMcpError, Exception)


def test_horizon_mcp_error_required_attributes_only() -> None:
    err = HorizonMcpError(code="HZ_X", message="oops")
    assert err.code == "HZ_X"
    assert err.message == "oops"
    assert err.details is None


def test_horizon_mcp_error_with_structured_details() -> None:
    details = {"stage": "scored", "reason": "empty"}
    err = HorizonMcpError(code="HZ_X", message="oops", details=details)
    assert err.details == details


def test_horizon_mcp_error_str() -> None:
    err = HorizonMcpError(code="HZ_BAD", message="something bad")
    assert str(err) == "HZ_BAD: something bad"


def test_horizon_mcp_error_can_be_raised_and_caught() -> None:
    with pytest.raises(HorizonMcpError) as einfo:
        raise HorizonMcpError(code="HZ_X", message="boom")
    assert einfo.value.code == "HZ_X"
    assert einfo.value.message == "boom"


def test_horizon_mcp_error_details_can_be_arbitrary_type() -> None:
    err = HorizonMcpError(code="HZ_X", message="oops", details=[1, 2, 3])
    err2 = HorizonMcpError(code="HZ_Y", message="oops", details="string detail")
    err3 = HorizonMcpError(code="HZ_Z", message="oops", details={"k": "v"})
    assert err.details == [1, 2, 3]
    assert err2.details == "string detail"
    assert err3.details == {"k": "v"}


def test_horizon_mcp_error_str_ignores_details() -> None:
    err = HorizonMcpError(code="C", message="M", details={"nested": [1, 2]})
    assert str(err) == "C: M"
