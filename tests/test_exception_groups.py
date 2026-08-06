from __future__ import annotations

from agent_core.runtime.exception_groups import RuntimeExceptionGroup


def test_runtime_exception_group_preserves_all_failures() -> None:
    first = ValueError("first")
    second = RuntimeError("second")

    error = RuntimeExceptionGroup("aggregate", [first, second])

    assert tuple(error.exceptions) == (first, second)
