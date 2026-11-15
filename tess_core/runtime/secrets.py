from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..storage.paths import get_state_root


SECRET_STORE_VERSION = 1
MAX_SECRET_BYTES = 1024 * 1024
MAX_RECORD_BYTES = ((MAX_SECRET_BYTES * 4 + 2) // 3) + 4096
SECRET_READ_DEADLINE_SECONDS = 1.0
_REF_ID_PATTERN = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_SECRET_KEY_PATTERN = re.compile(r"(?:authorization|cookie|token|secret|password|api[-_]?key|credential)", re.I)
_PURPOSES = {
    "mcp_header",
    "mcp_oauth",
    "mcp_env",
    "provider_api_key",
    "provider_oauth_token",
    "provider_setup_token",
    "channel_token",
}


class RuntimeSecretError(RuntimeError):
    def __init__(self, code: str, reason: str):
        super().__init__(f"Runtime secret {reason}")
        self.code = code


@dataclass(frozen=True, slots=True, weakref_slot=True)
class SecretWriteAuthorization:
    kind: str
    purpose: str
    idempotencyKey: str
    expiresAt: float


_authorization_registry: "weakref.WeakSet[SecretWriteAuthorization]" = weakref.WeakSet()
_store_locks_guard = threading.Lock()
_store_locks: dict[str, threading.Lock] = {}


def authorize_runtime_secret_write(
    args: Mapping[str, Any] | None = None,
    *,
    purpose: str | None = None,
    idempotencyKey: str | None = None,
    ttlMs: int | None = None,
) -> SecretWriteAuthorization:
    values = dict(args or {})
    selected_purpose = values.get("purpose", purpose)
    key = values.get("idempotencyKey", idempotencyKey)
    if selected_purpose not in _PURPOSES or not isinstance(key, str) or not key.strip():
        raise RuntimeSecretError("secret_store_unauthorized", "authorization requires a valid purpose and idempotency key")
    ttl = values.get("ttlMs", ttlMs)
    ttl_seconds = min(max((int(ttl) if ttl is not None else 300_000) / 1000, 1), 1800)
    authorization = SecretWriteAuthorization(
        kind="runtime_secret_write_authorization",
        purpose=selected_purpose,
        idempotencyKey=key,
        expiresAt=time.time() + ttl_seconds,
    )
    _authorization_registry.add(authorization)
    return authorization


async def store_runtime_secret(
    args: Mapping[str, Any] | None = None,
    *,
    purpose: str | None = None,
    secret: bytes | bytearray | memoryview | None = None,
    authorization: SecretWriteAuthorization | None = None,
    idempotencyKey: str | None = None,
) -> dict[str, Any]:
    values = dict(args or {})
    selected_purpose = values.get("purpose", purpose)
    selected_secret = values.get("secret", secret)
    selected_authorization = values.get("authorization", authorization)
    key_id = values.get("idempotencyKey", idempotencyKey)
    if (
        not isinstance(selected_authorization, SecretWriteAuthorization)
        or selected_authorization not in _authorization_registry
        or selected_authorization.expiresAt < time.time()
        or selected_authorization.purpose != selected_purpose
        or selected_authorization.idempotencyKey != key_id
    ):
        raise RuntimeSecretError("secret_store_unauthorized", "write authorization is invalid or expired")
    if not isinstance(selected_secret, (bytes, bytearray, memoryview)):
        raise RuntimeSecretError("secret_store_invalid", "value has an invalid byte length")
    secret_bytes = bytes(selected_secret)
    if not secret_bytes or len(secret_bytes) > MAX_SECRET_BYTES:
        raise RuntimeSecretError("secret_store_invalid", "value has an invalid byte length")
    integrity_key = _load_or_create_integrity_key()
    secret_id = _deterministic_secret_id(integrity_key, selected_purpose, str(key_id))
    lock = _store_lock(secret_id)
    await asyncio.to_thread(lock.acquire)
    try:
        ref = {"kind": "runtime_secret", "id": secret_id, "purpose": selected_purpose, "version": 1}
        target = os.path.join(_records_dir(), f"{secret_id}.json")
        try:
            existing = await resolve_runtime_secret(ref, selected_purpose)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not hmac.compare_digest(bytes(existing), secret_bytes):
                _zero(existing)
                raise RuntimeSecretError("secret_store_unauthorized", "idempotency key was reused for another value")
            _zero(existing)
            return ref
        encoded = base64.b64encode(secret_bytes).decode("ascii")
        unsigned = {"version": 1, "id": secret_id, "purpose": selected_purpose, "value": encoded}
        record = {**unsigned, "mac": _hmac(integrity_key, _record_mac_input(unsigned))}
        installed = _atomic_private_write_exclusive(target, f"{json.dumps(record, separators=(',', ':'))}\n".encode("utf-8"))
        if not installed:
            existing = await resolve_runtime_secret(ref, selected_purpose)
            try:
                if not hmac.compare_digest(bytes(existing), secret_bytes):
                    raise RuntimeSecretError("secret_store_unauthorized", "idempotency key was reused for another value")
            finally:
                _zero(existing)
        return ref
    finally:
        lock.release()
        _zero(integrity_key)


def is_runtime_secret_ref(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("kind") == "runtime_secret"
        and value.get("version") == 1
        and isinstance(value.get("id"), str)
        and _REF_ID_PATTERN.fullmatch(value["id"])
        and value.get("purpose") in _PURPOSES
    )


async def resolve_runtime_secret(ref: Mapping[str, Any], expectedPurpose: str) -> bytearray:
    if not is_runtime_secret_ref(ref) or ref.get("purpose") != expectedPurpose:
        raise RuntimeSecretError("secret_store_invalid", "reference purpose or shape is invalid")
    if ref.get('backend') is not None:
        # Managed refs never fall back to the legacy integrity-only file store.
        # Resolve only here, at the engine-owned trusted transport boundary.
        if expectedPurpose == 'provider_oauth_token':
            # A ChatGPT account's bundle: its access token, refreshed when due.
            from ..providers.chatgpt_account import current_access_token
            return bytearray((await current_access_token(dict(ref))).encode())
        from .credentials import CredentialStore
        store = CredentialStore.open(ref.get('installation_id'), ref.get('backend'))
        value = await store.bounded('consume', ref, account_id=ref.get('account_id'), purpose=expectedPurpose)
        key = {'provider_api_key': 'api_key', 'channel_token': 'token'}.get(expectedPurpose, 'access_token')
        result = value.payload.get(key)
        if not isinstance(result, str) or not result:
            raise RuntimeSecretError('secret_store_invalid', 'managed credential payload is invalid')
        return bytearray(result.encode())
    _ensure_private_store()
    raw = bytearray(_read_bounded_regular_file(os.path.join(_records_dir(), f"{ref['id']}.json"), MAX_RECORD_BYTES))
    try:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeSecretError("secret_store_invalid", "record JSON is malformed") from exc
    finally:
        _zero(raw)
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or parsed.get("id") != ref.get("id")
        or parsed.get("purpose") != expectedPurpose
        or not isinstance(parsed.get("value"), str)
        or not isinstance(parsed.get("mac"), str)
    ):
        raise RuntimeSecretError("secret_store_invalid", "record fields are invalid")
    integrity_key = _load_or_create_integrity_key()
    try:
        expected = _hmac(
            integrity_key,
            _record_mac_input(
                {"version": 1, "id": ref["id"], "purpose": expectedPurpose, "value": parsed["value"]}
            ),
        )
        if not hmac.compare_digest(expected, parsed["mac"]):
            raise RuntimeSecretError("secret_store_invalid", "record integrity check failed")
        try:
            decoded = bytearray(base64.b64decode(parsed["value"], validate=True))
        except (binascii.Error, ValueError) as exc:
            raise RuntimeSecretError("secret_store_invalid", "record value is not valid base64") from exc
        if not decoded or len(decoded) > MAX_SECRET_BYTES:
            _zero(decoded)
            raise RuntimeSecretError("secret_store_invalid", "decoded value has an invalid byte length")
        return decoded
    finally:
        _zero(integrity_key)


