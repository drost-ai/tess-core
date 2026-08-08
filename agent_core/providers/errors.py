from __future__ import annotations

from .error_diagnostics import (
    looksLikeAuth as _looks_like_auth,
    looksLikeQuota as _looks_like_quota,
    looksLikeTimeout as _looks_like_timeout,
    looksLikeTransport as _looks_like_transport,
)

import json
import http.client as _http_client
import random
import re
from collections.abc import Mapping as _Mapping
from typing import Any

import typing as _typing

from .._utils import now_iso
from .error_diagnostics import (
    boundDiagnosticCount as _bound_diagnostic_count,
    boundedInterruptedPreview as _bounded_interrupted_preview,
    boundDiagnosticText as _bound_diagnostic_text,
    boundOptionalDiagnosticText as _bound_optional_diagnostic_text,
    nonnegativeOptionalInteger as _nonnegative_optional_integer,
    partialOutputMode as _partial_output_mode,
    positiveOptionalInteger as _positive_optional_integer,
    validHttpStatus as _valid_http_status,
)
from .error_sdk import (
    classifyOpenAiSdkEvidence as _classify_openai_sdk_evidence,
    extractOpenAiSdkException as _extract_openai_sdk_exception,
)

if _typing.TYPE_CHECKING:
    from ..contracts.providers import ProviderFailureRecord


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
        "component": _bound_optional_diagnostic_text(args.get("component"), 64),
        "operation": _bound_optional_diagnostic_text(args.get("operation"), 32),
        "retryAfterMs": _nonnegative_optional_integer(args.get("retryAfterMs")),
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
    wire_evidence = _provider_wire_evidence(error)
    if wire_evidence is not None:
        return _classify_wire_evidence(args, wire_evidence, raw)
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
    sdk_stream = (
        _extract_openai_sdk_stream_error(error)
        if args.get("provider") == "openai"
        else None
    )
    if sdk_stream is not None:
        status = sdk_stream.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            return classifyProviderHttpFailure(
                {
                    "provider": args["provider"],
                    "providerLabel": args["providerLabel"],
                    "phase": args["phase"],
                    "attempt": args["attempt"],
                    "hadAssistantOutput": args["hadAssistantOutput"],
                    "hadToolUse": args["hadToolUse"],
                    "status": status,
                    "body": sdk_stream.get("body"),
                    "errorType": sdk_stream.get("errorType"),
                    "code": sdk_stream.get("code"),
                    "streamEventType": (
                        args.get("streamEventType")
                        or sdk_stream.get("streamEventType")
                    ),
                }
            )
        category = _classify_openai_sdk_stream_error(sdk_stream)
        return createProviderRequestError(
            createProviderFailureRecord(
                {
                    "provider": args["provider"],
                    "providerLabel": args["providerLabel"],
                    "phase": args["phase"],
                    "category": category,
                    "retryable": category in {"rate_limit", "timeout", "server"},
                    "attempt": args["attempt"],
                    "hadAssistantOutput": args["hadAssistantOutput"],
                    "hadToolUse": args["hadToolUse"],
                    "rawMessage": raw,
                    "rawBody": sdk_stream.get("body"),
                    "errorType": sdk_stream.get("errorType"),
                    "code": sdk_stream.get("code"),
                    "streamEventType": (
                        args.get("streamEventType")
                        or sdk_stream.get("streamEventType")
                    ),
                }
            )
        )
    category = "unknown"
    retryable = False
    if _looks_like_timeout(lower):
        category = "timeout"
        retryable = True
    elif isinstance(error, (ConnectionError, OSError, EOFError, _http_client.HTTPException)) or _looks_like_transport(lower):
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
    # Call-time import: the config package imports providers at module load.
    from ..config._knobs import knobValue

    ladder = tuple(knobValue("provider.anthropic_retry_delays_ms"))
    return retryDelayFromLadder(ladder, attempt) + random.randint(0, 119)


