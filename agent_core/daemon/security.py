from __future__ import annotations

from .._utils import is_process_alive as _is_process_alive

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import uuid
from pathlib import Path
from typing import Any

from ..storage import (
    daemon_instance_dir,
    daemon_logs_dir,
    daemon_socket_path,
    daemon_state_path,
    get_daemon_dir,
    get_daemon_instances_dir,
    get_state_root,
    parse_daemon_instance_id,
)
from .protocol import DAEMON_PROTOCOL_VERSION



PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_DAEMON_TREE_ENTRIES = 4_096
MAX_DAEMON_TREE_DEPTH = 8
MAX_DAEMON_STATE_BYTES = 2 * 1024 * 1024
MAX_DAEMON_TOKEN_BYTES = 256
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")


class DaemonSecurityError(RuntimeError):
    def __init__(self, code: str, message: str, targetPath: str):
        super().__init__(f"{message} Manual remediation is required for {targetPath}.")
        self.name = "DaemonSecurityError"
        self.code = code
        self.targetPath = targetPath


def daemonAuthTokenPath(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "instance-token")


async def prepareDaemonRuntimeSecurity(
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    settings = {**(options or {}), **kwargs}
    instance_id = parse_daemon_instance_id(settings["instanceId"])
    home = get_state_root()
    await _verify_canonical_ancestors(home)
    _ensure_owned_directory(home)
    _ensure_owned_directory(os.path.join(home, "locks"))
    from ..storage import with_record_transaction

    async with with_record_transaction(f"daemon-security-{instance_id}", home, timeoutMs=5_000):
        await _prepare_under_lock({**settings, "instanceId": instance_id})


async def _prepare_under_lock(settings: dict[str, Any]) -> None:
    instance_id = settings["instanceId"]
    instance_dir = daemon_instance_dir(instance_id)
    _ensure_owned_directory(get_daemon_dir())
    _ensure_owned_directory(get_daemon_instances_dir())
    _ensure_owned_directory(instance_dir)
    _ensure_owned_directory(daemon_logs_dir(instance_id))
    changes = _inspect_daemon_tree(instance_dir, instance_id)
    external_socket = daemon_socket_path(instance_id)
    if not _is_within(external_socket, instance_dir):
        socket_change = _inspect_optional_socket(external_socket)
        if socket_change:
            changes.append(socket_change)
    if not changes:
        return
    if settings.get("requireStoppedForMigration"):
        state = _read_legacy_state_for_security(instance_id)
        pid = (state or {}).get("pid")
        if isinstance(pid, int) and _is_process_alive(pid):
            raise DaemonSecurityError(
                "daemon.security_live_migration",
                f"Refusing to migrate daemon permissions while pid {pid} is live. Stop the old daemon first.",
                instance_dir,
            )
        if await isDaemonSocketListenerActive(external_socket):
            raise DaemonSecurityError(
                "daemon.security_live_migration",
                "Refusing to migrate daemon permissions while the existing socket still accepts connections.",
                external_socket,
            )
    await _apply_mode_changes(changes, settings.get("chmod"), instance_id)
    _verify_private_tree(instance_dir, external_socket, instance_id)


async def verifyPrivateDaemonRuntime(instanceId: str) -> None:
    instance_id = parse_daemon_instance_id(instanceId)
    home = get_state_root()
    await _verify_canonical_ancestors(home)
    home_stat = os.lstat(home)
    _assert_owned_directory(home_stat, home)
    if stat.S_IMODE(home_stat.st_mode) & 0o022:
        raise _unsafe("Runtime home is writable by another user.", home)
    for ancestor in (get_daemon_dir(), get_daemon_instances_dir()):
        info = os.lstat(ancestor)
        _assert_owned_directory(info, ancestor)
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise _unsafe("Daemon directory is writable by another user.", ancestor)
    _verify_private_tree(daemon_instance_dir(instance_id), daemon_socket_path(instance_id), instance_id)


def generateDaemonAuthMaterial() -> dict[str, str]:
    token = secrets.token_urlsafe(32)
    return {"token": token, "tokenId": daemonTokenId(token)}


async def publishDaemonAuthMaterial(instanceId: str, auth: dict[str, str]) -> None:
    token = auth.get("token")
    token_id = auth.get("tokenId")
    if (
        not isinstance(token, str)
        or not _TOKEN_PATTERN.fullmatch(token)
        or not isinstance(token_id, str)
        or token_id != daemonTokenId(token)
    ):
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "Refusing to publish malformed daemon auth material.",
            daemonAuthTokenPath(instanceId),
        )
    target = daemonAuthTokenPath(instanceId)
    temp = f"{target}.{os.getpid()}.{uuid.uuid4()}.tmp"
    published = False
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
        try:
            _write_all_private(fd, f"{token}\n".encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temp, target)
        published = True
        os.unlink(temp)
        os.chmod(target, PRIVATE_FILE_MODE)
        _verify_owned_regular_file(target, require_private=True)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        if published:
            with contextlib.suppress(Exception):
                await removeDaemonAuthMaterialIfOwned(instanceId, token_id)
        raise


