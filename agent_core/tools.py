from __future__ import annotations

import asyncio
import base64
import difflib
import glob as glob_lib
import html
import json
import mimetypes
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from ._utils import maybe_await, now_iso, pretty_json
from .storage import (
    loadAutoMode,
    loadMonitor,
    loadSession,
    readTaskMeta,
    resolveMaybeRelative,
    saveMonitor,
    savePlanApproval,
    taskMetaPath,
    taskOutputPath,
    writePlanFile,
    writeTaskMeta,
)
from .daemon import connectTaskSupervisorDaemonClient, ensureTaskSupervisorDaemon
from .tool_payloads import (
    filterDefaultIgnoredGlobMatches,
    shapeGlobPayload,
    shapeGrepPayload,
    shapeReadPayload,
    shapeTextToolResult,
)


@dataclass
class ToolDefinition:
    name: str
    description: str
    inputSchema: dict[str, Any]
    handler: Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.inputSchema


class FetchResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    async def text(self) -> str:
        return self.body


async def fetch(url: str, *, headers: dict[str, str] | None = None, signal: Any = None) -> FetchResponse:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        return FetchResponse(response.status, body, dict(response.headers))


def createBuiltinTools(options: dict[str, Any] | None = None) -> list[ToolDefinition]:
    options = options or {}
    return [
        _read_tool(),
        _write_tool(),
        _edit_tool(),
        _glob_tool(),
        _grep_tool(),
        _bash_tool(),
        _todo_tool(),
        _web_fetch_tool(),
        _web_search_tool(),
        _set_status_tool(),
        _agent_tool(),
        _ask_user_question_tool(),
        _config_tool(),
        _exit_plan_mode_tool(),
        _notebook_edit_tool(),
        _task_output_tool(),
        _task_stop_tool(),
        _task_monitor_start_tool(),
        _task_monitor_stop_tool(),
        _task_health_check_start_tool(),
        _task_health_check_stop_tool(),
        _mcp_tool(options.get("mcpRuntime")),
        _list_mcp_resources_tool(options.get("mcpRuntime")),
        _read_mcp_resource_tool(options.get("mcpRuntime")),
        _enter_worktree_tool(options.get("workspaceController")),
        _exit_worktree_tool(options.get("workspaceController")),
    ]


create_builtin_tools = createBuiltinTools


def _read_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context)
        data = Path(file_path).read_bytes()
        ext = Path(file_path).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return {"content": pretty_json({"type": "image", "file": {"filePath": file_path, "base64": base64.b64encode(data).decode(), "mime": mimetypes.guess_type(file_path)[0] or "image/jpeg", "originalSize": len(data)}})}
        if ext == ".pdf":
            return {"content": pretty_json({"type": "pdf", "file": {"filePath": file_path, "base64": base64.b64encode(data).decode(), "originalSize": len(data)}})}
        if ext == ".ipynb":
            notebook = json.loads(data.decode("utf-8"))
            return {"content": pretty_json({"type": "notebook", "file": {"filePath": file_path, "cells": notebook.get("cells", [])}})}
        text = data.decode("utf-8", errors="replace")
        payload = await shapeReadPayload(
            config=context["config"],
            sessionId=context["session"]["id"],
            filePath=file_path,
            lines=text.splitlines(),
            offset=int(input.get("offset") or 1),
            requestedLimit=input.get("limit"),
        )
        return {"content": pretty_json(payload)}

    return ToolDefinition("Read", "Read files from disk, including partial line ranges for large text files.", {"type": "object"}, handler)


def _write_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context, True)
        content = _require_string(input.get("content"), "content")
        await _ensure_tool_permission(context, "Write", "edit", f"Write {_display_path(context, file_path)} ({len(content)} bytes)")
        previous = Path(file_path).read_text(encoding="utf-8") if Path(file_path).exists() else None
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        Path(file_path).write_text(content, encoding="utf-8")
        return {"content": pretty_json(_file_change_payload("Write", file_path, previous, content))}

    return ToolDefinition("Write", "Write full file contents to disk, creating parent directories when needed.", {"type": "object"}, handler)


def _edit_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context)
        old = _require_string(input.get("old_string"), "old_string")
        new = _require_string(input.get("new_string"), "new_string")
        replace_all = bool(input.get("replace_all"))
        await _ensure_tool_permission(context, "Edit", "edit", f"Edit {_display_path(context, file_path)}")
        original = Path(file_path).read_text(encoding="utf-8")
        if old not in original:
            return {"content": f"String not found in {file_path}", "isError": True}
        updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
        Path(file_path).write_text(updated, encoding="utf-8")
        return {"content": pretty_json(_file_change_payload("Edit", file_path, original, updated, {"note": "Replaced all occurrences." if replace_all else "Replaced the first matching occurrence."}))}

    return ToolDefinition("Edit", "Apply exact string replacements inside a file.", {"type": "object"}, handler)


