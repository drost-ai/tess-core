from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import math
import re
import struct
from typing import Any


IMAGE_PREFIX_BYTES = 64 * 1024
_IMAGE_MIME_PATTERN = re.compile(r"image/[a-z0-9.+-]{1,64}")


class ImageBudgetError(RuntimeError):
    def __init__(self, code: str, message: str, measurement: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.measurement = dict(measurement or {})


def estimate_encoded_image_request_bytes(mimeType: str, decodedBytes: int) -> int:
    """An upper estimate of the bytes an image adds to a request once base64-encoded."""
    mime_type = normalize_image_mime_type(mimeType)
    if isinstance(decodedBytes, bool) or not isinstance(decodedBytes, int) or decodedBytes < 0:
        raise ValueError("decoded image bytes must be a non-negative integer")
    base64_bytes = 4 * math.ceil(decodedBytes / 3)
    data_url_prefix = f"data:{mime_type};base64,"
    skeletons = [
        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": ""}},
        {"type": "input_image", "image_url": data_url_prefix},
        {"type": "image_url", "image_url": {"url": data_url_prefix}},
    ]
    overhead = max(len(_compact_json(value).encode("utf-8")) for value in skeletons)
    return overhead + base64_bytes + 1


def admit_image_messages(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    capabilities = values["capabilities"]
    provider = str(values.get("provider") or "openai")
    on_drop = values.get("onDrop")
    hasher = hashlib.sha256()
    count = decoded_bytes = encoded_bytes = input_tokens = image_index = 0
    images: list[dict[str, Any]] = []
    output_messages: list[dict[str, Any]] = []

    for raw_message in values.get("messages") or []:
        message = copy.deepcopy(raw_message)
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            output_messages.append(message)
            continue
        changed = False
        next_content: list[Any] = []
        for block in content:
            if not _is_image_block(block):
                next_content.append(block)
                continue
            image_index += 1
            measured = _measure_image_block(block, image_index)
            rejection = _per_image_rejection(capabilities, measured)
            if rejection:
                changed = True
                if callable(on_drop):
                    on_drop({
                        "mimeType": measured["mimeType"],
                        "decodedBytes": measured["decodedBytes"],
                        "reason": rejection,
                    })
                next_content.append(_image_omission_block(measured, rejection))
                continue
            next_count = count + 1
            next_decoded = decoded_bytes + measured["decodedBytes"]
            next_encoded = encoded_bytes + measured["encodedRequestBytes"]
            if next_count > capabilities["maxImageCount"]:
                raise _aggregate_error(
                    "image_count_limit", measured,
                    count=next_count, limit=capabilities["maxImageCount"],
                    decodedBytes=next_decoded, encodedRequestBytes=next_encoded,
                )
            if next_decoded > capabilities["maxAggregateImageBytes"]:
                raise _aggregate_error(
                    "aggregate_decoded_image_limit", measured,
                    count=next_count, decodedBytes=next_decoded,
                    limit=capabilities["maxAggregateImageBytes"], encodedRequestBytes=next_encoded,
                )
            if next_encoded > capabilities["maxEncodedImageRequestBytes"]:
                raise _aggregate_error(
                    "aggregate_encoded_image_limit", measured,
                    count=next_count, decodedBytes=next_decoded,
                    encodedRequestBytes=next_encoded,
                    limit=capabilities["maxEncodedImageRequestBytes"],
                )
            count, decoded_bytes, encoded_bytes = next_count, next_decoded, next_encoded
            estimated = _estimate_measured_image_tokens(measured, provider)
            input_tokens += estimated
            images.append(_item_measurement(measured, estimated))
            _update_fingerprint(hasher, measured)
            next_content.append(block)
        if changed:
            message["content"] = next_content
        output_messages.append(message)
    return {
        "messages": output_messages,
        "summary": {
            "count": count,
            "decodedBytes": decoded_bytes,
            "encodedRequestBytes": encoded_bytes,
            "estimatedInputTokens": input_tokens,
            "fingerprint": hasher.hexdigest(),
            "images": images,
        },
    }


def measure_admitted_image_set(messages: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    hasher = hashlib.sha256()
    count = decoded_bytes = encoded_bytes = input_tokens = 0
    images: list[dict[str, Any]] = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not _is_image_block(block):
                continue
            measured = _measure_image_block(block, count + 1)
            count += 1
            decoded_bytes += measured["decodedBytes"]
            encoded_bytes += measured["encodedRequestBytes"]
            estimated = _estimate_measured_image_tokens(measured, provider)
            input_tokens += estimated
            images.append(_item_measurement(measured, estimated))
            _update_fingerprint(hasher, measured)
    return {
        "count": count,
        "decodedBytes": decoded_bytes,
        "encodedRequestBytes": encoded_bytes,
        "estimatedInputTokens": input_tokens,
        "fingerprint": hasher.hexdigest(),
        "images": images,
    }


class _image_budget_reservation:
    def __init__(self, capabilities: dict[str, Any], reservation_order: list[int]) -> None:
        self.capabilities = capabilities
        self.order = list(dict.fromkeys(value for value in reservation_order if isinstance(value, int)))
        self.expected = set(self.order)
        self.completed: set[int] = set()
        self.pending: set[int] = set()
        self.cursor = 0
        self.active: int | None = None
        self.count = self.decoded_bytes = self.encoded_bytes = 0
        self.condition = asyncio.Condition()

    async def reserve(self, input: dict[str, Any], priority: int | None = None) -> dict[str, Any]:
        async with self.condition:
            if priority is None or priority not in self.expected:
                return self._reserve_now(input)
            if priority in self.completed or priority in self.pending or self.active == priority:
                return {"accepted": False, "reason": "duplicate image attachment reservation"}
            self.pending.add(priority)
            try:
                await self.condition.wait_for(lambda: self.active is None and self._current() == priority)
                self.active = priority
                return self._reserve_now(input)
            finally:
                self.pending.discard(priority)

    async def complete(self, priority: int) -> None:
        async with self.condition:
            if priority not in self.expected or priority in self.completed:
                return
            self.completed.add(priority)
            if self.active == priority:
                self.active = None
            while self.cursor < len(self.order) and self.order[self.cursor] in self.completed:
                self.cursor += 1
            self.condition.notify_all()

    def _current(self) -> int | None:
        return self.order[self.cursor] if self.cursor < len(self.order) else None

    def _reserve_now(self, input: dict[str, Any]) -> dict[str, Any]:
        try:
            mime_type = normalize_image_mime_type(input.get("mimeType"))
            decoded = input.get("decodedBytes")
            predicted = estimate_encoded_image_request_bytes(mime_type, decoded)
        except Exception as error:
            return {"accepted": False, "reason": _bounded_error_message(error)}
        rejection = _per_file_reservation_rejection(
            self.capabilities,
            mimeType=mime_type,
            decodedBytes=decoded,
            encodedRequestBytes=predicted,
            nextCount=self.count + 1,
            nextDecodedBytes=self.decoded_bytes + decoded,
            nextEncodedRequestBytes=self.encoded_bytes + predicted,
        )
        if rejection:
            return {"accepted": False, "reason": rejection}
        self.count += 1
        self.decoded_bytes += decoded
        self.encoded_bytes += predicted
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self.count -= 1
            self.decoded_bytes -= decoded
            self.encoded_bytes -= predicted

        return {"accepted": True, "encodedRequestBytes": predicted, "release": release}


def create_image_budget_reservation(
    capabilities: dict[str, Any], reservationOrder: list[int] | None = None
) -> _image_budget_reservation:
    return _image_budget_reservation(capabilities, reservationOrder or [])


def assert_image_context_capacity(measurement: dict[str, Any], budget: dict[str, Any]) -> None:
    if not budget.get("insufficient") or not measurement.get("imageCount"):
        return
    raise ImageBudgetError(
        "image_context_limit",
        f"The admitted {measurement['imageCount']} image(s) reserve "
        f"{measurement.get('imageInputTokens', 0)} input tokens and leave only "
        f"{budget.get('remainingOutputTokens')} output tokens; at least "
        f"{budget.get('minUsefulOutputTokens')} are required.",
        {
            "imageCount": measurement["imageCount"],
            "imageDecodedBytes": measurement.get("imageDecodedBytes", 0),
            "imageEncodedRequestBytes": measurement.get("imageEncodedRequestBytes", 0),
            "imageInputTokens": measurement.get("imageInputTokens", 0),
            "imageSetFingerprint": measurement.get("imageSetFingerprint", ""),
            "images": measurement.get("imageMeasurements", []),
            "estimatedInputTokens": measurement.get("inputTokens"),
            "maxContextLength": budget.get("maxContextLength"),
            "safetyMarginTokens": budget.get("safetyMarginTokens"),
            "remainingOutputTokens": budget.get("remainingOutputTokens"),
            "minUsefulOutputTokens": budget.get("minUsefulOutputTokens"),
        },
    )


def normalize_image_mime_type(value: Any) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _IMAGE_MIME_PATTERN.fullmatch(normalized):
        raise ImageBudgetError("invalid_image_mime", "Image MIME type is invalid or too long.")
    return normalized


def measure_canonical_base64(value: Any, index: int = 1) -> int:
    if not isinstance(value, str) or not value or len(value) % 4:
        raise _invalid_base64(index)
    padding = 2 if value.endswith("==") else 1 if value.endswith("=") else 0
    data_end = len(value) - padding
    for offset, character in enumerate(value):
        valid = character.isascii() and (
            character.isalnum() or character in "+/" or (character == "=" and offset >= data_end)
        )
        if not valid:
            raise _invalid_base64(index)
    if padding == 2 and (_base64_sextet(value[data_end - 1]) & 0x0F):
        raise _invalid_base64(index)
    if padding == 1 and (_base64_sextet(value[data_end - 1]) & 0x03):
        raise _invalid_base64(index)
    return len(value) // 4 * 3 - padding


def _measure_image_block(block: dict[str, Any], index: int) -> dict[str, Any]:
    source = block.get("source") if isinstance(block.get("source"), dict) else block
    mime_type = normalize_image_mime_type(
        source.get("media_type") or source.get("mime_type") or source.get("mimeType") or source.get("mime")
    )
    data = source.get("data") or source.get("data_base64") or source.get("base64")
    decoded = measure_canonical_base64(data, index)
    return {
        "index": index,
        "mimeType": mime_type,
        "decodedBytes": decoded,
        "encodedRequestBytes": estimate_encoded_image_request_bytes(mime_type, decoded),
        "data": data,
        **_read_image_dimensions(mime_type, data),
    }


def _read_image_dimensions(mime_type: str, data: str) -> dict[str, int]:
    prefix_characters = min(len(data), 4 * math.ceil(IMAGE_PREFIX_BYTES / 3))
    try:
        prefix = base64.b64decode(data[:prefix_characters], validate=False)
    except Exception:
        return {}
    if mime_type == "image/png" and len(prefix) >= 24 and prefix[:8] == b"\x89PNG\r\n\x1a\n":
        return _valid_dimensions(struct.unpack(">I", prefix[16:20])[0], struct.unpack(">I", prefix[20:24])[0])
    if mime_type == "image/gif" and len(prefix) >= 10 and prefix[:6] in {b"GIF87a", b"GIF89a"}:
        return _valid_dimensions(*struct.unpack("<HH", prefix[6:10]))
    if mime_type == "image/webp" and len(prefix) >= 30 and prefix[:4] == b"RIFF" and prefix[8:16] == b"WEBPVP8X":
        width = 1 + int.from_bytes(prefix[24:27], "little")
        height = 1 + int.from_bytes(prefix[27:30], "little")
        return _valid_dimensions(width, height)
    if mime_type == "image/jpeg":
        return _read_jpeg_dimensions(prefix)
    return {}


def _read_jpeg_dimensions(prefix: bytes) -> dict[str, int]:
    if len(prefix) < 4 or prefix[:2] != b"\xff\xd8":
        return {}
    offset = 2
    while offset + 8 < len(prefix):
        if prefix[offset] != 0xFF:
            offset += 1
            continue
        marker = prefix[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(prefix):
            break
        length = int.from_bytes(prefix[offset:offset + 2], "big")
        if length < 2 or offset + length > len(prefix):
            break
        if marker in {*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)}:
            return _valid_dimensions(
                int.from_bytes(prefix[offset + 5:offset + 7], "big"),
                int.from_bytes(prefix[offset + 3:offset + 5], "big"),
            )
        offset += length
    return {}


def _valid_dimensions(width: int, height: int) -> dict[str, int]:
    return {"width": width, "height": height} if 0 < width <= 1_000_000 and 0 < height <= 1_000_000 else {}


def _estimate_measured_image_tokens(image: dict[str, Any], provider: str) -> int:
    width, height = image.get("width"), image.get("height")
    if provider == "google":
        # Gemini counts 258 tokens for an image up to 384 pixels a side, and
        # 258 for each 768-pixel tile of a larger one.
        if width and height:
            if width <= 384 and height <= 384:
                return 258
            return 258 * math.ceil(width / 768) * math.ceil(height / 768)
        return max(258, 258 * math.ceil(image["decodedBytes"] / (512 * 1024)))
    if width and height:
        if provider == "anthropic":
            return max(256, math.ceil(width * height / 750))
        longest = max(width, height)
        if longest > 2048:
            width, height = width * 2048 / longest, height * 2048 / longest
        shortest = min(width, height)
        if shortest > 768:
            width, height = width * 768 / shortest, height * 768 / shortest
        return 85 + 170 * math.ceil(width / 512) * math.ceil(height / 512)
    if provider == "anthropic":
        return max(256, math.ceil(image["decodedBytes"] / 512))
    return max(255, 85 + 170 * math.ceil(image["decodedBytes"] / (512 * 1024)))


def _per_image_rejection(capabilities: dict[str, Any], image: dict[str, Any]) -> str | None:
    if not capabilities["imageInput"]:
        return "selected provider profile does not support image input"
    if image["mimeType"] not in capabilities["imageMimeTypes"]:
        return f"selected provider profile does not support {image['mimeType']}"
    if image["decodedBytes"] > capabilities["maxImageBytes"]:
        return f"image exceeds provider byte limit ({image['decodedBytes']} > {capabilities['maxImageBytes']})"
    return None


def _per_file_reservation_rejection(capabilities: dict[str, Any], **image: Any) -> str | None:
    rejection = _per_image_rejection(capabilities, image)
    if rejection:
        return rejection
    if image["nextCount"] > capabilities["maxImageCount"]:
        return f"image count limit exceeded ({image['nextCount']} > {capabilities['maxImageCount']})"
    if image["nextDecodedBytes"] > capabilities["maxAggregateImageBytes"]:
        return "aggregate decoded image byte limit exceeded " f"({image['nextDecodedBytes']} > {capabilities['maxAggregateImageBytes']})"
    if image["nextEncodedRequestBytes"] > capabilities["maxEncodedImageRequestBytes"]:
        return "aggregate encoded image request byte limit exceeded " f"({image['nextEncodedRequestBytes']} > {capabilities['maxEncodedImageRequestBytes']})"
    return None


def _aggregate_error(code: str, image: dict[str, Any], **measurement: int) -> ImageBudgetError:
    key = "count" if code == "image_count_limit" else "decodedBytes" if code == "aggregate_decoded_image_limit" else "encodedRequestBytes"
    label = "image count" if code == "image_count_limit" else "aggregate decoded image bytes" if code == "aggregate_decoded_image_limit" else "aggregate encoded image request bytes"
    return ImageBudgetError(
        code,
        f"Image {image['index']} would exceed the {label} limit ({measurement[key]} > {measurement['limit']}).",
        {"imageIndex": image["index"], **measurement},
    )


def _image_omission_block(image: dict[str, Any], reason: str) -> dict[str, str]:
    return {"type": "text", "text": "\n".join([
        "Image input omitted.", f"Reason: {reason}.",
        f"MIME type: {image['mimeType']}", f"Payload bytes: {image['decodedBytes']}",
    ])}


def _item_measurement(image: dict[str, Any], tokens: int) -> dict[str, Any]:
    return {key: image[key] for key in ("index", "mimeType", "decodedBytes", "encodedRequestBytes")} | {"estimatedInputTokens": tokens}


def _update_fingerprint(hasher: Any, image: dict[str, Any]) -> None:
    hasher.update(str(image["index"]).encode("ascii") + b"\0")
    hasher.update(image["mimeType"].encode("ascii") + b"\0")
    hasher.update(image["data"].encode("ascii") + b"\0")


def _is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and str(block.get("type") or "").strip().lower() == "image"


def _invalid_base64(index: int) -> ImageBudgetError:
    return ImageBudgetError(
        "invalid_image_base64", f"Image {index} contains invalid or non-canonical base64 data.", {"imageIndex": index}
    )


def _base64_sextet(character: str) -> int:
    if "A" <= character <= "Z":
        return ord(character) - ord("A")
    if "a" <= character <= "z":
        return ord(character) - ord("a") + 26
    if "0" <= character <= "9":
        return ord(character) - ord("0") + 52
    return 62 if character == "+" else 63


def _bounded_error_message(error: BaseException) -> str:
    message = str(error)
    return message if len(message) <= 512 else f"{message[:509]}..."


def _compact_json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
