from __future__ import annotations

from ..contracts.config import AutoModePolicy, SettingsRecord

from .shared import *

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
        return os.path.join(getStateRoot(), filename)
    return os.path.join(getProjectStateDir(cwd) if source == "project" else getLocalStateDir(cwd), filename)

async def loadSettings(source: Scope, cwd: str) -> SettingsRecord:
    return await readJsonFile(_settings_path(source, cwd), {})

async def saveSettings(source: Scope, cwd: str, settings: SettingsRecord) -> None:
    await mutateJsonFile(_settings_path(source, cwd), lambda _current: settings, {})

async def mutateSettings(source: Scope, cwd: str, mutate) -> SettingsRecord:
    return await mutateJsonFile(_settings_path(source, cwd), mutate, {})

async def loadSettingsBySources(sources: list[Scope], cwd: str) -> SettingsRecord:
    merged: dict[str, Any] = {}
    for source in sources:
        merged.update(await loadSettings(source, cwd))
    return merged

async def loadProjectMemoryRecords(cwd: str) -> list[dict[str, Any]]:
    return await readJsonFile(getProjectMemoryRecordsPath(cwd), [])

async def saveProjectMemoryRecords(cwd: str, records: list[dict[str, Any]]) -> None:
    await mutateJsonFile(getProjectMemoryRecordsPath(cwd), lambda _current: records, [])

async def mutateProjectMemoryRecords(cwd: str, mutate) -> list[dict[str, Any]]:
    return await mutateJsonFile(getProjectMemoryRecordsPath(cwd), mutate, [])

async def loadMcpServers(scope: Scope, cwd: str) -> list[dict[str, Any]]:
    return await readJsonFile(_settings_path(scope, cwd, "mcp.json"), [])

async def saveMcpServers(scope: Scope, cwd: str, servers: list[dict[str, Any]]) -> None:
    await mutateJsonFile(_settings_path(scope, cwd, "mcp.json"), lambda _current: servers, [])

async def mutateMcpServers(scope: Scope, cwd: str, mutate) -> list[dict[str, Any]]:
    return await mutateJsonFile(_settings_path(scope, cwd, "mcp.json"), mutate, [])

async def loadPlugins(scope: Scope, cwd: str) -> list[dict[str, Any]]:
    return await readJsonFile(_settings_path(scope, cwd, "plugins.json"), [])

async def savePlugins(scope: Scope, cwd: str, records: list[dict[str, Any]]) -> None:
    await mutateJsonFile(_settings_path(scope, cwd, "plugins.json"), lambda _current: records, [])

async def mutatePlugins(scope: Scope, cwd: str, mutate) -> list[dict[str, Any]]:
    return await mutateJsonFile(_settings_path(scope, cwd, "plugins.json"), mutate, [])

async def loadMarketplaces() -> list[dict[str, Any]]:
    return await readJsonFile(os.path.join(getStateRoot(), "marketplaces.json"), [])

async def saveMarketplaces(records: list[dict[str, Any]]) -> None:
    await mutateJsonFile(os.path.join(getStateRoot(), "marketplaces.json"), lambda _current: records, [])

async def mutateMarketplaces(mutate) -> list[dict[str, Any]]:
    return await mutateJsonFile(os.path.join(getStateRoot(), "marketplaces.json"), mutate, [])

async def loadAutoMode(cwd: str) -> AutoModePolicy:
    return await readJsonFile(os.path.join(getProjectStateDir(cwd), "auto-mode.json"), clone(DEFAULT_AUTO_MODE))

async def saveAutoMode(cwd: str, config: AutoModePolicy) -> None:
    await mutateJsonFile(os.path.join(getProjectStateDir(cwd), "auto-mode.json"), lambda _current: config, clone(DEFAULT_AUTO_MODE))

async def mutateAutoMode(cwd: str, mutate) -> AutoModePolicy:
    return await mutateJsonFile(os.path.join(getProjectStateDir(cwd), "auto-mode.json"), mutate, clone(DEFAULT_AUTO_MODE))

def getDefaultAutoMode() -> AutoModePolicy:
    return clone(DEFAULT_AUTO_MODE)

def resolveScope(scope: str | None) -> Scope:
    return scope if scope in ("user", "project", "local") else "user"  # type: ignore[return-value]
