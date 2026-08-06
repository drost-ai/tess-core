from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .._utils import maybe_await
from ..storage import getStateRoot


def mcpTokenStoreKey(config: dict[str, Any]) -> str:
    return f"{config.get('name', '')}\0{_canonical_url(str(config.get('commandOrUrl') or ''))}"




class FileMcpTokenStore:
    """Private, atomic runtime-home storage for MCP OAuth token records."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root) if root is not None else Path(getStateRoot()) / "mcp-tokens"

    async def get(self, server_key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, server_key)

    async def set(self, server_key: str, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._set_sync, server_key, record)

    async def delete(self, server_key: str) -> None:
        await asyncio.to_thread(self._delete_sync, server_key)

    def _get_sync(self, server_key: str) -> dict[str, Any] | None:
        path = self._path(server_key)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("MCP token store record is invalid.")
        return value

    def _set_sync(self, server_key: str, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _suppress_os_error():
            os.chmod(self.root, 0o700)
        target = self._path(server_key)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=".tmp", dir=self.root)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(record, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with _suppress_os_error():
                os.unlink(temporary)

    def _delete_sync(self, server_key: str) -> None:
        try:
            self._path(server_key).unlink()
        except FileNotFoundError:
            return

    def _path(self, server_key: str) -> Path:
        digest = hashlib.sha256(server_key.encode("utf-8")).hexdigest()[:40]
        return self.root / f"{digest}.json"


def createFileMcpTokenStore(root: str | os.PathLike[str] | None = None) -> FileMcpTokenStore:
    return FileMcpTokenStore(root)




async def token_store_get(store: Any, server_key: str) -> dict[str, Any] | None:
    value = await maybe_await(_store_method(store, "get")(server_key))
    return value if isinstance(value, dict) else None


async def token_store_set(store: Any, server_key: str, record: dict[str, Any]) -> None:
    await maybe_await(_store_method(store, "set")(server_key, dict(record)))


async def token_store_delete(store: Any, server_key: str) -> None:
    await maybe_await(_store_method(store, "delete")(server_key))


def _store_method(store: Any, name: str):
    candidate = store.get(name) if isinstance(store, dict) else getattr(store, name, None)
    if not callable(candidate):
        raise TypeError(f"MCP token store must provide an async {name} method.")
    return candidate


def _canonical_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except Exception:
        return raw


class _suppress_os_error:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, _exc, _traceback):
        return bool(exc_type and issubclass(exc_type, OSError))
