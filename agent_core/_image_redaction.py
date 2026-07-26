from __future__ import annotations

from typing import Any

from ._media_persistence import sanitizeMediaForPersistence


def redactImagePayloads(value: Any) -> Any:
    """Return the shared content-free persistence projection for media."""

    return sanitizeMediaForPersistence(value, includeByteLength=False)


redact_image_payloads = redactImagePayloads
