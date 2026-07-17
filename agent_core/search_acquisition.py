from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from typing import Any, Callable

from .subprocess_output import (
    SUBPROCESS_RING_MAX_BYTES,
    SUBPROCESS_SPOOL_MAX_BYTES,
    createSubprocessOutputCollector,
)
from .utf8_bounds import normalizeUtf8String, sliceUtf8Prefix, utf8ByteLength


SEARCH_RECORD_MAX_BYTES = 1024 * 1024
SEARCH_TIMEOUT_MS = 120_000
SEARCH_READ_CHUNK_BYTES = 64 * 1024


class _BoundedLineSampler:
    def __init__(
        self,
        *,
        offset: int,
        max_items: int,
        max_sample_bytes: int,
        max_record_bytes: int,
        accept: Callable[[str], bool] | None = None,
    ):
        self.offset = max(0, offset)
        self.max_items = max(1, max_items)
        self.max_sample_bytes = max(1, max_sample_bytes)
        self.max_record_bytes = max(1, max_record_bytes)
        self.accept = accept
        self.record = bytearray()
        self.source_bytes = 0
        self.sample_bytes = 0
        self.finished = False
        self.digest = hashlib.sha256()
        self.total_matches = 0
        self.page_records_advanced = 0
        self.skipped_records = 0
        self.fragmented_records = 0
        self.oversized_records = 0
        self.matches: list[str] = []
        self.result_fingerprint = ""

    def append(self, chunk: bytes) -> None:
        if self.finished:
            return
        self.digest.update(chunk)
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline < 0:
                self._append_record_bytes(chunk[start:])
                return
            self._append_record_bytes(chunk[start:newline])
            self._emit_record()
            start = newline + 1

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self.source_bytes > 0:
            self._emit_record()
        self.result_fingerprint = self.digest.hexdigest()[:24]

    def _append_record_bytes(self, value: bytes) -> None:
        self.source_bytes += len(value)
        remaining = self.max_record_bytes - len(self.record)
        if remaining > 0:
            self.record.extend(value[:remaining])

    def _emit_record(self) -> None:
        source_bytes = self.source_bytes
        stored = bytes(self.record)
        if stored.endswith(b"\r"):
            stored = stored[:-1]
        record = normalizeUtf8String(stored.decode("utf-8", errors="replace"))
        if source_bytes > self.max_record_bytes:
            self.oversized_records += 1
            marker = f"...[record truncated from {source_bytes} bytes]"
            prefix_limit = max(0, self.max_record_bytes - utf8ByteLength(marker))
            record = f"{sliceUtf8Prefix(record, prefix_limit)}{marker}"
        self.record.clear()
        self.source_bytes = 0
        if not record or (self.accept and not self.accept(record)):
            return
        ordinal = self.total_matches
        self.total_matches += 1
        if ordinal < self.offset or self.page_records_advanced >= self.max_items:
            return
        separator_bytes = 1 if self.matches else 0
        remaining = self.max_sample_bytes - self.sample_bytes - separator_bytes
        if remaining <= 0:
            self._skip_if_no_progress()
            return
        sampled, fragmented = _fit_record(record, source_bytes, remaining)
        if not sampled:
            self._skip_if_no_progress()
            return
        self.matches.append(sampled)
        self.page_records_advanced += 1
        if fragmented:
            self.fragmented_records += 1
        self.sample_bytes += separator_bytes + utf8ByteLength(sampled)

    def _skip_if_no_progress(self) -> None:
        if self.page_records_advanced == 0:
            self.page_records_advanced = 1
            self.skipped_records = 1

    def values(self) -> dict[str, Any]:
        return {
            "matches": self.matches,
            "totalMatches": self.total_matches,
            "pageRecordsAdvanced": self.page_records_advanced,
            "skippedRecords": self.skipped_records,
            "fragmentedRecords": self.fragmented_records,
            "resultFingerprint": self.result_fingerprint,
            "oversizedRecords": self.oversized_records,
        }


def _fit_record(record: str, source_bytes: int, max_bytes: int) -> tuple[str, bool]:
    if utf8ByteLength(record) <= max_bytes:
        return record, False
    marker = f"...[record fragment; {source_bytes} source bytes]"
    marker_bytes = utf8ByteLength(marker)
    if marker_bytes >= max_bytes:
        return sliceUtf8Prefix(marker, max_bytes), True
    return f"{sliceUtf8Prefix(record, max_bytes - marker_bytes)}{marker}", True