def _glob_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        pattern = _require_string(input.get("pattern"), "pattern")
        search_path = _authorize_path(input.get("path") or context["cwd"], context)
        matches = glob_lib.glob(os.path.join(search_path, pattern), recursive=True)
        rel = [os.path.relpath(path, search_path) if os.path.isabs(path) else path for path in matches if os.path.isfile(path)]
        rel = filterDefaultIgnoredGlobMatches(sorted(rel))
        payload = await shapeGlobPayload(config=context["config"], sessionId=context["session"]["id"], pattern=pattern, searchPath=search_path, matches=rel, ignoredDefaultsApplied=True)
        return {"content": pretty_json(payload)}

    return ToolDefinition("Glob", "List files that match a glob pattern.", {"type": "object"}, handler)


def _grep_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        pattern = _require_string(input.get("pattern"), "pattern")
        search_path = _authorize_path(input.get("path") or context["cwd"], context)
        regex = re.compile(pattern, re.I if input.get("-i") else 0)
        output_mode = input.get("output_mode") or "files_with_matches"
        matches: list[str] = []
        glob_pattern = input.get("glob") or "**/*"
        for path in glob_lib.glob(os.path.join(search_path, glob_pattern), recursive=True):
            if not os.path.isfile(path):
                continue
            if not filterDefaultIgnoredGlobMatches([os.path.relpath(path, search_path)]):
                continue
            try:
                lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            file_hits = []
            for idx, line in enumerate(lines, 1):
                if regex.search(line):
                    file_hits.append((idx, line))
            if not file_hits:
                continue
            rel = os.path.relpath(path, search_path)
            if output_mode == "files_with_matches":
                matches.append(rel)
            elif output_mode == "count":
                matches.append(f"{rel}:{len(file_hits)}")
            else:
                matches.extend(f"{rel}:{idx}:{line}" for idx, line in file_hits)
        payload = await shapeGrepPayload(config=context["config"], sessionId=context["session"]["id"], pattern=pattern, searchPath=search_path, outputMode=output_mode, matches=matches, offset=int(input.get("offset") or 0), requestedHeadLimit=input.get("head_limit"))
        return {"content": pretty_json(payload)}

    return ToolDefinition("Grep", "Search file contents.", {"type": "object"}, handler)


def _bash_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        command = _require_string(input.get("command"), "command")
        timeout = _normalize_bash_timeout(command, input.get("timeout"))
        await _ensure_tool_permission(context, "Bash", "shell", f"Run shell command\n{command}", always_ask=bool(input.get("run_in_background")), command=command)
        if input.get("run_in_background"):
            supervisor = context.get("daemon") or context.get("taskSupervisor") or await ensureTaskSupervisorDaemon({"cwd": context["session"]["cwd"]})
            started = await supervisor["startBackgroundCommand"](
                {
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "title": input.get("description") or input.get("title"),
                }
            )
            task_id = started.get("taskId") or started.get("task_id")
            output_path = started.get("outputFile") or started.get("output_file") or taskOutputPath(task_id)
            context["session"].setdefault("metadata", {}).setdefault("taskIds", []).append(task_id)
            await maybe_await(context.get("persistSession", lambda: None)())
            return {"content": pretty_json({"task_id": task_id, "status": "running", "output_file": output_path})}
        proc = await asyncio.create_subprocess_shell(command, cwd=context["session"]["cwd"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            proc.terminate()
            stdout, stderr = await proc.communicate()
        shaped_stdout = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="Bash", family="bash", text=stdout.decode(errors="replace"), strategy="head_tail")
        shaped_stderr = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="Bash", family="bash", text=stderr.decode(errors="replace"), strategy="head_tail")
        return {"content": pretty_json({"command": command, "cwd": context["session"]["cwd"], "exitCode": proc.returncode, "timedOut": timed_out, "stdout": shaped_stdout["text"], "stderr": shaped_stderr["text"], "stdout_manifest": shaped_stdout["view"], "stderr_manifest": shaped_stderr["view"]}), "isError": bool(timed_out or proc.returncode)}

    return ToolDefinition("Bash", "Run shell commands in the current workspace.", {"type": "object"}, handler)


def _todo_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        todos = input.get("todos")
        if not isinstance(todos, list):
            raise ValueError("Expected todos to be an array")
        context["session"]["todos"] = todos
        await maybe_await(context.get("persistSession", lambda: None)())
        return {"content": pretty_json({"updated": len(todos), "todos": todos})}

    return ToolDefinition("TodoWrite", "Update the session todo list.", {"type": "object"}, handler)


