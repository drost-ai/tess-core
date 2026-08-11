from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import ssl
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass


TransportObserver = Callable[[str, dict[str, Any]], None]


class _Headers(dict[str, str]):
    def get(self, key: str, default: Any = None) -> Any:
        return super().get(str(key).lower(), default)


class _async_response_body:
    def __init__(self, reader: asyncio.StreamReader, headers: _Headers) -> None:
        self._reader = reader
        self._buffer = bytearray()
        self._done = False
        self._chunked = "chunked" in str(headers.get("transfer-encoding", "")).lower()
        length = headers.get("content-length")
        self._remaining = int(length) if isinstance(length, str) and length.isdigit() else None

    @property
    def complete(self) -> bool:
        return self._done and not self._buffer

    async def read(self) -> bytes:
        while not self._done:
            await self._fill()
        value = bytes(self._buffer)
        self._buffer.clear()
        return value

    async def readline(self) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                value = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return value
            if self._done:
                value = bytes(self._buffer)
                self._buffer.clear()
                return value
            await self._fill()

    async def _fill(self) -> None:
        if self._done:
            return
        if self._chunked:
            size_line = await self._reader.readline()
            if not size_line:
                raise EOFError("Anthropic response ended inside a chunked body")
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise OSError("Invalid chunk size in Anthropic response") from exc
            if size == 0:
                while True:
                    trailer = await self._reader.readline()
                    if trailer in {b"", b"\r\n", b"\n"}:
                        break
                self._done = True
                return
            self._buffer.extend(await self._reader.readexactly(size))
            if await self._reader.readexactly(2) != b"\r\n":
                raise OSError("Invalid chunk terminator in Anthropic response")
            return
        if self._remaining is not None:
            if self._remaining == 0:
                self._done = True
                return
            value = await self._reader.read(min(65_536, self._remaining))
            if not value:
                raise EOFError("Anthropic response ended before Content-Length bytes arrived")
            self._remaining -= len(value)
            self._buffer.extend(value)
            if self._remaining == 0:
                self._done = True
            return
        value = await self._reader.read(65_536)
        if value:
            self._buffer.extend(value)
        else:
            self._done = True


class AsyncAnthropicHttpResponse:
    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        status: int,
        reason: str,
        version: str,
        headers: _Headers,
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers = headers
        self.will_close = (
            str(headers.get("connection", "")).lower() == "close"
            or version.upper() == "HTTP/1.0"
            or (headers.get("content-length") is None and "chunked" not in str(headers.get("transfer-encoding", "")).lower())
        )
        self._body = _async_response_body(reader, headers)

    @property
    def complete(self) -> bool:
        return self._body.complete

    async def read(self) -> bytes:
        return await self._body.read()

    async def readline(self) -> bytes:
        return await self._body.readline()


