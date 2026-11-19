"""The Interactions API over the runtime's own HTTP transport: standard library only."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from ..anthropic_async_transport import AnthropicAsyncHttpConnectionOwner as _AsyncHttpConnectionOwner
from ..error_evidence import ProviderWireError
from ..openai._wire import (
    OpenAiWireStream as _SseStream,
    _drain_error_body,
    _error_body_fields,
    _first_header,
    _retry_after_ms,
)
from .params import DEFAULT_GOOGLE_BASE_URL

# The caller races every wait against its own deadlines; this bounds a request nobody races.
_WIRE_TIMEOUT_SECONDS = 600.0
_RESERVED_HEADERS = frozenset({"x-goog-api-key", "authorization", "content-type", "accept", "host", "content-length"})


class GoogleWireClient:
    """``interactions.create(params)`` returns an async iterator of parsed SSE events.

    The key travels in the ``x-goog-api-key`` header and nowhere else: never in the
    URL, where a proxy or a log would record it. A profile's own headers cannot
    replace it or any other header the request depends on.
    """

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, default_headers: dict[str, Any] | None = None) -> None:
        self._api_key = str(api_key or "")
        self._base_url = str(base_url or "").strip().rstrip("/") or DEFAULT_GOOGLE_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._streams: set[Any] = set()
        self.interactions = SimpleNamespace(create=self._create_interaction)

    def close(self) -> None:
        for stream in list(self._streams):
            stream.close()
        self._streams.clear()

    def _headers(self) -> dict[str, str]:
        headers = {
            str(key): str(value)
            for key, value in self._default_headers.items()
            if value is not None and str(key).lower() not in _RESERVED_HEADERS
        }
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        if self._api_key:
            headers["x-goog-api-key"] = self._api_key
        return headers

    async def _create_interaction(self, params: dict[str, Any]) -> Any:
        body = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        owner = _AsyncHttpConnectionOwner(keep_alive=False)
        try:
            response = await owner.start(
                url=f"{self._base_url}/interactions",
                body=body,
                headers=self._headers(),
                timeout_seconds=_WIRE_TIMEOUT_SECONDS,
            )
        except BaseException:
            owner.close()
            raise
        content_type = str(response.headers.get("content-type") or "").lower()
        if response.status >= 400 or (content_type and "text/event-stream" not in content_type):
            body_text = await _drain_error_body(owner, response)
            code, error_type, message = _error_body_fields(body_text)
            status = response.status if response.status >= 400 else None
            if status is None and not (code or message):
                raise ValueError(f"Google transport received a malformed SSE response (content-type {content_type})")
            raise ProviderWireError(
                message or f"Google request failed with status {response.status}",
                status=status,
                body=body_text or None,
                code=code,
                errorType=error_type,
                requestId=_first_header(response, "x-request-id") or _first_header(response, "x-goog-request-id"),
                retryAfterMs=_retry_after_ms(response),
            )
        stream = _SseStream(owner=owner, response=response, on_close=self._streams.discard)
        self._streams.add(stream)
        return stream