def _web_fetch_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        url = _require_string(input.get("url"), "url")
        prompt = _require_string(input.get("prompt"), "prompt")
        response = await fetch(url, headers={"user-agent": "tess/0.1"}, signal=context.get("abortSignal"))
        if not response.ok:
            return {"content": f"Fetch failed with status {response.status}", "isError": True}
        raw = await response.text()
        shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="WebFetch", family="web", text=_strip_html(raw), strategy="head")
        return {"content": pretty_json({"url": url, "prompt": prompt, "content": shaped["text"], "content_manifest": shaped["view"]})}

    return ToolDefinition("WebFetch", "Fetch a URL and return a text snapshot of its contents.", {"type": "object"}, handler)


def _web_search_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        query = _require_string(input.get("query"), "query")
        allowed = [item for item in input.get("allowed_domains", []) if isinstance(item, str)]
        blocked = [item for item in input.get("blocked_domains", []) if isinstance(item, str)]
        results = []
        for result in await searchWeb(query, context.get("abortSignal")):
            hostname = _safe_hostname(result["url"])
            if not hostname:
                continue
            if allowed and not any(hostname.endswith(domain) for domain in allowed):
                continue
            if any(hostname.endswith(domain) for domain in blocked):
                continue
            results.append(result)
            if len(results) >= 8:
                break
        return {"content": pretty_json({"query": query, "results": results})}

    return ToolDefinition("WebSearch", "Perform a lightweight web search and return ranked results.", {"type": "object"}, handler)


def _set_status_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        message = _require_string(input.get("message"), "message").strip()
        detail = input.get("detail") if isinstance(input.get("detail"), str) and input.get("detail").strip() else None
        context.get("outputJson", lambda *_: None)("status.updated", {"message": message, "detail": detail, "session_id": context["session"]["id"]})
        return {"content": pretty_json({"message": message, "detail": detail})}

    return ToolDefinition("SetStatus", "Update the assistant's live status line.", {"type": "object"}, handler)


def _agent_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        description = _require_string(input.get("description"), "description")
        prompt = _require_string(input.get("prompt"), "prompt")
        if callable(context.get("runSubagent")):
            result = await maybe_await(context["runSubagent"](input, context))
            return {"content": pretty_json(result) if not isinstance(result, str) else result}
        if input.get("run_in_background"):
            supervisor = context.get("daemon") or context.get("taskSupervisor") or await ensureTaskSupervisorDaemon({"cwd": context["session"]["cwd"]})
            child_session_id = input.get("session_id") or str(uuid.uuid4())
            started = await supervisor["startSubagentTask"](
                {
                    "parentSessionId": context["session"]["id"],
                    "sessionId": child_session_id,
                    "agentId": input.get("subagent_type") or input.get("name") or "default",
                    "cwd": context["session"]["cwd"],
                    "label": description,
                }
            )
            return {"content": pretty_json({"task_id": started.get("taskId") or started.get("task_id"), "session_id": child_session_id, "description": description})}
        return {
            "content": pretty_json(
                {
                    "description": description,
                    "prompt": prompt,
                    "status": "requires_runtime",
                    "message": "Foreground subagent execution requires a runSubagent callback from the product runtime.",
                }
            ),
            "isError": True,
        }

    return ToolDefinition("Agent", "Spawn a delegated subagent to work on a focused task.", {"type": "object"}, handler)


def _ask_user_question_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        prompt = _require_string(input.get("prompt") or input.get("question"), "prompt")
        options = input.get("options") if isinstance(input.get("options"), list) else None
        answer = await maybe_await(context.get("askQuestion", lambda *_: "")(prompt, options))
        return {"content": pretty_json({"answer": answer})}

    return ToolDefinition("AskUserQuestion", "Ask the user a question.", {"type": "object"}, handler)


def _config_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        config = context["config"]
        public = {key: config.get(key) for key in ("provider", "providerProfileName", "model", "effort", "permissionMode", "cwd")}
        return {"content": pretty_json(public)}

    return ToolDefinition("Config", "Inspect current runtime configuration.", {"type": "object"}, handler)


def _exit_plan_mode_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        plan = input.get("plan")
        allowed = parseAllowedPrompts(input.get("allowedPrompts"))
        request_id = str(uuid.uuid4())
        created_at = now_iso()
        file_path = await writePlanFile(requestId=request_id, sessionId=context["session"]["id"], cwd=context["session"]["cwd"], plan=plan, allowedPrompts=allowed, status="pending", createdAt=created_at)
        context["session"].setdefault("metadata", {})["planMode"] = {"state": "pending", "requestId": request_id, "plan": plan, "filePath": file_path, "allowedPrompts": allowed}
        await savePlanApproval({"requestId": request_id, "sessionId": context["session"]["id"], "agentId": context["session"].get("metadata", {}).get("agentId"), "parentSessionId": context["session"].get("metadata", {}).get("parentSessionId"), "status": "pending", "plan": plan, "filePath": file_path, "allowedPrompts": allowed, "createdAt": created_at})
        await maybe_await(context.get("persistSession", lambda: None)())
        return {"content": pretty_json({"request_id": request_id, "status": "pending", "file_path": file_path}), "pause": "approval"}

    return ToolDefinition("ExitPlanMode", "Submit a plan for approval and pause before implementation.", {"type": "object"}, handler)