async def createDaemonAuthMaterial(instanceId: str) -> dict[str, str]:
    auth = generateDaemonAuthMaterial()
    await publishDaemonAuthMaterial(instanceId, auth)
    return auth


async def readDaemonAuthMaterial(instanceId: str, state: dict[str, Any]) -> dict[str, str]:
    if state.get("protocolVersion") != DAEMON_PROTOCOL_VERSION or not isinstance(state.get("authTokenId"), str):
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "The daemon state uses an unauthenticated or unsupported protocol version.",
            daemon_state_path(instanceId),
        )
    target = daemonAuthTokenPath(instanceId)
    info = _verify_owned_regular_file(target, require_private=True)
    if info.st_size > MAX_DAEMON_TOKEN_BYTES:
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "The daemon instance token exceeds its byte limit.",
            target,
        )
    with open(target, "r", encoding="utf-8") as handle:
        token = handle.read(MAX_DAEMON_TOKEN_BYTES + 1).strip()
    token_id = daemonTokenId(token)
    if not _TOKEN_PATTERN.fullmatch(token) or token_id != state["authTokenId"]:
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "The daemon instance token does not match the authenticated state record.",
            target,
        )
    return {"token": token, "tokenId": token_id}


def verifyOwnedDaemonSocket(socketPath: str) -> os.stat_result | None:
    try:
        info = os.lstat(socketPath)
    except FileNotFoundError:
        return None
    _assert_owned(info, socketPath)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise _unsafe("Daemon socket path is not an owned Unix socket.", socketPath)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _unsafe("Daemon socket permissions are not owner-only.", socketPath)
    return info


async def isDaemonSocketListenerActive(socketPath: str) -> bool:
    try:
        info = os.lstat(socketPath)
    except FileNotFoundError:
        return False
    _assert_owned(info, socketPath)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise _unsafe("Daemon socket path is not an owned Unix socket.", socketPath)

    def probe() -> bool:
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        candidate.settimeout(0.25)
        try:
            candidate.connect(socketPath)
            return True
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except OSError:
            return True
        finally:
            candidate.close()

    return await asyncio.to_thread(probe)


async def removeInactiveOwnedDaemonSocket(socketPath: str) -> None:
    before = verifyOwnedDaemonSocket(socketPath)
    if not before:
        return
    if await isDaemonSocketListenerActive(socketPath):
        raise DaemonSecurityError(
            "daemon.security_live_migration",
            "Refusing to remove a daemon socket that still accepts connections.",
            socketPath,
        )
    after = verifyOwnedDaemonSocket(socketPath)
    if not after:
        return
    if not _same_identity(before, after):
        raise _unsafe("Daemon socket identity changed during verified cleanup.", socketPath)
    os.unlink(socketPath)


