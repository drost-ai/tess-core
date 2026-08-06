from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal as signal_module
import subprocess
import sys
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Mapping

from .identity_darwin import inspect_darwin_process as _native_inspect_darwin_process


MAX_PROCESS_COMMAND_BYTES = 128 * 1024
PROCESS_IDENTITY_RETRY_DELAYS_SECONDS = (0.02, 0.06)
_STABLE_IDENTITY_DELAYS_SECONDS = (0.015, 0.03, 0.045)


class ProcessIdentityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


ObservedProcessIdentity = dict[str, Any]
ProcessIdentityObserver = Callable[[int], Awaitable[ObservedProcessIdentity] | ObservedProcessIdentity]
_process_identity_observer_for_tests: ProcessIdentityObserver | None = None


def __setProcessIdentityObserverForTests(observer: ProcessIdentityObserver | None) -> None:
    """Internal deterministic test seam; not part of the public package API."""
    global _process_identity_observer_for_tests
    _process_identity_observer_for_tests = observer


async def captureOwnedProcessIdentity(
    args: Mapping[str, Any] | None = None,
    *,
    pid: int | None = None,
    expectedArgv: list[str] | None = None,
    spawnedAt: str | None = None,
) -> dict[str, Any]:
    values = dict(args or {})
    process_id = _require_pid(values.get("pid", pid))
    argv = values.get("expectedArgv", expectedArgv)
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise _invalid_identity("expected argv must be a string array")
    observed = await _inspect_stable_process_identity(process_id)
    if sys.platform != "win32" and observed["processGroupId"] != process_id:
        raise ProcessIdentityError(
            "process.group_not_owned",
            f"Spawned process {process_id} is not the leader of its own process group "
            f"({observed['processGroupId']}).",
        )
    return {
        "schemaVersion": 1,
        **observed,
        "spawnedAt": values.get("spawnedAt", spawnedAt) or _now_iso(),
        "expectedCommandDigest": digestExpectedCommand(argv),
    }


async def captureProcessBirthMarker(pid: int | None = None) -> dict[str, Any]:
    observed = await _inspect_process_identity(_require_pid(os.getpid() if pid is None else pid))
    return {
        "schemaVersion": 1,
        "pid": observed["pid"],
        "startToken": observed["startToken"],
        "platform": observed["platform"],
    }


async def verifyProcessBirthMarker(value: Any) -> Literal["live", "dead", "unknown"]:
    if not isinstance(value, dict):
        return "unknown"
    pid = value.get("pid")
    if (
        value.get("schemaVersion") != 1
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not _is_bounded_token(value.get("startToken"))
        or not isinstance(value.get("platform"), str)
        or len(value["platform"]) > 32
    ):
        return "unknown"
    try:
        observed = await _inspect_process_identity(pid)
    except ProcessIdentityError as exc:
        return "dead" if exc.code == "process.not_found" else "unknown"
    return (
        "live"
        if observed["startToken"] == value["startToken"]
        and observed["platform"] == value["platform"]
        else "dead"
    )


async def verifyOwnedProcessIdentity(value: Any) -> dict[str, Any]:
    persisted = parseOwnedProcessIdentity(value)
    observed = await _inspect_process_identity(persisted["pid"])
    mismatches: list[str] = []
    if persisted["platform"] != observed["platform"]:
        mismatches.append("platform")
    if persisted["processGroupId"] != observed["processGroupId"]:
        mismatches.append("process group")
    if persisted["startToken"] != observed["startToken"]:
        mismatches.append("start token")
    if persisted["observedCommandDigest"] != observed["observedCommandDigest"]:
        mismatches.append("command digest")
    if mismatches:
        raise ProcessIdentityError(
            "process.identity_mismatch",
            f"Refusing to signal PID {persisted['pid']}: persisted {', '.join(mismatches)} "
            "no longer match the live process.",
        )
    if sys.platform != "win32" and persisted["processGroupId"] != persisted["pid"]:
        raise ProcessIdentityError(
            "process.group_not_owned",
            f"Refusing to signal process group {persisted['processGroupId']}: task PID "
            f"{persisted['pid']} is not its leader.",
        )
    return persisted


async def signalOwnedProcess(value: Any, signal: int | signal_module.Signals = signal_module.SIGTERM) -> dict[str, Any]:
    persisted = await verifyOwnedProcessIdentity(value)
    if sys.platform == "win32":
        os.kill(persisted["pid"], int(signal))
    else:
        os.killpg(persisted["processGroupId"], int(signal))
    return persisted


