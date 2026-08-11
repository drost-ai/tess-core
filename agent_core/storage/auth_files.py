from __future__ import annotations

"""Stored-credential file access (auth.json, codex auth)."""

import json
import os
from typing import Any

from .._utils import note_swallowed
from .io_executor import run_storage_io as _run_storage_io
from .json_io import read_json_file, write_json_file
from .paths import get_state_root


async def load_auth() -> dict[str, Any] | None:
    auth = await read_json_file(os.path.join(get_state_root(), "auth.json"), None)
    if not auth:
        return None
    from ..runtime.secret_provider import migrateLegacyStoredAuthSecrets

    migration = await migrateLegacyStoredAuthSecrets(auth)
    if migration["changed"]:
        await write_json_file(os.path.join(get_state_root(), "auth.json"), migration["auth"])
    return migration["auth"]

async def load_codex_auth_token(authPath: str) -> str | None:
    return await _run_storage_io(_load_codex_auth_token_sync, authPath)


def _load_codex_auth_token_sync(auth_path: str) -> str | None:
    try:
        with open(auth_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        api_key = str(payload.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            return api_key
        token = str((payload.get("tokens") or {}).get("access_token") or "").strip()
        return token or None
    except FileNotFoundError:
        return None
    except Exception as error:
        note_swallowed("codex auth file unreadable", error)
        return None

async def save_auth(auth: dict[str, Any]) -> None:
    from ..runtime.secret_provider import migrateLegacyStoredAuthSecrets

    normalized = await migrateLegacyStoredAuthSecrets(auth)
    await write_json_file(os.path.join(get_state_root(), "auth.json"), normalized["auth"])

async def clear_auth() -> None:
    await _run_storage_io(_clear_auth_sync, os.path.join(get_state_root(), "auth.json"))


def _clear_auth_sync(auth_path: str) -> None:
    try:
        os.remove(auth_path)
    except FileNotFoundError:
        pass

