from __future__ import annotations

import random
import re
from typing import Any

from ._utils import now_iso
from .contracts.providers import ProviderFailureRecord


class ProviderRequestError(Exception):
    def __init__(self, record: dict[str, Any]):
        super().__init__(record["message"])
        self.name = "ProviderRequestError"
        self.record = record


def isProviderRequestError(error: BaseException | Any) -> bool:
    return isinstance(error, ProviderRequestError)


def createProviderFailureRecord(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderFailureRecord:
    args = {**(args or {}), **kwargs}
    return {
        "provider": args["provider"],
        "providerLabel": args["providerLabel"],
        "phase": args["phase"],
        "category": args["category"],
        "message": _format_provider_failure_message(args),
        "retryable": bool(args["retryable"]),
        "attempt": int(args["attempt"]),
        "hadAssistantOutput": bool(args["hadAssistantOutput"]),
        "hadToolUse": bool(args["hadToolUse"]),
        "occurredAt": now_iso(),
        "status": args.get("status"),
        "code": args.get("code"),
        "errorType": args.get("errorType"),
        "rawMessage": args.get("rawMessage"),
        "rawBody": args.get("rawBody"),
        "streamEventType": args.get("streamEventType"),
    }


def createProviderRequestError(record: dict[str, Any]) -> ProviderRequestError:
    return ProviderRequestError(record)


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
                "rawMessage": _build_raw_message(args.get("body"), args.get("errorType"), args.get("code")),
                "rawBody": args.get("body"),
            }
        )
    )


def normalizeUnknownProviderFailure(args: dict[str, Any] | None = None, **kwargs: Any) -> ProviderRequestError:
    args = {**(args or {}), **kwargs}
    error = args["error"]
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
    sdk_http = _parse_openai_sdk_http_error(raw)
    if sdk_http is not None:
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
