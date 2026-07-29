from __future__ import annotations

import math
import random
import re
from collections.abc import Mapping as _Mapping
from typing import Any

import typing as _typing

from ._utils import now_iso
from .provider_error_sdk import (
    classifyOpenAiSdkEvidence as _classify_openai_sdk_evidence,
    extractOpenAiSdkException as _extract_openai_sdk_exception,
)

if _typing.TYPE_CHECKING:
    from .contracts.providers import ProviderFailureRecord


class ProviderRequestError(Exception):
    def __init__(self, record: dict[str, Any]):
        super().__init__(record["message"])
        self.name = "ProviderRequestError"
        self.record = record


class ProviderIncompleteResponseError(ProviderRequestError):
    def __init__(self, record: dict[str, Any], details: dict[str, Any]):
        super().__init__(record)
        self.name = "ProviderIncompleteResponseError"
        self.details = details


def isProviderRequestError(error: BaseException | Any) -> bool:
    return isinstance(error, ProviderRequestError)


def createProviderFailureRecord(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderFailureRecord:
    args = {**(args or {}), **kwargs}
    record: ProviderFailureRecord = {
        "provider": _bound_diagnostic_text(str(args["provider"]), 64),
        "providerLabel": _bound_diagnostic_text(str(args["providerLabel"]), 128),
        "phase": _bound_diagnostic_text(str(args["phase"]), 64),
        "category": args["category"],
        "message": _format_provider_failure_message(args),
        "retryable": bool(args["retryable"]),
        "attempt": int(args["attempt"]),
        "hadAssistantOutput": bool(args["hadAssistantOutput"]),
        "hadToolUse": bool(args["hadToolUse"]),
        "occurredAt": now_iso(),
        "status": _valid_http_status(args.get("status")),
        "code": _bound_optional_diagnostic_text(args.get("code"), 256),
        "errorType": _bound_optional_diagnostic_text(args.get("errorType"), 256),
        "rawMessage": _bound_optional_diagnostic_text(args.get("rawMessage"), 2_048),
        "rawBody": _bound_optional_diagnostic_text(args.get("rawBody"), 8_192),
        "streamEventType": _bound_optional_diagnostic_text(args.get("streamEventType"), 256),
    }
    optional = {
        "requestId": _bound_optional_diagnostic_text(args.get("requestId"), 256),
        "logicalCallId": _bound_optional_diagnostic_text(args.get("logicalCallId"), 128),
        "generation": _positive_optional_integer(args.get("generation")),
        "partialOutputMode": _partial_output_mode(args.get("partialOutputMode")),
        "responseId": _bound_optional_diagnostic_text(args.get("responseId"), 256),
        "sequenceNumber": _nonnegative_optional_integer(args.get("sequenceNumber")),
        "recoveryFailure": _bound_optional_diagnostic_text(args.get("recoveryFailure"), 128),
        "interruptedPreview": _bounded_interrupted_preview(args.get("interruptedPreview")),
    }
    record.update({key: value for key, value in optional.items() if value is not None})
    return record


def createProviderRequestError(record: dict[str, Any]) -> ProviderRequestError:
    return ProviderRequestError(record)


def createProviderIncompleteResponseError(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ProviderIncompleteResponseError:
    args = {**(args or {}), **kwargs}
    details = {
        "provider": args["provider"],
        "mode": _bound_diagnostic_text(str(args["mode"]), 64),
        "phase": args["phase"],
        "expectedTerminal": _bound_diagnostic_text(str(args["expectedTerminal"]), 128),
        "reason": _bound_diagnostic_text(str(args["reason"]), 256),
        "partialAssistantBytes": _bound_diagnostic_count(args.get("partialAssistantBytes")),
        "partialToolArgumentBytes": _bound_diagnostic_count(args.get("partialToolArgumentBytes")),
        "openItems": _bound_diagnostic_count(args.get("openItems")),
    }
    raw_message = (
        f"Incomplete {details['provider']}/{details['mode']} response: {details['reason']}. "
        f"Expected {details['expectedTerminal']}. "
        "Partial bytes: "
        f"assistant={details['partialAssistantBytes']}, "
        f"tool_arguments={details['partialToolArgumentBytes']}; "
        f"open_items={details['openItems']}."
    )
    record = createProviderFailureRecord(
        provider=args["provider"],
        providerLabel=args["providerLabel"],
        phase=args["phase"],
        category="protocol",
        retryable=not bool(args["hadAssistantOutput"]) and not bool(args["hadToolUse"]),
        attempt=args["attempt"],
        hadAssistantOutput=args["hadAssistantOutput"],
        hadToolUse=args["hadToolUse"],
        rawMessage=raw_message,
    )
    return ProviderIncompleteResponseError(record, details)


def classifyProviderHttpFailure(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderRequestError:
    args = {**(args or {}), **kwargs}
    text = f"{args.get('body') or ''} {args.get('errorType') or ''} {args.get('code') or ''}".lower()
    status = int(args["status"])
    category = "server"
    retryable = False
    if status in (401, 403):
        category = "auth"
    elif status == 408:
        category = "timeout"
        retryable = True
    elif status == 429:
        category = "quota" if any(word in text for word in ("quota", "credit", "billing")) else "rate_limit"
        retryable = category == "rate_limit"
    elif status >= 500:
        category = "server"
        retryable = True
    elif status >= 400:
        category = "quota" if _looks_like_quota(text) else "client_request"
    return createProviderRequestError(
        createProviderFailureRecord(
            {
                **args,
                "category": category,
                "retryable": retryable,
                "rawMessage": args.get("rawMessage")
                or _build_raw_message(args.get("body"), args.get("errorType"), args.get("code")),
                "rawBody": args.get("body"),
            }
        )
    )


def normalizeUnknownProviderFailure(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderRequestError:
    args = {**(args or {}), **kwargs}
    error = args["error"]
    if isinstance(error, ProviderIncompleteResponseError):
        record = dict(error.record)
        record.update(
            {
                "phase": record.get("phase") or args["phase"],
                "attempt": args["attempt"],
                "hadAssistantOutput": args["hadAssistantOutput"],
                "hadToolUse": args["hadToolUse"],
                "retryable": not bool(args["hadAssistantOutput"]) and not bool(args["hadToolUse"]),
                "streamEventType": record.get("streamEventType") or args.get("streamEventType"),
            }
        )
        return ProviderIncompleteResponseError(
            record,
            {**error.details, "phase": record["phase"]},
        )
    if isinstance(error, ProviderRequestError):
        record = dict(error.record)
        record.update(
            {
                "phase": record.get("phase") or args["phase"],
                "attempt": args["attempt"],
                "hadAssistantOutput": args["hadAssistantOutput"],
                "hadToolUse": args["hadToolUse"],
                "streamEventType": record.get("streamEventType") or args.get("streamEventType"),
            }
        )
        return createProviderRequestError(record)
    raw = str(error)
    lower = raw.lower()
    sdk_evidence = _extract_openai_sdk_exception(error, args.get("provider"))
    if sdk_evidence is not None:
        return _classify_openai_sdk_exception(args, sdk_evidence)
    sdk_http = _parse_openai_sdk_http_error(raw)
    if sdk_http is not None and args.get("provider") == "openai":
        return classifyProviderHttpFailure(
            {
                "provider": args["provider"],
                "providerLabel": args["providerLabel"],
                "phase": args["phase"],
                "attempt": args["attempt"],
                "hadAssistantOutput": args["hadAssistantOutput"],
                "hadToolUse": args["hadToolUse"],
                "status": sdk_http["status"],
                "body": sdk_http["body"],
                "errorType": sdk_http.get("errorType"),
                "code": sdk_http.get("code"),
                "rawMessage": raw,
                "streamEventType": args.get("streamEventType"),
            }
        )
    category = "unknown"
    retryable = False
    if _looks_like_timeout(lower):
        category = "timeout"
        retryable = True
    elif _looks_like_transport(lower):
        category = "transport"
        retryable = True
    elif _looks_like_protocol(lower):
        category = "protocol"
    elif _looks_like_auth(lower):
        category = "auth"
    return createProviderRequestError(
        createProviderFailureRecord(
            {
                "provider": args["provider"],
                "providerLabel": args["providerLabel"],
                "phase": args["phase"],
                "category": category,
                "retryable": retryable,
                "attempt": args["attempt"],
                "hadAssistantOutput": args["hadAssistantOutput"],
                "hadToolUse": args["hadToolUse"],
                "rawMessage": raw,
                "streamEventType": args.get("streamEventType"),
            }
        )
    )


def canRetryProviderFailure(error: ProviderRequestError, maxAttempts: int) -> bool:
    return bool(
        error.record.get("retryable")
        and error.record.get("attempt", 0) < maxAttempts
        and not error.record.get("hadAssistantOutput")
        and not error.record.get("hadToolUse")
    )


def fallbackEligibleProviderFailure(error: Any) -> bool:
    return isinstance(error, ProviderRequestError) and error.record.get("category") in {"rate_limit", "transport", "timeout", "server"}


def providerRetryDelayMs(attempt: int) -> int:
    return (350 if attempt <= 1 else 700) + random.randint(0, 119)


def _bound_diagnostic_text(value: str, max_length: int) -> str:
    normalized = re.sub(r"[\r\n\t]+", " ", value).strip()
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 1]}…"


def _bound_optional_diagnostic_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    text = _bound_diagnostic_text(str(value), max_length)
    return text or None


def _valid_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _positive_optional_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return min(value, 9_007_199_254_740_991)


def _nonnegative_optional_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 9_007_199_254_740_991)


def _partial_output_mode(value: Any) -> str | None:
    return value if value in {"buffered", "replaceable", "append_only"} else None


def _bounded_interrupted_preview(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, _Mapping):
        return None
    return {
        "type": "assistant_interrupted_preview",
        "logicalCallId": _bound_optional_diagnostic_text(value.get("logicalCallId"), 128),
        "generation": _positive_optional_integer(value.get("generation")),
        "deliveryMode": _partial_output_mode(value.get("deliveryMode")),
        "textHash": _bound_optional_diagnostic_text(value.get("textHash"), 128),
        "textLength": _nonnegative_optional_integer(value.get("textLength")),
        "reasoningTextHash": _bound_optional_diagnostic_text(
            value.get("reasoningTextHash"), 128
        ),
        "reasoningTextLength": _nonnegative_optional_integer(
            value.get("reasoningTextLength")
        ),
        "deliveryAcknowledged": bool(value.get("deliveryAcknowledged")),
        "reason": _bound_optional_diagnostic_text(value.get("reason"), 256),
    }


def _bound_diagnostic_count(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return 0
    return min(9_007_199_254_740_991, int(value))


def _format_provider_failure_message(args: dict[str, Any]) -> str:
    label = args["providerLabel"]
    category = args["category"]
    if category == "auth":
        return f"{label} request failed due to authentication or permission issues."
    if category == "rate_limit":
        return f"{label} request was rate limited. Retry in a moment."
    if category == "quota":
        return f"{label} request failed because the account has insufficient quota or billing capacity."
    if category == "timeout":
        return f"{label} request timed out before completion."
    if category == "transport":
        if args.get("hadAssistantOutput") or args.get("hadToolUse"):
            return f"{label} stream terminated unexpectedly after partial output. The turn did not complete."
        return f"{label} stream terminated unexpectedly before a response was produced."
    if category == "protocol":
        return f"{label} returned a malformed or incomplete streaming response."
    if category == "server":
        status = f" ({args['status']})" if isinstance(args.get("status"), int) else ""
        return f"{label} request failed with a server error{status}."
    if category == "client_request":
        status = f" ({args['status']})" if isinstance(args.get("status"), int) else ""
        return f"{label} rejected the request as invalid{status}."
    if category == "cancelled":
        return f"{label} request was cancelled."
    return f"{label} request failed unexpectedly."


def _build_raw_message(body: str | None = None, error_type: str | None = None, code: str | None = None) -> str | None:
    parts = [part for part in (code, error_type, body) if isinstance(part, str) and part.strip()]
    return " ".join(parts) if parts else None


def _looks_like_timeout(message: str) -> bool:
    return bool(re.search(r"timed out|timeout|etimedout|connect timeout|headers timeout|body timeout|deadline exceeded", message))


def _looks_like_transport(message: str) -> bool:
    return bool(re.search(r"terminated|socket hang up|econnreset|econnrefused|enotfound|network|fetch failed|connection closed|other side closed|premature close|broken pipe", message))


def _looks_like_protocol(message: str) -> bool:
    return bool(re.search(r"failed to parse streamed provider event|unexpected end of json input|invalid json|malformed sse|response body was empty|unable to parse tool parameter json", message))


def _looks_like_auth(message: str) -> bool:
    return bool(re.search(r"unauthorized|forbidden|invalid api key|authentication|auth token|insufficient permissions?|missing scopes?|insufficient_scope|permission denied", message))


def _looks_like_quota(message: str) -> bool:
    return bool(re.search(r"quota|credit|billing|insufficient funds|payment required|usage limit|out of extra usage", message))


def _classify_openai_sdk_exception(
    args: dict[str, Any], evidence: dict[str, Any]
) -> ProviderRequestError:
    common = {
        "provider": args["provider"],
        "providerLabel": args["providerLabel"],
        "phase": args["phase"],
        "attempt": args["attempt"],
        "hadAssistantOutput": args["hadAssistantOutput"],
        "hadToolUse": args["hadToolUse"],
        "status": evidence.get("status"),
        "body": evidence.get("body"),
        "errorType": evidence.get("errorType"),
        "code": evidence.get("code"),
        "requestId": evidence.get("requestId"),
        "rawMessage": evidence.get("rawMessage"),
        "streamEventType": args.get("streamEventType"),
    }
    if evidence.get("status") is not None:
        return classifyProviderHttpFailure(common)
    category, retryable = _classify_openai_sdk_evidence(evidence)
    return createProviderRequestError(
        createProviderFailureRecord(
            {
                **common,
                "category": category,
                "retryable": retryable,
                "rawBody": evidence.get("body"),
            }
        )
    )
def _parse_openai_sdk_http_error(raw: str) -> dict[str, Any] | None:
    match = re.search(r"Error code:\s*(\d{3})\s*-\s*(.*)", raw, re.S)
    if not match:
        return None
    status = int(match.group(1))
    body = (match.group(2) or "").strip()
    error_type = None
    code = None
    type_match = re.search(r"['\"]type['\"]\s*:\s*['\"]([^'\"]+)['\"]", body)
    if type_match:
        error_type = type_match.group(1)
    code_match = re.search(r"['\"]code['\"]\s*:\s*['\"]([^'\"]+)['\"]", body)
    if code_match:
        code = code_match.group(1)
    return {"status": status, "body": body or raw, "errorType": error_type, "code": code}
