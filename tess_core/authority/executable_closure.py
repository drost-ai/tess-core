from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import time
import uuid
from typing import Any, Callable

from .repository_trust import resolve_repository_identity
from ..storage.paths import get_state_root


EXECUTABLE_CLOSURE_POLICY_VERSION = 1
DEFAULT_EXECUTABLE_CLOSURE_LIMITS = {
    "maxFiles": 4_096,
    "maxFileBytes": 8 * 1024 * 1024,
    "maxTotalFileBytes": 64 * 1024 * 1024,
    "maxRelativePathBytes": 4 * 1024,
    "maxDepth": 64,
    "maxTraversalMs": 5_000,
    "maxHashConcurrency": 4,
    "maxSnapshotDiskBytes": 72 * 1024 * 1024,
    "minFreeDiskBytesAfterSnapshot": 64 * 1024 * 1024,
}


class ExecutableClosureError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        limitKind: str,
        observed: int = 0,
        limit: int | None = None,
    ):
        comparison = "" if limit is None else f" (limit {limit})"
        super().__init__(f"Executable closure {limitKind} rejected at {observed}{comparison}")
        self.code = code
        self.limitKind = limitKind
        self.observed = observed
        self.limit = limit


_closure_test_hook: Callable[[str], Any] | None = None


def _set_executable_closure_test_hook_for_tests(hook: Callable[[str], Any] | None) -> None:
    global _closure_test_hook
    _closure_test_hook = hook


async def _call_hook(phase: str) -> None:
    if _closure_test_hook is None:
        return
    result = _closure_test_hook(phase)
    if inspect.isawaitable(result):
        await result


def _limit(kind: str, observed: int, limit: int) -> ExecutableClosureError:
    return ExecutableClosureError(
        code="executable_closure_limit",
        limitKind=kind,
        observed=observed,
        limit=limit,
    )


def _unavailable(kind: str) -> ExecutableClosureError:
    return ExecutableClosureError(code="executable_closure_unavailable", limitKind=kind)


def _check_deadline(started: float, limits: dict[str, int]) -> None:
    elapsed = int((time.monotonic() - started) * 1000)
    if elapsed > limits["maxTraversalMs"]:
        raise _limit("traversal_ms", elapsed, limits["maxTraversalMs"])