async def resolve_runtime_secret_string(value: Any, purpose: str) -> str:
    if isinstance(value, str):
        return value
    resolved = await resolve_runtime_secret(value, purpose)
    try:
        return resolved.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeSecretError("secret_store_invalid", "value is not valid UTF-8") from exc
    finally:
        _zero(resolved)


def mcp_field_is_secret(key: str) -> bool:
    return bool(_SECRET_KEY_PATTERN.search(key))


def assert_no_literal_mcp_secrets(records: Iterable[Mapping[str, Any]], source: str) -> None:
    for record in records:
        for key, value in (record.get("headers") or {}).items():
            if isinstance(value, str):
                raise RuntimeSecretError(
                    "secret_store_invalid",
                    f"{source} MCP header {key} requires a runtime secret reference",
                )
        for key, value in (record.get("env") or {}).items():
            if mcp_field_is_secret(str(key)) and isinstance(value, str):
                raise RuntimeSecretError(
                    "secret_store_invalid",
                    f"{source} MCP env {key} requires a runtime secret reference",
                )
        oauth = record.get("oauth") if isinstance(record.get("oauth"), dict) else {}
        if isinstance(oauth.get("clientSecret"), str):
            raise RuntimeSecretError(
                "secret_store_invalid",
                f"{source} MCP OAuth client secret requires a runtime secret reference",
            )


