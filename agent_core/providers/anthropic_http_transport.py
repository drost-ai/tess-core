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

    # This path now exists only for test doubles that monkeypatch urllib.  A
    # hostile double must not pin interpreter shutdown after cancellation.
    thread = threading.Thread(target=worker_target, name=name, daemon=True)
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
