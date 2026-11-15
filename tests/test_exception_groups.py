from __future__ import annotations

from tess_core.runtime.exception_groups import RuntimeExceptionGroup, aggregate_runtime_failures


def test_runtime_exception_group_preserves_all_failures() -> None:
    first = ValueError("first")
    second = RuntimeError("second")

    error = RuntimeExceptionGroup("aggregate", [first, second])

    assert tuple(error.exceptions) == (first, second)


def test_aggregate_keeps_plain_exceptions_in_the_runtime_group() -> None:
    failures = [ValueError("first"), RuntimeError("second")]

    error = aggregate_runtime_failures("aggregate", failures)

    assert isinstance(error, RuntimeExceptionGroup)
    assert tuple(error.exceptions) == tuple(failures)


def test_aggregate_carries_base_exceptions_instead_of_raising_type_error() -> None:
    # A Ctrl+C that lands mid-teardown is collected by close_many; the
    # aggregate must surface it, not crash with "Cannot nest BaseExceptions".
    failures = [KeyboardInterrupt(), RuntimeError("component close failed")]

    error = aggregate_runtime_failures("aggregate", failures)

    assert isinstance(error, BaseException)
    assert tuple(error.exceptions) == tuple(failures)
    assert any(isinstance(item, KeyboardInterrupt) for item in error.exceptions)
