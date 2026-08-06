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


__all__ = ["RuntimeExceptionGroup"]