def _notebook_edit_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        notebook_path = _authorize_path(_require_string(input.get("notebook_path"), "notebook_path"), context)
        edit_mode = input.get("edit_mode") if isinstance(input.get("edit_mode"), str) else "replace"
        await _ensure_tool_permission(context, "NotebookEdit", "edit", f"{edit_mode} notebook cell in {_display_path(context, notebook_path)}")
        notebook = json.loads(Path(notebook_path).read_text(encoding="utf-8"))
        cells = notebook.get("cells") if isinstance(notebook.get("cells"), list) else []
        cell_id = input.get("cell_id") if isinstance(input.get("cell_id"), str) else None
        new_source = _require_string(input.get("new_source"), "new_source")
        cell_type = input.get("cell_type") if isinstance(input.get("cell_type"), str) else "code"
        index = next((idx for idx, cell in enumerate(cells) if isinstance(cell, dict) and cell.get("id") == cell_id), -1) if cell_id else (0 if cells else -1)
        if edit_mode == "delete":
            if index < 0:
                return {"content": "Cell not found", "isError": True}
            cells.pop(index)
        elif edit_mode == "insert":
            cell = {"id": str(uuid.uuid4()), "cell_type": cell_type, "source": [new_source], "metadata": {}, "outputs": []}
            if index < 0:
                cells.insert(0, cell)
            else:
                cells.insert(index + 1, cell)
        else:
            if index < 0:
                return {"content": "Cell not found", "isError": True}
            cells[index] = {**cells[index], "cell_type": cell_type, "source": [new_source]}
        notebook["cells"] = cells
        Path(notebook_path).write_text(f"{json.dumps(notebook, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
        return {"content": f"Updated notebook {notebook_path}"}

    return ToolDefinition("NotebookEdit", "Edit notebook cells.", {"type": "object"}, handler)


def _task_output_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        if input.get("block"):
            started = asyncio.get_running_loop().time()
            timeout = float(input.get("timeout") or 30000) / 1000
            while asyncio.get_running_loop().time() - started < timeout:
                meta = await readTaskMeta(task_id)
                if not meta or meta.get("status") not in ("running", "reconciling"):
                    break
                await asyncio.sleep(0.25)
        meta = await readTaskMeta(task_id)
        if not meta:
            return {"content": f"Task not found: {task_id}", "isError": True}
        output_result = None
        supervisor = context.get("daemon") or context.get("taskSupervisor") or await connectTaskSupervisorDaemonClient()
        if supervisor:
            try:
                output_result = await supervisor["readTaskOutput"](task_id)
            except Exception:
                output_result = None
        output_file = (meta or {}).get("outputFile") or taskOutputPath(task_id)
        text = output_result.get("output") if output_result else Path(output_file).read_text(encoding="utf-8", errors="replace") if Path(output_file).exists() else ""
        shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="TaskOutput", family="bash", text=text, taskId=task_id, strategy="tail")
        return {"content": pretty_json({**meta, "task_id": task_id, "output": shaped["text"], "output_file": output_file, "output_truncated": bool(output_result and output_result.get("truncated")), "output_manifest": shaped["view"]})}

    return ToolDefinition("TaskOutput", "Read background task output.", {"type": "object"}, handler)


def _task_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        supervisor = context.get("daemon") or context.get("taskSupervisor") or await connectTaskSupervisorDaemonClient()
        await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id}")
        if supervisor:
            stopped = await supervisor["stopTask"](task_id)
            return {"content": pretty_json({"task_id": task_id, "status": "terminated" if stopped else "not_found", "stopped": stopped}), "isError": not stopped}
        meta = await readTaskMeta(task_id)
        if not meta:
            return {"content": f"Task not found: {task_id}", "isError": True}
        pid = meta.get("pid")
        if not isinstance(pid, int):
            return {"content": f"Task {task_id} has no PID", "isError": True}
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        meta.update({"status": "stopped", "stoppedAt": now_iso(), "updatedAt": now_iso()})
        await writeTaskMeta(task_id, meta)
        return {"content": pretty_json({"task_id": task_id, "status": "terminated", "stopped": True})}

    return ToolDefinition("TaskStop", "Stop a task.", {"type": "object"}, handler)