class AnthropicAsyncHttpConnectionOwner:
    """Turn-scoped asyncio HTTP/1.1 transport with proxy and cancel ownership."""

    def __init__(self, *, keep_alive: bool = False, observe: TransportObserver | None = None) -> None:
        self.keep_alive = bool(keep_alive)
        self._observe = observe
        self._request_lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connection_key: tuple[Any, ...] | None = None
        self._connecting_socket: socket.socket | None = None
        self._closed = False
        self._last_request_reused = False

    async def start(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> AsyncAnthropicHttpResponse:
        """Send one request and return the parsed response head.

        Pull-mode entry: the caller owns body consumption and must close the
        owner when done. Does not take the request lock — single-caller use
        only (``request()`` provides the locked consume-callback form).
        """
        if self._closed:
            raise ConnectionError("Anthropic transport connection closed")
        parts = urlsplit(url)
        proxy = _proxy_for(parts.scheme, parts.hostname or "")
        key = _connection_key(parts, proxy)
        reused = self._writer is not None and self._connection_key == key and not self._writer.is_closing()
        self._last_request_reused = reused
        try:
            if not reused:
                self._close_stream()
                self._reader, self._writer = await asyncio.wait_for(
                    self._open_stream(parts, proxy),
                    timeout=max(0.001, timeout_seconds),
                )
                self._connection_key = key
                self._emit("connection_opened")
            self._emit("request_started", reused=reused)
            assert self._reader is not None and self._writer is not None
            request_target = url if proxy is not None and parts.scheme == "http" else _origin_form(parts)
            wire_headers = _wire_headers(parts, headers, body, self.keep_alive, proxy)
            request_head = [f"POST {request_target} HTTP/1.1\r\n"]
            request_head.extend(f"{key}: {value}\r\n" for key, value in wire_headers.items())
            request_head.append("\r\n")
            self._writer.write("".join(request_head).encode("latin-1") + body)
            await self._writer.drain()
            return await asyncio.wait_for(
                _read_response_head(self._reader),
                timeout=max(0.001, timeout_seconds),
            )
        except asyncio.CancelledError:
            self.cancel()
            raise
        except (ConnectionError, OSError, EOFError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            if not self._closed:
                self._emit("transport_failed", reused=reused)
            self._close_stream()
            raise

    async def request(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
        consume: Callable[[AsyncAnthropicHttpResponse], Awaitable[Any]],
    ) -> Any:
        async with self._request_lock:
            response = await self.start(
                url=url, body=body, headers=headers, timeout_seconds=timeout_seconds
            )
            reused = self._last_request_reused
            try:
                if response.status >= 400:
                    error_body = await response.read()
                    self._finish_response(response)
                    raise HTTPError(url, response.status, response.reason, response.headers, _bytes_reader(error_body))
                result = await consume(response)
                if not response.complete:
                    await response.read()
                self._finish_response(response)
                return result
            except HTTPError:
                raise
            except asyncio.CancelledError:
                self.cancel()
                raise
            except (ConnectionError, OSError, EOFError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                if not self._closed:
                    self._emit("transport_failed", reused=reused)
                self._close_stream()
                raise
            except BaseException:
                # A typed consumer failure (SSE error event, protocol violation)
                # that leaves body bytes unread poisons the connection for
                # keep-alive reuse. A failure after a fully drained body leaves
                # the connection clean, so it stays reusable.
                if not response.complete:
                    self._close_stream()
                raise

    def cancel(self) -> None:
        self._emit("cancel_closed")
        self._closed = True
        connecting = self._connecting_socket
        self._connecting_socket = None
        if connecting is not None:
            with suppress(OSError):
                connecting.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connecting.close()
        self._close_stream()

    def close(self) -> None:
        self._closed = True
        self._close_stream()

    async def _open_stream(self, parts: Any, proxy: Any) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        target_host = proxy.hostname if proxy is not None else parts.hostname
        target_port = (
            proxy.port or (443 if proxy.scheme == "https" else 80)
            if proxy is not None
            else parts.port or (443 if parts.scheme == "https" else 80)
        )
        sock = await _connect_socket(str(target_host), int(target_port))
        self._connecting_socket = sock
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(sock=sock)
            self._connecting_socket = None
            if proxy is not None and parts.scheme == "https":
                connect_host = f"{parts.hostname}:{parts.port or 443}"
                proxy_headers = [
                    f"CONNECT {connect_host} HTTP/1.1\r\n",
                    f"Host: {connect_host}\r\n",
                ]
                authorization = _proxy_authorization(proxy)
                if authorization:
                    proxy_headers.append(f"Proxy-Authorization: {authorization}\r\n")
                proxy_headers.append("\r\n")
                writer.write("".join(proxy_headers).encode("latin-1"))
                await writer.drain()
                tunnel = await _read_response_head(reader)
                if tunnel.status != 200:
                    writer.close()
                    raise OSError(f"HTTP proxy CONNECT failed with {tunnel.status}")
                await writer.start_tls(
                    ssl.create_default_context(),
                    server_hostname=parts.hostname,
                )
            elif parts.scheme == "https":
                await writer.start_tls(
                    ssl.create_default_context(),
                    server_hostname=parts.hostname,
                )
            return reader, writer
        except BaseException:
            self._connecting_socket = None
            if writer is not None:
                writer.close()
            else:
                with suppress(OSError):
                    sock.close()
            raise

    def _finish_response(self, response: AsyncAnthropicHttpResponse) -> None:
        if not self.keep_alive or response.will_close:
            self._close_stream()

    def _close_stream(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        self._connection_key = None
        if writer is not None:
            writer.close()

    def _emit(self, event: str, **fields: Any) -> None:
        if self._observe is not None:
            with suppress(Exception):
                self._observe(event, fields)


class _bytes_reader:
    def __init__(self, value: bytes) -> None:
        self._value = value

    def read(self, *_args: Any) -> bytes:
        value, self._value = self._value, b""
        return value

    def close(self) -> None:
        self._value = b""


async def _read_response_head(reader: asyncio.StreamReader) -> AsyncAnthropicHttpResponse:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw[:-4].decode("latin-1").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise OSError("Invalid HTTP status line from Anthropic provider")
    headers = _Headers()
    for line in lines[1:]:
        if ":" not in line:
            raise OSError("Invalid HTTP header from Anthropic provider")
        key, value = line.split(":", 1)
        normalized = key.strip().lower()
        headers[normalized] = f"{headers[normalized]}, {value.strip()}" if normalized in headers else value.strip()
    return AsyncAnthropicHttpResponse(
        reader=reader,
        status=int(status_parts[1]),
        reason=status_parts[2] if len(status_parts) > 2 else "",
        version=status_parts[0],
        headers=headers,
    )


async def _connect_socket(host: str, port: int) -> socket.socket:
    addresses = await _resolve_host(host, port)
    loop = asyncio.get_running_loop()
    last_error: BaseException | None = None
    for family, socktype, protocol, _canonical, sockaddr in addresses:
        sock = socket.socket(family, socktype, protocol)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, sockaddr)
            return sock
        except BaseException as exc:
            last_error = exc
            sock.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
    raise OSError(f"Unable to connect to {host}:{port}") from last_error


async def _resolve_host(host: str, port: int) -> list[tuple[Any, ...]]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        return socket.getaddrinfo(host, port, family, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[list[tuple[Any, ...]]] = loop.create_future()

    def resolve() -> None:
        try:
            result = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_future_exception, future, exc)
        else:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_future_result, future, result)

    # getaddrinfo has no portable cancellation primitive. A daemon resolver is
    # deliberately isolated from transport workers: cancelling a turn settles
    # immediately and the resolver can never pin interpreter shutdown.
    threading.Thread(target=resolve, name="agent-core-anthropic-dns", daemon=True).start()
    return await future


def _set_future_result(future: asyncio.Future[Any], result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _set_future_exception(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def _proxy_for(scheme: str, hostname: str) -> Any:
    if proxy_bypass(hostname):
        return None
    raw = getproxies().get(scheme)
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    proxy = urlsplit(raw)
    if proxy.scheme not in {"http", "https"} or not proxy.hostname:
        return None
    if proxy.scheme == "https":
        raise OSError("HTTPS proxy endpoints are not supported by the stdlib Anthropic transport")
    return proxy


def _connection_key(parts: Any, proxy: Any) -> tuple[Any, ...]:
    return (
        parts.scheme,
        parts.hostname,
        parts.port or (443 if parts.scheme == "https" else 80),
        proxy.scheme if proxy else None,
        proxy.hostname if proxy else None,
        proxy.port if proxy else None,
    )


def _origin_form(parts: Any) -> str:
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _wire_headers(parts: Any, headers: dict[str, str], body: bytes, keep_alive: bool, proxy: Any) -> dict[str, str]:
    output = dict(headers)
    existing = {key.lower() for key in output}
    host = str(parts.hostname)
    default_port = 443 if parts.scheme == "https" else 80
    if parts.port and parts.port != default_port:
        host = f"{host}:{parts.port}"
    if "host" not in existing:
        output["Host"] = host
    if "content-length" not in existing:
        output["Content-Length"] = str(len(body))
    if "accept-encoding" not in existing:
        # urllib.request supplied this implicitly on the legacy transport.
        # Retain the same wire contract and explicitly decline compression,
        # which the stdlib response reader does not decode.
        output["Accept-Encoding"] = "identity"
    if "connection" not in existing:
        output["Connection"] = "keep-alive" if keep_alive else "close"
    authorization = _proxy_authorization(proxy)
    if proxy is not None and parts.scheme == "http" and authorization and "proxy-authorization" not in existing:
        output["Proxy-Authorization"] = authorization
    for key, value in output.items():
        if "\r" in key or "\n" in key or "\r" in str(value) or "\n" in str(value):
            raise ValueError("Unsafe HTTP header in Anthropic request")
    return output


def _proxy_authorization(proxy: Any) -> str | None:
    if proxy is None or proxy.username is None:
        return None
    token = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode("utf-8")
    return "Basic " + base64.b64encode(token).decode("ascii")
