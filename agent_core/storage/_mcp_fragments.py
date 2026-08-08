"""User-scope MCP registration as a settings/mcp/ fragment directory.

One JSON file per server: settings/mcp/<name>.json holds a single server
record object (or a small list of them). Duplicate server names across
fragments fail loud, matching the settings/config/*.yaml strict-merge
convention. Non-.json files are ignored, so a server's own configuration
(e.g. a TOML referenced from its env block) can travel beside its
registration. The legacy <runtime-home>/mcp.json is no longer read: its
presence is a fatal error naming the new location — settings errors fail
loud, never silently degrade.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable

from .io_executor import runStorageIo as _run_storage_io
from .json_io import fileLock, readJsonFile, writeJsonFile
from .paths import getStateRoot


def userMcpFragmentsDir() -> str:
    return os.path.join(getStateRoot(), "settings", "mcp")


def _legacy_user_mcp_path() -> str:
    return os.path.join(getStateRoot(), "mcp.json")


def _assert_no_legacy_user_mcp() -> None:
    legacy = _legacy_user_mcp_path()
    if os.path.lexists(legacy):
        raise ValueError(
            f"{legacy}: legacy user-scope mcp.json is no longer read. "
            f"Move each server record into {userMcpFragmentsDir()}{os.sep}<server-name>.json "
            "(one file per server) and delete mcp.json."
        )


def _fragment_names(folder: str) -> list[str]:
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted(name for name in names if not name.startswith(".") and name.endswith(".json"))


async def _read_fragments() -> tuple[list[dict[str, Any]], dict[str, str], dict[str, list[dict[str, Any]]]]:
    folder = userMcpFragmentsDir()
    records: list[dict[str, Any]] = []
    owners: dict[str, str] = {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for name in await _run_storage_io(_fragment_names, folder):
        path = os.path.join(folder, name)
        value = await readJsonFile(path, None)
        if value is None:
            continue
        entries = value if isinstance(value, list) else [value]
        parsed: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{path}: expected an MCP server record object, or a list of them."
                )
            server = entry.get("name")
            if isinstance(server, str):
                if server in owners:
                    raise ValueError(
                        f"MCP server {server!r} is defined in both settings/mcp/{owners[server]} "
                        f"and settings/mcp/{name} — each server may live in exactly one file; remove one."
                    )
                owners[server] = name
            parsed.append(entry)
        by_file[name] = parsed
        records.extend(parsed)
    return records, owners, by_file


async def loadUserMcpFragments() -> list[dict[str, Any]]:
    _assert_no_legacy_user_mcp()
    records, _owners, _by_file = await _read_fragments()
    return records


def _fragment_filename(name: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._-") or "server"
    return f"{normalized}.json"


async def saveUserMcpFragments(servers: list[dict[str, Any]]) -> None:
    """Replace the user-scope server list, keeping each record in its file.

    Servers already registered keep their fragment file (updated in place);
    new servers get <name>.json; fragment files whose servers all left the
    list are removed — list-replacement semantics distributed over files.
    """
    _assert_no_legacy_user_mcp()
    folder = userMcpFragmentsDir()
    _records, owners, by_file = await _read_fragments()
    updated: dict[str, list[dict[str, Any]]] = {}
    for server in servers:
        server_name = server.get("name")
        target = owners.get(server_name) if isinstance(server_name, str) else None
        updated.setdefault(target or _fragment_filename(server_name), []).append(server)
    for name, entries in updated.items():
        await writeJsonFile(
            os.path.join(folder, name),
            entries[0] if len(entries) == 1 else entries,
        )
    for name in by_file:
        if name not in updated:
            await _run_storage_io(os.remove, os.path.join(folder, name))


async def mutateUserMcpFragments(mutate: Callable[[list[dict[str, Any]]], Any]) -> list[dict[str, Any]]:
    _assert_no_legacy_user_mcp()
    async with fileLock(os.path.join(userMcpFragmentsDir(), ".fragments")):
        records, _owners, _by_file = await _read_fragments()
        updated = mutate(records)
        if updated is None:
            updated = records
        await saveUserMcpFragments(updated)
        return updated
