from __future__ import annotations

import base64
import copy
import hashlib
import re
from typing import Any

from ._media_persistence import sanitizeMediaForPersistence
from .config_store.shared import (
    DEFAULT_IMAGE_INPUT_MIME_TYPES,
    DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES,
    DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES,
    DEFAULT_MAX_IMAGE_INPUT_BYTES,
    DEFAULT_MAX_IMAGE_INPUT_COUNT,
    MAX_PROVIDER_AGGREGATE_IMAGE_INPUT_BYTES,
    MAX_PROVIDER_ENCODED_IMAGE_REQUEST_BYTES,
    MAX_PROVIDER_IMAGE_INPUT_BYTES,
    MAX_PROVIDER_IMAGE_INPUT_COUNT,
)
from .image_budget import admitImageMessages, estimateEncodedImageRequestBytes


_IMAGE_READ_PATH_PATTERN = re.compile(r"\.(?:gif|jpe?g|png|webp)$", re.IGNORECASE)
_MIME_PATTERN = re.compile(r"image/[a-z0-9.+-]{1,64}")
_PERSISTED_IMAGE_PLACEHOLDER_PREFIX = "Image attachment was available to the model on the original turn."
_CURRENT_TURN_PLACEHOLDER = re.compile(r"^<image:(?P<mime>[^:>]+)(?::(?P<path>.*?))?:current-turn-only>$")


def imageAttachmentReservationOrder(toolUses: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, tool_use in enumerate(toolUses)
        if tool_use.get("name") == "Read"
        and isinstance((tool_use.get("input") or {}).get("file_path"), str)
        and _IMAGE_READ_PATH_PATTERN.search((tool_use.get("input") or {})["file_path"])
    ]


def resolveRuntimeProviderCapabilities(config: dict[str, Any]) -> dict[str, Any]:
    profile = config.get("providerProfile") if isinstance(config.get("providerProfile"), dict) else {}
    configured = config.get("providerCapabilities")
    if not isinstance(configured, dict):
        configured = profile.get("capabilities") if isinstance(profile.get("capabilities"), dict) else {}
    explicit_image_input = configured.get("imageInput")
    if not isinstance(explicit_image_input, bool):
        explicit_image_input = _legacy_image_input_capability(profile)
    raw_mimes = configured.get("imageMimeTypes")
    image_mimes = _normalize_mime_types(raw_mimes) if isinstance(raw_mimes, list) else list(DEFAULT_IMAGE_INPUT_MIME_TYPES)
    return {
        "imageInput": explicit_image_input is True,
        "imageMimeTypes": image_mimes,
        "maxImageCount": _bounded_runtime_limit(
            configured.get("maxImageCount"), DEFAULT_MAX_IMAGE_INPUT_COUNT, MAX_PROVIDER_IMAGE_INPUT_COUNT
        ),
        "maxImageBytes": _bounded_runtime_limit(
            configured.get("maxImageBytes"), DEFAULT_MAX_IMAGE_INPUT_BYTES, MAX_PROVIDER_IMAGE_INPUT_BYTES
        ),
        "maxAggregateImageBytes": _bounded_runtime_limit(
            configured.get("maxAggregateImageBytes"),
            DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES,
            MAX_PROVIDER_AGGREGATE_IMAGE_INPUT_BYTES,
        ),
        "maxEncodedImageRequestBytes": _bounded_runtime_limit(
            configured.get("maxEncodedImageRequestBytes"),
            DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES,
            MAX_PROVIDER_ENCODED_IMAGE_REQUEST_BYTES,
        ),
    }


def createImageToolResultAttachment(
    *, filePath: str, mimeType: str, buffer: bytes
) -> dict[str, Any]:
    return {
        "type": "image",
        "filePath": filePath,
        "mimeType": mimeType,
        "byteLength": len(buffer),
        "sha256": sha256ForBuffer(buffer),
        "data": base64.b64encode(buffer).decode("ascii"),
    }


def sha256ForBuffer(buffer: bytes) -> str:
    return hashlib.sha256(buffer).hexdigest()


