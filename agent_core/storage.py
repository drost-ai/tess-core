from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Literal

from ._utils import clone, now_iso


Scope = Literal["user", "project", "local"]

DEFAULT_RUNTIME_HOME = "~/.agent-core"
_configured_runtime_home: str | None = None

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


def expandHome(rawPath: str) -> str:
    if rawPath == "~":
        return str(Path.home())
    if rawPath.startswith("~/"):
        return str(Path.home() / rawPath[2:])
    return rawPath


expand_home = expandHome


def resolveRuntimeHome(cwd: str | None = None, candidate: str | None = None) -> str:
    raw = candidate or os.environ.get("AGENT_CORE_HOME") or DEFAULT_RUNTIME_HOME
    expanded = expandHome(raw)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(cwd or os.getcwd(), expanded))


resolve_runtime_home = resolveRuntimeHome


def configureRuntimeHome(runtimeHome: str) -> str:
    global _configured_runtime_home
    _configured_runtime_home = resolveRuntimeHome(None, runtimeHome)
    return _configured_runtime_home


configure_runtime_home = configureRuntimeHome


def resetConfiguredRuntimeHome() -> None:
    global _configured_runtime_home
    _configured_runtime_home = None


reset_configured_runtime_home = resetConfiguredRuntimeHome


def getStateRoot() -> str:
    return _configured_runtime_home or resolveRuntimeHome()


get_state_root = getStateRoot


