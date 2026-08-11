from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


import asyncio
import contextlib
import os
import signal
from typing import Any

from .owned_launch import createOwnedProcessActivation
from .identity import captureOwnedProcessIdentity
from .output import createSubprocessOutputCollector



STDIO_LEAK_DIAGNOSTIC = (
    "The shell exited, but stdout/stderr did not close. A background descendant likely inherited "
    "stdio, so it was terminated to prevent the Bash tool from hanging. Use run_in_background for "
    "long-lived commands."
)
BACKGROUND_DESCENDANT_DIAGNOSTIC = (
    "The shell exited while a background descendant was still running. It was terminated to keep "
    "inline Bash foreground-only. Use run_in_background for long-lived commands."
)
_READ_CHUNK_BYTES = 64 * 1024


async def runInlineShellCommand(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    command = str(values["command"])
    argv = list(values.get("argv") or [values["shell"], "-lc", command])
    activation = createOwnedProcessActivation(argv) if values.get("onProcessIdentity") else None
    launched_argv = activation["argv"] if activation else argv
    collector = values.get("outputCollector") or await createSubprocessOutputCollector(
        sessionId=values.get("sessionId"),
        toolName=values.get("toolName") or "Bash",
        family="bash",
        maxSpoolBytes=values.get("maxSpoolBytes"),
        maxRingBytes=values.get("maxRingBytes"),
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *launched_argv,
            cwd=values["cwd"],
            env=values.get("env"),
            stdin=asyncio.subprocess.PIPE if activation else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    except Exception:
        await collector.finalize()
        raise

    process_identity = None
    if activation:
        try:
            process_identity = await captureOwnedProcessIdentity(
                pid=process.pid,
                expectedArgv=launched_argv,
            )
            pending = values["onProcessIdentity"](process_identity)
            if hasattr(pending, "__await__"):
                await pending
            if process.stdin is None:
                raise RuntimeError("Owned process runner activation channel is unavailable.")
            process.stdin.write(
                f"activate:{activation['activationToken']}\n".encode("ascii")
            )
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except BaseException:
            _signal_process_tree(process, signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 3)
            await collector.finalize()
            raise

    limit_event = asyncio.Event()

    def terminate_for_limit() -> None:
        limit_event.set()
        _signal_process_tree(process, signal.SIGTERM)

    collector.set_output_limit_handler(terminate_for_limit)
    stdout_task = asyncio.create_task(_read_stream(process.stdout, "stdout", collector))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, "stderr", collector))
    reader_tasks = {stdout_task, stderr_task}
    wait_task = asyncio.create_task(process.wait())
    limit_task = asyncio.create_task(limit_event.wait())
    abort_task = _create_abort_task(values.get("abortSignal"))
    pending: set[asyncio.Task[Any]] = {wait_task, limit_task}
    if abort_task:
        pending.add(abort_task)

    timeout_seconds = max(0.001, float(values["timeoutMs"]) / 1000)
    done, _ = await asyncio.wait(
        pending, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
    )
    output_limit = limit_task in done or limit_event.is_set()
    aborted = bool(abort_task and abort_task in done and not output_limit)
    timed_out = wait_task not in done and not output_limit and not aborted
    stdio_leak = False
    background_descendants = False
    background_terminated = False
    pipes_closed = False

    if wait_task not in done:
        _signal_process_tree(process, signal.SIGTERM)
        await _settle_process(
            process,
            wait_task,
            int(values.get("terminationGraceMs") or int(_knob("process.bash_termination_grace_ms"))),
            int(values.get("terminationFallbackMs") or int(_knob("process.bash_termination_fallback_ms"))),
        )
    else:
        await wait_task

    if not output_limit and not aborted and not timed_out:
        readers_done, _ = await asyncio.wait(
            reader_tasks,
            timeout=max(
                0.001,
                float(values.get("stdioLeakGraceMs") or int(_knob("process.stdio_leak_grace_ms"))) / 1000,
            ),
        )
        if len(readers_done) != len(reader_tasks):
            stdio_leak = True
            background_descendants = True
            background_terminated = _signal_process_tree(process, signal.SIGTERM)
    if _process_group_live(process.pid):
        background_descendants = True
        background_terminated = _signal_process_tree(process, signal.SIGTERM) or background_terminated

    if any(not task.done() for task in reader_tasks) or _process_group_live(process.pid):
        pipes_closed = await _drain_readers_and_group(
            process,
            reader_tasks,
            int(values.get("terminationGraceMs") or int(_knob("process.bash_termination_grace_ms"))),
        )
    await asyncio.gather(*reader_tasks, return_exceptions=True)

    for task in (limit_task, abort_task):
        if task and not task.done():
            task.cancel()
    await asyncio.gather(
        *(task for task in (limit_task, abort_task) if task), return_exceptions=True
    )
    capture = await collector.finalize()
    exit_code = process.returncode if process.returncode is not None and process.returncode >= 0 else None
    return {
        "exitCode": exit_code,
        "exitSignal": _exit_signal(process.returncode),
        **({"processIdentity": process_identity} if process_identity else {}),
        **capture,
        "timedOut": timed_out,
        "aborted": aborted,
        "stdioLeakDetected": stdio_leak,
        "backgroundDescendantsDetected": background_descendants,
        "backgroundDescendantsTerminated": background_terminated,
        "pipesForciblyClosed": pipes_closed,
        **(
            {
                "diagnostic": (
                    STDIO_LEAK_DIAGNOSTIC
                    if stdio_leak
                    else BACKGROUND_DESCENDANT_DIAGNOSTIC
                )
            }
            if background_descendants
            else {}
        ),
    }