def _contained(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False


def _validate_relative(relative: str, limits: dict[str, int]) -> None:
    size = len(relative.encode("utf-8"))
    if size > limits["maxRelativePathBytes"]:
        raise _limit("relative_path_bytes", size, limits["maxRelativePathBytes"])
    depth = len(relative.split(os.sep))
    if depth > limits["maxDepth"]:
        raise _limit("depth", depth, limits["maxDepth"])


async def _collect(
    spec: dict[str, Any], limits: dict[str, int], started: float
) -> dict[str, Any]:
    repository = await resolve_repository_identity(str(spec["cwd"]))
    entries: list[dict[str, Any]] = []
    roots: list[dict[str, str]] = []
    counters = {"fileCount": 0, "totalFileBytes": 0, "memberCount": 0}

    def add_entry(source: str, relative: str) -> None:
        _check_deadline(started, limits)
        _validate_relative(relative, limits)
        try:
            info = os.lstat(source)
        except FileNotFoundError:
            raise _unavailable("missing_member") from None
        if stat.S_ISLNK(info.st_mode):
            raise _unavailable("symbolic_link")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise _unavailable("non_regular_member")
        counters["memberCount"] += 1
        entry = {
            "sourcePath": source,
            "relativePath": relative,
            "type": "directory" if stat.S_ISDIR(info.st_mode) else "file",
            "mode": stat.S_IMODE(info.st_mode) & 0o777,
            "size": info.st_size,
            "dev": info.st_dev,
            "ino": info.st_ino,
            "mtimeNs": info.st_mtime_ns,
        }
        entries.append(entry)
        if entry["type"] == "file":
            counters["fileCount"] += 1
            if counters["fileCount"] > limits["maxFiles"]:
                raise _limit("files", counters["fileCount"], limits["maxFiles"])
            if info.st_size > limits["maxFileBytes"]:
                raise _limit("file_bytes", info.st_size, limits["maxFileBytes"])
            counters["totalFileBytes"] += info.st_size
            if counters["totalFileBytes"] > limits["maxTotalFileBytes"]:
                raise _limit(
                    "total_file_bytes",
                    counters["totalFileBytes"],
                    limits["maxTotalFileBytes"],
                )
            return
        try:
            children = os.listdir(source)
        except OSError:
            raise _unavailable("unreadable_directory") from None
        if len(children) + counters["memberCount"] > limits["maxFiles"] * 2 + limits["maxDepth"]:
            raise _limit(
                "members",
                len(children) + counters["memberCount"],
                limits["maxFiles"] * 2 + limits["maxDepth"],
            )
        children.sort(key=lambda value: os.fsencode(value))
        for child in children:
            add_entry(os.path.join(source, child), os.path.join(relative, child))

    for index, configured in enumerate(spec.get("roots") or []):
        _check_deadline(started, limits)
        configured = os.path.abspath(str(configured))
        try:
            initial = os.lstat(configured)
        except FileNotFoundError:
            raise _unavailable("missing_root") from None
        if stat.S_ISLNK(initial.st_mode):
            raise _unavailable("symbolic_link_root")
        canonical = os.path.realpath(configured)
        if not _contained(repository["canonicalRoot"], canonical):
            raise _unavailable("root_escape")
        relative_root = os.path.join("roots", f"{index:04d}")
        if not stat.S_ISDIR(initial.st_mode):
            relative_root = os.path.join(relative_root, os.path.basename(canonical))
        roots.append({"sourcePath": canonical, "snapshotRelativePath": relative_root})
        add_entry(canonical, relative_root)
    entries.sort(key=lambda value: os.fsencode(value["relativePath"]))
    return {
        **counters,
        "entries": entries,
        "roots": roots,
        "repositoryKey": repository["repositoryKey"],
        "canonicalRoot": repository["canonicalRoot"],
    }


def _open_no_follow(path: str) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def _hash_files(
    collected: dict[str, Any],
    limits: dict[str, int],
    started: float,
    snapshot_temp: str | None,
) -> int:
    actual_total = 0
    snapshot_bytes = 0
    for entry in collected["entries"]:
        if entry["type"] != "file":
            continue
        _check_deadline(started, limits)
        source_fd = _open_no_follow(entry["sourcePath"])
        destination_fd: int | None = None
        try:
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                entry["dev"], entry["ino"], entry["size"]
            ):
                raise _unavailable("changed_during_traversal")
            if snapshot_temp:
                destination_path = os.path.join(snapshot_temp, entry["relativePath"])
                os.makedirs(os.path.dirname(destination_path), mode=0o700, exist_ok=True)
                mode = 0o500 if entry["mode"] & 0o111 else 0o400
                destination_fd = os.open(
                    destination_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    mode,
                )
            digest = hashlib.sha256()
            position = 0
            while True:
                _check_deadline(started, limits)
                chunk = os.read(source_fd, 64 * 1024)
                if not chunk:
                    break
                position += len(chunk)
                actual_total += len(chunk)
                if destination_fd is not None:
                    snapshot_bytes += len(chunk)
                if position > limits["maxFileBytes"]:
                    raise _limit("file_bytes", position, limits["maxFileBytes"])
                if actual_total > limits["maxTotalFileBytes"]:
                    raise _limit("total_file_bytes", actual_total, limits["maxTotalFileBytes"])
                digest.update(chunk)
                if destination_fd is not None:
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        view = view[written:]
            final = os.fstat(source_fd)
            if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
                entry["dev"], entry["ino"], position, entry["mtimeNs"]
            ):
                raise _unavailable("changed_during_hash")
            entry["contentDigest"] = digest.hexdigest()
            if destination_fd is not None:
                os.fsync(destination_fd)
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            os.close(source_fd)
    return snapshot_bytes


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _closure_digest(spec: dict[str, Any], collected: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(
        _json_bytes(
            {
                "version": EXECUTABLE_CLOSURE_POLICY_VERSION,
                "family": spec["family"],
                "scope": spec["scope"],
                "descriptorDigest": spec["descriptorDigest"],
                "limits": DEFAULT_EXECUTABLE_CLOSURE_LIMITS,
            }
        )
    )
    for entry in collected["entries"]:
        digest.update(b"\0")
        digest.update(
            _json_bytes(
                {
                    "path": entry["relativePath"].replace(os.sep, "/"),
                    "type": entry["type"],
                    "mode": entry["mode"],
                    "size": entry["size"],
                    "contentDigest": entry.get("contentDigest"),
                }
            )
        )
    return digest.hexdigest()


def _normalized_limits(overrides: dict[str, Any] | None) -> dict[str, int]:
    limits = {**DEFAULT_EXECUTABLE_CLOSURE_LIMITS, **(overrides or {})}
    for key, value in limits.items():
        if not isinstance(value, int) or value < 1:
            raise ValueError(f"Invalid executable closure limit {key}")
    return limits


async def _inspect_internal(
    spec: dict[str, Any], limits: dict[str, int], snapshot_temp: str | None = None
) -> dict[str, Any]:
    if spec.get("family") not in {"plugin", "mcp"} or spec.get("scope") not in {"project", "local"}:
        raise ValueError("Invalid executable closure family or scope")
    started = time.monotonic()
    collected = await _collect(spec, limits, started)
    metadata_bytes = sum(len(entry["relativePath"].encode("utf-8")) + 128 for entry in collected["entries"])
    if snapshot_temp and collected["totalFileBytes"] + metadata_bytes > limits["maxSnapshotDiskBytes"]:
        raise _limit(
            "snapshot_disk_bytes",
            collected["totalFileBytes"] + metadata_bytes,
            limits["maxSnapshotDiskBytes"],
        )
    copied = _hash_files(collected, limits, started, snapshot_temp)
    return {
        "version": EXECUTABLE_CLOSURE_POLICY_VERSION,
        "digest": _closure_digest(spec, collected),
        "repositoryKey": collected["repositoryKey"],
        "canonicalRoot": collected["canonicalRoot"],
        "fileCount": collected["fileCount"],
        "totalFileBytes": collected["totalFileBytes"],
        "memberCount": collected["memberCount"],
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "roots": collected["roots"],
        "snapshotDiskBytes": (copied if snapshot_temp else collected["totalFileBytes"]) + metadata_bytes,
    }


async def inspect_executable_closure(
    spec: dict[str, Any], limitOverrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = await _inspect_internal(spec, _normalized_limits(limitOverrides))
    result.pop("snapshotDiskBytes", None)
    return result


def _ensure_snapshot_base() -> str:
    base = os.path.join(get_state_root(), "trust", "snapshots")
    os.makedirs(base, mode=0o700, exist_ok=True)
    info = os.lstat(base)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise _unavailable("unsafe_snapshot_directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise _unavailable("foreign_snapshot_directory")
    return base


async def snapshot_executable_closure(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    spec = args["spec"]
    expected = str(args["expectedDigest"])
    limits = _normalized_limits(args.get("limitOverrides"))
    await _call_hook("before_snapshot")
    base = _ensure_snapshot_base()
    preflight = await _inspect_internal(spec, limits)
    if preflight["digest"] != expected:
        raise _unavailable("digest_changed")
    free = shutil.disk_usage(base).free
    remaining = max(0, free - preflight["snapshotDiskBytes"])
    if remaining < limits["minFreeDiskBytesAfterSnapshot"]:
        raise _limit(
            "free_disk_bytes_after_snapshot",
            remaining,
            limits["minFreeDiskBytesAfterSnapshot"],
        )
    temporary = os.path.join(base, f".snapshot-{os.getpid()}-{uuid.uuid4().hex}")
    os.mkdir(temporary, 0o700)
    try:
        inspection = await _inspect_internal(spec, limits, temporary)
        if inspection["digest"] != expected:
            raise _unavailable("digest_changed")
        target = os.path.join(
            base,
            inspection["repositoryKey"],
            spec["family"],
            spec["scope"],
            inspection["digest"],
        )
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        marker = {
            "version": EXECUTABLE_CLOSURE_POLICY_VERSION,
            "digest": inspection["digest"],
            "repositoryKey": inspection["repositoryKey"],
            "roots": inspection["roots"],
            "fileCount": inspection["fileCount"],
            "totalFileBytes": inspection["totalFileBytes"],
        }
        marker_path = os.path.join(temporary, ".closure.json")
        descriptor = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(marker) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.rename(temporary, target)
        except OSError:
            existing_marker = os.path.join(target, ".closure.json")
            try:
                with open(existing_marker, "r", encoding="utf-8") as handle:
                    existing = json.load(handle)
            except Exception:
                raise _unavailable("snapshot_collision") from None
            if existing.get("digest") != inspection["digest"]:
                raise _unavailable("snapshot_collision")
            shutil.rmtree(temporary)
        await _call_hook("after_snapshot")
        return {**inspection, "snapshotRoot": target}
    except Exception:
        if os.path.exists(temporary):
            shutil.rmtree(temporary)
        raise


def map_closure_root_to_snapshot(snapshot: dict[str, Any], sourcePath: str) -> str | None:
    normalized = os.path.normpath(sourcePath)
    for root in snapshot.get("roots") or []:
        if os.path.normpath(str(root.get("sourcePath"))) == normalized:
            return os.path.join(snapshot["snapshotRoot"], root["snapshotRelativePath"])
    return None