def _task_monitor_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        if not await readTaskMeta(task_id):
            return {"content": f"Task not found: {task_id}", "isError": True}
        monitor_id = str(uuid.uuid4())
        record = {"id": monitor_id, "sessionId": context["session"]["id"], "taskId": task_id, "createdAt": now_iso(), "updatedAt": now_iso(), "status": "active", "goal": input.get("goal") or "", "checkIntervalMs": int(input.get("check_interval_ms") or 5000), "heartbeatIntervalMs": int(input.get("heartbeat_interval_ms") or 30000)}
        await saveMonitor(record)
        context["session"].setdefault("metadata", {}).setdefault("monitorIds", []).append(monitor_id)
        await maybe_await(context.get("persistSession", lambda: None)())
        return {"content": pretty_json(record)}

    return ToolDefinition("TaskMonitorStart", "Start monitoring a task.", {"type": "object"}, handler)


def _task_monitor_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        monitor_id = _require_string(input.get("monitor_id"), "monitor_id")
        monitor = await loadMonitor(monitor_id)
        if not monitor or monitor.get("sessionId") != context["session"]["id"]:
            return {"content": f"Monitor not found: {monitor_id}", "isError": True}
        monitor.update({"status": "stopped", "updatedAt": now_iso(), "stoppedAt": now_iso()})
        await saveMonitor(monitor)
        return {"content": pretty_json({"monitor_id": monitor_id, "task_id": monitor.get("taskId"), "status": "stopped", "stopped": True})}

    return ToolDefinition("TaskMonitorStop", "Stop monitoring a task.", {"type": "object"}, handler)


def _task_health_check_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        supervisor = context.get("daemon") or context.get("taskSupervisor") or await ensureTaskSupervisorDaemon({"cwd": context["session"]["cwd"]})
        started = await supervisor["startHealthCheck"](
            {
                "sessionId": context["session"]["id"],
                "cwd": context["session"]["cwd"],
                "title": _require_string(input.get("title"), "title"),
                "mode": input.get("mode") or ("command" if input.get("command") else "http"),
                "command": input.get("command"),
                "url": input.get("url"),
                "intervalMs": input.get("interval_ms") or input.get("intervalMs"),
                "timeoutMs": input.get("timeout_ms") or input.get("timeoutMs"),
            }
        )
        task_id = started.get("taskId") or started.get("task_id")
        context["session"].setdefault("metadata", {}).setdefault("taskIds", []).append(task_id)
        await maybe_await(context.get("persistSession", lambda: None)())
        return {"content": pretty_json({"task_id": task_id, "status": "running", "kind": "health_check", "title": input.get("title")})}

    return ToolDefinition("TaskHealthCheckStart", "Start a periodic health check.", {"type": "object"}, handler)


def _task_health_check_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        supervisor = context.get("daemon") or context.get("taskSupervisor") or await ensureTaskSupervisorDaemon({"cwd": context["session"]["cwd"]})
        stopped = await supervisor["stopTask"](task_id)
        return {"content": pretty_json({"task_id": task_id, "stopped": stopped}), "isError": not stopped}

    return ToolDefinition("TaskHealthCheckStop", "Stop a health check.", {"type": "object"}, handler)


def _mcp_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if not runtime:
            return {
                "content": pretty_json(
                    {
                        "servers": [
                            {"name": server.get("name"), "transport": server.get("transport"), "connected": False, "state": "disconnected"}
                            for server in context["config"].get("mcpConfig", [])
                        ],
                        "tools": [],
                    }
                )
            }
        if input.get("server") and input.get("name"):
            result = await runtime["callTool"](input["server"], input["name"], input.get("arguments") or input.get("input") or {})
            return result
        diagnostics = runtime["getDiagnostics"]() if callable(runtime.get("getDiagnostics")) else runtime.get("diagnostics", {})
        return {"content": pretty_json({"diagnostics": diagnostics, "servers": runtime["listServerStatus"](), "tools": runtime["listTools"]()})}

    return ToolDefinition("Mcp", "Call an MCP tool.", {"type": "object"}, handler)


def _list_mcp_resources_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        server = input.get("server") if isinstance(input.get("server"), str) else None
        if runtime:
            return {"content": pretty_json(await runtime["listResources"](server))}
        configs = context["config"].get("mcpConfig", [])
        return {"content": pretty_json([{"server": item.get("name"), "name": item.get("name"), "uri": item.get("commandOrUrl"), "description": f"{item.get('transport')} server (not connected)"} for item in configs if not server or item.get("name") == server])}

    return ToolDefinition("ListMcpResources", "List MCP resources.", {"type": "object"}, handler)


