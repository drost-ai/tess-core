"""Runtime-compatible aggregate exceptions for Python 3.10 and newer."""

from __future__ import annotations

import sys
from collections.abc import Sequence

if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup as RuntimeExceptionGroup
else:

    class RuntimeExceptionGroup(Exception):
        """Python 3.10 fallback preserving the aggregate failure payload."""

        def __init__(self, message: str, exceptions: Sequence[BaseException]) -> None:
            self.exceptions = tuple(exceptions)
            super().__init__(message)


def aggregateRuntimeFailures(message: str, failures: Sequence[BaseException]) -> BaseException:
    """Build the aggregate for a batch of close/activation failures.

    ExceptionGroup refuses BaseExceptions such as KeyboardInterrupt, and a
    teardown interrupted mid-close must surface the interrupt itself — not a
    TypeError thrown by the aggregation.
    """
    if sys.version_info >= (3, 11) and any(not isinstance(failure, Exception) for failure in failures):
        return BaseExceptionGroup(message, list(failures))
    return RuntimeExceptionGroup(message, list(failures))


__all__ = ["RuntimeExceptionGroup", "aggregateRuntimeFailures"]
