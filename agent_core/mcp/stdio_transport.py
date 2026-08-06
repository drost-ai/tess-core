from __future__ import annotations

from .clients import JsonRpcClient
from .shared import *
from .stdio_decoder import McpStdioFrameDecoder
from .transport_policy import *
from .transport_shared import *


class StdioMcpClient(JsonRpcClient):
    def __init__(
        self,
        config: dict[str, Any],
        cwd: str,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        super().__init__(config, cwd, on_notification)
        self.policy = resolveMcpTransportPolicy(config)
        self.proc: subprocess.Popen[bytes] | None = None
        self.pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self.pending_lock = threading.Lock()
        self.decoder = McpStdioFrameDecoder()
        self.notifications = BoundedNotificationQueue(on_notification)
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.wait_thread: threading.Thread | None = None
        self.stderr_tail = bytearray()
        self.stderr_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.state = "open"
        self.close_done = threading.Event()
        self.close_error: BaseException | None = None

    async def initialize(self) -> None:
        self._start_process()
        await super().initialize()

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_open(str(request.get("method") or "request"))
        request_id = request.get("id")
        if request_id is None:
            await asyncio.to_thread(self._write_message, request)
            return {"jsonrpc": "2.0", "id": None, "result": {}}
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise McpTransportError("invalid_framing", "MCP request id is not numeric.")
        response_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
        with self.pending_lock:
            if len(self.pending) >= MCP_PENDING_REQUEST_MAX:
                raise McpTransportError(
                    "pending_limit",
                    f"MCP stdio pending request limit {MCP_PENDING_REQUEST_MAX} reached for {self.config.get('name')}.",
                )
            self.pending[request_id] = response_queue
        try:
            await asyncio.to_thread(self._write_message, request)
            try:
                response = await asyncio.to_thread(
                    response_queue.get,
                    True,
                    self.policy["requestTimeoutMs"] / 1000,
                )
            except queue.Empty as error:
                raise McpTransportError(
                    "request_timeout",
                    f"MCP request {request.get('method')} exceeded {self.policy['requestTimeoutMs']}ms for {self.config.get('name')}.",
                ) from error
            if isinstance(response, BaseException):
                raise response
            return response
        finally:
            with self.pending_lock:
                if self.pending.get(request_id) is response_queue:
                    self.pending.pop(request_id, None)

    async def close(self) -> None:
        owner = self._begin_close()
        if owner:
            self.notifications.close()
            self._reject_all(
                McpTransportError(
                    "transport_closed",
                    f"MCP stdio client is closing: {self.config.get('name')}.",
                )
            )
            await asyncio.to_thread(self._close_process_sync)
        elif not self.close_done.is_set():
            await asyncio.to_thread(self.close_done.wait)
        if self.close_error:
            raise self.close_error

    def _start_process(self) -> None:
        self._assert_open("initialize")
        if self.proc:
            return
        env = {**os.environ, **(self.config.get("env") or {})}
        try:
            self.proc = subprocess.Popen(
                [self.config["commandOrUrl"], *(self.config.get("args") or [])],
                cwd=self.cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                bufsize=0,
            )
        except Exception as error:
            self._terminal_failure(error, background=False)
            raise
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.wait_thread = threading.Thread(target=self._wait_loop, daemon=True)
        self.reader_thread.start()
        self.stderr_thread.start()
        self.wait_thread.start()

    def _write_message(self, request: dict[str, Any]) -> None:
        self._assert_open(str(request.get("method") or "request"))
        proc = self.proc
        if not proc or not proc.stdin:
            raise McpTransportError(
                "transport_closed",
                f"MCP stdio server is not running: {self.config.get('name')}.",
            )
        try:
            payload = json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except Exception as error:
            raise McpTransportError(
                "invalid_framing",
                f"MCP stdio request could not be encoded: {error}",
            ) from error
        if len(payload) > MCP_FRAME_MAX_BYTES:
            raise McpTransportError(
                "frame_limit",
                f"MCP stdio request body exceeds {MCP_FRAME_MAX_BYTES} bytes.",
            )
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        with self.write_lock:
            self._assert_open(str(request.get("method") or "request"))
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except Exception as error:
                failure = _transport_error(error, "transport_closed")
                self._terminal_failure(failure)
                raise failure

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        try:
            while True:
                chunk = (
                    proc.stdout.read1(64 * 1024)
                    if hasattr(proc.stdout, "read1")
                    else proc.stdout.read(64 * 1024)
                )
                if not chunk:
                    raise RuntimeError(self._format_exit())
                for payload in self.decoder.push(chunk):
                    self._handle_message(payload)
                self.notifications.flush()
        except Exception as error:
            self.decoder.reset()
            self._terminal_failure(error)

    def _stderr_loop(self) -> None:
        proc = self.proc
        assert proc and proc.stderr
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    return
                with self.stderr_lock:
                    combined = bytes(self.stderr_tail) + chunk
                    self.stderr_tail[:] = combined[-MCP_STDERR_TAIL_MAX_BYTES:]
        except Exception:
            return

    def _wait_loop(self) -> None:
        proc = self.proc
        if not proc:
            return
        with contextlib.suppress(Exception):
            proc.wait()
        self._terminal_failure(RuntimeError(self._format_exit()))

    def _handle_message(self, payload: str) -> None:
        message = _decode_json_rpc_payload(payload, "stdio frame")
        request_id = message.get("id")
        method = message.get("method")
        if isinstance(method, str) and request_id is None:
            self.notifications.push({"method": method, "params": message.get("params")})
            return
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            self._settle(request_id, message)
            return
        if "result" in message or "error" in message:
            raise McpTransportError(
                "invalid_framing",
                "MCP stdio response id did not match a numeric pending request.",
            )

    def _settle(self, request_id: int, value: dict[str, Any] | BaseException) -> None:
        with self.pending_lock:
            response_queue = self.pending.pop(request_id, None)
        if response_queue:
            with contextlib.suppress(queue.Full):
                response_queue.put_nowait(value)

    def _reject_all(self, error: BaseException) -> None:
        with self.pending_lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for response_queue in pending:
            with contextlib.suppress(queue.Full):
                response_queue.put_nowait(error)

    def _terminal_failure(self, error: BaseException, background: bool = True) -> None:
        self.notifications.close()
        self._reject_all(error)
        if not self._begin_close():
            return
        if background:
            threading.Thread(target=self._close_process_sync, daemon=True).start()
        else:
            self._close_process_sync()

    def _begin_close(self) -> bool:
        with self.state_lock:
            if self.state != "open":
                return False
            self.state = "closing"
            return True

    def _close_process_sync(self) -> None:
        try:
            proc = self.proc
            if proc and proc.stdin:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            if proc:
                self._wait_parent(MCP_CLOSE_STDIN_GRACE_MS)
                if self._owned_group_alive():
                    self._signal_owned_group(signal.SIGTERM)
                    if not self._wait_group(MCP_CLOSE_GRACE_MS):
                        self._signal_owned_group(signal.SIGKILL)
                        if not self._wait_group(MCP_CLOSE_KILL_WAIT_MS):
                            raise RuntimeError(
                                f"MCP stdio process group survived SIGKILL: {self.config.get('name')}."
                            )
                if not self._wait_parent(MCP_CLOSE_KILL_WAIT_MS):
                    raise RuntimeError(
                        f"MCP stdio child did not close after termination: {self.config.get('name')}."
                    )
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream:
                        with contextlib.suppress(Exception):
                            stream.close()
            current = threading.current_thread()
            for thread in (self.reader_thread, self.stderr_thread, self.wait_thread):
                if thread and thread is not current and thread.is_alive():
                    thread.join(timeout=1)
        except BaseException as error:
            self.close_error = error
        finally:
            with self.state_lock:
                self.state = "closed"
            self.close_done.set()

    def _owned_group_alive(self) -> bool:
        proc = self.proc
        if not proc:
            return False
        if os.name == "nt":
            return proc.poll() is None
        try:
            os.killpg(proc.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _signal_owned_group(self, signum: int) -> None:
        proc = self.proc
        if not proc:
            return
        try:
            if os.name == "nt":
                proc.send_signal(signum)
            else:
                os.killpg(proc.pid, signum)
        except ProcessLookupError:
            pass

    def _wait_group(self, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if not self._owned_group_alive():
                return True
            time.sleep(0.01)
        return not self._owned_group_alive()

    def _wait_parent(self, timeout_ms: int) -> bool:
        proc = self.proc
        if not proc or proc.poll() is not None:
            return True
        try:
            proc.wait(timeout=timeout_ms / 1000)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _format_exit(self) -> str:
        proc = self.proc
        code = proc.poll() if proc else None
        if proc and code is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                code = proc.wait(timeout=0.1)
        status = f"code {code}" if code is not None else "unknown status"
        with self.stderr_lock:
            stderr = bytes(self.stderr_tail).decode("utf-8", errors="replace").strip()
        return (
            f"MCP stdio server exited ({status}): {self.config.get('name')}"
            + (f"; stderr tail: {stderr}" if stderr else "")
        )

    def _assert_open(self, operation: str) -> None:
        with self.state_lock:
            state = self.state
        if state != "open":
            raise McpTransportError(
                "transport_closed",
                f"Cannot {operation}: MCP stdio client is {state} ({self.config.get('name')}).",
            )
