from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from ..anthropic_async_transport import (
    AnthropicAsyncHttpConnectionOwner as _AsyncHttpConnectionOwner,
    AsyncAnthropicHttpResponse as _AsyncHttpResponse,
)
from ..error_evidence import ProviderWireError

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# Wall-clock ceilings live with the caller (_race_openai_activity cancels the
# in-flight task); this only bounds a request nobody is racing.
_WIRE_TIMEOUT_SECONDS = 600.0
_ERROR_BODY_BYTE_LIMIT = 65_536
_EVENT_PREVIEW_LIMIT = 512

_TRANSPORT_FAILURES = (
    ConnectionError,
    OSError,
    EOFError,
    asyncio.IncompleteReadError,
)


class OpenAiWireClient:
    """Stdlib OpenAI streaming client over the shared asyncio HTTP transport.

    Duck-types the surface the request paths already consume from the SDK
    adapter and injected factories: ``responses.create(params)`` and
    ``chat.completions.create(params)`` return async-iterable SSE streams of
    plain event dicts; ``capabilities`` and ``close`` round out the contract.
    HTTP failures raise :class:`ProviderWireError` so classification works
    without the SDK installed.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, Any] | None = None,
    ) -> None:
        self._api_key = str(api_key or "")
        trimmed = str(base_url or "").strip().rstrip("/")
        self._base_url = trimmed or DEFAULT_OPENAI_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._streams: set[OpenAiWireStream] = set()
        self.responses = SimpleNamespace(create=self._create_responses)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_chat_completions)
        )
        self.capabilities: dict[str, Any] = {}

    def close(self) -> None:
        for stream in list(self._streams):
            stream.close()
        self._streams.clear()

    async def _create_responses(self, params: dict[str, Any]) -> OpenAiWireStream:
        return await self._create("/responses", params)

    async def _create_chat_completions(self, params: dict[str, Any]) -> OpenAiWireStream:
        return await self._create("/chat/completions", params)

    async def _create(self, path: str, params: dict[str, Any]) -> OpenAiWireStream:
        url, body, headers = self._prepare_request(path, params)
        owner = _AsyncHttpConnectionOwner(keep_alive=False)
        try:
            response = await owner.start(
                url=url,
                body=body,
                headers=headers,
                timeout_seconds=_WIRE_TIMEOUT_SECONDS,
            )
        except BaseException:
            owner.close()
            raise
        if response.status >= 400:
            raise await _http_failure(owner, response)
        content_type = response.headers.get("content-type")
        # A present-but-wrong content type is a real error shape (JSON error
        # envelopes, HTML interstitials). A missing header is not: the codex
        # backend streams valid SSE with no content-type at all, so let the
        # frame parser be the judge there.
        if content_type is not None and "text/event-stream" not in str(content_type).lower():
            raise await _content_type_failure(owner, response, str(content_type))
        stream = OpenAiWireStream(
            owner=owner, response=response, on_close=self._streams.discard
        )
        self._streams.add(stream)
        return stream

    def _prepare_request(
        self, path: str, params: dict[str, Any]
    ) -> tuple[str, bytes, dict[str, str]]:
        wire_params = dict(params or {})
        extra_headers = wire_params.pop("extra_headers", None)
        extra_query = wire_params.pop("extra_query", None)
        extra_body = wire_params.pop("extra_body", None)
        wire_params.pop("timeout", None)
        if isinstance(extra_body, dict):
            wire_params.update(extra_body)
        url = f"{self._base_url}{path}"
        if isinstance(extra_query, dict) and extra_query:
            url = f"{url}?{urlencode({str(key): str(value) for key, value in extra_query.items()})}"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        for source in (self._default_headers, extra_headers or {}):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if value is None:
                    continue
                _set_header(headers, str(key), str(value))
        if self._api_key and not _has_header(headers, "authorization"):
            headers["Authorization"] = f"Bearer {self._api_key}"
        return url, json.dumps(wire_params).encode("utf-8"), headers


class OpenAiWireStream:
    """Async iterator over parsed SSE event payloads for one wire request.

    Owns the connection: iteration exhaustion, ``close()``, errors, and task
    cancellation all release the socket.
    """

    def __init__(
        self,
        *,
        owner: _AsyncHttpConnectionOwner,
        response: _AsyncHttpResponse,
        on_close: Any = None,
    ) -> None:
        self._owner = owner
        self._response = response
        self._on_close = on_close
        self._frame_lines: list[str] = []
        self._done = False

    def __aiter__(self) -> OpenAiWireStream:
        return self

    async def __anext__(self) -> Any:
        while True:
            if self._done:
                raise StopAsyncIteration
            try:
                line = await self._response.readline()
            except BaseException:
                self.close()
                raise
            if line == b"":
                event = self._flush_frame()
                self.close()
                if event is not None:
                    return event
                raise StopAsyncIteration
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text == "":
                event = self._flush_frame()
                if event is not None:
                    return event
                continue
            self._frame_lines.append(text)

    def close(self) -> None:
        self._done = True
        self._owner.close()
        on_close = self._on_close
        self._on_close = None
        if callable(on_close):
            on_close(self)

    def _flush_frame(self) -> Any:
        lines, self._frame_lines = self._frame_lines, []
        data_lines = [
            line[5:].lstrip() for line in lines if line.startswith("data:")
        ]
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        if data == "[DONE]":
            self._done = True
            return None
        try:
            return json.loads(data)
        except Exception as exc:
            self.close()
            preview = data[:_EVENT_PREVIEW_LIMIT]
            raise ValueError(
                f"Failed to parse streamed provider event: {preview}"
            ) from exc


async def _http_failure(
    owner: _AsyncHttpConnectionOwner, response: _AsyncHttpResponse
) -> ProviderWireError:
    body_text = await _drain_error_body(owner, response)
    code, error_type, message = _error_body_fields(body_text)
    return ProviderWireError(
        message or f"OpenAI request failed with status {response.status}",
        status=response.status,
        body=body_text or None,
        code=code,
        errorType=error_type,
        requestId=_first_header(response, "x-request-id"),
        retryAfterMs=_retry_after_ms(response),
    )


async def _content_type_failure(
    owner: _AsyncHttpConnectionOwner,
    response: _AsyncHttpResponse,
    content_type: str,
) -> Exception:
    body_text = await _drain_error_body(owner, response)
    code, error_type, message = _error_body_fields(body_text)
    if code or error_type or message:
        return ProviderWireError(
            message or "OpenAI request failed",
            body=body_text or None,
            code=code,
            errorType=error_type,
            requestId=_first_header(response, "x-request-id"),
        )
    return ValueError(
        "OpenAI transport received a malformed SSE response "
        f"(content-type {content_type or 'unknown'})"
    )


async def _drain_error_body(
    owner: _AsyncHttpConnectionOwner, response: _AsyncHttpResponse
) -> str:
    try:
        raw = await response.read()
    except _TRANSPORT_FAILURES:
        raw = b""
    finally:
        owner.close()
    return raw[:_ERROR_BODY_BYTE_LIMIT].decode("utf-8", errors="replace")


def _error_body_fields(body_text: str) -> tuple[str | None, str | None, str | None]:
    try:
        parsed = json.loads(body_text)
    except Exception:
        return None, None, None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return None, None, None
    return (
        _optional_text(error.get("code")),
        _optional_text(error.get("type")),
        _optional_text(error.get("message")),
    )


def _retry_after_ms(response: _AsyncHttpResponse) -> int | None:
    from .._core import retry_after_ms_from_headers

    return retry_after_ms_from_headers(response.headers)


def _first_header(response: _AsyncHttpResponse, name: str) -> str | None:
    value = response.headers.get(name)
    if value is None:
        return None
    return str(value).split(",")[0].strip() or None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _set_header(headers: dict[str, str], key: str, value: str) -> None:
    lowered = key.lower()
    for existing in list(headers):
        if existing.lower() == lowered:
            del headers[existing]
    headers[key] = value


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(key.lower() == name for key in headers)