async def migrate_legacy_mcp_server_secrets(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    changed = False
    migrated: list[dict[str, Any]] = []
    for source_record in records:
        record = _deep_copy(source_record)
        headers = record.get("headers") if isinstance(record.get("headers"), dict) else None
        env = record.get("env") if isinstance(record.get("env"), dict) else None
        for key, value in list((headers or {}).items()):
            if mcp_field_is_secret(key) and isinstance(value, str):
                headers[key] = await _migrate_value(
                    "mcp_header",
                    value,
                    f"legacy-mcp:{record.get('name')}:header:{key}:{hashlib.sha256(value.encode()).hexdigest()}",
                )
                changed = True
        for key, value in list((env or {}).items()):
            if mcp_field_is_secret(key) and isinstance(value, str):
                env[key] = await _migrate_value(
                    "mcp_env",
                    value,
                    f"legacy-mcp:{record.get('name')}:env:{key}:{hashlib.sha256(value.encode()).hexdigest()}",
                )
                changed = True
        oauth = record.get("oauth") if isinstance(record.get("oauth"), dict) else None
        if oauth and isinstance(oauth.get("clientSecret"), str):
            value = oauth["clientSecret"]
            oauth["clientSecret"] = await _migrate_value(
                "mcp_oauth",
                value,
                f"legacy-mcp:{record.get('name')}:oauth:{hashlib.sha256(value.encode()).hexdigest()}",
            )
            changed = True
        migrated.append(record)
    return {"records": migrated, "changed": changed}


async def resolve_mcp_server_config_secrets(record: Mapping[str, Any]) -> dict[str, Any]:
    resolved = _deep_copy(record)
    resolved["headers"] = {
        key: await resolve_runtime_secret_string(value, "mcp_header")
        for key, value in (record.get("headers") or {}).items()
    }
    resolved["env"] = {
        key: await resolve_runtime_secret_string(value, "mcp_env")
        for key, value in (record.get("env") or {}).items()
    }
    if isinstance(record.get("oauth"), dict):
        resolved["oauth"] = _deep_copy(record["oauth"])
        if record["oauth"].get("clientSecret") is not None:
            resolved["oauth"]["clientSecret"] = await resolve_runtime_secret_string(
                record["oauth"]["clientSecret"], "mcp_oauth"
            )
    return resolved


async def _migrate_value(purpose: str, value: str, idempotency_key: str) -> dict[str, Any]:
    material = bytearray(value.encode("utf-8"))
    try:
        return await store_runtime_secret(
            purpose=purpose,
            secret=material,
            idempotencyKey=idempotency_key,
            authorization=authorize_runtime_secret_write(purpose=purpose, idempotencyKey=idempotency_key),
        )
    finally:
        _zero(material)


def _private_root() -> str:
    return os.path.join(get_state_root(), "private-secrets")


def _records_dir() -> str:
    return os.path.join(_private_root(), "records")


def _integrity_key_path() -> str:
    return os.path.join(_private_root(), "integrity.key")


def _assert_owned(info: os.stat_result, target: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeSecretError("secret_store_unsafe", f"path is not owner-controlled: {os.path.basename(target)}")


def _assert_private_directory(target: str) -> None:
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeSecretError("secret_store_unsafe", f"path is not a real directory: {os.path.basename(target)}")
    _assert_owned(info, target)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeSecretError("secret_store_unsafe", f"directory permissions are not owner-only: {os.path.basename(target)}")


def _ensure_private_store() -> None:
    root = _private_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    _assert_private_directory(root)
    records = _records_dir()
    try:
        os.mkdir(records, 0o700)
    except FileExistsError:
        pass
    _assert_private_directory(records)


def _read_bounded_regular_file(target: str, max_bytes: int) -> bytes:
    started = time.monotonic()
    info = os.lstat(target)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeSecretError("secret_store_unsafe", f"record is not a regular file: {os.path.basename(target)}")
    _assert_owned(info, target)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeSecretError("secret_store_unsafe", f"record permissions are not owner-only: {os.path.basename(target)}")
    if info.st_size > max_bytes:
        raise RuntimeSecretError("secret_store_invalid", "record exceeds its byte limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size != info.st_size
        ):
            raise RuntimeSecretError("secret_store_unsafe", f"record changed while opening: {os.path.basename(target)}")
        _assert_owned(opened, target)
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise RuntimeSecretError("secret_store_unsafe", f"record permissions are not owner-only: {os.path.basename(target)}")
        value = os.read(fd, max_bytes + 1)
        if len(value) > max_bytes or len(value) > info.st_size:
            raise RuntimeSecretError("secret_store_invalid", "record changed beyond its byte limit")
        if time.monotonic() - started > SECRET_READ_DEADLINE_SECONDS:
            raise RuntimeSecretError("secret_store_unavailable", "record read exceeded its deadline")
        return value
    finally:
        os.close(fd)


def _load_or_create_integrity_key() -> bytearray:
    _ensure_private_store()
    try:
        existing = bytearray(_read_bounded_regular_file(_integrity_key_path(), 64))
        if len(existing) != 32:
            _zero(existing)
            raise RuntimeSecretError("secret_store_invalid", "integrity key has an invalid length")
        return existing
    except FileNotFoundError:
        pass
    key = bytearray(secrets.token_bytes(32))
    try:
        if _atomic_private_write_exclusive(_integrity_key_path(), bytes(key)):
            return key
        _zero(key)
        return _load_or_create_integrity_key()
    except Exception:
        _zero(key)
        raise


def _atomic_private_write_exclusive(target: str, value: bytes) -> bool:
    temp = f"{target}.{os.getpid()}.{uuid.uuid4()}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temp, flags, 0o600)
        try:
            offset = 0
            while offset < len(value):
                offset += os.write(fd, value[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temp, target)
        except FileExistsError:
            return False
        os.chmod(target, 0o600)
        return True
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _hmac(key: bytes | bytearray, value: str) -> str:
    return hmac.new(bytes(key), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _deterministic_secret_id(key: bytes | bytearray, purpose: str, idempotency_key: str) -> str:
    value = _hmac(key, f"id\0{purpose}\0{idempotency_key}")[:32]
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"


def _record_mac_input(record: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "version": record["version"],
            "id": record["id"],
            "purpose": record["purpose"],
            "value": record["value"],
        },
        separators=(",", ":"),
    )


def _store_lock(secret_id: str) -> threading.Lock:
    with _store_locks_guard:
        return _store_locks.setdefault(secret_id, threading.Lock())


def _zero(value: bytearray | bytes) -> None:
    if isinstance(value, bytearray):
        value[:] = b"\0" * len(value)


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


