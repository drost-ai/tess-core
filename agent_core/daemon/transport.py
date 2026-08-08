from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any, Callable

from .protocol import (
    DAEMON_MAX_PENDING_GLOBAL,
    DAEMON_MAX_PENDING_PER_CLIENT,
    DAEMON_MAX_REQUEST_FRAME_BYTES,
    DAEMON_MAX_REQUEST_IDS_PER_CONNECTION,
    DAEMON_MAX_RESPONSE_FRAME_BYTES,
    DAEMON_PROTOCOL_VERSION,
    DaemonLineDecoder,
    DaemonProtocolError,
    boundedProtocolDiagnostic,
    daemonTokensEqual,
    encodeDaemonFrame,
    isDaemonAuthRequest,
    isDaemonAuthResponse,
    parseDaemonFrame,
    parseDaemonRequest,
    resolveDaemonAuthTimeoutMs,
    resolveDaemonRpcTimeoutMs,
)


def _close_grace_seconds() -> float:
    from ..config._knobs import knobValue

    return int(knobValue("daemon.close_grace_ms")) / 1000


def _dispatch_drain_seconds() -> float:
    from ..config._knobs import knobValue

    return int(knobValue("daemon.dispatch_drain_ms")) / 1000


class DaemonAbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self.reason: BaseException | None = None
        self._event = asyncio.Event()

    def abort(self, reason: BaseException | None = None) -> None:
        if self.aborted:
            return
        self.aborted = True
        self.reason = reason or RuntimeError("Daemon request aborted.")
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class DaemonServerPeer:
    def __init__(
        self,
        owner: "AuthenticatedDaemonServer",
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.owner = owner
        self.reader = reader
        self.writer = writer
        self.decoder = DaemonLineDecoder(DAEMON_MAX_REQUEST_FRAME_BYTES)
        self.id = str(uuid.uuid4())
        self.authenticated = False
        self.closed = False
        self.seen_ids: set[str] = set()
        self.active: dict[str, tuple[DaemonAbortSignal, asyncio.Task[Any]]] = {}
        self.write_lock = asyncio.Lock()
        self._disconnect_called = False

    async def run(self) -> None:
        try:
            await asyncio.wait_for(
                self._read_until_authenticated(),
                timeout=self.owner.auth_timeout_ms / 1000,
            )
            await self._read_requests()
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        finally:
            await self.close()

    async def _read_until_authenticated(self) -> None:
        while not self.authenticated and not self.closed:
            chunk = await self.reader.read(64 * 1024)
            if not chunk:
                raise DaemonProtocolError(
                    "daemon.auth_failed",
                    "Daemon connection closed during authentication.",
                )
            for frame in self.decoder.push(chunk):
                if not frame:
                    continue
                value = parseDaemonFrame(frame)
                if not self.authenticated:
                    await self._authenticate(value)
                else:
                    await self._accept_request(value)

    async def _authenticate(self, value: Any) -> None:
        if not self.owner.accepting:
            raise DaemonProtocolError(
                "daemon.not_ready",
                "Daemon recovery is not ready to admit clients.",
            )
        if _is_legacy_request(value):
            await self._write_frame(
                {
                    "id": value["id"],
                    "ok": False,
                    "error": "Daemon protocol v2 requires authentication; upgrade the client.",
                }
            )
            raise DaemonProtocolError(
                "daemon.version_mismatch",
                "Legacy daemon client rejected.",
            )
        if not isDaemonAuthRequest(value) or not daemonTokensEqual(value["token"], self.owner.token):
            raise DaemonProtocolError("daemon.auth_failed", "Daemon authentication failed.")
        self.authenticated = True
        await self._write_frame(
            {
                "type": "auth_ok",
                "protocolVersion": DAEMON_PROTOCOL_VERSION,
                "epoch": self.owner.epoch,
            }
        )
        if callable(self.owner.on_authenticated):
            self.owner._schedule_cleanup(self._notify_authenticated())

    async def _notify_authenticated(self) -> None:
        try:
            await _maybe_await_transport(self.owner.on_authenticated(self))
        except Exception:
            await self.close()

    async def _read_requests(self) -> None:
        while not self.closed and not self.owner.closing:
            chunk = await self.reader.read(64 * 1024)
            if not chunk:
                return
            for frame in self.decoder.push(chunk):
                if frame:
                    await self._accept_request(parseDaemonFrame(frame))

    async def _accept_request(self, value: Any) -> None:
        request = parseDaemonRequest(value)
        request_id = request["id"]
        if (
            request_id in self.seen_ids
            or len(self.seen_ids) >= DAEMON_MAX_REQUEST_IDS_PER_CONNECTION
        ):
            raise DaemonProtocolError(
                "daemon.frame_malformed",
                "Daemon request id was reused or exhausted.",
            )
        self.seen_ids.add(request_id)
        if self.owner.closing:
            await self._write_response(
                {"id": request_id, "ok": False, "error": "Daemon is shutting down."}
            )
            return
        if (
            len(self.active) >= DAEMON_MAX_PENDING_PER_CLIENT
            or self.owner.active_global >= DAEMON_MAX_PENDING_GLOBAL
        ):
            await self._write_response(
                {
                    "id": request_id,
                    "ok": False,
                    "error": "Daemon RPC pending-request limit reached.",
                }
            )
            return
        signal = DaemonAbortSignal()
        task = asyncio.create_task(self._dispatch(request, signal))
        self.active[request_id] = (signal, task)
        self.owner.active_global += 1
        self.owner.dispatch_work.add(task)
        task.add_done_callback(self.owner._discard_dispatch_task)

    async def _dispatch(self, request: dict[str, Any], signal: DaemonAbortSignal) -> None:
        request_id = request["id"]

        async def invoke() -> Any:
            return await _maybe_await_transport(self.owner.dispatch(self, request, signal))

        operation = asyncio.create_task(invoke())
        response: dict[str, Any]
        after_write: Any = None
        try:
            done, _pending = await asyncio.wait(
                {operation},
                timeout=self.owner.rpc_timeout_ms / 1000,
            )
            if operation not in done:
                signal.abort(
                    DaemonProtocolError(
                        "daemon.request_timeout",
                        f"Daemon RPC '{request['method']}' exceeded {self.owner.rpc_timeout_ms}ms.",
                    )
                )
                response = {
                    "id": request_id,
                    "ok": False,
                    "error": "Daemon RPC timed out.",
                }
            else:
                outcome = operation.result()
                result = outcome.get("result") if isinstance(outcome, dict) else outcome
                after_write = outcome.get("afterWrite") if isinstance(outcome, dict) else None
                response = {"id": request_id, "ok": True, "result": result}
        except asyncio.CancelledError:
            signal.abort(RuntimeError("Daemon peer closed."))
            await _drain_or_cancel(operation, _dispatch_drain_seconds())
            raise
        except Exception as error:
            response = {
                "id": request_id,
                "ok": False,
                "error": boundedProtocolDiagnostic(str(error)),
            }
        finally:
            if self.active.pop(request_id, None) is not None:
                self.owner.active_global -= 1
        if not self.closed:
            await self._write_response(response)
        if callable(after_write):
            try:
                result = after_write()
                if hasattr(result, "__await__"):
                    after_task = asyncio.create_task(result)
                    self.owner.cleanup_work.add(after_task)
                    after_task.add_done_callback(self.owner._discard_cleanup_task)
            except Exception:
                pass
        if not operation.done():
            self.owner.cleanup_work.add(operation)
            operation.add_done_callback(self.owner._discard_cleanup_task)

    async def _write_response(self, response: dict[str, Any]) -> None:
        try:
            await self._write_frame(response)
        except DaemonProtocolError as error:
            fallback = {
                "id": response.get("id") or "unknown",
                "ok": False,
                "error": (
                    "Daemon response exceeded the 2 MiB frame limit."
                    if error.code == "daemon.frame_oversized"
                    else "Daemon response serialization failed."
                ),
            }
            try:
                await self._write_frame(fallback)
            except Exception:
                await self.close()
        except Exception:
            await self.close()

    async def sendEvent(self, value: Any) -> bool:
        if not self.authenticated or self.closed:
            return False
        try:
            await self._write_frame(value)
            return True
        except Exception:
            await self.close()
            return False

    async def _write_frame(self, value: Any) -> None:
        frame = encodeDaemonFrame(value, DAEMON_MAX_RESPONSE_FRAME_BYTES)
        async with self.write_lock:
            if self.closed or self.writer.is_closing():
                raise RuntimeError("Daemon peer is closed.")
            self.writer.write(frame)
            await self.writer.drain()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.decoder.reset()
        for request_id, (signal, _task) in list(self.active.items()):
            signal.abort(RuntimeError("Daemon peer closed."))
            if self.active.pop(request_id, None) is not None:
                self.owner.active_global -= 1
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.writer.wait_closed(),
                timeout=_close_grace_seconds(),
            )
        self.owner.peers.discard(self)
        if not self._disconnect_called and callable(self.owner.on_disconnect):
            self._disconnect_called = True
            with contextlib.suppress(Exception):
                await _maybe_await_transport(self.owner.on_disconnect(self))


