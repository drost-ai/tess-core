from __future__ import annotations

import copy
import json
import re
from pathlib import PurePosixPath
from typing import Any


_MEDIA_BLOCK_TYPES = frozenset(
    {
        "attachment",
        "audio",
        "document",
        "file",
        "image",
        "image_url",
        "input_audio",
        "input_file",
        "input_image",
        "pdf",
    }
)
_MEDIA_SHAPE_KEYS = frozenset(
    {
        "base64",
        "data",
        "data_base64",
        "file_data",
        "image_url",
        "source",
        "url",
    }
)
_MIME_PATTERN = re.compile(
    r"^(?:image|audio|video)/[a-z0-9.+-]{1,64}$|^application/(?:pdf|octet-stream)$",
    re.IGNORECASE,
)
_SAFE_LABEL_CHARACTER = re.compile(r"[^A-Za-z0-9._/ -]+")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")
_DIAGNOSTIC_JSON_KEYS = frozenset(
    {
        "body",
        "error",
        "message",
        "raw_body",
        "raw_error",
        "raw_message",
        "rawbody",
        "rawerror",
        "rawmessage",
    }
)


def sanitizeMediaForPersistence(
    value: Any,
    *,
    includeByteLength: bool = True,
) -> Any:
    """Return a content-free copy suitable for persistent or diagnostic output."""

    return _sanitize(value, include_byte_length=includeByteLength, parent_key="")


def _sanitize(
    value: Any,
    *,
    include_byte_length: bool,
    parent_key: str,
) -> Any:
    if isinstance(value, list):
        return [
            _sanitize(
                item,
                include_byte_length=include_byte_length,
                parent_key=parent_key,
            )
            for item in value
        ]
    if isinstance(value, str):
        return _sanitize_string(
            value,
            parent_key=parent_key,
            include_byte_length=include_byte_length,
        )
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if _is_media_block(value):
        return _canonical_media_reference(
            value,
            include_byte_length=include_byte_length,
        )
    return {
        str(key): _sanitize(
            item,
            include_byte_length=include_byte_length,
            parent_key=str(key),
        )
        for key, item in value.items()
    }


def _is_media_block(value: dict[str, Any]) -> bool:
    block_type = str(value.get("type") or "").strip().lower()
    return block_type in _MEDIA_BLOCK_TYPES and bool(_MEDIA_SHAPE_KEYS & value.keys())


def _canonical_media_reference(
    value: dict[str, Any],
    *,
    include_byte_length: bool,
) -> dict[str, Any]:
    kind = _media_kind(value)
    mime_type = _media_mime_type(value, kind)
    byte_length = _media_byte_length(value)
    label = _media_label(value)
    details = [
        f"{kind.capitalize()} attachment was available to the model on the original turn."
    ]
    if mime_type:
        details.append(f"MIME type: {mime_type}")
    if include_byte_length and byte_length is not None:
        details.append(f"Payload bytes: {byte_length}")
    if label:
        details.append(f"Attachment label: {label}")
    details.append("Payload: <redacted-media-payload>")
    details.append(
        "Raw media bytes are current-turn-only and are not retained or replayed."
    )
    reference: dict[str, Any] = {
        "kind": kind,
        "retention": "current-turn-only",
    }
    if mime_type:
        reference["mime_type"] = mime_type
    if include_byte_length and byte_length is not None:
        reference["byte_length"] = byte_length
    if label:
        reference["label"] = label
    attachment_id = _safe_identifier(
        value.get("attachment_id")
        or value.get("attachmentId")
        or value.get("sha256")
    )
    if attachment_id:
        reference["attachment_id"] = attachment_id
    return {
        "type": "text",
        "text": "\n".join(details),
        "media_reference": reference,
    }


def _media_kind(value: dict[str, Any]) -> str:
    block_type = str(value.get("type") or "").strip().lower()
    mime_type = _raw_mime_type(value).lower()
    if "image" in block_type or mime_type.startswith("image/"):
        return "image"
    if "audio" in block_type or mime_type.startswith("audio/"):
        return "audio"
    if block_type in {"pdf", "document"} or mime_type == "application/pdf":
        return "document"
    return "file"