async def _read_stream(reader: asyncio.StreamReader | None, kind: str, collector: Any) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            return
        await collector.append(kind, chunk)


async def _settle_process(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
    grace_ms: int,
    fallback_ms: int,
) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), max(0.001, grace_ms / 1000))
        return
    except asyncio.TimeoutError:
        _signal_process_tree(process, signal.SIGKILL)
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), max(0.001, fallback_ms / 1000))
    except asyncio.TimeoutError:
        _close_pipe_transports(process)
        if not wait_task.done():
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)


async def _drain_readers_and_group(
    process: asyncio.subprocess.Process,
    reader_tasks: set[asyncio.Task[Any]],
    grace_ms: int,
) -> bool:
    timeout = max(0.001, grace_ms / 1000)
    done, pending = await asyncio.wait(reader_tasks, timeout=timeout)
    if len(done) == len(reader_tasks) and not _process_group_live(process.pid):
        return False
    _signal_process_tree(process, signal.SIGKILL)
    done, pending = await asyncio.wait(reader_tasks, timeout=timeout)
    if not pending and not _process_group_live(process.pid):
        return False
    _close_pipe_transports(process)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return True


def _signal_process_tree(process: asyncio.subprocess.Process, sig: signal.Signals) -> bool:
    if os.name != "nt" and process.pid:
        try:
            os.killpg(process.pid, sig)
            return True
        except OSError:
            pass
    if process.returncode is not None:
        return False
    try:
        process.send_signal(sig)
        return True
    except ProcessLookupError:
        return False


def _process_group_live(pid: int | None) -> bool:
    if os.name == "nt" or not pid:
        return False
    try:
        os.killpg(pid, 0)
        return True
    except OSError:
        return False


def _close_pipe_transports(process: asyncio.subprocess.Process) -> None:
    transport = getattr(process, "_transport", None)
    for descriptor in (1, 2):
        try:
            pipe = transport.get_pipe_transport(descriptor) if transport else None
            if pipe:
                pipe.close()
        except Exception:
            pass


def _create_abort_task(abort_signal: Any) -> asyncio.Task[Any] | None:
    if abort_signal is None:
        return None
    if isinstance(abort_signal, asyncio.Event):
        return asyncio.create_task(abort_signal.wait())

    async def poll() -> None:
        while not bool(getattr(abort_signal, "aborted", False)):
            await asyncio.sleep(0.02)

    return asyncio.create_task(poll())


def _exit_signal(returncode: int | None) -> str | None:
    if returncode is None or returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"SIG{-returncode}"