class AuthenticatedDaemonServer:
    def __init__(
        self,
        *,
        token: str,
        epoch: str,
        dispatch: Callable[..., Any],
        authTimeoutMs: int | None = None,
        rpcTimeoutMs: int | None = None,
        onAuthenticated: Callable[..., Any] | None = None,
        onDisconnect: Callable[..., Any] | None = None,
        accepting: bool = True,
    ) -> None:
        self.token = token
        self.epoch = epoch
        self.dispatch = dispatch
        self.auth_timeout_ms = resolveDaemonAuthTimeoutMs(authTimeoutMs)
        self.rpc_timeout_ms = resolveDaemonRpcTimeoutMs(rpcTimeoutMs)
        self.on_authenticated = onAuthenticated
        self.on_disconnect = onDisconnect
        self.accepting = accepting
        self.server: asyncio.AbstractServer | None = None
        self.peers: set[DaemonServerPeer] = set()
        self.dispatch_work: set[asyncio.Task[Any]] = set()
        self.cleanup_work: set[asyncio.Task[Any]] = set()
        self.active_global = 0
        self.closing = False
        self._close_task: asyncio.Task[Any] | None = None

    async def listen(self, socketPath: str) -> None:
        if self.closing:
            raise RuntimeError("Daemon server is closing.")
        self.server = await asyncio.start_unix_server(self._accept, path=socketPath)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.closing:
            writer.close()
            return
        peer = DaemonServerPeer(self, reader, writer)
        self.peers.add(peer)
        await peer.run()

    async def close(self) -> None:
        if self._close_task:
            await self._close_task
            return
        self._close_task = asyncio.create_task(self._close())
        await self._close_task

    async def _close(self) -> None:
        self.closing = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await asyncio.gather(*(peer.close() for peer in list(self.peers)), return_exceptions=True)
        await _drain_task_set(self.dispatch_work | self.cleanup_work, _dispatch_drain_seconds())

    def _discard_dispatch_task(self, task: asyncio.Task[Any]) -> None:
        self.dispatch_work.discard(task)
        _consume_transport_task(task)

    def _discard_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self.cleanup_work.discard(task)
        _consume_transport_task(task)

    def _schedule_cleanup(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self.cleanup_work.add(task)
        task.add_done_callback(self._discard_cleanup_task)


def createAuthenticatedDaemonServer(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AuthenticatedDaemonServer:
    return AuthenticatedDaemonServer(**{**(args or {}), **kwargs})


async def openAuthenticatedDaemonConnection(
    socketPath: str,
    token: str,
    expectedEpoch: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    timeout = resolveDaemonAuthTimeoutMs() / 1000
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socketPath),
            timeout=timeout,
        )
    except asyncio.TimeoutError as error:
        raise DaemonProtocolError(
            "daemon.auth_timeout",
            "Timed out connecting to the daemon socket.",
        ) from error
    try:
        writer.write(
            encodeDaemonFrame(
                {
                    "type": "auth",
                    "protocolVersion": DAEMON_PROTOCOL_VERSION,
                    "token": token,
                },
                DAEMON_MAX_REQUEST_FRAME_BYTES,
            )
        )
        await writer.drain()
        decoder = DaemonLineDecoder(DAEMON_MAX_RESPONSE_FRAME_BYTES)
        response = await readOneDaemonFrame(reader, decoder, timeout)
        if decoder.bufferedBytes or not isDaemonAuthResponse(response):
            raise DaemonProtocolError(
                "daemon.auth_failed",
                "Daemon rejected the authenticated protocol handshake.",
            )
        if response["epoch"] != expectedEpoch:
            raise DaemonProtocolError(
                "daemon.auth_failed",
                "Daemon instance changed during authentication.",
            )
        return reader, writer
    except asyncio.TimeoutError as error:
        writer.close()
        raise DaemonProtocolError(
            "daemon.auth_timeout",
            "Daemon authentication timed out.",
        ) from error
    except Exception:
        writer.close()
        raise


async def readOneDaemonFrame(
    reader: asyncio.StreamReader,
    decoder: DaemonLineDecoder,
    timeoutSeconds: float,
) -> Any:
    async def read() -> Any:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                raise RuntimeError("Daemon connection closed before a frame arrived.")
            frames = decoder.push(chunk)
            if not frames:
                continue
            if len(frames) != 1:
                raise DaemonProtocolError(
                    "daemon.frame_malformed",
                    "Daemon returned more than one frame where one was expected.",
                )
            return parseDaemonFrame(frames[0])

    return await asyncio.wait_for(read(), timeout=timeoutSeconds)


async def _drain_task_set(tasks: set[asyncio.Task[Any]], timeout: float) -> None:
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        _consume_transport_task(task)


async def _drain_or_cancel(task: asyncio.Task[Any], timeout: float) -> None:
    if task.done():
        _consume_transport_task(task)
        return
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _maybe_await_transport(value: Any) -> Any:
    return await value if hasattr(value, "__await__") else value


def _consume_transport_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _is_legacy_request(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("id"), str) and isinstance(value.get("method"), str)