def _read_mcp_resource_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        server = _require_string(input.get("server"), "server")
        uri = _require_string(input.get("uri"), "uri")
        if runtime:
            return {"content": pretty_json(await runtime["readResource"](server, uri))}
        config = next((entry for entry in context["config"].get("mcpConfig", []) if entry.get("name") == server), None)
        if not config:
            return {"content": f"MCP server not found: {server}", "isError": True}
        if config.get("transport") in ("http", "sse") and re.match(r"^https?://", uri):
            response = await fetch(uri, headers=config.get("headers"))
            text = await response.text()
            shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="ReadMcpResource", family="web", text=text, strategy="head")
            return {"content": pretty_json({"contents": [{"uri": uri, "text": shaped["text"], "manifest": shaped["view"]}]})}
        return {"content": f"MCP runtime is not connected for server: {server}", "isError": True}

    return ToolDefinition("ReadMcpResource", "Read an MCP resource.", {"type": "object"}, handler)


def _enter_worktree_tool(controller: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        requested_name = input.get("name") if isinstance(input.get("name"), str) and input.get("name").strip() else f"tess-{int(__import__('time').time())}"
        await _ensure_tool_permission(context, "EnterWorktree", "worktree", f"Create git worktree for {requested_name}")
        if controller and callable(getattr(controller, "createWorktree", None)):
            result = await maybe_await(controller.createWorktree(requested_name))
            worktree = result.get("worktree") if isinstance(result, dict) else None
        else:
            worktree = await _create_git_worktree(context["session"]["cwd"], requested_name)
            result = {"worktree": worktree, "snapshot": None}
        if not worktree:
            return {"content": "Unable to create git worktree.", "isError": True}
        context["session"].setdefault("metadata", {})["worktreePath"] = worktree["targetPath"]
        await maybe_await(context.get("updateCwd", lambda next_cwd: context["session"].__setitem__("cwd", next_cwd))(worktree["targetPath"]))
        return {"content": pretty_json({"cwd": worktree["targetPath"], "branch": worktree["branchName"], "created": True, "workspace": result.get("snapshot")})}

    return ToolDefinition("EnterWorktree", "Enter a git worktree.", {"type": "object"}, handler)


def _exit_worktree_tool(controller: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        original = context["session"].get("metadata", {}).get("originalCwd") or context["config"].get("cwd") or context["session"]["cwd"]
        snapshot = await maybe_await(controller.exitWorktree()) if controller and callable(getattr(controller, "exitWorktree", None)) else None
        await maybe_await(context.get("updateCwd", lambda next_cwd: context["session"].__setitem__("cwd", next_cwd))(original))
        context["session"].setdefault("metadata", {}).pop("worktreePath", None)
        return {"content": pretty_json({"cwd": original, "workspace": snapshot})}

    return ToolDefinition("ExitWorktree", "Exit a git worktree.", {"type": "object"}, handler)


def parseAllowedPrompts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and item.get("tool") == "Bash" and isinstance(item.get("prompt"), str):
            out.append({"tool": "Bash", "prompt": item["prompt"]})
    return out


def _file_change_payload(tool: str, file_path: str, previous: str | None, next_text: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "file_change",
        "tool": tool,
        "action": "create" if previous is None else "update",
        "filePath": file_path,
        "bytes": len(next_text.encode("utf-8")),
        "note": (options or {}).get("note"),
        "diff": "\n".join(difflib.unified_diff((previous or "").splitlines(), next_text.splitlines(), fromfile=file_path, tofile=file_path, lineterm="")) or "(no changes)",
    }


async def searchWeb(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    duck = await searchDuckDuckGo(query, abortSignal)
    return duck if duck else await searchBing(query, abortSignal)


async def searchDuckDuckGo(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
    response = await fetch(url, headers=_web_search_headers(), signal=abortSignal)
    if not response.ok:
        return []
    raw = await response.text()
    if "anomaly-modal__mask" in raw or "anomaly-modal__modal" in raw:
        return []
    return parseDuckDuckGoResults(raw)


async def searchBing(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    url = f"https://www.bing.com/search?{urlencode({'q': query})}"
    response = await fetch(url, headers=_web_search_headers(), signal=abortSignal)
    if not response.ok:
        return []
    return parseBingResults(await response.text())


def parseDuckDuckGoResults(raw: str) -> list[dict[str, str]]:
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>')
    out = []
    for match in pattern.finditer(raw):
        url = normalizeDuckDuckGoResultUrl(match.group(1))
        if url:
            out.append({"url": url, "title": _strip_html(html.unescape(match.group(2))), "snippet": _strip_html(html.unescape(match.group(3)))})
    return out


def normalizeDuckDuckGoResultUrl(raw: str) -> str | None:
    decoded = html.unescape(raw).strip()
    if not decoded:
        return None
    absolute = f"https:{decoded}" if decoded.startswith("//") else f"https://duckduckgo.com{decoded}" if decoded.startswith("/") else decoded
    try:
        parsed = urlparse(absolute)
        if parsed.hostname == "duckduckgo.com" and parsed.path == "/l/":
            redirected = parse_qs(parsed.query).get("uddg", [None])[0]
            if redirected:
                return redirected
        return urlunparse(parsed)
    except Exception:
        return None


def parseBingResults(raw: str) -> list[dict[str, str]]:
    sections = re.split(r'<li class="b_algo"[^>]*>', raw, flags=re.I)[1:]
    out = []
    for section in sections:
        title_match = re.search(r'<h2[^>]*>[\s\S]*?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?</h2>', section, re.I)
        if not title_match:
            continue
        url = normalizeBingResultUrl(title_match.group(1))
        if not url:
            continue
        snippet_match = re.search(r"<p[^>]*>([\s\S]*?)</p>", section, re.I)
        out.append({"url": url, "title": _strip_html(html.unescape(title_match.group(2))), "snippet": _strip_html(html.unescape(snippet_match.group(1) if snippet_match else ""))})
    return out


def normalizeBingResultUrl(raw: str) -> str | None:
    decoded = html.unescape(raw).strip()
    if not decoded:
        return None
    try:
        parsed = urlparse(decoded)
        if parsed.hostname == "www.bing.com" and parsed.path == "/ck/a":
            encoded = parse_qs(parsed.query).get("u", [None])[0]
            if encoded:
                target = decodeBingRedirectTarget(encoded)
                if target:
                    return target
        return urlunparse(parsed)
    except Exception:
        return None


def decodeBingRedirectTarget(value: str) -> str | None:
    normalized = value.strip()
    if normalized.startswith("a1"):
        encoded = normalized[2:]
        try:
            padding = "=" * ((4 - len(encoded) % 4) % 4)
            return base64.b64decode(f"{encoded}{padding}").decode("utf-8")
        except Exception:
            return None
    return None


def _strip_html(raw: str) -> str:
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _safe_hostname(value: str) -> str | None:
    try:
        normalized = f"https:{value}" if value.startswith("//") else value
        return urlparse(normalized).hostname
    except Exception:
        return None


def _web_search_headers() -> dict[str, str]:
    return {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "accept-language": "en-US,en;q=0.9"}


def _authorize_path(raw_path: str, context: dict[str, Any], create_allowed: bool = False) -> str:
    return os.path.abspath(resolveMaybeRelative(context["session"]["cwd"], raw_path))


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) == 0:
        raise ValueError(f"Expected {field} to be a non-empty string")
    return value


def _display_path(context: dict[str, Any], file_path: str) -> str:
    try:
        return os.path.relpath(file_path, context["session"]["cwd"])
    except Exception:
        return file_path


async def _ensure_tool_permission(context: dict[str, Any], tool_name: str, category: str, summary: str, always_ask: bool = False, command: str | None = None) -> None:
    mode = await _get_effective_permission_mode(context)
    resolution = await _resolve_tool_permission(context, tool_name, category, mode, always_ask, command)
    output_json = context.get("outputJson", lambda *_: None)
    if resolution["decision"] == "allow":
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "allow", "summary": summary, "source": resolution.get("source")})
        return

    if resolution["decision"] == "deny":
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "deny", "summary": summary, "source": resolution.get("source"), "reason": resolution.get("reason")})
        raise PermissionError(resolution.get("reason") or f"Permission denied for {tool_name}.")

    if not context.get("interactive"):
        switch = resolution.get("switchMode") or "bypassPermissions"
        raise PermissionError(f"{tool_name} requires approval in {mode} mode. Re-run with --permission-mode {switch} or switch modes interactively with /permissions.")

    approve_label = "Approve once"
    switch_label = f"Switch to {resolution['switchMode']}" if resolution.get("switchMode") else None
    deny_label = "Deny"
    options = [label for label in (approve_label, switch_label, deny_label) if label]
    prompt = "\n".join(part for part in (f"{tool_name} wants permission", "", summary, "", f"mode: {mode}", f"policy: {resolution.get('reason')}" if resolution.get("reason") else "") if part is not None)
    output_json("permission.requested", {"tool": tool_name, "mode": mode, "summary": summary, "options": options})
    answer = await maybe_await(context.get("askQuestion", lambda *_: deny_label)(prompt, options))
    if answer == approve_label or str(answer).lower().startswith("y"):
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "allow", "summary": summary, "source": "prompt"})
        return
    if switch_label and answer == switch_label:
        context["config"]["permissionMode"] = resolution["switchMode"]
        context["session"]["permissionMode"] = resolution["switchMode"]
        await maybe_await(context.get("persistSession", lambda: None)())
        output_json("permission.decided", {"tool": tool_name, "mode": resolution["switchMode"], "decision": "allow", "summary": summary, "source": "prompt-switch"})
        return
    output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "deny", "summary": summary, "source": "prompt"})
    raise PermissionError(f"Permission denied for {tool_name}. Use /permissions to change modes.")


