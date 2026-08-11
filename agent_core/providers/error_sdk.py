from __future__ import annotations

from .error_diagnostics import (
    bound_diagnostic_text as _bound,
    bound_optional_diagnostic_text as _bound_optional,
    looks_like_auth as _looks_like_auth,
    looks_like_quota as _looks_like_quota,
    looks_like_timeout as _looks_like_timeout,
    looks_like_transport as _looks_like_transport,
    valid_http_status as _valid_http_status,
)

import json
import math
import re
from collections.abc import Mapping
from typing import Any


def extract_openai_sdk_exception(error: Any, provider: Any) -> dict[str, Any] | None:
    if provider != "openai":
        return None
    error_type = type(error)
    module = str(getattr(error_type, "__module__", "") or "")
    if module != "openai" and not module.startswith("openai."):
        return None
    status = _valid_http_status(getattr(error, "status_code", None))
    if status is None:
        status = _valid_http_status(
            getattr(getattr(error, "response", None), "status_code", None)
        )
    body_value = getattr(error, "body", None)
    nested = body_value.get("error") if isinstance(body_value, Mapping) else None
    body_error = nested if isinstance(nested, Mapping) else body_value
    structured_type = getattr(error, "type", None)
    structured_code = getattr(error, "code", None)
    structured_message = None
    if isinstance(body_error, Mapping):
        structured_type = structured_type or body_error.get("type")
        structured_code = structured_code or body_error.get("code")
        structured_message = body_error.get("message")
    return {
        "status": status,
        "body": _serialize_sdk_body(body_value, compact=module != "openai"),
        "errorType": _bound_optional(structured_type, 256),
        "code": _bound_optional(structured_code, 256),
        "message": _bound_redacted_optional(structured_message, 2_048),
        "rawMessage": _bound_redacted_optional(str(error), 2_048),
        "requestId": _bound_optional(
            getattr(error, "request_id", None) or getattr(error, "requestId", None),
            256,
        ),
        "className": _bound(error_type.__name__, 128),
        "streamEventType": "error" if module == "openai" else None,
    }


def classify_openai_sdk_evidence(evidence: Mapping[str, Any]) -> tuple[str, bool]:
    text = " ".join(
        str(evidence.get(key) or "")
        for key in ("errorType", "code", "message", "rawMessage", "className")
    ).lower()
    if _looks_like_auth(text):
        return "auth", False
    if _looks_like_quota(text):
        return "quota", False
    if re.search(r"invalid[_ -]?request|bad[_ -]?request|unprocessable|not[_ -]?found", text):
        return "client_request", False
    if _looks_like_timeout(text):
        return "timeout", True
    if re.search(r"rate[_ -]?limit|too many requests", text):
        return "rate_limit", True
    if re.search(
        r"server[_ -]?error|internal[_ -]?error|apierror|api[_ -]?error|"
        r"error occurred while processing your request|processing your request",
        text,
    ):
        return "server", True
    if _looks_like_transport(text):
        return "transport", True
    return "unknown", False


def _serialize_sdk_body(value: Any, *, compact: bool) -> str | None:
    safe = _safe_sdk_value(value, depth=0)
    try:
        serialized = json.dumps(
            safe,
            sort_keys=True,
            separators=(",", ":") if compact else None,
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        return None
    return _bound(serialized, 8_192)


def _safe_sdk_value(value: Any, *, depth: int) -> Any:
    if depth > 6:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _bound(_redact_sensitive_text(value), 2_048)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(
            sorted(value.items(), key=lambda pair: str(pair[0]))
        ):
            if index >= 100:
                out["<truncated>"] = True
                break
            bounded_key = _bound(str(key), 256)
            out[bounded_key] = (
                "<redacted>"
                if _sensitive_body_key(bounded_key)
                else _safe_sdk_value(item, depth=depth + 1)
            )
        return out
    if isinstance(value, (list, tuple)):
        out = [_safe_sdk_value(item, depth=depth + 1) for item in value[:100]]
        if len(value) > 100:
            out.append("<truncated>")
        return out
    return "<unsupported>"






def _bound_redacted_optional(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return _bound(_redact_sensitive_text(str(value)), maximum) or None


def _sensitive_body_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in {"message", "error_message", "request_id", "requestid"}:
        return False
    parts = set(normalized.split("_"))
    return bool(
        parts
        & {
            "authorization",
            "apikey",
            "token",
            "secret",
            "password",
            "cookie",
            "header",
            "headers",
            "request",
            "input",
            "prompt",
            "messages",
            "content",
            "output",
        }
        or "api_key" in normalized
    )


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(\b(?:authorization|api[_ -]?key|token|secret|password|"
        r"input|prompt|messages?|content|request)\b\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1<redacted>",
        value,
    )
    redacted = re.sub(
        r"(?i)\bbearer\s+[a-z0-9._~+/=-]+",
        "Bearer <redacted>",
        redacted,
    )
    return re.sub(r"\bsk-[a-zA-Z0-9_-]{8,}\b", "sk-<redacted>", redacted)


