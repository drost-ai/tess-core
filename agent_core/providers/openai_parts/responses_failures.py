from __future__ import annotations

from typing import Any

from ...provider_errors import (
    ProviderRequestError,
    createProviderFailureRecord,
    createProviderRequestError,
)
from ..._utils import get_value
from .normalization import _response_error_text


def response_terminal_failure(
    event_type: str,
    event: Any,
    *,
    provider_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
) -> ProviderRequestError | None:
    response = get_value(event, "response")
    if event_type == "response.failed":
        return _failure(
            provider_label, phase, "server", True, attempt,
            had_output, had_tool_use,
            _response_error_text(response), event_type,
        )
    if event_type == "response.cancelled":
        return _failure(
            provider_label, "response_complete", "cancelled", False, attempt,
            had_output, had_tool_use,
            _response_error_text(response) or "OpenAI response was cancelled.",
            event_type,
        )
    if event_type == "response.incomplete":
        return _failure(
            provider_label, "response_complete", "protocol", False, attempt,
            had_output, had_tool_use,
            _response_error_text(response), event_type,
        )
    if event_type != "error":
        return None
    message = str(get_value(event, "message", "") or "")
    code = str(get_value(event, "code", "") or "")
    combined = f"{code} {message}".lower()
    category = (
        "rate_limit"
        if "rate" in combined or "limit" in combined
        else "auth"
        if any(word in combined for word in ("auth", "unauthor", "forbidden"))
        else "server"
    )
    return _failure(
        provider_label, phase, category, category in {"rate_limit", "server"},
        attempt, had_output, had_tool_use,
        f"{code}: {message}" if code and message else message or "OpenAI stream error",
        event_type, code or None,
    )


def _failure(
    provider_label: str,
    phase: str,
    category: str,
    retryable: bool,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
    raw_message: str,
    stream_event_type: str,
    code: str | None = None,
) -> ProviderRequestError:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category=category,
            retryable=retryable,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage=raw_message,
            code=code,
            streamEventType=stream_event_type,
        )
    )