def parseOwnedProcessIdentity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid_identity(
            "identity is missing; legacy live tasks must finish naturally or be migrated before stop/restart"
        )
    pid = _require_pid(value.get("pid"))
    process_group_id = _require_pid(value.get("processGroupId"))
    if value.get("schemaVersion") != 1:
        raise _invalid_identity("unsupported schema version")
    if not _is_iso_timestamp(value.get("spawnedAt")):
        raise _invalid_identity("spawnedAt is invalid")
    if not _is_bounded_token(value.get("startToken")):
        raise _invalid_identity("start token is invalid")
    if not _is_sha256(value.get("observedCommandDigest")):
        raise _invalid_identity("observed command digest is invalid")
    if not _is_sha256(value.get("expectedCommandDigest")):
        raise _invalid_identity("expected command digest is invalid")
    platform = value.get("platform")
    if not isinstance(platform, str) or not platform or len(platform) > 32:
        raise _invalid_identity("platform is invalid")
    return {
        "schemaVersion": 1,
        "pid": pid,
        "processGroupId": process_group_id,
        "spawnedAt": value["spawnedAt"],
        "startToken": value["startToken"],
        "observedCommandDigest": value["observedCommandDigest"],
        "expectedCommandDigest": value["expectedCommandDigest"],
        "platform": platform,
    }