def imageAttachmentEligibility(
    *, capabilities: dict[str, Any], mimeType: str, byteLength: int
) -> dict[str, Any]:
    if not capabilities["imageInput"]:
        return {"eligible": False, "reason": "selected provider profile does not support image input"}
    if mimeType not in capabilities["imageMimeTypes"]:
        return {"eligible": False, "reason": f"selected provider profile does not support {mimeType}"}
    if byteLength > capabilities["maxImageBytes"]:
        return {
            "eligible": False,
            "reason": f"image exceeds provider byte limit ({byteLength} > {capabilities['maxImageBytes']})",
        }
    if byteLength > capabilities["maxAggregateImageBytes"]:
        return {
            "eligible": False,
            "reason": "image exceeds aggregate decoded byte limit "
            f"({byteLength} > {capabilities['maxAggregateImageBytes']})",
        }
    encoded_bytes = estimateEncodedImageRequestBytes(mimeType, byteLength)
    if encoded_bytes > capabilities["maxEncodedImageRequestBytes"]:
        return {
            "eligible": False,
            "reason": "image exceeds encoded request byte limit "
            f"({encoded_bytes} > {capabilities['maxEncodedImageRequestBytes']})",
        }
    return {"eligible": True}


def imageAttachmentToBlock(attachment: dict[str, Any]) -> dict[str, Any] | None:
    if attachment.get("type") != "image" or not attachment.get("data"):
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": attachment["mimeType"],
            "data": attachment["data"],
        },
    }


def filterImagesForProviderCapabilities(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    emitEvent: Any = None,
    sessionId: str | None = None,
) -> list[dict[str, Any]]:
    dropped = 0

    def on_drop(_detail: dict[str, Any]) -> None:
        nonlocal dropped
        dropped += 1

    admitted = admitImageMessages(
        messages=_textify_legacy_image_placeholders(messages),
        capabilities=resolveRuntimeProviderCapabilities(config),
        provider=config.get("provider") or (config.get("providerProfile") or {}).get("kind") or "openai",
        onDrop=on_drop,
    )
    if dropped and callable(emitEvent):
        emitEvent(
            "tool.attachment_dropped",
            {
                "session_id": sessionId,
                "type": "image",
                "count": dropped,
                "reason": "provider_image_capability_filter",
            },
        )
    return admitted["messages"]


def persistedImagePlaceholderText(mediaType: str, byteLength: int) -> str:
    return "\n".join(
        [
            _PERSISTED_IMAGE_PLACEHOLDER_PREFIX,
            f"MIME type: {mediaType}",
            f"Payload bytes: {byteLength}",
            "Raw image bytes are intentionally not retained in persistent model history.",
        ]
    )


def stripInMemoryImageData(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sanitizeMediaForPersistence(messages)


def _normalize_mime_types(values: list[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        normalized = value.strip().lower() if isinstance(value, str) else ""
        if normalized and _MIME_PATTERN.fullmatch(normalized) and normalized not in output:
            output.append(normalized)
        if len(output) >= 32:
            break
    return output


def _bounded_runtime_limit(value: Any, fallback: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return fallback
    return min(maximum, int(value))


def _legacy_image_input_capability(profile: dict[str, Any]) -> bool:
    for key in ("supportsVision", "supports_vision", "vision"):
        if isinstance(profile.get(key), bool):
            return profile[key]
    capabilities = profile.get("capabilities")
    if isinstance(capabilities, dict):
        for key in ("vision", "image_inputs", "imageInputs", "multimodal"):
            if isinstance(capabilities.get(key), bool):
                return capabilities[key]
    return False


def _textify_legacy_image_placeholders(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = copy.deepcopy(messages)
    for message in output:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, block in enumerate(content):
            if not isinstance(block, dict) or str(block.get("type") or "").lower() != "image":
                continue
            source = block.get("source") if isinstance(block.get("source"), dict) else block
            data = source.get("data") or source.get("data_base64") or source.get("base64")
            match = _CURRENT_TURN_PLACEHOLDER.fullmatch(str(data or ""))
            if not match:
                continue
            path = str(block.get("path") or block.get("file_path") or block.get("filePath") or match.group("path") or "image attachment")
            mime = str(source.get("media_type") or source.get("mime_type") or source.get("mime") or match.group("mime") or "")
            suffix = f" ({mime})" if mime else ""
            content[index] = {
                "type": "text",
                "text": f"[Image attachment from an earlier turn: {path}{suffix}. Image bytes are current-turn-only and were not replayed.]",
            }
    return output
