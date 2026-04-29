from __future__ import annotations

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

async def loadSettings(source: Scope, cwd: str) -> dict[str, Any]:
    return await readJsonFile(_settings_path(source, cwd), {})

async def saveSettings(source: Scope, cwd: str, settings: dict[str, Any]) -> None:
    await writeJsonFile(_settings_path(source, cwd), settings)

async def loadSettingsBySources(sources: list[Scope], cwd: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        merged.update(await loadSettings(source, cwd))
    return merged

async def loadProjectMemoryRecords(cwd: str) -> list[dict[str, Any]]:
    return await readJsonFile(getProjectMemoryRecordsPath(cwd), [])

async def saveProjectMemoryRecords(cwd: str, records: list[dict[str, Any]]) -> None:
    await writeJsonFile(getProjectMemoryRecordsPath(cwd), records)

async def loadMcpServers(scope: Scope, cwd: str) -> list[dict[str, Any]]:
    return await readJsonFile(_settings_path(scope, cwd, "mcp.json"), [])

async def saveMcpServers(scope: Scope, cwd: str, servers: list[dict[str, Any]]) -> None:
    await writeJsonFile(_settings_path(scope, cwd, "mcp.json"), servers)

async def loadPlugins(scope: Scope, cwd: str) -> list[dict[str, Any]]:
    return await readJsonFile(_settings_path(scope, cwd, "plugins.json"), [])

async def savePlugins(scope: Scope, cwd: str, records: list[dict[str, Any]]) -> None:
    await writeJsonFile(_settings_path(scope, cwd, "plugins.json"), records)

async def loadMarketplaces() -> list[dict[str, Any]]:
    return await readJsonFile(os.path.join(getStateRoot(), "marketplaces.json"), [])

async def saveMarketplaces(records: list[dict[str, Any]]) -> None:
    await writeJsonFile(os.path.join(getStateRoot(), "marketplaces.json"), records)

async def loadAutoMode(cwd: str) -> dict[str, Any]:
    return await readJsonFile(os.path.join(getProjectStateDir(cwd), "auto-mode.json"), clone(DEFAULT_AUTO_MODE))

async def saveAutoMode(cwd: str, config: dict[str, Any]) -> None:
    await writeJsonFile(os.path.join(getProjectStateDir(cwd), "auto-mode.json"), config)

def getDefaultAutoMode() -> dict[str, Any]:
    return clone(DEFAULT_AUTO_MODE)

def resolveScope(scope: str | None) -> Scope:
    return scope if scope in ("user", "project", "local") else "user"  # type: ignore[return-value]