def retryDelayFromLadder(ladder: tuple[int, ...], attempt: int) -> int:
    index = min(max(int(attempt), 1), len(ladder)) - 1
    return ladder[index]


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
    if category == "consumer_sink":
        return f"A local response-presentation component failed while handling the {label} response."
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


def _provider_wire_evidence(error: BaseException) -> dict[str, Any] | None:
    raw_evidence = getattr(error, "provider_error_evidence", None)
    if not isinstance(raw_evidence, _Mapping):
        return None
    return {
        "status": _valid_http_status(raw_evidence.get("status")),
        "body": _bound_optional_diagnostic_text(raw_evidence.get("body"), 8_192),
        "code": _bound_optional_diagnostic_text(raw_evidence.get("code"), 256),
        "errorType": _bound_optional_diagnostic_text(raw_evidence.get("errorType"), 256),
        "requestId": _bound_optional_diagnostic_text(raw_evidence.get("requestId"), 256),
        "retryAfterMs": _nonnegative_optional_integer(raw_evidence.get("retryAfterMs")),
        "streamEventType": _bound_optional_diagnostic_text(
            raw_evidence.get("streamEventType"), 256
        ),
    }


def _classify_wire_evidence(
    args: dict[str, Any], evidence: dict[str, Any], raw: str
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
        "retryAfterMs": evidence.get("retryAfterMs"),
        "rawMessage": raw,
        "streamEventType": args.get("streamEventType") or evidence.get("streamEventType"),
    }
    if evidence.get("status") is not None:
        return classifyProviderHttpFailure(common)
    category, retryable = _classify_openai_sdk_evidence(
        {
            "errorType": evidence.get("errorType"),
            "code": evidence.get("code"),
            "message": evidence.get("body"),
            "rawMessage": raw,
            "className": "",
        }
    )
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






def _looks_like_protocol(message: str) -> bool:
    return bool(re.search(r"failed to parse streamed provider event|unexpected end of json input|invalid json|malformed sse|response body was empty|unable to parse tool parameter json", message))




def _extract_openai_sdk_stream_error(error: BaseException) -> dict[str, Any] | None:
    error_type = type(error)
    module = str(getattr(error_type, "__module__", "") or "")
    if not module.startswith("openai"):
        return None

    raw_body = getattr(error, "body", None)
    status = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    sdk_error_type = getattr(error, "type", None)
    if isinstance(raw_body, _Mapping):
        nested = raw_body.get("error")
        body_fields = nested if isinstance(nested, _Mapping) else raw_body
        code = code or body_fields.get("code")
        sdk_error_type = sdk_error_type or body_fields.get("type")
    body = _serialize_provider_diagnostic_body(raw_body)
    return {
        "status": (
            status
            if isinstance(status, int) and not isinstance(status, bool)
            else None
        ),
        "body": body,
        "code": _optional_provider_diagnostic_scalar(code),
        "errorType": _optional_provider_diagnostic_scalar(sdk_error_type),
        "streamEventType": "error",
    }


def _serialize_provider_diagnostic_body(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rendered = str(value)
    rendered = rendered.strip()
    if not rendered:
        return None
    return _bound_diagnostic_text(rendered, 4_000)


def _optional_provider_diagnostic_scalar(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return _bound_diagnostic_text(rendered, 256) if rendered else None


def _classify_openai_sdk_stream_error(details: dict[str, Any]) -> str:
    text = " ".join(
        str(details.get(key) or "")
        for key in ("body", "errorType", "code")
    ).lower()
    if _looks_like_quota(text):
        return "quota"
    if "rate" in text and ("limit" in text or "exceed" in text):
        return "rate_limit"
    if _looks_like_timeout(text):
        return "timeout"
    if _looks_like_auth(text):
        return "auth"
    if re.search(r"invalid[_ -]?request|bad request|unsupported|not found", text):
        return "client_request"
    return "server"




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
        "streamEventType": (
            args.get("streamEventType") or evidence.get("streamEventType")
        ),
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
