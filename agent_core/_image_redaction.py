from __future__ import annotations

from typing import Any


_IMAGE_BLOCK_TYPES = frozenset({"image", "input_image", "image_url"})
_IMAGE_PAYLOAD_KEYS = frozenset(
    {"data", "data_base64", "base64", "image_url", "url"}
)


def redactImagePayloads(value: Any) -> Any:
    """Return a copy with transport payloads below image blocks made opaque."""

    return _redact_image_payloads(value, image_block=None)


def _redact_image_payloads(value: Any, image_block: dict[str, Any] | None) -> Any:
    if isinstance(value, list):
        return [
            _redact_image_payloads(item, image_block=image_block) for item in value
        ]
    if not isinstance(value, dict):
        return value

    current_image = (
        value
        if str(value.get("type") or "").strip().lower() in _IMAGE_BLOCK_TYPES
        else image_block
    )
    out: dict[str, Any] = {}
    for key, item in value.items():
        if current_image is not None and key in _IMAGE_PAYLOAD_KEYS:
            out[key] = _image_placeholder(current_image)
        else:
            out[key] = _redact_image_payloads(item, image_block=current_image)
    return out


def _image_placeholder(value: dict[str, Any]) -> str:
    source = value.get("source") if isinstance(value.get("source"), dict) else value
    mime_type = (
        source.get("media_type")
        or source.get("mime_type")
        or source.get("mimeType")
        or source.get("mime")
        or "image"
    )
    return f"<image:{mime_type}>"


redact_image_payloads = redactImagePayloads
