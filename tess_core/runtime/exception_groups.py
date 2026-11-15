"""Runtime-compatible aggregate exceptions."""

from __future__ import annotations

from collections.abc import Sequence

RuntimeExceptionGroup = ExceptionGroup


def aggregate_runtime_failures(message: str, failures: Sequence[BaseException]) -> BaseException:
    """Build the aggregate for a batch of close/activation failures.

    ExceptionGroup refuses BaseExceptions such as KeyboardInterrupt, and a
    teardown interrupted mid-close must surface the interrupt itself — not a
    TypeError thrown by the aggregation.
    """
    if any(not isinstance(failure, Exception) for failure in failures):
        return BaseExceptionGroup(message, list(failures))
    return RuntimeExceptionGroup(message, list(failures))


__all__ = ["RuntimeExceptionGroup", "aggregate_runtime_failures"]
