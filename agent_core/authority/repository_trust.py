from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from typing import Any

from ..storage.json_io import file_lock
from ..storage.paths import get_local_state_dir, get_project_root, get_state_root


REPOSITORY_TRUST_VERSION = 1
_APPROVAL_FAMILIES = {"hooks", "permission_allow", "plugin", "mcp"}
_APPROVAL_SCOPES = {"project", "local"}
_DIGEST_PATTERN = __import__("re").compile(r"^[a-f0-9]{64}$")
_MAX_TRUST_RECORD_BYTES = 256 * 1024


def _trust_directory() -> str:
    return os.path.join(get_state_root(), "trust", "repositories")


def _normalize_canonical_path(value: str) -> str:
    normalized = os.path.normpath(value)
    return os.path.normcase(normalized) if os.name == "nt" else normalized


async def resolve_repository_identity(cwd: str) -> dict[str, Any]:
    canonical_root = _normalize_canonical_path(os.path.realpath(get_project_root(cwd)))
    info = os.stat(canonical_root, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"Repository root is not a directory: {canonical_root}")
    filesystem_identity = f"{info.st_dev}:{info.st_ino}"
    repository_key = hashlib.sha256(
        json.dumps(
            {
                "version": REPOSITORY_TRUST_VERSION,
                "canonicalRoot": canonical_root,
                "filesystemIdentity": filesystem_identity,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "version": REPOSITORY_TRUST_VERSION,
        "canonicalRoot": canonical_root,
        "filesystemIdentity": filesystem_identity,
        "repositoryKey": repository_key,
    }


def repository_trust_record_path(identity: dict[str, Any]) -> str:
    key = str(identity.get("repositoryKey") or "")
    if not _DIGEST_PATTERN.fullmatch(key):
        raise ValueError("Invalid repository identity key")
    return os.path.join(_trust_directory(), f"{key}.json")


def _assert_digest(content_digest: str) -> None:
    if not isinstance(content_digest, str) or not _DIGEST_PATTERN.fullmatch(content_digest):
        raise ValueError("Repository approval content digest must be a lowercase SHA-256 digest")


def _assert_family_scope(family: str, scope: str) -> None:
    if family not in _APPROVAL_FAMILIES:
        raise ValueError(f"Unknown repository approval family: {family}")
    if scope not in _APPROVAL_SCOPES:
        raise ValueError(f"Unknown repository approval scope: {scope}")


def _assert_owned(info: os.stat_result, target: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"Repository trust path is not owned by the current user: {target}")


def _ensure_private_directory() -> None:
    trust_root = os.path.dirname(_trust_directory())
    for directory in (trust_root, _trust_directory()):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise PermissionError(f"Repository trust path is not a real directory: {directory}")
        _assert_owned(info, directory)
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(directory, 0o700)


def _parse_record(value: Any, expected: dict[str, Any], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != REPOSITORY_TRUST_VERSION:
        raise ValueError(f"Invalid repository trust record: {path}")
    repository = value.get("repository")
    if not isinstance(repository, dict) or any(
        repository.get(key) != expected.get(key)
        for key in ("version", "canonicalRoot", "filesystemIdentity", "repositoryKey")
    ):
        raise ValueError(f"Repository trust identity mismatch: {path}")
    approvals = value.get("approvals")
    if not isinstance(approvals, dict):
        raise ValueError(f"Invalid repository trust approvals: {path}")
    for family, scopes in approvals.items():
        if family not in _APPROVAL_FAMILIES or not isinstance(scopes, dict):
            raise ValueError(f"Invalid repository approval family in {path}")
        for scope, approval in scopes.items():
            if scope not in _APPROVAL_SCOPES or not isinstance(approval, dict):
                raise ValueError(f"Invalid repository approval in {path}")
            digest = approval.get("contentDigest")
            approved_at = approval.get("approvedAt")
            if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
                raise ValueError(f"Invalid repository approval in {path}")
            try:
                datetime.fromisoformat(str(approved_at).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise ValueError(f"Invalid repository approval in {path}") from None
    return value


def _read_record(identity: dict[str, Any]) -> dict[str, Any] | None:
    path = repository_trust_record_path(identity)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"Repository trust record is not a regular file: {path}")
    _assert_owned(info, path)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(f"Repository trust record permissions are unsafe: {path}")
    if info.st_size > _MAX_TRUST_RECORD_BYTES:
        raise ValueError(f"Repository trust record is oversized: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return _parse_record(json.load(handle), identity, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _write_record(record: dict[str, Any]) -> None:
    _ensure_private_directory()
    path = repository_trust_record_path(record["repository"])
    temporary = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


async def check_repository_approval(args: dict[str, Any] | None = None, **kwargs: Any) -> bool:
    args = {**(args or {}), **kwargs}
    family, scope = str(args.get("family")), str(args.get("scope"))
    digest = str(args.get("contentDigest") or "")
    _assert_family_scope(family, scope)
    _assert_digest(digest)
    identity = await resolve_repository_identity(str(args["cwd"]))
    record = _read_record(identity)
    return ((record or {}).get("approvals", {}).get(family, {}).get(scope, {}).get("contentDigest") == digest)


async def approve_repository_content(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    family, scope = str(args.get("family")), str(args.get("scope"))
    digest = str(args.get("contentDigest") or "")
    _assert_family_scope(family, scope)
    _assert_digest(digest)
    identity = await resolve_repository_identity(str(args["cwd"]))
    path = repository_trust_record_path(identity)
    _ensure_private_directory()
    async with file_lock(path):
        record = _read_record(identity) or {
            "version": REPOSITORY_TRUST_VERSION,
            "repository": identity,
            "approvals": {},
        }
        record.setdefault("approvals", {}).setdefault(family, {})[scope] = {
            "contentDigest": digest,
            "approvedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _write_record(record)
    return await get_repository_trust_status(str(args["cwd"]))


async def revoke_repository_approval(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    family, scope = str(args.get("family")), str(args.get("scope"))
    _assert_family_scope(family, scope)
    identity = await resolve_repository_identity(str(args["cwd"]))
    path = repository_trust_record_path(identity)
    _ensure_private_directory()
    async with file_lock(path):
        record = _read_record(identity)
        scopes = (record or {}).get("approvals", {}).get(family)
        if isinstance(scopes, dict) and scope in scopes:
            del scopes[scope]
            if not scopes:
                del record["approvals"][family]
            _write_record(record)
    return await get_repository_trust_status(str(args["cwd"]))


async def get_repository_trust_status(cwd: str) -> dict[str, Any]:
    identity = await resolve_repository_identity(cwd)
    record = _read_record(identity)
    legacy_root = get_local_state_dir(cwd)
    return {
        "repository": identity,
        "recordPath": repository_trust_record_path(identity),
        "approvals": (record or {}).get("approvals", {}),
        "legacyHints": {
            "hooks": os.path.lexists(os.path.join(legacy_root, "approved-hooks.json")),
            "permissionAllow": os.path.lexists(os.path.join(legacy_root, "approved-permissions.json")),
        },
    }