def processIdentityFromRecord(
    record: Mapping[str, Any] | None,
    meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record_metadata = record.get("metadata") if isinstance(record, Mapping) else None
    from_record = record.get("processIdentity") if isinstance(record, Mapping) else None
    if from_record is None and isinstance(record_metadata, Mapping):
        from_record = record_metadata.get("processIdentity")
    from_meta = meta.get("processIdentity") if isinstance(meta, Mapping) else None
    identity = parseOwnedProcessIdentity(from_record if from_record is not None else from_meta)
    if from_record is not None and from_meta is not None:
        other = parseOwnedProcessIdentity(from_meta)
        if identity != other:
            raise ProcessIdentityError(
                "process.identity_mismatch",
                f"Refusing process operation for PID {identity['pid']}: task record and metadata "
                "identities disagree.",
            )
    return identity


def digestExpectedCommand(argv: list[str]) -> str:
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _inspect_process_identity(pid: int) -> ObservedProcessIdentity:
    for attempt in range(len(PROCESS_IDENTITY_RETRY_DELAYS_SECONDS) + 1):
        try:
            return await _inspect_process_identity_once(pid)
        except ProcessIdentityError as exc:
            if exc.code != "process.identity_unavailable" or attempt >= len(PROCESS_IDENTITY_RETRY_DELAYS_SECONDS):
                raise
            await asyncio.sleep(PROCESS_IDENTITY_RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("unreachable")


async def _inspect_process_identity_once(pid: int) -> ObservedProcessIdentity:
    if _process_identity_observer_for_tests is not None:
        result = _process_identity_observer_for_tests(pid)
        return await result if hasattr(result, "__await__") else result  # type: ignore[return-value]
    if sys.platform.startswith("linux"):
        return await asyncio.to_thread(_inspect_linux_process, pid)
    if sys.platform == "darwin":
        return await asyncio.to_thread(_inspect_darwin_process, pid)
    if sys.platform in {"freebsd", "openbsd"}:
        return await asyncio.to_thread(_inspect_ps_process, pid)
    if sys.platform == "win32":
        return await asyncio.to_thread(_inspect_windows_process, pid)
    raise ProcessIdentityError(
        "process.identity_unsupported",
        f"Process identity inspection is not implemented on {sys.platform}; refusing "
        "ownership-sensitive process control.",
    )


async def _inspect_stable_process_identity(pid: int) -> ObservedProcessIdentity:
    previous = await _inspect_process_identity(pid)
    for delay in _STABLE_IDENTITY_DELAYS_SECONDS:
        await asyncio.sleep(delay)
        current = await _inspect_process_identity(pid)
        if (
            previous["startToken"] == current["startToken"]
            and previous["processGroupId"] == current["processGroupId"]
            and previous["observedCommandDigest"] == current["observedCommandDigest"]
        ):
            return current
        if (
            previous["startToken"] != current["startToken"]
            or previous["processGroupId"] != current["processGroupId"]
        ):
            raise ProcessIdentityError(
                "process.identity_mismatch",
                f"Process {pid} changed birth token or process group during launch identity capture.",
            )
        previous = current
    return previous


def _inspect_linux_process(pid: int) -> ObservedProcessIdentity:
    try:
        stat = _read_bounded_file(f"/proc/{pid}/stat", 64 * 1024)
        command = _read_bounded_file(f"/proc/{pid}/cmdline", MAX_PROCESS_COMMAND_BYTES)
        boot_id = _read_bounded_file("/proc/sys/kernel/random/boot_id", 256).decode("utf-8")
    except Exception as exc:
        raise _classify_inspection_failure(pid, "Linux process metadata could not be read", exc) from exc
    stat_text = stat.decode("utf-8", errors="strict")
    closing_paren = stat_text.rfind(")")
    if closing_paren < 0:
        raise _invalid_identity(f"unable to parse /proc/{pid}/stat")
    fields = stat_text[closing_paren + 1 :].strip().split()
    try:
        process_group_id = int(fields[2])
        start_ticks = fields[19]
    except (IndexError, TypeError, ValueError) as exc:
        raise _invalid_identity(f"unable to parse process group/start token for PID {pid}") from exc
    if process_group_id <= 0 or not start_ticks:
        raise _invalid_identity(f"unable to parse process group/start token for PID {pid}")
    return {
        "pid": pid,
        "processGroupId": process_group_id,
        "startToken": f"linux:{boot_id.strip()}:{start_ticks}",
        "observedCommandDigest": hashlib.sha256(command).hexdigest(),
        "platform": sys.platform,
    }


def _inspect_darwin_process(pid: int) -> ObservedProcessIdentity:
    """Read stable process identity without invoking a user-configured shell.

    Darwin exposes the process group and start timestamp through
    ``PROC_PIDTBSDINFO`` and the executable through ``proc_pidpath``.  Besides
    avoiding locale-sensitive ``ps`` output, this keeps identity inspection
    available in restricted runtimes that permit inspection of owned processes
    but deny process-listing executables.
    """

    try:
        return _native_inspect_darwin_process(
            pid,
            max_command_bytes=MAX_PROCESS_COMMAND_BYTES,
        )
    except Exception as exc:
        raise _classify_inspection_failure(
            pid, "Darwin process metadata could not be read", exc
        ) from exc


def _inspect_ps_process(pid: int) -> ObservedProcessIdentity:
    env = dict(os.environ)
    env.update({"LANG": "C", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "pgid=", "-o", "lstart=", "-o", "command="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            env=env,
        )
    except Exception as exc:
        raise _classify_inspection_failure(pid, "ps did not return process metadata", exc) from exc
    if len(completed.stdout) > MAX_PROCESS_COMMAND_BYTES or completed.returncode != 0:
        raise _classify_inspection_failure(pid, "ps did not return process metadata")
    line = completed.stdout.decode("utf-8", errors="replace").strip()
    # BSD lstart is five whitespace-separated fields: weekday month day time year.
    parts = line.split(None, 6)
    if len(parts) != 7:
        raise _classify_inspection_failure(pid, "ps output could not be parsed")
    try:
        process_group_id = int(parts[0])
    except ValueError as exc:
        raise _classify_inspection_failure(pid, "ps returned an invalid process group", exc) from exc
    if process_group_id <= 0:
        raise _classify_inspection_failure(pid, "ps returned an invalid process group")
    birth = " ".join(parts[1:6])
    command = parts[6]
    return {
        "pid": pid,
        "processGroupId": process_group_id,
        "startToken": f"ps:{birth}",
        "observedCommandDigest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "platform": sys.platform,
    }


def _inspect_windows_process(pid: int) -> ObservedProcessIdentity:  # pragma: no cover - Windows CI only
    script = (
        f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
        "if ($null -eq $p) { exit 3 }; "
        "$p | Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
        if completed.returncode != 0 or len(completed.stdout) > MAX_PROCESS_COMMAND_BYTES:
            raise OSError(f"powershell exited {completed.returncode}")
        parsed = json.loads(completed.stdout)
    except Exception as exc:
        raise _classify_inspection_failure(pid, "Windows process metadata could not be read", exc) from exc
    if parsed.get("ProcessId") != pid or not isinstance(parsed.get("CreationDate"), str):
        raise _classify_inspection_failure(pid, "Windows process metadata was incomplete")
    command = str(parsed.get("CommandLine") or "")
    return {
        "pid": pid,
        "processGroupId": pid,
        "startToken": f"windows:{parsed['CreationDate']}",
        "observedCommandDigest": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "platform": sys.platform,
    }


def _classify_inspection_failure(pid: int, reason: str, cause: Exception | None = None) -> ProcessIdentityError:
    liveness = _probe_process_liveness(pid)
    if liveness == "dead":
        return ProcessIdentityError("process.not_found", f"Process {pid} is no longer live.")
    code = getattr(cause, "errno", None) or getattr(cause, "code", None)
    suffix = f" ({code})" if code is not None else ""
    return ProcessIdentityError(
        "process.identity_unavailable",
        f"Process {pid} is live or its liveness is indeterminate, but its identity is temporarily "
        f"unavailable: {reason}{suffix}.",
    )


def _probe_process_liveness(pid: int) -> Literal["live", "dead", "unknown"]:
    try:
        os.kill(pid, 0)
        return "live"
    except PermissionError:
        return "live"
    except ProcessLookupError:
        return "dead"
    except OSError:
        return "unknown"


def _read_bounded_file(path: str, max_bytes: int) -> bytes:
    with open(path, "rb", buffering=0) as handle:
        value = handle.read(max_bytes + 1)
    if len(value) > max_bytes:
        raise _invalid_identity(f"process identity field exceeds {max_bytes} bytes")
    return value


def _require_pid(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid_identity("PID/process group must be a positive safe integer")
    return value


def _is_iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _is_bounded_token(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 512


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _invalid_identity(reason: str) -> ProcessIdentityError:
    return ProcessIdentityError("process.identity_invalid", f"Invalid persisted process identity: {reason}.")


def _now_iso() -> str:
    from .._utils import now_iso

    return now_iso()


capture_owned_process_identity = captureOwnedProcessIdentity
capture_process_birth_marker = captureProcessBirthMarker
verify_process_birth_marker = verifyProcessBirthMarker
verify_owned_process_identity = verifyOwnedProcessIdentity
signal_owned_process = signalOwnedProcess
parse_owned_process_identity = parseOwnedProcessIdentity
process_identity_from_record = processIdentityFromRecord
digest_expected_command = digestExpectedCommand
