from __future__ import annotations

from ..contracts.config import AutoModePolicy, SettingsRecord

import os
from typing import Any

from .._utils import clone
from ._mcp_fragments import (
    load_user_mcp_fragments,
    mutate_user_mcp_fragments,
    save_user_mcp_fragments,
)
from ._settings_folder import load_settings_folder, merge_settings_records
from .json_io import mutate_json_file, read_json_file
from .paths import (
    Scope,
    get_local_state_dir,
    get_project_memory_records_path,
    get_project_state_dir,
    get_state_root,
)

DEFAULT_AUTO_MODE = {
    "allow": [
        "git status",
        "git diff",
        "npm test",
        "pnpm test",
        "bun test",
        "rg",
        "ls",
        "find",
    ],
    "deny": ["git reset --hard", "rm -rf /", "sudo rm -rf", "dd if="],
    "environment": {},
}
def _settings_path(source: Scope, cwd: str, filename: str = "settings.json") -> str:
    if source == "user":
        return os.path.join(get_state_root(), filename)
    return os.path.join(get_project_state_dir(cwd) if source == "project" else get_local_state_dir(cwd), filename)

async def load_settings(source: Scope, cwd: str) -> SettingsRecord:
    record = await read_json_file(_settings_path(source, cwd), {})
    if source == "user":
        # The settings/ folder (.env + providers.yaml + config.yaml) is the
        # successor of user-scope settings.json; it augments and overrides
        # the legacy record per key. Single integration point: config
        # resolution AND authority (permission rules, hooks) both read
        # through here and must see the same view.
        folder = load_settings_folder()
        if folder:
            record = merge_settings_records(record, folder)
    return record

async def save_settings(source: Scope, cwd: str, settings: SettingsRecord) -> None:
    await mutate_json_file(_settings_path(source, cwd), lambda _current: settings, {})

async def mutate_settings(source: Scope, cwd: str, mutate) -> SettingsRecord:
    return await mutate_json_file(_settings_path(source, cwd), mutate, {})

async def load_settings_by_sources(sources: list[Scope], cwd: str) -> SettingsRecord:
    merged: dict[str, Any] = {}
    for source in sources:
        merged.update(await load_settings(source, cwd))
    return merged

async def load_project_memory_records(cwd: str) -> list[dict[str, Any]]:
    return await read_json_file(get_project_memory_records_path(cwd), [])

async def save_project_memory_records(cwd: str, records: list[dict[str, Any]]) -> None:
    await mutate_json_file(get_project_memory_records_path(cwd), lambda _current: records, [])

async def mutate_project_memory_records(cwd: str, mutate) -> list[dict[str, Any]]:
    return await mutate_json_file(get_project_memory_records_path(cwd), mutate, [])

# User-scope MCP registration lives in the settings/mcp/ fragment directory
# (one file per server); project/local scopes keep mcp.json in their state
# dirs. See storage/_mcp_fragments.py for the fragment contract.
async def load_mcp_servers(scope: Scope, cwd: str) -> list[dict[str, Any]]:
    """The MCP servers registered at a scope: ``user``, ``project`` or ``local``."""
    if scope == "user":
        return await load_user_mcp_fragments()
    return await read_json_file(_settings_path(scope, cwd, "mcp.json"), [])

async def save_mcp_servers(scope: Scope, cwd: str, servers: list[dict[str, Any]]) -> None:
    """Replace the MCP servers registered at a scope."""
    if scope == "user":
        await save_user_mcp_fragments(servers)
        return
    await mutate_json_file(_settings_path(scope, cwd, "mcp.json"), lambda _current: servers, [])

async def mutate_mcp_servers(scope: Scope, cwd: str, mutate) -> list[dict[str, Any]]:
    if scope == "user":
        return await mutate_user_mcp_fragments(mutate)
    return await mutate_json_file(_settings_path(scope, cwd, "mcp.json"), mutate, [])

async def load_plugins(scope: Scope, cwd: str) -> list[dict[str, Any]]:
    """The plugin records installed at a scope."""
    return await read_json_file(_settings_path(scope, cwd, "plugins.json"), [])

async def save_plugins(scope: Scope, cwd: str, records: list[dict[str, Any]]) -> None:
    """Replace the plugin records installed at a scope."""
    await mutate_json_file(_settings_path(scope, cwd, "plugins.json"), lambda _current: records, [])

async def mutate_plugins(scope: Scope, cwd: str, mutate) -> list[dict[str, Any]]:
    return await mutate_json_file(_settings_path(scope, cwd, "plugins.json"), mutate, [])

async def load_marketplaces() -> list[dict[str, Any]]:
    """The plugin marketplaces known to this runtime home."""
    return await read_json_file(os.path.join(get_state_root(), "marketplaces.json"), [])

async def save_marketplaces(records: list[dict[str, Any]]) -> None:
    """Replace the plugin marketplaces known to this runtime home."""
    await mutate_json_file(os.path.join(get_state_root(), "marketplaces.json"), lambda _current: records, [])

async def mutate_marketplaces(mutate) -> list[dict[str, Any]]:
    return await mutate_json_file(os.path.join(get_state_root(), "marketplaces.json"), mutate, [])

async def load_auto_mode(cwd: str) -> AutoModePolicy:
    """The project's auto-mode policy, or the default when it has none."""
    return await read_json_file(os.path.join(get_project_state_dir(cwd), "auto-mode.json"), clone(DEFAULT_AUTO_MODE))

async def save_auto_mode(cwd: str, config: AutoModePolicy) -> None:
    await mutate_json_file(os.path.join(get_project_state_dir(cwd), "auto-mode.json"), lambda _current: config, clone(DEFAULT_AUTO_MODE))

async def mutate_auto_mode(cwd: str, mutate) -> AutoModePolicy:
    return await mutate_json_file(os.path.join(get_project_state_dir(cwd), "auto-mode.json"), mutate, clone(DEFAULT_AUTO_MODE))

def get_default_auto_mode() -> AutoModePolicy:
    """A copy of the default auto-mode policy."""
    return clone(DEFAULT_AUTO_MODE)

def resolve_scope(scope: str | None) -> Scope:
    return scope if scope in ("user", "project", "local") else "user"  # type: ignore[return-value]