async def runStreamingSearch(**kwargs: Any) -> dict[str, Any]:
    collector = await createSubprocessOutputCollector(
        sessionId=kwargs.get("sessionId"),
        toolName=kwargs["toolName"],
        family=kwargs["family"],
        stdoutContentType=kwargs["contentType"],
        stderrContentType="text",
        maxSpoolBytes=kwargs.get("maxSpoolBytes", SUBPROCESS_SPOOL_MAX_BYTES),
        maxRingBytes=kwargs.get("maxRingBytes", SUBPROCESS_RING_MAX_BYTES),
    )
    sampler = _make_sampler(kwargs)
    try:
        process = await asyncio.create_subprocess_exec(
            kwargs["binary"],
            *kwargs["argv"],
            cwd=kwargs["cwd"],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    except Exception:
        await collector.finalize()
        raise

    timed_out = False
    aborted = False
    limit_termination = False

    def terminate() -> None:
        _signal_process_tree(process, signal.SIGTERM)

    def terminate_for_limit() -> None:
        nonlocal limit_termination
        limit_termination = True
        terminate()

    collector.set_output_limit_handler(terminate_for_limit)
    stdout_task = asyncio.create_task(
        _read_stream(process.stdout, "stdout", collector, sampler)
    )
    stderr_task = asyncio.create_task(
        _read_stream(process.stderr, "stderr", collector, None)
    )
    abort_task = _create_abort_task(kwargs.get("abortSignal"))
    timeout_seconds = _positive(kwargs.get("timeoutMs"), SEARCH_TIMEOUT_MS) / 1000
    wait_task = asyncio.create_task(process.wait())
    pending: set[asyncio.Task[Any]] = {wait_task}
    if abort_task:
        pending.add(abort_task)
    done, _ = await asyncio.wait(
        pending, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
    )
    if wait_task not in done:
        if abort_task and abort_task in done:
            aborted = True
        else:
            timed_out = True
        terminate()
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.5)
        except asyncio.TimeoutError:
            _signal_process_tree(process, signal.SIGKILL)
            await wait_task
    if abort_task:
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)
    await asyncio.gather(stdout_task, stderr_task)
    sampler.finish()
    capture = await collector.finalize()
    process_group_drained = True
    if limit_termination or timed_out or aborted:
        process_group_drained = await _drain_process_group(process.pid)
    return {
        **capture,
        **sampler.values(),
        "exitCode": process.returncode if process.returncode is not None and process.returncode >= 0 else None,
        "exitSignal": _exit_signal(process.returncode),
        "timedOut": timed_out,
        "aborted": aborted,
        "processGroupDrained": process_group_drained,
    }


async def buildStreamingSearchFromOutput(
    *, stdout: str, stderr: str, **kwargs: Any
) -> dict[str, Any]:
    collector = await createSubprocessOutputCollector(
        sessionId=kwargs.get("sessionId"),
        toolName=kwargs["toolName"],
        family=kwargs["family"],
        stdoutContentType=kwargs["contentType"],
        stderrContentType="text",
        maxSpoolBytes=kwargs.get("maxSpoolBytes", SUBPROCESS_SPOOL_MAX_BYTES),
        maxRingBytes=kwargs.get("maxRingBytes", SUBPROCESS_RING_MAX_BYTES),
    )
    sampler = _make_sampler(kwargs)
    out = stdout.encode("utf-8")
    err = stderr.encode("utf-8")
    sampler.append(out)
    await collector.append("stdout", out)
    await collector.append("stderr", err)
    sampler.finish()
    return {
        **(await collector.finalize()),
        **sampler.values(),
        "exitCode": 0,
        "exitSignal": None,
        "timedOut": False,
        "aborted": False,
        "processGroupDrained": True,
    }


def _make_sampler(kwargs: dict[str, Any]) -> _BoundedLineSampler:
    return _BoundedLineSampler(
        offset=max(0, int(kwargs.get("offset", 0))),
        max_items=_positive(kwargs.get("sampleItems"), 1),
        max_sample_bytes=_positive(kwargs.get("sampleBytes"), 1),
        max_record_bytes=_positive(
            kwargs.get("maxRecordBytes"), SEARCH_RECORD_MAX_BYTES
        ),
        accept=kwargs.get("accept"),
    )


async def _read_stream(
    reader: asyncio.StreamReader | None,
    kind: str,
    collector: Any,
    sampler: _BoundedLineSampler | None,
) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(SEARCH_READ_CHUNK_BYTES)
        if not chunk:
            return
        if sampler:
            sampler.append(chunk)
        await collector.append(kind, chunk)


def _signal_process_tree(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if process.returncode is not None:
        return
    if os.name != "nt" and process.pid:
        try:
            os.killpg(process.pid, sig)
            return
        except OSError:
            pass
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        pass


async def _drain_process_group(pid: int | None) -> bool:
    if os.name == "nt" or not pid:
        return True
    if not _process_group_live(pid):
        return True
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(20):
        await asyncio.sleep(0.01)
        if not _process_group_live(pid):
            return True
    return not _process_group_live(pid)


def _process_group_live(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except OSError:
        return False


def _create_abort_task(abort_signal: Any) -> asyncio.Task[Any] | None:
    if abort_signal is None:
        return None
    if isinstance(abort_signal, asyncio.Event):
        return asyncio.create_task(abort_signal.wait())

    async def poll() -> None:
        while not bool(getattr(abort_signal, "aborted", False)):
            await asyncio.sleep(0.02)

    return asyncio.create_task(poll())


def _positive(value: Any, fallback: int) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else fallback


def _exit_signal(returncode: int | None) -> str | None:
    if returncode is None or returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"SIG{-returncode}"


run_streaming_search = runStreamingSearch
build_streaming_search_from_output = buildStreamingSearchFromOutput
