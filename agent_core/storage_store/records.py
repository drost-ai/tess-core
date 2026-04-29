from __future__ import annotations

from .shared import *

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