async def _get_effective_permission_mode(context: dict[str, Any]) -> str:
    mode = context["session"].get("permissionMode") or context["config"].get("permissionMode", "default")
    parent_session_id = context["session"].get("metadata", {}).get("parentSessionId") if context.get("interactive") else None
    if not parent_session_id:
        return mode
    parent = await loadSession(parent_session_id)
    inherited = parent.get("permissionMode") if parent else None
    if not inherited or inherited == mode:
        return mode
    context["session"]["permissionMode"] = inherited
    context["config"]["permissionMode"] = inherited
    await maybe_await(context.get("persistSession", lambda: None)())
    return inherited


async def _resolve_tool_permission(context: dict[str, Any], tool_name: str, category: str, mode: str, always_ask: bool, command: str | None) -> dict[str, str]:
    if mode == "bypassPermissions":
        return {"decision": "allow", "source": "mode"}
    if mode == "plan":
        return {"decision": "deny", "source": "mode", "reason": f"{tool_name} is blocked in plan mode. Switch to /permissions default or /permissions acceptEdits to execute changes."}
    if category == "edit":
        if mode in ("acceptEdits", "auto", "dontAsk"):
            return {"decision": "allow", "source": "mode"}
        return {"decision": "ask", "source": "mode", "switchMode": "acceptEdits", "reason": "This tool changes files on disk."}
    if category == "worktree":
        if mode in ("auto", "dontAsk"):
            return {"decision": "deny", "source": "mode", "reason": "Worktree creation is blocked in non-interactive permission modes."}
        return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This tool creates or changes repository state."}
    if category == "process":
        if mode in ("auto", "dontAsk"):
            return {"decision": "deny", "source": "mode", "reason": "Stopping processes is blocked in non-interactive permission modes."}
        return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This tool sends signals to a running process."}

    command = command or ""
    if not always_ask and _is_safe_read_only_shell_command(command):
        return {"decision": "allow", "source": "heuristic"}
    if mode in ("auto", "dontAsk"):
        auto_mode = await loadAutoMode(context["session"]["cwd"])
        if _matches_command_policy(command, auto_mode.get("deny") or []):
            return {"decision": "deny", "source": "auto-mode", "reason": "Command matched the deny policy for auto mode."}
        if not always_ask and _matches_command_policy(command, auto_mode.get("allow") or []):
            return {"decision": "allow", "source": "auto-mode"}
        return {"decision": "deny", "source": "auto-mode", "reason": "Command is not in the allow policy for auto mode."}
    return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This shell command can modify the environment or repository."}


