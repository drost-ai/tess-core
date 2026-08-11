from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .error_diagnostics import (
    bound_optional_diagnostic_text as _bound_optional,
    nonnegative_optional_integer as _nonnegative_optional_integer,
    valid_http_status as _valid_http_status,
)


class ProviderWireError(Exception):
    """Transport-neutral provider failure with classification evidence.

    Any transport can raise this (or expose a ``provider_error_evidence``
    mapping on its own exception type) to make HTTP-status and error-body
    classification reachable without the classifier depending on the
    exception's module or class.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        code: str | None = None,
        errorType: str | None = None,
        requestId: str | None = None,
        retryAfterMs: int | None = None,
        streamEventType: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_error_evidence: dict[str, Any] = {
            "status": status,
            "body": body,
            "code": code,
            "errorType": errorType,
            "requestId": requestId,
            "retryAfterMs": retryAfterMs,
            "streamEventType": streamEventType,
        }


def extract_provider_wire_evidence(error: BaseException) -> dict[str, Any] | None:
    """Validated wire evidence from any exception that opts into the protocol."""
    raw_evidence = getattr(error, "provider_error_evidence", None)
    if not isinstance(raw_evidence, Mapping):
        return None
    return {
        "status": _valid_http_status(raw_evidence.get("status")),
        "body": _bound_optional(raw_evidence.get("body"), 8_192),
        "code": _bound_optional(raw_evidence.get("code"), 256),
        "errorType": _bound_optional(raw_evidence.get("errorType"), 256),
        "requestId": _bound_optional(raw_evidence.get("requestId"), 256),
        "retryAfterMs": _nonnegative_optional_integer(raw_evidence.get("retryAfterMs")),
        "streamEventType": _bound_optional(raw_evidence.get("streamEventType"), 256),
    }
