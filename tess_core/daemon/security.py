"""Daemon auth material and socket security, plus the shared owner asserts.

Private-tree preparation, bounded permission migration, and tree verification
live in ``security_tree.py``, which imports this module's shared primitives.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import socket
import stat
import uuid
from typing import Any

from ..storage import daemon_instance_dir, daemon_state_path
from .protocol import DAEMON_PROTOCOL_VERSION

PRIVATE_FILE_MODE = 0o600
MAX_DAEMON_TOKEN_BYTES = 256
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")


class DaemonSecurityError(RuntimeError):
    def __init__(self, code: str, message: str, targetPath: str):
        super().__init__(f"{message} Manual remediation is required for {targetPath}.")
        self.name = "DaemonSecurityError"
        self.code = code
        self.targetPath = targetPath


def daemon_auth_token_path(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "instance-token")


def generate_daemon_auth_material() -> dict[str, str]:
    token = secrets.token_urlsafe(32)
    return {"token": token, "tokenId": daemon_token_id(token)}


async def publish_daemon_auth_material(instanceId: str, auth: dict[str, str]) -> None:
    token = auth.get("token")
    token_id = auth.get("tokenId")
    if (
        not isinstance(token, str)
        or not _TOKEN_PATTERN.fullmatch(token)
        or not isinstance(token_id, str)
        or token_id != daemon_token_id(token)
    ):
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "Refusing to publish malformed daemon auth material.",
            daemon_auth_token_path(instanceId),
        )
    target = daemon_auth_token_path(instanceId)
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
                await remove_daemon_auth_material_if_owned(instanceId, token_id)
        raise


async def create_daemon_auth_material(instanceId: str) -> dict[str, str]:
    auth = generate_daemon_auth_material()
    await publish_daemon_auth_material(instanceId, auth)
    return auth


async def read_daemon_auth_material(instanceId: str, state: dict[str, Any]) -> dict[str, str]:
    if state.get("protocolVersion") != DAEMON_PROTOCOL_VERSION or not isinstance(state.get("authTokenId"), str):
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "The daemon state uses an unauthenticated or unsupported protocol version.",
            daemon_state_path(instanceId),
        )
    target = daemon_auth_token_path(instanceId)
    info = _verify_owned_regular_file(target, require_private=True)
    if info.st_size > MAX_DAEMON_TOKEN_BYTES:
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "The daemon instance token exceeds its byte limit.",
            target,
        )
    with open(target, "r", encoding="utf-8") as handle:
        token = handle.read(MAX_DAEMON_TOKEN_BYTES + 1).strip()
    token_id = daemon_token_id(token)
    if not _TOKEN_PATTERN.fullmatch(token) or token_id != state["authTokenId"]:
        raise DaemonSecurityError(
            "daemon.auth_material_invalid",
            "The daemon instance token does not match the authenticated state record.",
            target,
        )
    return {"token": token, "tokenId": token_id}


def verify_owned_daemon_socket(socketPath: str) -> os.stat_result | None:
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


async def is_daemon_socket_listener_active(socketPath: str) -> bool:
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


async def remove_inactive_owned_daemon_socket(socketPath: str) -> None:
    before = verify_owned_daemon_socket(socketPath)
    if not before:
        return
    if await is_daemon_socket_listener_active(socketPath):
        raise DaemonSecurityError(
            "daemon.security_live_migration",
            "Refusing to remove a daemon socket that still accepts connections.",
            socketPath,
        )
    after = verify_owned_daemon_socket(socketPath)
    if not after:
        return
    if not _same_identity(before, after):
        raise _unsafe("Daemon socket identity changed during verified cleanup.", socketPath)
    os.unlink(socketPath)


async def remove_daemon_auth_material_if_owned(instanceId: str, tokenId: str) -> None:
    target = daemon_auth_token_path(instanceId)
    try:
        info = _verify_owned_regular_file(target, require_private=True)
    except FileNotFoundError:
        return
    if info.st_size > MAX_DAEMON_TOKEN_BYTES:
        return
    with open(target, "r", encoding="utf-8") as handle:
        token = handle.read(MAX_DAEMON_TOKEN_BYTES + 1).strip()
    if daemon_token_id(token) == tokenId:
        os.unlink(target)


async def remove_owned_daemon_auth_material(instanceId: str) -> None:
    target = daemon_auth_token_path(instanceId)
    try:
        before = _verify_owned_regular_file(target, require_private=True)
    except FileNotFoundError:
        return
    after = _verify_owned_regular_file(target, require_private=True)
    if not _same_identity(before, after):
        raise _unsafe("Daemon auth material changed during verified cleanup.", target)
    os.unlink(target)


def daemon_token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_owned_regular_file(target: str, require_private: bool) -> os.stat_result:
    info = os.lstat(target)
    _assert_owned(info, target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _unsafe("Daemon auth path is not a regular file.", target)
    if require_private and stat.S_IMODE(info.st_mode) & 0o077:
        raise _unsafe("Daemon auth path is not owner-only.", target)
    return info


def _assert_owned(info: os.stat_result, target: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise _unsafe("Daemon path has the wrong owner.", target)


_PRIVATE_GROUPS: dict[int, bool] = {}


def _group_is_private(gid: int) -> bool:
    """Whether a group has exactly one member: the user this process runs as.

    Many Linux systems give each user a group of their own and a umask of 002,
    so every directory the user makes is group-writable, by a group nobody
    else is in. That is not writable by another user. A group anyone else
    belongs to, as a listed member or as their primary group, is. Anything
    that cannot be established counts as shared."""
    if gid in _PRIVATE_GROUPS:
        return _PRIVATE_GROUPS[gid]
    private = False
    try:
        import grp
        import pwd

        me = pwd.getpwuid(os.getuid())
        members = set(grp.getgrgid(gid).gr_mem) - {me.pw_name}
        private = not members and not any(entry.pw_gid == gid and entry.pw_uid != me.pw_uid for entry in pwd.getpwall())
    except (ImportError, KeyError, OSError):
        private = False
    _PRIVATE_GROUPS[gid] = private
    return private


def _writable_by_others(info: os.stat_result) -> bool:
    """Whether anyone but the owner can write to a directory: every user can,
    or the members of a group that is not the owner's alone."""
    mode = stat.S_IMODE(info.st_mode)
    return bool(mode & stat.S_IWOTH) or (bool(mode & stat.S_IWGRP) and not _group_is_private(info.st_gid))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _unsafe(message: str, target: str) -> DaemonSecurityError:
    return DaemonSecurityError("daemon.security_unsafe", message, target)


def _write_all_private(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Daemon auth-material write made no progress.")
        view = view[written:]