def _media_mime_type(value: dict[str, Any], kind: str) -> str:
    mime_type = _raw_mime_type(value).lower()
    if mime_type and _MIME_PATTERN.fullmatch(mime_type):
        return mime_type
    return {
        "image": "image/unknown",
        "audio": "audio/unknown",
        "document": "application/pdf",
    }.get(kind, "application/octet-stream")


def _raw_mime_type(value: dict[str, Any]) -> str:
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    for record in (value, source):
        for key in ("media_type", "mime_type", "mimeType", "mime"):
            candidate = record.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    data_url = _find_payload_string(value)
    if data_url.lower().startswith("data:"):
        return data_url[5:].split(";", 1)[0].strip()
    return ""


def _media_byte_length(value: dict[str, Any]) -> int | None:
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    for record in (value, source):
        for key in (
            "byte_length",
            "byteLength",
            "decoded_bytes",
            "decodedBytes",
            "size",
        ):
            candidate = record.get(key)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                return candidate
    payload = _find_payload_string(value)
    if not payload:
        return None
    marker = payload.lower().find(";base64,")
    if payload.lower().startswith("data:") and marker >= 0:
        payload = payload[marker + len(";base64,") :]
    compact = "".join(payload.split())
    if not compact or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", compact):
        return None
    padding = len(compact) - len(compact.rstrip("="))
    return max(0, (len(compact) * 3) // 4 - padding)


def _find_payload_string(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            found = _find_payload_string(item)
            if found:
                return found
        return ""
    if not isinstance(value, dict):
        return ""
    for key in (
        "data",
        "data_base64",
        "base64",
        "file_data",
        "image_url",
        "url",
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            found = _find_payload_string(candidate)
            if found:
                return found
    for candidate in value.values():
        if isinstance(candidate, (dict, list)):
            found = _find_payload_string(candidate)
            if found:
                return found
    return ""


def _media_label(value: dict[str, Any]) -> str:
    for key in ("label", "path", "file_path", "filePath", "source_path"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            safe = _safe_label(candidate)
            if safe:
                return safe
    return ""


def _safe_label(value: str) -> str:
    candidate = value.strip().replace("\\", "/")
    if not candidate or candidate.lower().startswith(("data:", "http:", "https:")):
        return ""
    pure = PurePosixPath(candidate)
    parts = [part for part in pure.parts if part not in {"", "/", ".", ".."}]
    if not parts:
        return ""
    if candidate.startswith("/") or _WINDOWS_DRIVE.match(candidate) or ".." in pure.parts:
        parts = parts[-1:]
    cleaned = _SAFE_LABEL_CHARACTER.sub("_", "/".join(parts)).strip(" /")
    return cleaned[:256]


def _safe_identifier(value: Any) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > 128
        or not re.fullmatch(r"[A-Za-z0-9._:-]+", candidate)
    ):
        return ""
    return candidate


def _sanitize_string(
    value: str,
    *,
    parent_key: str,
    include_byte_length: bool,
) -> str:
    cleaned = _redact_data_urls(value)
    if parent_key.strip().lower() not in _DIAGNOSTIC_JSON_KEYS:
        return cleaned
    return _sanitize_diagnostic_json(
        cleaned,
        include_byte_length=include_byte_length,
    )


def _sanitize_diagnostic_json(value: str, *, include_byte_length: bool) -> str:
    candidate = value.strip()
    if not candidate.startswith(("{", "[")):
        return value
    try:
        decoded = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    sanitized = _sanitize(
        decoded,
        include_byte_length=include_byte_length,
        parent_key="",
    )
    if sanitized == decoded:
        return value
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))


def _redact_data_urls(value: str) -> str:
    lower = value.lower()
    start = 0
    output: list[str] = []
    while True:
        index = lower.find("data:", start)
        if index < 0:
            output.append(value[start:])
            break
        marker = lower.find(";base64,", index, min(len(value), index + 256))
        if marker < 0:
            output.append(value[start : index + 5])
            start = index + 5
            continue
        end = marker + len(";base64,")
        while end < len(value) and value[end] in (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"
        ):
            end += 1
        output.append(value[start:index])
        output.append("<redacted-data-url>")
        start = end
    return "".join(output)


sanitize_media_for_persistence = sanitizeMediaForPersistence
