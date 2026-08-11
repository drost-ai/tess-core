from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


import asyncio
import inspect
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Literal

from .._utils import now_iso
from ..storage import tool_artifact_data_path, tool_artifact_meta_path, write_json_file
from ..payloads.utf8_bounds import normalize_utf8_string, slice_utf8_prefix, slice_utf8_suffix, utf8_byte_length


SUBPROCESS_RING_MAX_BYTES = 128 * 1024
StreamKind = Literal["stdout", "stderr"]


class _head_tail_ring:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(1, max_bytes)
        marker_reserve = min(96, self.max_bytes // 4)
        content_bytes = max(1, self.max_bytes - marker_reserve)
        self.head_bytes = content_bytes // 2
        self.tail_bytes = content_bytes - self.head_bytes
        self.full = bytearray()
        self.head = b""
        self.tail = b""
        self.source_bytes = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self.source_bytes += len(chunk)
        if not self.truncated and len(self.full) + len(chunk) <= self.max_bytes:
            self.full.extend(chunk)
            return
        if not self.truncated:
            combined = bytes(self.full) + chunk
            self.head = combined[: self.head_bytes]
            self.tail = combined[-self.tail_bytes :]
            self.full.clear()
            self.truncated = True
            return
        combined_tail = self.tail + chunk
        self.tail = combined_tail[-self.tail_bytes :]

    def render(self) -> str:
        if not self.truncated:
            return normalize_utf8_string(bytes(self.full).decode("utf-8", errors="replace"))
        head = slice_utf8_prefix(
            normalize_utf8_string(self.head.decode("utf-8", errors="replace")),
            self.head_bytes,
        )
        tail = slice_utf8_suffix(
            normalize_utf8_string(self.tail.decode("utf-8", errors="replace")),
            self.tail_bytes,
        )
        omitted = max(0, self.source_bytes - utf8_byte_length(head) - utf8_byte_length(tail))
        return f"{head}\n...[omitted {omitted} bytes from bounded process ring]...\n{tail}"


class SubprocessOutputCollector:
    def __init__(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        family: str = "bash",
        stdout_content_type: str = "text",
        stderr_content_type: str = "text",
        max_spool_bytes: int = int(_knob("process.spool_max_bytes")),
        max_ring_bytes: int = SUBPROCESS_RING_MAX_BYTES,
        metadata_writer: Callable[[str, Any], Any] | None = None,
    ):
        self.session_id = session_id
        self.tool_name = tool_name
        self.family = family
        self.content_types = {
            "stdout": stdout_content_type,
            "stderr": stderr_content_type,
        }
        self.max_spool_bytes = _positive(max_spool_bytes, int(_knob("process.spool_max_bytes")))
        self.max_ring_bytes = _positive(max_ring_bytes, SUBPROCESS_RING_MAX_BYTES)
        self.metadata_writer = metadata_writer or write_json_file
        per_stream = max(1, self.max_ring_bytes // 2)
        self.rings = {"stdout": _head_tail_ring(per_stream), "stderr": _head_tail_ring(per_stream)}
        self.bytes = {"stdout": 0, "stderr": 0}
        self.lines = {"stdout": 0, "stderr": 0}
        self.spool_bytes = {"stdout": 0, "stderr": 0}
        self.ids = {"stdout": str(uuid.uuid4()), "stderr": str(uuid.uuid4())}
        self.paths: dict[str, str] = {}
        self.handles: dict[str, Any] = {}
        self.artifact_unavailable = False
        self.output_limit_exceeded = False
        self._limit_handler: Callable[[], None] | None = None
        self._limit_notified = False
        self._lock = asyncio.Lock()
        self._finalized: dict[str, Any] | None = None
        self._open_spools()

    def _open_spools(self) -> None:
        if not self.session_id:
            return
        try:
            for kind in ("stdout", "stderr"):
                path = tool_artifact_data_path(self.session_id, self.ids[kind], f".{kind}.txt")
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.paths[kind] = path
                self.handles[kind] = open(path, "xb")
        except Exception:
            self.artifact_unavailable = True
            self._close_and_remove_spools()

    async def append(self, kind: StreamKind, raw_chunk: bytes | bytearray | str) -> None:
        if self._finalized is not None:
            return
        chunk = raw_chunk.encode("utf-8") if isinstance(raw_chunk, str) else bytes(raw_chunk)
        if not chunk:
            return
        async with self._lock:
            self.rings[kind].append(chunk)
            self.bytes[kind] += len(chunk)
            self.lines[kind] += chunk.count(b"\n")
            measured_before = self.bytes["stdout"] + self.bytes["stderr"] - len(chunk)
            remaining = max(0, self.max_spool_bytes - measured_before)
            admitted = chunk[:remaining]
            if len(admitted) < len(chunk):
                self.output_limit_exceeded = True
                self._notify_limit()
            handle = self.handles.get(kind)
            if handle is not None and admitted and not self.artifact_unavailable:
                try:
                    await asyncio.to_thread(handle.write, admitted)
                    self.spool_bytes[kind] += len(admitted)
                except Exception:
                    self.artifact_unavailable = True

    def set_output_limit_handler(self, handler: Callable[[], None]) -> None:
        self._limit_handler = handler
        self._notify_limit()

    def _notify_limit(self) -> None:
        if self._limit_notified or not self.output_limit_exceeded or not self._limit_handler:
            return
        self._limit_notified = True
        try:
            self._limit_handler()
        except Exception:
            pass

    async def finalize(self) -> dict[str, Any]:
        if self._finalized is not None:
            return self._finalized
        artifacts: dict[str, Any] = {}
        for handle in self.handles.values():
            try:
                await asyncio.to_thread(handle.close)
            except Exception:
                self.artifact_unavailable = True
        if self.session_id and not self.artifact_unavailable:
            for kind in ("stdout", "stderr"):
                count = self.spool_bytes[kind]
                path = self.paths.get(kind)
                if not count or not path:
                    if path:
                        _unlink(path)
                    continue
                record = {
                    "artifactId": self.ids[kind],
                    "toolName": self.tool_name,
                    "sessionId": self.session_id,
                    "family": self.family,
                    "filePath": path,
                    "bytes": count,
                    "lines": self.lines[kind],
                    "contentType": self.content_types[kind],
                    "createdAt": now_iso(),
                    **({"truncated": True} if self.output_limit_exceeded else {}),
                    "sourceBytes": self.bytes[kind],
                }
                try:
                    pending = self.metadata_writer(
                        tool_artifact_meta_path(self.session_id, self.ids[kind]), record
                    )
                    if inspect.isawaitable(pending):
                        await pending
                    artifacts[kind] = record
                except Exception:
                    self.artifact_unavailable = True
                    _unlink(path)
        if self.artifact_unavailable:
            for kind, path in self.paths.items():
                if kind not in artifacts:
                    _unlink(path)
        stdout = self.rings["stdout"].render()
        stderr = self.rings["stderr"].render()
        self._finalized = {
            "stdout": stdout,
            "stderr": stderr,
            "stdoutBytes": self.bytes["stdout"],
            "stderrBytes": self.bytes["stderr"],
            "stdoutLines": self.lines["stdout"],
            "stderrLines": self.lines["stderr"],
            "ringBytes": min(
                self.max_ring_bytes, utf8_byte_length(stdout) + utf8_byte_length(stderr)
            ),
            "spoolBytes": self.spool_bytes["stdout"] + self.spool_bytes["stderr"],
            "outputLimitExceeded": self.output_limit_exceeded,
            "artifactUnavailable": self.artifact_unavailable,
            **({"stdoutArtifact": artifacts["stdout"]} if "stdout" in artifacts else {}),
            **({"stderrArtifact": artifacts["stderr"]} if "stderr" in artifacts else {}),
        }
        return self._finalized

    def _close_and_remove_spools(self) -> None:
        for handle in self.handles.values():
            try:
                handle.close()
            except Exception:
                pass
        for path in self.paths.values():
            _unlink(path)
        self.handles.clear()


async def create_subprocess_output_collector(**kwargs: Any) -> SubprocessOutputCollector:
    translated = {
        "session_id": kwargs.get("sessionId"),
        "tool_name": kwargs["toolName"],
        "family": kwargs.get("family", "bash"),
        "stdout_content_type": kwargs.get("stdoutContentType", "text"),
        "stderr_content_type": kwargs.get("stderrContentType", "text"),
        "max_spool_bytes": kwargs.get("maxSpoolBytes", int(_knob("process.spool_max_bytes"))),
        "max_ring_bytes": kwargs.get("maxRingBytes", SUBPROCESS_RING_MAX_BYTES),
        "metadata_writer": kwargs.get("metadataWriter"),
    }
    return SubprocessOutputCollector(**translated)


def _positive(value: Any, fallback: int) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else fallback


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


