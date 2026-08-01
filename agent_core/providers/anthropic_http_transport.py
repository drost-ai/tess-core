from __future__ import annotations

import asyncio
import http.client
import io
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit

TransportObserver = Callable[[str, dict[str, Any]], None]
_TRANSPORT_EXCEPTIONS = (ConnectionError, OSError, EOFError, http.client.HTTPException)

_ACTIVE_THREADS: set[threading.Thread] = set()
_ACTIVE_THREADS_LOCK = threading.Lock()


class AnthropicHttpConnectionOwner:
    """Owns the blocking stdlib connection so another thread can close it."""

    def __init__(
        self,
        *,
        keep_alive: bool = False,
        observe: TransportObserver | None = None,
    ) -> None:
        self.keep_alive = bool(keep_alive)
        self._observe = observe
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._response: http.client.HTTPResponse | None = None
        self._closed = False

    def request(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
        consume: Callable[[http.client.HTTPResponse], Any],
    ) -> Any:
        with self._request_lock:
            if self._closed:
                raise ConnectionError("Anthropic transport connection closed")
            connection, reused = self._acquire_connection(url, timeout_seconds)
            parts = urlsplit(url)
            path = parts.path or "/"
            if parts.query:
                path = f"{path}?{parts.query}"
            wire_headers = dict(headers)
            wire_headers.setdefault("Connection", "keep-alive" if self.keep_alive else "close")
            self._emit("request_started", reused=reused)
            response: http.client.HTTPResponse | None = None
            try:
                connection.request("POST", path, body=body, headers=wire_headers)
                with self._state_lock:
                    cancelled_during_connect = self._closed
                if cancelled_during_connect:
                    connection.close()
                    raise ConnectionError("Anthropic transport cancelled during connect")
                response = connection.getresponse()
                with self._state_lock:
                    self._response = response
                if response.status >= 400:
                    error_body = response.read()
                    self._finish_response(response, connection)
                    raise HTTPError(
                        url,
                        response.status,
                        response.reason,
                        response.headers,
                        io.BytesIO(error_body),
                    )
                result = consume(response)
                self._finish_response(response, connection)
                return result
            except HTTPError:
                raise
            except _TRANSPORT_EXCEPTIONS:
                with self._state_lock:
                    cancelled = self._closed
                if not cancelled:
                    self._emit("transport_failed", reused=reused)
                self.close()
                with suppress(Exception):
                    connection.close()
                raise
            except BaseException:
                if response is not None:
                    with suppress(Exception):
                        response.read()
                    self._finish_response(response, connection)
                raise

    def cancel(self) -> None:
        self._emit("cancel_closed")
        self.close()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            response = self._response
            connection = self._connection
            self._response = None
            self._connection = None
        sock = getattr(connection, "sock", None)
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        if response is not None:
            with suppress(Exception):
                response.close()
        if connection is not None:
            with suppress(Exception):
                connection.close()

    def _acquire_connection(
        self, url: str, timeout_seconds: float
    ) -> tuple[http.client.HTTPConnection, bool]:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"Unsupported Anthropic URL: {url}")
        with self._state_lock:
            existing = self._connection
            if existing is not None:
                existing.timeout = timeout_seconds
                return existing, True
            connection_type = (
                http.client.HTTPSConnection
                if parts.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_type(
                parts.hostname,
                parts.port,
                timeout=timeout_seconds,
            )
            self._connection = connection
        self._emit("connection_opened")
        return connection, False

    def _finish_response(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
    ) -> None:
        with self._state_lock:
            if self._response is response:
                self._response = None
        with suppress(Exception):
            response.close()
        if not self.keep_alive or response.will_close:
            with self._state_lock:
                if self._connection is connection:
                    self._connection = None
            with suppress(Exception):
                connection.close()

    def _emit(self, event: str, **fields: Any) -> None:
        if self._observe is None:
            return
        with suppress(Exception):
            self._observe(event, fields)


def registerAnthropicTransportThread(thread: threading.Thread) -> None:
    with _ACTIVE_THREADS_LOCK:
        _ACTIVE_THREADS.add(thread)


def unregisterAnthropicTransportThread(thread: threading.Thread) -> None:
    with _ACTIVE_THREADS_LOCK:
        _ACTIVE_THREADS.discard(thread)


def activeAnthropicTransportThreadCount() -> int:
    with _ACTIVE_THREADS_LOCK:
        return sum(thread.is_alive() for thread in _ACTIVE_THREADS)


def startAnthropicTransportWorker(
    *,
    target: Callable[[], Any],
    owner: AnthropicHttpConnectionOwner | None,
    turn_session: Any = None,
    name: str,
) -> asyncio.Future[Any]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def complete_result(result: Any) -> None:
        if not future.done():
            future.set_result(result)

    def complete_error(error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    def worker_target() -> None:
        try:
            result = target()
        except BaseException as exc:  # noqa: BLE001 - worker must settle its future
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(complete_error, exc)
        else:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(complete_result, result)
        finally:
            unregisterAnthropicTransportThread(thread)
            if turn_session is not None:
                turn_session.unregister_transport_worker(thread)

    thread = threading.Thread(target=worker_target, name=name, daemon=False)
    future._anthropic_transport_owner = owner
    future._anthropic_transport_thread = thread
    future._anthropic_turn_session = turn_session
    registerAnthropicTransportThread(thread)
    if turn_session is not None:
        turn_session.register_transport_worker(thread)
    try:
        thread.start()
    except BaseException:
        unregisterAnthropicTransportThread(thread)
        if turn_session is not None:
            turn_session.unregister_transport_worker(thread)
        raise
    return future


async def cancelAnthropicTransportFuture(
    future: asyncio.Future[Any],
    *,
    join_timeout: float = 0.25,
) -> bool:
    owner = getattr(future, "_anthropic_transport_owner", None)
    if owner is not None:
        owner.cancel()
    if not future.done():
        future.cancel()
    thread = getattr(future, "_anthropic_transport_thread", None)
    if not isinstance(thread, threading.Thread) or thread is threading.current_thread():
        return True
    await asyncio.to_thread(thread.join, join_timeout)
    if not thread.is_alive():
        return True
    turn_session = getattr(future, "_anthropic_turn_session", None)
    if turn_session is not None:
        turn_session._emit_thread_leak(thread, join_timeout)
    return False