def findGitRoot(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        value = result.stdout.strip()
        return value or None
    except Exception:
        return None


find_git_root = findGitRoot


def getProjectRoot(cwd: str) -> str:
    return findGitRoot(cwd) or cwd


get_project_root = getProjectRoot


def getProjectStateDir(cwd: str) -> str:
    return os.path.join(getProjectRoot(cwd), ".tess", "project")


get_project_state_dir = getProjectStateDir


def getLocalStateDir(cwd: str) -> str:
    return os.path.join(cwd, ".tess", "local")


get_local_state_dir = getLocalStateDir


def getProjectMemoryPath(cwd: str) -> str:
    return os.path.join(getProjectStateDir(cwd), "memory.md")


def getProjectMemoryRecordsPath(cwd: str) -> str:
    return os.path.join(getProjectStateDir(cwd), "memory-records.json")


def getPlansDir() -> str:
    return os.path.join(getStateRoot(), "plans")


def getDaemonDir() -> str:
    return os.path.join(getStateRoot(), "daemon")


def daemonSocketPath() -> str:
    return os.path.join(getDaemonDir(), "socket.sock")


def daemonPidPath() -> str:
    return os.path.join(getDaemonDir(), "pid")


def daemonStatePath() -> str:
    return os.path.join(getDaemonDir(), "state.json")


def daemonEventsPath() -> str:
    return os.path.join(getDaemonDir(), "events.jsonl")


def daemonLogsDir() -> str:
    return os.path.join(getDaemonDir(), "logs")


def getCompactionsDir() -> str:
    return os.path.join(getStateRoot(), "compactions")


def getToolArtifactsDir() -> str:
    return os.path.join(getStateRoot(), "tool-artifacts")


def getSessionToolArtifactsDir(sessionId: str) -> str:
    return os.path.join(getToolArtifactsDir(), sessionId)


def toolArtifactMetaPath(sessionId: str, artifactId: str) -> str:
    return os.path.join(getSessionToolArtifactsDir(sessionId), f"{artifactId}.meta.json")


def toolArtifactDataPath(sessionId: str, artifactId: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    return os.path.join(getSessionToolArtifactsDir(sessionId), f"{artifactId}{ext}")


def getSessionCompactionsDir(sessionId: str) -> str:
    return os.path.join(getCompactionsDir(), sessionId)


def compactionManifestPath(sessionId: str) -> str:
    return os.path.join(getSessionCompactionsDir(sessionId), "manifest.json")


def compactionCheckpointPath(sessionId: str, generation: int) -> str:
    return os.path.join(getSessionCompactionsDir(sessionId), "checkpoints", f"{generation:04d}.json")


async def ensureBaseLayout(cwd: str) -> None:
    dirs = [
        getStateRoot(),
        getDaemonDir(),
        os.path.join(getStateRoot(), "sessions"),
        getCompactionsDir(),
        getToolArtifactsDir(),
        os.path.join(getStateRoot(), "tasks"),
        os.path.join(getStateRoot(), "task-records"),
        os.path.join(getStateRoot(), "monitors"),
        os.path.join(getStateRoot(), "agents"),
        os.path.join(getStateRoot(), "plan-approvals"),
        getPlansDir(),
        os.path.join(getStateRoot(), "remote-sessions"),
        os.path.join(getStateRoot(), "bridges"),
        os.path.join(getStateRoot(), "logs"),
        daemonLogsDir(),
        getProjectStateDir(cwd),
        getLocalStateDir(cwd),
    ]
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)


ensure_base_layout = ensureBaseLayout


async def readJsonFile(filePath: str, fallback: Any) -> Any:
    try:
        with open(filePath, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback


read_json_file = readJsonFile


async def writeJsonFile(filePath: str, value: Any) -> None:
    os.makedirs(os.path.dirname(filePath), exist_ok=True)
    tmp = f"{filePath}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, filePath)


write_json_file = writeJsonFile


async def appendJsonLine(filePath: str, value: Any) -> None:
    os.makedirs(os.path.dirname(filePath), exist_ok=True)
    with open(filePath, "a", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.write("\n")


append_json_line = appendJsonLine


def parseJsonString(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def fileOrJson(value: str | None) -> Any:
    if not value:
        return None
    trimmed = value.strip()
    if trimmed.startswith("{") or trimmed.startswith("["):
        return json.loads(trimmed)
    with open(trimmed, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolveMaybeRelative(baseDir: str, candidate: str) -> str:
    expanded = expandHome(candidate)
    return os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(baseDir, expanded))


resolve_maybe_relative = resolveMaybeRelative


async def loadAuth() -> dict[str, Any] | None:
    return await readJsonFile(os.path.join(getStateRoot(), "auth.json"), None)


load_auth = loadAuth


async def loadCodexAuthToken(authPath: str) -> str | None:
    try:
        with open(authPath, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        api_key = str(payload.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            return api_key
        token = str((payload.get("tokens") or {}).get("access_token") or "").strip()
        return token or None
    except Exception:
        return None


load_codex_auth_token = loadCodexAuthToken


async def saveAuth(auth: dict[str, Any]) -> None:
    await writeJsonFile(os.path.join(getStateRoot(), "auth.json"), auth)


async def clearAuth() -> None:
    try:
        os.remove(os.path.join(getStateRoot(), "auth.json"))
    except FileNotFoundError:
        pass


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


def sessionPath(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", f"{sessionId}.json")


def sessionArchiveDir(sessionId: str) -> str:
    return os.path.join(getStateRoot(), "sessions", sessionId)


def sessionTranscriptLogPath(sessionId: str) -> str:
    return os.path.join(sessionArchiveDir(sessionId), "full-transcript-with-tools.jsonl")


async def saveSession(session: dict[str, Any]) -> None:
    await writeJsonFile(sessionPath(session["id"]), session)


async def appendSessionTranscriptLogEntries(sessionId: str, entries: list[Any]) -> str:
    path = sessionTranscriptLogPath(sessionId)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if entries:
        with open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                json.dump(entry, handle, ensure_ascii=False)
                handle.write("\n")
    return path


def _parse_archived_messages(raw: str) -> list[dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") == "message" and isinstance(entry.get("message_index"), int):
            role = entry.get("role")
            if role in ("user", "assistant") and "content" in entry:
                indexed[entry["message_index"]] = {"role": role, "content": entry["content"]}
    return [indexed[index] for index in sorted(indexed)]


def _latest_archived_timestamp(raw: str) -> str | None:
    latest = None
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry.get("persisted_at"), str):
            latest = entry["persisted_at"]
    return latest


async def loadSession(sessionId: str) -> dict[str, Any] | None:
    path = sessionPath(sessionId)
    record = await readJsonFile(path, None)
    if not record:
        return None
    transcript_path = sessionTranscriptLogPath(sessionId)
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return record
    archived = _parse_archived_messages(raw)
    latest = _latest_archived_timestamp(raw)
    if len(archived) > len(record.get("messages", [])):
        record["messages"] = archived
    metadata = record.setdefault("metadata", {})
    metadata["fullTranscriptLog"] = {
        "path": transcript_path,
        "archivedMessageCount": max(len(archived), len(record.get("messages", []))),
        "lastArchivedAt": latest or metadata.get("fullTranscriptLog", {}).get("lastArchivedAt") or record.get("updatedAt"),
    }
    return record


async def loadCompactionManifest(sessionId: str) -> dict[str, Any] | None:
    return await readJsonFile(compactionManifestPath(sessionId), None)


async def saveCompactionManifest(manifest: dict[str, Any]) -> None:
    await writeJsonFile(compactionManifestPath(manifest["sessionId"]), manifest)


async def saveCompactionCheckpoint(checkpoint: dict[str, Any]) -> None:
    await writeJsonFile(compactionCheckpointPath(checkpoint["sessionId"], int(checkpoint["generation"])), checkpoint)


def hasPersistableSessionMessages(session: dict[str, Any]) -> bool:
    for message in session.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    return True
    return False


async def listSessions() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "sessions")
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json")]
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for name in names:
        record = await loadSession(name[:-5])
        if record and hasPersistableSessionMessages(record):
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


def createEmptySession(params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    params = {**(params or {}), **kwargs}
    timestamp = now_iso()
    return {
        "id": params.get("sessionId") or str(uuid.uuid4()),
        "cwd": params["cwd"],
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "name": params.get("name"),
        "provider": params["provider"],
        "providerProfileName": params.get("providerProfileName"),
        "model": params["model"],
        "effort": params.get("effort"),
        "systemPrompt": params["systemPrompt"],
        "permissionMode": params["permissionMode"],
        "messages": [],
        "todos": [],
        "metadata": {
            "originalCwd": params.get("originalCwd") or params["cwd"],
            "assistantName": params.get("assistantName"),
            "assistantDescription": params.get("assistantDescription"),
            "usage": {
                "turns": 0,
                "toolCalls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheCreationInputTokens": 0,
                "cacheReadInputTokens": 0,
            },
            "compactions": 0,
            "depth": 0,
            "childSessionIds": [],
            "childAgentIds": [],
            "planMode": {"state": "inactive"},
        },
    }


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


def taskDir(taskId: str) -> str:
    return os.path.join(getStateRoot(), "tasks", taskId)


def taskRecordPath(taskId: str) -> str:
    return os.path.join(getStateRoot(), "task-records", f"{taskId}.json")


def taskMetaPath(taskId: str) -> str:
    return os.path.join(taskDir(taskId), "meta.json")


def taskOutputPath(taskId: str) -> str:
    return os.path.join(taskDir(taskId), "output.log")


def healthCheckMetaPath(taskId: str) -> str:
    return os.path.join(taskDir(taskId), "health-check.json")


def monitorPath(monitorId: str) -> str:
    return os.path.join(getStateRoot(), "monitors", f"{monitorId}.json")


def childAgentPath(agentId: str) -> str:
    return os.path.join(getStateRoot(), "agents", f"{agentId}.json")


async def _save_by_path(path: str, record: dict[str, Any]) -> None:
    await writeJsonFile(path, record)


async def _load_by_path(path: str) -> dict[str, Any] | None:
    return await readJsonFile(path, None)


def _omit_none_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _omit_none_fields(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_none_fields(item) for item in value]
    return value


async def _list_json_records(directory: str, loader) -> list[dict[str, Any]]:
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json")]
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        value = await loader(name[:-5])
        if value:
            records.append(value)
    return records


async def saveChildAgent(record: dict[str, Any]) -> None:
    await _save_by_path(childAgentPath(record["agentId"]), _omit_none_fields(record))


async def loadChildAgent(agentId: str) -> dict[str, Any] | None:
    return await _load_by_path(childAgentPath(agentId))


async def listChildAgents() -> list[dict[str, Any]]:
    records = await _list_json_records(os.path.join(getStateRoot(), "agents"), loadChildAgent)
    return sorted(records, key=lambda item: item.get("startedAt", ""), reverse=True)


def planApprovalPath(requestId: str) -> str:
    return os.path.join(getStateRoot(), "plan-approvals", f"{requestId}.json")


async def savePlanApproval(record: dict[str, Any]) -> None:
    await _save_by_path(planApprovalPath(record["requestId"]), record)


async def loadPlanApproval(requestId: str) -> dict[str, Any] | None:
    return await _load_by_path(planApprovalPath(requestId))


async def listPlanApprovals() -> list[dict[str, Any]]:
    records = await _list_json_records(os.path.join(getStateRoot(), "plan-approvals"), loadPlanApproval)
    return sorted(records, key=lambda item: item.get("createdAt", ""), reverse=True)


PLAN_ADJECTIVES = ["amber", "brisk", "clear", "copper", "ember", "gentle", "midnight", "quiet", "rapid", "silver", "solid", "steady", "sunlit", "tidy", "velvet"]
PLAN_NOUNS = ["anchor", "beacon", "bridge", "compass", "falcon", "harbor", "lantern", "meadow", "signal", "sparrow", "summit", "thunder", "trail", "valley", "willow"]


async def writePlanFile(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    file_path = args.get("filePath")
    if not file_path:
        for _ in range(12):
            candidate = os.path.join(getPlansDir(), f"{random.choice(PLAN_ADJECTIVES)}-{random.choice(PLAN_NOUNS)}.md")
            if not os.path.exists(candidate):
                file_path = candidate
                break
        file_path = file_path or os.path.join(getPlansDir(), f"{uuid.uuid4()}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    lines = [
        "# Assistant Plan",
        "",
        f"- request id: `{args['requestId']}`",
        f"- session id: `{args['sessionId']}`",
        f"- status: `{args['status']}`",
        f"- cwd: `{args['cwd']}`",
        f"- created at: `{args['createdAt']}`",
    ]
    if args.get("resolvedAt"):
        lines.append(f"- resolved at: `{args['resolvedAt']}`")
    if args.get("reason"):
        lines.append(f"- reason: {args['reason']}")
    lines.extend(["", "## Plan", "", (args.get("plan") or "").strip() or "_No plan text captured._"])
    if args.get("allowedPrompts"):
        lines.extend(["", "## Allowed Prompts", ""])
        for prompt in args["allowedPrompts"]:
            lines.append(f"- `{prompt.get('tool')}`: {prompt.get('prompt')}")
    lines.extend(["", "## Retention", "", "This plan is retained indefinitely for session history and auditability.", ""])
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return file_path


async def writeTaskMeta(taskId: str, value: dict[str, Any]) -> None:
    await writeJsonFile(taskMetaPath(taskId), value)


async def readTaskMeta(taskId: str) -> dict[str, Any] | None:
    return await readJsonFile(taskMetaPath(taskId), None)


async def saveTaskRecord(record: dict[str, Any]) -> None:
    await writeJsonFile(taskRecordPath(record["id"]), record)


async def loadTaskRecord(taskId: str) -> dict[str, Any] | None:
    return await readJsonFile(taskRecordPath(taskId), None)


async def listTaskRecords() -> list[dict[str, Any]]:
    records = await _list_json_records(os.path.join(getStateRoot(), "task-records"), loadTaskRecord)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


async def listSessionTaskRecords(sessionId: str) -> list[dict[str, Any]]:
    return [task for task in await listTaskRecords() if task.get("sessionId") == sessionId]


async def saveMonitor(record: dict[str, Any]) -> None:
    await writeJsonFile(monitorPath(record["id"]), record)


async def loadMonitor(monitorId: str) -> dict[str, Any] | None:
    return await readJsonFile(monitorPath(monitorId), None)


async def listMonitors() -> list[dict[str, Any]]:
    records = await _list_json_records(os.path.join(getStateRoot(), "monitors"), loadMonitor)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


async def listSessionMonitors(sessionId: str) -> list[dict[str, Any]]:
    return [monitor for monitor in await listMonitors() if monitor.get("sessionId") == sessionId]


def remoteSessionPath(remoteSessionId: str) -> str:
    return os.path.join(getStateRoot(), "remote-sessions", f"{remoteSessionId}.json")


async def saveRemoteSession(record: dict[str, Any]) -> None:
    await writeJsonFile(remoteSessionPath(record["id"]), _omit_none_fields(record))


async def loadRemoteSession(remoteSessionId: str) -> dict[str, Any] | None:
    return await readJsonFile(remoteSessionPath(remoteSessionId), None)


async def listRemoteSessions() -> list[dict[str, Any]]:
    records = await _list_json_records(os.path.join(getStateRoot(), "remote-sessions"), loadRemoteSession)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


def bridgePath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{bridgeId}.json")


def bridgeInboxPath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{bridgeId}.inbox.json")


def bridgeEventsPath(bridgeId: str) -> str:
    return os.path.join(getStateRoot(), "bridges", f"{bridgeId}.events.json")


async def saveBridge(record: dict[str, Any]) -> None:
    await writeJsonFile(bridgePath(record["id"]), _omit_none_fields(record))


async def loadBridge(bridgeId: str) -> dict[str, Any] | None:
    return await readJsonFile(bridgePath(bridgeId), None)


async def listBridges() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "bridges")
    try:
        names = [
            name for name in os.listdir(directory)
            if name.endswith(".json") and not name.endswith(".inbox.json") and not name.endswith(".events.json")
        ]
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        record = await loadBridge(name[:-5])
        if record:
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


async def loadBridgeInbox(bridgeId: str) -> list[dict[str, Any]]:
    return await readJsonFile(bridgeInboxPath(bridgeId), [])


async def appendBridgeInboxMessage(message: dict[str, Any]) -> None:
    existing = await loadBridgeInbox(message["bridgeId"])
    await writeJsonFile(bridgeInboxPath(message["bridgeId"]), _omit_none_fields([*existing, message]))


async def loadBridgeEvents(bridgeId: str) -> list[dict[str, Any]]:
    return await readJsonFile(bridgeEventsPath(bridgeId), [])


async def appendBridgeEvent(record: dict[str, Any]) -> None:
    existing = await loadBridgeEvents(record["bridgeId"])
    await writeJsonFile(bridgeEventsPath(record["bridgeId"]), _omit_none_fields([*existing, record]))


async def saveDaemonState(record: dict[str, Any]) -> None:
    await writeJsonFile(daemonStatePath(), record)
    os.makedirs(os.path.dirname(daemonPidPath()), exist_ok=True)
    with open(daemonPidPath(), "w", encoding="utf-8") as handle:
        handle.write(f"{record.get('pid')}\n")


async def loadDaemonState() -> dict[str, Any] | None:
    return await readJsonFile(daemonStatePath(), None)


async def clearDaemonState() -> None:
    for path in (daemonPidPath(), daemonStatePath(), daemonSocketPath()):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


async def appendDaemonEvent(record: dict[str, Any]) -> None:
    await appendJsonLine(daemonEventsPath(), record)


async def loadDaemonEvents() -> list[dict[str, Any]]:
    try:
        with open(daemonEventsPath(), "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except Exception:
        return []


async def saveHealthCheckTask(record: dict[str, Any]) -> None:
    await writeJsonFile(healthCheckMetaPath(record["taskId"]), record)


async def loadHealthCheckTask(taskId: str) -> dict[str, Any] | None:
    return await readJsonFile(healthCheckMetaPath(taskId), None)


async def listHealthCheckTasks() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "tasks")
    try:
        names = [name for name in os.listdir(directory) if os.path.isdir(os.path.join(directory, name))]
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        record = await loadHealthCheckTask(name)
        if record:
            records.append(record)
    return sorted(records, key=lambda item: item.get("updatedAt", ""), reverse=True)


def resolveScope(scope: str | None) -> Scope:
    return scope if scope in ("user", "project", "local") else "user"  # type: ignore[return-value]


async def runCommand(command: str, args: list[str], cwd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(stderr.decode("utf-8", errors="replace"))
    return stdout.decode("utf-8", errors="replace")
