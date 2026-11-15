from __future__ import annotations

from typing import Any

from .media_persistence import sanitize_media_for_persistence


def redact_image_payloads(value: Any) -> Any:
    """Return the shared content-free persistence projection for media."""

    return sanitize_media_for_persistence(value, includeByteLength=False)