async def removeDaemonAuthMaterialIfOwned(instanceId: str, tokenId: str) -> None:
    target = daemonAuthTokenPath(instanceId)
    try:
        info = _verify_owned_regular_file(target, require_private=True)
    except FileNotFoundError:
        return
    if info.st_size > MAX_DAEMON_TOKEN_BYTES:
        return
    with open(target, "r", encoding="utf-8") as handle:
        token = handle.read(MAX_DAEMON_TOKEN_BYTES + 1).strip()
    if daemonTokenId(token) == tokenId:
        os.unlink(target)


async def removeOwnedDaemonAuthMaterial(instanceId: str) -> None:
    target = daemonAuthTokenPath(instanceId)
    try:
        before = _verify_owned_regular_file(target, require_private=True)
    except FileNotFoundError:
        return
    after = _verify_owned_regular_file(target, require_private=True)
    if not _same_identity(before, after):
        raise _unsafe("Daemon auth material changed during verified cleanup.", target)
    os.unlink(target)


def daemonTokenId(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _apply_mode_changes(changes: list[dict[str, Any]], chmod: Any, instance_id: str) -> None:
    applied: list[dict[str, Any]] = []
    try:
        for change in changes:
            _assert_change_identity(change)
            if callable(chmod):
                result = chmod(change["path"], change["next"])
                if hasattr(result, "__await__"):
                    await result
            else:
                _chmod_without_following(change, change["next"])
            _assert_change_identity(change, change["next"])
            applied.append(change)
    except Exception as error:
        for change in reversed(applied):
            with contextlib.suppress(Exception):
                _chmod_without_following(change, change["previous"])
        raise DaemonSecurityError(
            "daemon.security_migration_failed",
            f"Daemon permission migration failed: {error}",
            daemon_instance_dir(instance_id),
        ) from error


def _inspect_daemon_tree(root: str, instance_id: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    queue: list[tuple[str, int]] = [(root, 0)]
    visited = 0
    while queue:
        target, depth = queue.pop(0)
        visited += 1
        if visited > MAX_DAEMON_TREE_ENTRIES or depth > MAX_DAEMON_TREE_DEPTH:
            raise _unsafe("Daemon state tree exceeds its bounded migration scope.", root)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            if depth:
                continue
            raise
        _assert_owned(info, target)
        if stat.S_ISLNK(info.st_mode):
            raise _unsafe("Daemon state contains a symbolic link.", target)
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise _unsafe("Daemon state contains a group/world-writable path.", target)
        if stat.S_ISDIR(info.st_mode):
            next_mode, kind = PRIVATE_DIRECTORY_MODE, "directory"
            with os.scandir(target) as entries:
                queue.extend((entry.path, depth + 1) for entry in entries)
        elif stat.S_ISREG(info.st_mode):
            next_mode, kind = PRIVATE_FILE_MODE, "file"
        elif stat.S_ISSOCK(info.st_mode) and target == daemon_socket_path(instance_id):
            next_mode, kind = PRIVATE_FILE_MODE, "socket"
        else:
            raise _unsafe("Daemon state contains an unexpected filesystem object.", target)
        if mode != next_mode:
            changes.append(_mode_change(target, info, mode, next_mode, kind))
    return changes


def _inspect_optional_socket(target: str) -> dict[str, Any] | None:
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    _assert_owned(info, target)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode) or mode & 0o022:
        raise _unsafe("External daemon socket path is unsafe.", target)
    return None if mode == PRIVATE_FILE_MODE else _mode_change(target, info, mode, PRIVATE_FILE_MODE, "socket")


def _verify_private_tree(root: str, external_socket: str, instance_id: str) -> None:
    if _inspect_daemon_tree(root, instance_id):
        raise _unsafe("Daemon state permissions are not private.", root)
    if not _is_within(external_socket, root) and _inspect_optional_socket(external_socket):
        raise _unsafe("External daemon socket permissions are not private.", external_socket)


def _ensure_owned_directory(target: str) -> None:
    created = False
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        os.makedirs(target, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
        info = os.lstat(target)
        created = True
    _assert_owned_directory(info, target)
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise _unsafe("Daemon path is group/world-writable.", target)
    if created:
        os.chmod(target, PRIVATE_DIRECTORY_MODE)


def _read_legacy_state_for_security(instance_id: str) -> dict[str, Any] | None:
    target = daemon_state_path(instance_id)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > MAX_DAEMON_STATE_BYTES:
        raise _unsafe("Daemon state record cannot be safely inspected.", target)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except Exception as error:
        raise _unsafe("Daemon state record is malformed.", target) from error


async def _verify_canonical_ancestors(target: str) -> None:
    resolved = os.path.abspath(target)
    nearest = resolved
    while not os.path.lexists(nearest):
        parent = os.path.dirname(nearest)
        if parent == nearest:
            break
        nearest = parent
    canonical = os.path.realpath(nearest)
    current = os.path.sep
    for part in Path(canonical).parts[1:]:
        current = os.path.join(current, part)
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise _unsafe("Daemon ancestor is not a canonical directory.", current)
        mode = stat.S_IMODE(info.st_mode)
        trusted_sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if mode & 0o022 and not trusted_sticky_root:
            raise _unsafe("Daemon ancestor is writable by another user.", current)
        if hasattr(os, "getuid") and info.st_uid not in (os.getuid(), 0):
            raise _unsafe("Daemon ancestor has an untrusted owner.", current)


def _verify_owned_regular_file(target: str, require_private: bool) -> os.stat_result:
    info = os.lstat(target)
    _assert_owned(info, target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _unsafe("Daemon auth path is not a regular file.", target)
    if require_private and stat.S_IMODE(info.st_mode) & 0o077:
        raise _unsafe("Daemon auth path is not owner-only.", target)
    return info


def _assert_owned_directory(info: os.stat_result, target: str) -> None:
    _assert_owned(info, target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _unsafe("Daemon path is not an owned directory.", target)


def _assert_owned(info: os.stat_result, target: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise _unsafe("Daemon path has the wrong owner.", target)


def _mode_change(target: str, info: os.stat_result, previous: int, next_mode: int, kind: str) -> dict[str, Any]:
    return {"path": target, "previous": previous, "next": next_mode, "kind": kind, "dev": info.st_dev, "ino": info.st_ino}


def _assert_change_identity(change: dict[str, Any], expected_mode: int | None = None) -> None:
    info = os.lstat(change["path"])
    _assert_owned(info, change["path"])
    if not _same_identity_values(change, info) or not _kind_matches(change["kind"], info):
        raise _unsafe("Daemon path identity changed during permission migration.", change["path"])
    if expected_mode is not None and stat.S_IMODE(info.st_mode) != expected_mode:
        raise _unsafe("Daemon permission migration did not install the expected mode.", change["path"])


def _chmod_without_following(change: dict[str, Any], mode: int) -> None:
    if change["kind"] == "socket":
        _assert_change_identity(change)
        os.chmod(change["path"], mode, follow_symlinks=False)
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(change["path"], flags)
    try:
        info = os.fstat(fd)
        if not _same_identity_values(change, info):
            raise _unsafe("Daemon path identity changed during permission migration.", change["path"])
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _kind_matches(kind: str, info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) if kind == "directory" else stat.S_ISREG(info.st_mode) if kind == "file" else stat.S_ISSOCK(info.st_mode)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_identity_values(change: dict[str, Any], info: os.stat_result) -> bool:
    return change["dev"] == info.st_dev and change["ino"] == info.st_ino


def _is_within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.abspath(candidate), os.path.abspath(root))) == os.path.abspath(root)
    except ValueError:
        return False


def _unsafe(message: str, target: str) -> DaemonSecurityError:
    return DaemonSecurityError("daemon.security_unsafe", message, target)


def _write_all_private(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Daemon auth-material write made no progress.")
        view = view[written:]