def _is_safe_read_only_shell_command(command: str) -> bool:
    normalized = _normalize_shell_command(command)
    if not normalized:
        return False
    safe_prefixes = [
        "git status",
        "git diff",
        "git log",
        "git show",
        "git branch",
        "ls",
        "pwd",
        "find",
        "rg",
        "cat",
        "head",
        "tail",
        "sed -n",
        "wc",
        "which",
        "date",
        "npm test",
        "pnpm test",
        "bun test",
        "pytest",
        "cargo test",
        "go test",
    ]
    risky_fragments = [" rm ", " rm-", "rm -", "git reset", "git clean", "chmod ", "chown ", "sudo ", "curl ", "wget ", ">|", " >", ">>", "tee "]
    if any(fragment in normalized for fragment in risky_fragments):
        return False
    return any(normalized == prefix or normalized.startswith(f"{prefix} ") for prefix in safe_prefixes)


def _matches_command_policy(command: str, patterns: list[str]) -> bool:
    normalized = _normalize_shell_command(command)
    for pattern in patterns:
        normalized_pattern = _normalize_shell_command(pattern)
        if normalized == normalized_pattern or normalized.startswith(f"{normalized_pattern} "):
            return True
    return False


def _normalize_shell_command(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip().lower()


def _normalize_bash_timeout(command: str, value: Any) -> int:
    raw = 120000 if value is None else int(value)
    timeout = raw * 1000 if 0 < raw <= 600 else raw
    if re.search(r"\b(?:npm|pnpm|yarn|bun)\s+install\b", command) and timeout < 300000:
        timeout = 300000
    return min(timeout, 600000)


async def _create_git_worktree(cwd: str, requested_name: str) -> dict[str, str] | None:
    def run_git(args: list[str]) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    try:
        root = run_git(["rev-parse", "--show-toplevel"])
    except Exception:
        return None
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", requested_name).strip("-") or f"worktree-{uuid.uuid4().hex[:8]}"
    target = os.path.abspath(os.path.join(root, "..", safe_name))
    branch = safe_name
    suffix = 1
    while os.path.exists(target):
        suffix += 1
        target = os.path.abspath(os.path.join(root, "..", f"{safe_name}-{suffix}"))
        branch = f"{safe_name}-{suffix}"
    await asyncio.to_thread(run_git, ["worktree", "add", "-b", branch, target])
    return {"targetPath": target, "branchName": branch}
