from __future__ import annotations

import asyncio
import http.client
import inspect
import math
import socket
import time
import zlib
from typing import Any
from urllib.parse import urljoin, urlparse


WEB_FETCH_COMPRESSED_MAX_BYTES = 8 * 1024 * 1024
WEB_FETCH_DECODED_MAX_BYTES = 16 * 1024 * 1024
WEB_FETCH_CONNECT_TIMEOUT_MS = 10_000
WEB_FETCH_HEADERS_TIMEOUT_MS = 10_000
WEB_FETCH_BODY_IDLE_TIMEOUT_MS = 10_000
WEB_FETCH_TOTAL_TIMEOUT_MS = 30_000
WEB_FETCH_MAX_REDIRECTS = 5
WEB_FETCH_MAX_HEADER_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class WebFetchPolicyError(RuntimeError):
    pass


async def boundedWebFetch(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    requested_url = _validate_http_url(values["url"])
    compressed_limit = _positive(values.get("compressedMaxBytes"), WEB_FETCH_COMPRESSED_MAX_BYTES)
    decoded_limit = _positive(values.get("decodedMaxBytes"), WEB_FETCH_DECODED_MAX_BYTES)
    connect_timeout = _positive(values.get("connectTimeoutMs"), WEB_FETCH_CONNECT_TIMEOUT_MS)
    headers_timeout = _positive(values.get("headersTimeoutMs"), WEB_FETCH_HEADERS_TIMEOUT_MS)
    body_idle_timeout = _positive(values.get("bodyIdleTimeoutMs"), WEB_FETCH_BODY_IDLE_TIMEOUT_MS)
    total_timeout = _positive(values.get("totalTimeoutMs"), WEB_FETCH_TOTAL_TIMEOUT_MS)
    max_redirects = _nonnegative(values.get("maxRedirects"), WEB_FETCH_MAX_REDIRECTS)
    max_headers = _positive(values.get("maxHeaderBytes"), WEB_FETCH_MAX_HEADER_BYTES)
    accepted = values.get("acceptedStatusCodes")
    accepted_statuses = set(accepted) if isinstance(accepted, (list, tuple, set)) else None
    deadline = time.monotonic() + total_timeout / 1000
    redirects: list[dict[str, Any]] = []
    current = requested_url

    while True:
        if _aborted(values.get("abortSignal")):
            raise asyncio.CancelledError("WebFetch aborted")
        authorize = values.get("authorizeHop")
        if callable(authorize):
            pending = authorize(current)
            if inspect.isawaitable(pending):
                await pending
        remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            return _empty_result(
                "timeout", requested_url, current, redirects,
                reason="total_timeout", timeoutPhase="total", truncated=True,
            )
        hop = await asyncio.to_thread(
            _request_hop,
            current,
            values.get("headers") or {},
            compressed_limit,
            decoded_limit,
            min(connect_timeout, remaining_ms),
            min(headers_timeout, remaining_ms),
            min(body_idle_timeout, remaining_ms),
            deadline,
            max_headers,
            accepted_statuses,
        )
        if hop["kind"] == "redirect":
            location = hop.get("location")
            status = hop["statusCode"]
            if not location:
                return {
                    **_empty_result("http_error", requested_url, current, redirects),
                    "statusCode": status,
                }
            if len(redirects) >= max_redirects:
                return _empty_result(
                    "refused", requested_url, current, redirects, reason="redirect_limit"
                )
            try:
                next_url = _validate_http_url(urljoin(current, location))
            except Exception:
                return _empty_result(
                    "refused", requested_url, current, redirects, reason="invalid_redirect"
                )
            redirects.append({"statusCode": status, "from": current, "to": next_url})
            current = next_url
            continue
        if hop["kind"] in {"refused", "http_error", "timeout"}:
            return {
                **_empty_result(
                    hop["kind"], requested_url, current, redirects,
                    reason=hop.get("reason"),
                    timeoutPhase=hop.get("timeoutPhase"),
                    truncated=bool(hop.get("truncated")),
                ),
                **({"statusCode": hop["statusCode"]} if hop.get("statusCode") is not None else {}),
                **({"contentType": hop["contentType"]} if hop.get("contentType") else {}),
            }
        return {
            "outcome": "partial" if hop["truncated"] else "ok",
            "requestedUrl": requested_url,
            "finalUrl": current,
            "statusCode": hop["statusCode"],
            **({"contentType": hop["contentType"]} if hop.get("contentType") else {}),
            "body": hop["body"],
            "compressedBytes": hop["compressedBytes"],
            "decodedBytes": hop["decodedBytes"],
            "redirects": redirects,
            "truncated": hop["truncated"],
            **({"reason": hop["reason"]} if hop.get("reason") else {}),
            **({"timeoutPhase": hop["timeoutPhase"]} if hop.get("timeoutPhase") else {}),
        }


def _request_hop(
    url: str,
    headers: dict[str, str],
    compressed_limit: int,
    decoded_limit: int,
    connect_timeout_ms: int,
    headers_timeout_ms: int,
    body_idle_timeout_ms: int,
    deadline: float,
    max_header_bytes: int,
    accepted_statuses: set[int] | None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = connection_class(parsed.hostname, port, timeout=max(0.001, connect_timeout_ms / 1000))
    phase = "connect"
    try:
        connection.connect()
        if connection.sock:
            connection.sock.settimeout(max(0.001, headers_timeout_ms / 1000))
        phase = "headers"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        request_headers = {
            "user-agent": "tess/0.1",
            "accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.5",
            "accept-encoding": "gzip, deflate",
            **{str(key): str(value) for key, value in headers.items()},
        }
        connection.request("GET", path, headers=request_headers)
        response = connection.getresponse()
        header_items = response.getheaders()
        if _header_bytes(header_items) > max_header_bytes:
            return {"kind": "refused", "reason": "header_limit"}
        response_headers = {name.lower(): value for name, value in header_items}
        status = int(response.status)
        content_type = response_headers.get("content-type")
        if status in _REDIRECT_STATUSES and not (accepted_statuses and status in accepted_statuses):
            return {
                "kind": "redirect", "statusCode": status,
                "location": response_headers.get("location"),
            }
        status_accepted = status in accepted_statuses if accepted_statuses is not None else 200 <= status < 300
        if not status_accepted:
            return {"kind": "http_error", "statusCode": status, "contentType": content_type}
        declared = _content_length(response_headers.get("content-length"))
        if declared is not None and declared > compressed_limit:
            return {
                "kind": "refused", "reason": "declared_compressed_limit",
                "statusCode": status, "contentType": content_type,
            }
        encoding = (response_headers.get("content-encoding") or "identity").strip().lower()
        decoder = _decoder(encoding)
        if encoding != "identity" and decoder is None:
            return {
                "kind": "refused", "reason": "unsupported_content_encoding",
                "statusCode": status, "contentType": content_type,
            }
        return _read_body(
            response, connection, status, content_type, decoder,
            compressed_limit, decoded_limit, body_idle_timeout_ms, deadline,
        )
    except http.client.LineTooLong:
        return {"kind": "refused", "reason": "header_limit"}
    except (TimeoutError, socket.timeout):
        if time.monotonic() >= deadline:
            return {
                "kind": "timeout", "reason": "total_timeout", "timeoutPhase": "total",
                "truncated": True,
            }
        timeout_phase = "connect" if phase == "connect" else "headers"
        reason = "connect_timeout" if timeout_phase == "connect" else "headers_timeout"
        return {
            "kind": "timeout", "reason": reason, "timeoutPhase": timeout_phase,
            "truncated": True,
        }
    finally:
        connection.close()


def _read_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    status: int,
    content_type: str | None,
    decoder: Any,
    compressed_limit: int,
    decoded_limit: int,
    body_idle_timeout_ms: int,
    deadline: float,
) -> dict[str, Any]:
    chunks: list[bytes] = []
    compressed_bytes = decoded_bytes = admitted_decoded = 0
    reason = timeout_phase = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason, timeout_phase = "total_timeout", "total"
            break
        active_socket = _response_socket(response, connection)
        if active_socket:
            active_socket.settimeout(min(body_idle_timeout_ms / 1000, remaining))
        try:
            chunk = response.read1(_READ_CHUNK_BYTES)
        except (TimeoutError, socket.timeout):
            if time.monotonic() >= deadline:
                reason, timeout_phase = "total_timeout", "total"
            else:
                reason, timeout_phase = "body_idle_timeout", "body_idle"
            break
        if not chunk:
            break
        compressed_bytes += len(chunk)
        compressed_remaining = max(0, compressed_limit - (compressed_bytes - len(chunk)))
        admitted_compressed = chunk[:compressed_remaining]
        decoded = _decode(decoder, admitted_compressed)
        decoded_bytes += len(decoded)
        decoded_remaining = max(0, decoded_limit - admitted_decoded)
        if decoded_remaining:
            selected = decoded[:decoded_remaining]
            chunks.append(selected)
            admitted_decoded += len(selected)
        if len(decoded) > decoded_remaining:
            reason = "decoded_limit"
            break
        if len(admitted_compressed) < len(chunk):
            reason = "compressed_limit"
            break
    if reason is None:
        flushed = _flush(decoder)
        decoded_bytes += len(flushed)
        remaining = max(0, decoded_limit - admitted_decoded)
        if remaining:
            selected = flushed[:remaining]
            chunks.append(selected)
            admitted_decoded += len(selected)
        if len(flushed) > remaining:
            reason = "decoded_limit"
    body = b"".join(chunks).decode("utf-8", errors="replace")
    return {
        "kind": "body",
        "statusCode": status,
        "contentType": content_type,
        "body": body,
        "compressedBytes": compressed_bytes,
        "decodedBytes": decoded_bytes,
        "truncated": reason is not None,
        "reason": reason,
        "timeoutPhase": timeout_phase,
    }


def _decoder(encoding: str) -> Any:
    if encoding == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj()
    return None


def _response_socket(
    response: http.client.HTTPResponse, connection: http.client.HTTPConnection
) -> Any:
    if connection.sock:
        return connection.sock
    raw = getattr(getattr(response, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


def _decode(decoder: Any, value: bytes) -> bytes:
    return decoder.decompress(value) if decoder is not None else value


def _flush(decoder: Any) -> bytes:
    return decoder.flush() if decoder is not None else b""


def _validate_http_url(value: Any) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise WebFetchPolicyError("WebFetch permits only HTTP(S) URLs without embedded credentials.")
    return parsed.geturl()


def _header_bytes(values: list[tuple[str, str]]) -> int:
    return sum(len(name.encode()) + len(value.encode()) + 4 for name, value in values)


def _content_length(value: str | None) -> int | None:
    if not value or not value.isdigit():
        return None
    return int(value)


def _empty_result(
    outcome: str,
    requested_url: str,
    final_url: str,
    redirects: list[dict[str, Any]],
    **details: Any,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "requestedUrl": requested_url,
        "finalUrl": final_url,
        "body": "",
        "compressedBytes": 0,
        "decodedBytes": 0,
        "redirects": redirects,
        "truncated": bool(details.pop("truncated", False)),
        **{key: value for key, value in details.items() if value is not None},
    }


def _positive(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return fallback
    return int(value)


def _nonnegative(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return fallback
    return int(value)


def _aborted(signal_value: Any) -> bool:
    if isinstance(signal_value, asyncio.Event):
        return signal_value.is_set()
    return bool(getattr(signal_value, "aborted", False))


bounded_web_fetch = boundedWebFetch
