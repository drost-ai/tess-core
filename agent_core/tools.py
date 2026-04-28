from __future__ import annotations

import asyncio
import base64
import copy
import html
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from ._utils import maybe_await, now_iso, pretty_json
from .execution_descriptors import createTaskExecutionDescriptor, createWorkspaceExecutionDescriptor, summarizeOutputManifest
from .monitors import createMonitorRecord, defaultMonitorCheckIntervalMs, defaultMonitorHeartbeatIntervalMs
from .storage import (
    listSessionMonitors,
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
from .tool_payloads import (
    applyDefaultGlobExcludes,
    filterDefaultIgnoredGlobMatches,
    shapeGlobPayload,
    shapeGrepPayload,
    shapeReadPayload,
    shapeTextToolResult,
    splitToolTextLines,
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


BUILTIN_TOOL_ORDER = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "SetStatus",
    "Agent",
    "AskUserQuestion",
    "Config",
    "ExitPlanMode",
    "NotebookEdit",
    "TaskOutput",
    "TaskStop",
    "TaskMonitorStart",
    "TaskMonitorStop",
    "TaskHealthCheckStart",
    "TaskHealthCheckStop",
    "Mcp",
    "ListMcpResources",
    "ReadMcpResource",
    "EnterWorktree",
    "ExitWorktree",
]


_BACKGROUND_RUNNERS: list[subprocess.Popen[Any]] = []

PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}


BUILTIN_TOOL_DESCRIPTIONS: dict[str, str] = {
    "Read": "Read files from disk, including partial line ranges for large text files.",
    "Write": "Write full file contents to disk, creating parent directories when needed.",
    "Edit": "Apply exact string replacements inside a file.",
    "Glob": "List files that match a glob pattern.",
    "Grep": "Search file contents with ripgrep.",
    "Bash": "Run shell commands in the current workspace. When you launch a long-running background task and promise to keep watching it, call TaskMonitorStart immediately afterward.",
    "TodoWrite": "Update the session todo list.",
    "WebFetch": "Fetch a URL and return a text snapshot of its contents.",
    "WebSearch": "Perform a lightweight web search and return ranked results.",
    "SetStatus": "Update the assistant's live status line with a short progress message. Use this when changing phases or starting meaningful work. Keep messages brief and concrete, such as 'Reading plan...', 'Searching code...', 'Updating plan...', or 'Running tests...'.",
    "Agent": "Spawn a delegated subagent to work on a focused task.",
    "AskUserQuestion": "Ask the interactive user one or more multiple-choice questions.",
    "Config": "Return the active runtime configuration snapshot.",
    "ExitPlanMode": "Request approval to exit plan mode and continue with implementation.",
    "NotebookEdit": "Edit Jupyter notebook cells by ID.",
    "TaskOutput": "Read output from a background task. This is a pull-based inspection tool, not proactive monitoring.",
    "TaskStop": "Stop a running background task.",
    "TaskMonitorStart": "Start proactive monitoring for a background task so the assistant can keep watching it and update the user without another prompt.",
    "TaskMonitorStop": "Stop proactive monitoring for a background task without stopping the underlying task itself.",
    "TaskHealthCheckStart": "Start a daemon-backed periodic health check for a URL or shell command.",
    "TaskHealthCheckStop": "Stop a daemon-backed health check task.",
    "Mcp": "Inspect the connected MCP runtime and discovered capabilities.",
    "ListMcpResources": "List resources published by connected MCP servers.",
    "ReadMcpResource": "Read a resource published by a connected MCP server.",
    "EnterWorktree": "Create and enter a git worktree for isolated work.",
    "ExitWorktree": "Return to the original workspace and optionally leave the worktree intact.",
}


BUILTIN_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "Read": {"type": "object", "properties": {"file_path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}, "pages": {"type": "string"}}, "required": ["file_path"]},
    "Write": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]},
    "Edit": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["file_path", "old_string", "new_string"]},
    "Glob": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]},
    "Grep": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "output_mode": {"type": "string"}, "-A": {"type": "number"}, "-B": {"type": "number"}, "-C": {"type": "number"}, "context": {"type": "number"}, "-n": {"type": "boolean"}, "-i": {"type": "boolean"}, "type": {"type": "string"}, "head_limit": {"type": "number"}, "offset": {"type": "number"}, "multiline": {"type": "boolean"}}, "required": ["pattern"]},
    "Bash": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}, "description": {"type": "string"}, "run_in_background": {"type": "boolean"}, "dangerouslyDisableSandbox": {"type": "boolean"}}, "required": ["command"]},
    "TodoWrite": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string"}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["todos"]},
    "WebFetch": {"type": "object", "properties": {"url": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["url", "prompt"]},
    "WebSearch": {"type": "object", "properties": {"query": {"type": "string"}, "allowed_domains": {"type": "array", "items": {"type": "string"}}, "blocked_domains": {"type": "array", "items": {"type": "string"}}}, "required": ["query"]},
    "SetStatus": {"type": "object", "properties": {"message": {"type": "string"}, "detail": {"type": "string"}}, "required": ["message"], "additionalProperties": False},
    "Agent": {"type": "object", "properties": {"description": {"type": "string"}, "prompt": {"type": "string"}, "subagent_type": {"type": "string"}, "model": {"type": "string"}, "run_in_background": {"type": "boolean"}, "name": {"type": "string"}, "team_name": {"type": "string"}, "mode": {"type": "string", "enum": ["default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"]}, "isolation": {"type": "string"}}, "required": ["description", "prompt"]},
    "AskUserQuestion": {"type": "object", "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 4}}, "required": ["questions"]},
    "Config": {"type": "object", "additionalProperties": True},
    "ExitPlanMode": {"type": "object", "properties": {"allowedPrompts": {"type": "array"}}, "additionalProperties": True},
    "NotebookEdit": {"type": "object", "properties": {"notebook_path": {"type": "string"}, "cell_id": {"type": "string"}, "new_source": {"type": "string"}, "cell_type": {"type": "string"}, "edit_mode": {"type": "string"}}, "required": ["notebook_path", "new_source"]},
    "TaskOutput": {"type": "object", "properties": {"task_id": {"type": "string"}, "block": {"type": "boolean"}, "timeout": {"type": "number"}}, "required": ["task_id", "block", "timeout"]},
    "TaskStop": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    "TaskMonitorStart": {"type": "object", "properties": {"task_id": {"type": "string"}, "goal": {"type": "string"}, "summary_style": {"type": "string"}, "check_interval_ms": {"type": "number"}, "heartbeat_interval_ms": {"type": "number"}}, "required": ["task_id", "goal"]},
    "TaskMonitorStop": {"type": "object", "properties": {"monitor_id": {"type": "string"}}, "required": ["monitor_id"]},
    "TaskHealthCheckStart": {"type": "object", "properties": {"title": {"type": "string"}, "mode": {"type": "string", "enum": ["http", "command"]}, "url": {"type": "string"}, "command": {"type": "string"}, "interval_ms": {"type": "number"}, "timeout_ms": {"type": "number"}, "failure_threshold": {"type": "number"}, "backoff_multiplier": {"type": "number"}, "max_interval_ms": {"type": "number"}, "expected_status_codes": {"type": "array", "items": {"type": "number"}}, "expected_body_substring": {"type": "string"}}, "required": ["title", "mode"]},
    "TaskHealthCheckStop": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    "Mcp": {"type": "object", "additionalProperties": True},
    "ListMcpResources": {"type": "object", "properties": {"server": {"type": "string"}}},
    "ReadMcpResource": {"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}}, "required": ["server", "uri"]},
    "EnterWorktree": {"type": "object", "properties": {"name": {"type": "string"}}},
    "ExitWorktree": {"type": "object", "additionalProperties": True},
}


def _tool_description(name: str) -> str:
    return BUILTIN_TOOL_DESCRIPTIONS[name]


def _tool_schema(name: str) -> dict[str, Any]:
    return copy.deepcopy(BUILTIN_TOOL_INPUT_SCHEMAS[name])


def createBuiltinToolContractSnapshot(options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema}
        for tool in createBuiltinTools(options)
    ]


create_builtin_tool_contract_snapshot = createBuiltinToolContractSnapshot


class FetchResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    async def text(self) -> str:
        return self.body


async def fetch(url: str, *, headers: dict[str, str] | None = None, signal: Any = None) -> FetchResponse:
    if _signal_aborted(signal):
        raise _create_tool_abort_error()
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        if _signal_aborted(signal):
            raise _create_tool_abort_error()
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
            lines=splitToolTextLines(text),
            offset=int(input.get("offset") or 1),
            requestedLimit=input.get("limit"),
        )
        return {"content": pretty_json(payload)}

    return ToolDefinition("Read", _tool_description("Read"), _tool_schema("Read"), handler)


def _write_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        file_path = _authorize_path(_require_string(input.get("file_path"), "file_path"), context, True)
        content = _require_string(input.get("content"), "content")
        await _ensure_tool_permission(context, "Write", "edit", f"Write {_display_path(context, file_path)} ({len(content)} bytes)")
        previous = Path(file_path).read_text(encoding="utf-8") if Path(file_path).exists() else None
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        Path(file_path).write_text(content, encoding="utf-8")
        return {"content": pretty_json(_file_change_payload("Write", file_path, previous, content))}

    return ToolDefinition("Write", _tool_description("Write"), _tool_schema("Write"), handler)


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

    return ToolDefinition("Edit", _tool_description("Edit"), _tool_schema("Edit"), handler)


def _glob_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        pattern = _require_string(input.get("pattern"), "pattern")
        cwd = context.get("cwd") or context["session"]["cwd"]
        search_path = _authorize_path(_require_string(input.get("path"), "path"), context) if input.get("path") else cwd
        rg_path = await _resolve_ripgrep_binary()
        if not rg_path:
            return {"content": "ripgrep is required for Glob and was not found.", "isError": True}
        args = applyDefaultGlobExcludes(["--files", "-g", pattern])
        args.append(search_path)
        stdout, _stderr = await _exec_ripgrep_allowing_no_matches(rg_path, args, cwd=cwd)
        matches = filterDefaultIgnoredGlobMatches([line for line in stdout.strip().splitlines() if line])
        payload = await shapeGlobPayload(config=context["config"], sessionId=context["session"]["id"], pattern=pattern, searchPath=search_path, matches=matches, ignoredDefaultsApplied=True)
        return {"content": pretty_json(payload)}

    return ToolDefinition("Glob", _tool_description("Glob"), _tool_schema("Glob"), handler)


def _grep_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        pattern = _require_string(input.get("pattern"), "pattern")
        cwd = context.get("cwd") or context["session"]["cwd"]
        search_path = _authorize_path(_require_string(input.get("path"), "path"), context) if input.get("path") else cwd
        rg_path = await _resolve_ripgrep_binary()
        if not rg_path:
            return {"content": "ripgrep is required for Grep and was not found.", "isError": True}
        output_mode = input.get("output_mode") if isinstance(input.get("output_mode"), str) else "files_with_matches"
        args = ["--color", "never"]
        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        else:
            args.append("-n")
        applyDefaultGlobExcludes(args)
        if input.get("glob"):
            args.extend(["--glob", _require_string(input.get("glob"), "glob")])
        if input.get("type"):
            args.extend(["--type", _require_string(input.get("type"), "type")])
        if input.get("-i"):
            args.append("-i")
        context_lines = _require_optional_number(input.get("context"))
        if context_lines is None:
            context_lines = _require_optional_number(input.get("-C"))
        if context_lines is not None:
            args.extend(["-C", str(context_lines)])
        else:
            before = _require_optional_number(input.get("-B"))
            after = _require_optional_number(input.get("-A"))
            if after is not None:
                args.extend(["-A", str(after)])
            if before is not None:
                args.extend(["-B", str(before)])
        if input.get("multiline"):
            args.extend(["-U", "--multiline-dotall"])
        args.extend([pattern, search_path])
        stdout, _stderr = await _exec_ripgrep_allowing_no_matches(rg_path, args, cwd=cwd)
        matches = [line for line in stdout.strip().splitlines() if line]
        payload = await shapeGrepPayload(config=context["config"], sessionId=context["session"]["id"], pattern=pattern, searchPath=search_path, outputMode=output_mode, matches=matches, offset=int(input.get("offset") or 0), requestedHeadLimit=input.get("head_limit"))
        return {"content": pretty_json(payload)}

    return ToolDefinition("Grep", _tool_description("Grep"), _tool_schema("Grep"), handler)


def _bash_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        command = _require_string(input.get("command"), "command")
        timeout = _normalize_bash_timeout(command, input.get("timeout"))
        run_in_background = bool(input.get("run_in_background"))
        _throw_if_tool_cancelled(context)
        await _ensure_tool_permission(
            context,
            "Bash",
            "shell",
            f"{'Run shell command in background' if run_in_background else 'Run shell command'}\n{command}",
            always_ask=run_in_background,
            command=command,
        )
        if run_in_background:
            supervisor = context.get("daemon") or context.get("taskSupervisor")
            title = input.get("description") or input.get("title")
            if supervisor:
                started = await supervisor["startBackgroundCommand"](
                    {
                        "sessionId": context["session"]["id"],
                        "command": command,
                        "cwd": context["session"]["cwd"],
                        "title": title,
                    }
                )
                task_id = started.get("taskId") or started.get("task_id")
                output_path = started.get("outputFile") or started.get("output_file") or taskOutputPath(task_id)
                task_record = await readTaskMeta(task_id) or {
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "startedAt": now_iso(),
                    "status": "running",
                    "outputFile": output_path,
                    "title": title,
                    "description": title,
                }
            else:
                task_id = str(uuid.uuid4())
                output_path = taskOutputPath(task_id)
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                started_at = now_iso()
                task_record = {
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "startedAt": started_at,
                    "updatedAt": started_at,
                    "status": "running",
                    "outputFile": output_path,
                    "title": title,
                    "description": title,
                    "owner": "client",
                }
                runner = _spawn_background_task_runner(task_id, command, context["session"]["cwd"], output_path)
                _track_background_runner(runner)
                task_record["runnerPid"] = runner.pid
                await writeTaskMeta(task_id, task_record)
            task_record = {
                **{
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "startedAt": now_iso(),
                    "status": "running",
                    "outputFile": output_path,
                    "title": title,
                    "description": title,
                },
                **task_record,
                "taskId": task_id,
                "outputFile": output_path,
            }
            _append_unique_metadata_list(context["session"], "taskIds", task_id)
            await maybe_await(context.get("persistSession", lambda: None)())
            context.get("outputJson", lambda *_: None)(
                "task.registered",
                {
                    "session_id": context["session"]["id"],
                    "task_id": task_id,
                    "kind": "background_command",
                    "status": "backgrounded",
                    "title": task_record.get("title") or task_record.get("description") or command,
                    "owner": "daemon" if context.get("daemon") or context.get("taskSupervisor") else "client",
                },
            )
            execution = createTaskExecutionDescriptor(
                task_record,
                "",
                [
                    {
                        "kind": "task",
                        "status": "info",
                        "summary": "Background task launched. Call TaskMonitorStart if the assistant should keep watching it proactively.",
                        "subjectId": task_id,
                    }
                ],
            )
            return {
                "content": pretty_json(
                    {
                        "task_id": task_id,
                        "status": "running",
                        "output_file": output_path,
                        "execution": execution,
                        "next_step": "If you need to proactively keep watching this task and update the user without another prompt, call TaskMonitorStart with this task_id.",
                    }
                ),
                "meta": {"execution": execution},
            }
        proc = await asyncio.create_subprocess_exec(_resolve_shell(), "-lc", command, cwd=context["session"]["cwd"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr, timed_out = await _communicate_shell_process(proc, timeout, context)
        if _signal_aborted(context.get("abortSignal")):
            raise _create_tool_abort_error()
        exit_code = None if timed_out or (isinstance(proc.returncode, int) and proc.returncode < 0) else proc.returncode
        shaped_stdout = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="Bash", family="bash", text=stdout.decode(errors="replace"), strategy="head_tail")
        shaped_stderr = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="Bash", family="bash", text=stderr.decode(errors="replace"), strategy="head_tail")
        execution = {
            "id": f"{context['session']['id']}:bash",
            "kind": "tool",
            "title": "Shell command completed",
            "status": "failed" if timed_out or exit_code else "completed",
            "cwd": context["session"]["cwd"],
            "command": command,
            "output": summarizeOutputManifest(f"{stdout.decode(errors='replace')}{stderr.decode(errors='replace')}"),
            "metadata": {"exit_code": exit_code, "timed_out": timed_out},
        }
        return {
            "content": pretty_json({"command": command, "cwd": context["session"]["cwd"], "exitCode": exit_code, "timedOut": timed_out, "stdout": shaped_stdout["text"], "stderr": shaped_stderr["text"], "stdout_manifest": shaped_stdout["view"], "stderr_manifest": shaped_stderr["view"]}),
            "isError": bool(timed_out or exit_code),
            "meta": {"execution": execution},
        }

    return ToolDefinition("Bash", _tool_description("Bash"), _tool_schema("Bash"), handler)


def _todo_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        todos = [_normalize_todo_item(item) for item in _require_list(input.get("todos"), "todos")]
        context["session"]["todos"] = todos
        await maybe_await(context.get("persistSession", lambda: None)())
        return {"content": pretty_json({"updated": len(todos), "todos": todos})}

    return ToolDefinition("TodoWrite", _tool_description("TodoWrite"), _tool_schema("TodoWrite"), handler)


def _web_fetch_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        url = _require_string(input.get("url"), "url")
        prompt = _require_string(input.get("prompt"), "prompt")
        _throw_if_tool_cancelled(context)
        response = await fetch(url, headers={"user-agent": "tess/0.1"}, signal=context.get("abortSignal"))
        if not response.ok:
            return {"content": f"Fetch failed with status {response.status}", "isError": True}
        _throw_if_tool_cancelled(context)
        raw = await response.text()
        _throw_if_tool_cancelled(context)
        shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="WebFetch", family="web", text=_strip_html(raw), strategy="head")
        return {"content": pretty_json({"url": url, "prompt": prompt, "content": shaped["text"], "content_manifest": shaped["view"]})}

    return ToolDefinition("WebFetch", _tool_description("WebFetch"), _tool_schema("WebFetch"), handler)


def _web_search_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        query = _require_string(input.get("query"), "query")
        _throw_if_tool_cancelled(context)
        allowed = [item for item in input.get("allowed_domains", []) if isinstance(item, str)]
        blocked = [item for item in input.get("blocked_domains", []) if isinstance(item, str)]
        results = []
        for result in await searchWeb(query, context.get("abortSignal")):
            _throw_if_tool_cancelled(context)
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

    return ToolDefinition("WebSearch", _tool_description("WebSearch"), _tool_schema("WebSearch"), handler)


def _set_status_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        message = _require_string(input.get("message"), "message").strip()
        detail = input.get("detail") if isinstance(input.get("detail"), str) and input.get("detail").strip() else None
        context.get("outputJson", lambda *_: None)("status.updated", {"message": message, "detail": detail, "session_id": context["session"]["id"]})
        return {"content": pretty_json({"ok": True, "message": message, "detail": detail})}

    return ToolDefinition("SetStatus", _tool_description("SetStatus"), _tool_schema("SetStatus"), handler)


def _agent_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if int(context["session"].get("metadata", {}).get("depth") or 0) >= 2:
            return {"content": "Delegation depth limit reached.", "isError": True}
        _throw_if_tool_cancelled(context)
        description = _require_string(input.get("description"), "description")
        prompt = _require_string(input.get("prompt"), "prompt")
        agent_type = input.get("subagent_type").strip() if isinstance(input.get("subagent_type"), str) else None
        if agent_type and agent_type not in context["config"].get("agents", {}):
            return {"content": f"Unknown subagent type: {agent_type}", "isError": True}
        if input.get("isolation") == "worktree":
            await _ensure_tool_permission(context, "Agent", "worktree", f"Spawn subagent with isolated worktree\n{description}")
        _throw_if_tool_cancelled(context)
        from .subagents import runForegroundSubagent, startBackgroundSubagent

        agent_args = {
            "context": context,
            "description": description,
            "prompt": prompt,
            "agentType": agent_type,
            "model": input.get("model") if isinstance(input.get("model"), str) else None,
            "runInBackground": bool(input.get("run_in_background")),
            "name": input.get("name") if isinstance(input.get("name"), str) else None,
            "teamName": input.get("team_name") if isinstance(input.get("team_name"), str) else None,
            "mode": _parse_permission_mode_value(input.get("mode")),
            "isolation": "worktree" if input.get("isolation") == "worktree" else None,
        }
        if input.get("run_in_background"):
            return await startBackgroundSubagent(agent_args)
        return await runForegroundSubagent(agent_args)

    return ToolDefinition("Agent", _tool_description("Agent"), _tool_schema("Agent"), handler)


def _ask_user_question_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if not context.get("interactive"):
            return {"content": "Cannot ask the user a question in print mode.", "isError": True}
        answers = []
        questions = input.get("questions") if isinstance(input.get("questions"), list) else []
        for index, raw_question in enumerate(questions):
            _throw_if_tool_cancelled(context)
            if not isinstance(raw_question, dict):
                continue
            question = _require_string(raw_question.get("question"), f"questions[{index}].question")
            options = []
            if isinstance(raw_question.get("options"), list):
                for option in raw_question["options"]:
                    label = option.get("label") if isinstance(option, dict) else None
                    options.append(_require_string(label, "label"))
            answer = await maybe_await(context.get("askQuestion", lambda *_: "")(question, options))
            answers.append({"question": question, "answer": answer})
        return {"content": pretty_json({"answers": answers})}

    return ToolDefinition("AskUserQuestion", _tool_description("AskUserQuestion"), _tool_schema("AskUserQuestion"), handler)


def _config_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        config = context["config"]
        session = context["session"]
        public = {
            "cwd": session.get("cwd"),
            "providerProfile": session.get("providerProfileName") or config.get("providerProfileName"),
            "providerKind": config.get("provider"),
            "model": config.get("model"),
            "effort": session.get("effort") or config.get("effort"),
            "permissionMode": config.get("permissionMode"),
            "tools": config.get("tools"),
            "allowedRoots": config.get("allowedRoots"),
            "mcpServers": [server.get("name") for server in config.get("mcpConfig", [])],
        }
        return {"content": pretty_json(public)}

    return ToolDefinition("Config", _tool_description("Config"), _tool_schema("Config"), handler)


def _exit_plan_mode_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        session = context["session"]
        config = context["config"]
        if session.get("permissionMode") != "plan":
            return {"content": "ExitPlanMode is only available while the session is in plan mode.", "isError": True}
        allowed = parseAllowedPrompts(input.get("allowedPrompts"))
        plan = _extract_latest_assistant_text(session)
        request_id = str(uuid.uuid4())
        created_at = now_iso()
        metadata = session.setdefault("metadata", {})
        file_path = await writePlanFile(requestId=request_id, sessionId=session["id"], cwd=session["cwd"], plan=plan, allowedPrompts=allowed, status="pending", createdAt=created_at)
        record_base = {
            "requestId": request_id,
            "sessionId": session["id"],
            "agentId": metadata.get("agentId"),
            "parentSessionId": metadata.get("parentSessionId"),
            "plan": plan,
            "filePath": file_path,
            "allowedPrompts": allowed,
            "createdAt": created_at,
        }
        context.get("outputJson", lambda *_: None)("plan.requested", {"request_id": request_id, "session_id": session["id"], "agent_id": metadata.get("agentId"), "plan": plan, "file_path": file_path})

        if not context.get("interactive"):
            metadata["planMode"] = {"state": "pending", "requestId": request_id, "plan": plan, "filePath": file_path, "allowedPrompts": allowed}
            await savePlanApproval({**record_base, "status": "pending"})
            await maybe_await(context.get("persistSession", lambda: None)())
            return {
                "content": pretty_json(
                    {
                        "plan": plan,
                        "isAgent": bool(metadata.get("parentSessionId")),
                        "filePath": file_path,
                        "awaitingLeaderApproval": True,
                        "requestId": request_id,
                        "allowedPrompts": allowed,
                    }
                )
            }

        answer = await maybe_await(
            context.get("askQuestion", lambda *_: "Reject plan")(
                f"Review the plan and choose the next action.\nSaved to: {file_path}",
                ["Approve plan", "Chat with the assistant about the plan", "Reject plan"],
            )
        )
        if answer == "Approve plan":
            approved_at = now_iso()
            await writePlanFile(requestId=request_id, sessionId=session["id"], cwd=session["cwd"], plan=plan, allowedPrompts=allowed, status="approved", createdAt=created_at, resolvedAt=approved_at, filePath=file_path)
            next_mode = "plan" if metadata.get("gitRepoPlanOnly") else "acceptEdits"
            session["permissionMode"] = next_mode
            config["permissionMode"] = next_mode
            metadata["planMode"] = {"state": "approved", "requestId": request_id, "plan": plan, "filePath": file_path, "allowedPrompts": allowed, "approvedAt": approved_at}
            await savePlanApproval({**record_base, "status": "approved", "resolvedAt": approved_at})
            await maybe_await(context.get("persistSession", lambda: None)())
            context.get("outputJson", lambda *_: None)("plan.approved", {"request_id": request_id, "session_id": session["id"], "agent_id": metadata.get("agentId"), "file_path": file_path, "next_mode": next_mode})
            return {"content": pretty_json({"plan": plan, "isAgent": bool(metadata.get("parentSessionId")), "filePath": file_path, "awaitingLeaderApproval": False, "requestId": request_id, "approved": True, "nextMode": next_mode, "allowedPrompts": allowed})}

        if answer != "Reject plan":
            metadata["planMode"] = {"state": "chatting", "requestId": request_id, "plan": plan, "filePath": file_path, "allowedPrompts": allowed}
            await maybe_await(context.get("persistSession", lambda: None)())
            return {"content": pretty_json({"plan": plan, "isAgent": bool(metadata.get("parentSessionId")), "filePath": file_path, "awaitingLeaderApproval": False, "requestId": request_id, "approved": False, "chat": True})}

        rejected_at = now_iso()
        await writePlanFile(requestId=request_id, sessionId=session["id"], cwd=session["cwd"], plan=plan, allowedPrompts=allowed, status="rejected", createdAt=created_at, resolvedAt=rejected_at, reason="Plan rejected by user", filePath=file_path)
        session["permissionMode"] = "default"
        config["permissionMode"] = "default"
        metadata["planMode"] = {"state": "rejected", "requestId": request_id, "plan": plan, "filePath": file_path, "allowedPrompts": allowed, "rejectedAt": rejected_at, "reason": "Plan rejected by user"}
        await savePlanApproval({**record_base, "status": "rejected", "resolvedAt": rejected_at, "reason": "Plan rejected by user"})
        await maybe_await(context.get("persistSession", lambda: None)())
        context.get("outputJson", lambda *_: None)("plan.rejected", {"request_id": request_id, "session_id": session["id"], "agent_id": metadata.get("agentId"), "file_path": file_path})
        return {"content": pretty_json({"plan": plan, "isAgent": bool(metadata.get("parentSessionId")), "filePath": file_path, "awaitingLeaderApproval": False, "requestId": request_id, "approved": False, "reason": "Plan rejected by user"})}

    return ToolDefinition("ExitPlanMode", _tool_description("ExitPlanMode"), _tool_schema("ExitPlanMode"), handler)


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

    return ToolDefinition("NotebookEdit", _tool_description("NotebookEdit"), _tool_schema("NotebookEdit"), handler)


def _task_output_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        if input.get("block"):
            started = asyncio.get_running_loop().time()
            timeout = float(input.get("timeout") or 30000) / 1000
            while asyncio.get_running_loop().time() - started < timeout:
                _throw_if_tool_cancelled(context)
                meta = await readTaskMeta(task_id)
                if not meta or meta.get("status") not in ("running", "reconciling"):
                    break
                await asyncio.sleep(0.25)
        meta = await readTaskMeta(task_id)
        if not meta:
            return {"content": f"Task not found: {task_id}", "isError": True}
        output_result = None
        daemon = context.get("daemon")
        if daemon:
            try:
                output_result = await daemon["readTaskOutput"](task_id)
            except Exception:
                output_result = None
        text = output_result.get("output") if output_result else Path(taskOutputPath(task_id)).read_text(encoding="utf-8", errors="replace") if Path(taskOutputPath(task_id)).exists() else ""
        shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="TaskOutput", family="bash", text=text, taskId=task_id, strategy="tail")
        execution = createTaskExecutionDescriptor(meta, text)
        return {"content": pretty_json({**meta, "output": shaped["text"], "output_truncated": bool(output_result and output_result.get("truncated")), "output_manifest": shaped["view"], "execution": execution}), "meta": {"execution": execution}}

    return ToolDefinition("TaskOutput", _tool_description("TaskOutput"), _tool_schema("TaskOutput"), handler)


def _task_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        supervisor = context.get("daemon") or context.get("taskSupervisor")
        if supervisor:
            await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id}")
            stopped = await supervisor["stopTask"](task_id)
            if not stopped:
                return {"content": f"Task not found or not stoppable: {task_id}", "isError": True}
            context.get("outputJson", lambda *_: None)(
                "task.failed",
                {
                    "session_id": context["session"]["id"],
                    "task_id": task_id,
                    "summary": "Task terminated by user request.",
                },
            )
            return {"content": pretty_json({"task_id": task_id, "status": "terminated"})}
        meta = await readTaskMeta(task_id)
        if not meta:
            return {"content": f"Task not found: {task_id}", "isError": True}
        pid = meta.get("pid")
        if not isinstance(pid, int):
            return {"content": f"Task {task_id} has no PID", "isError": True}
        await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id} (pid {pid})")
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        meta.update({"status": "terminated", "stoppedAt": now_iso(), "updatedAt": now_iso()})
        await writeTaskMeta(task_id, meta)
        runner_pid = meta.get("runnerPid")
        if isinstance(runner_pid, int):
            await _wait_for_tracked_background_runner(runner_pid)
        execution = _stopped_task_execution_descriptor(task_id, meta)
        context.get("outputJson", lambda *_: None)(
            "task.failed",
            {
                "session_id": context["session"]["id"],
                "task_id": task_id,
                "kind": meta.get("type") or "background_command",
                "title": meta.get("title") or meta.get("description") or meta.get("command"),
                "summary": "Task terminated by user request.",
            },
        )
        return {"content": pretty_json({"task_id": task_id, "status": "terminated", "execution": execution}), "meta": {"execution": execution}}

    return ToolDefinition("TaskStop", _tool_description("TaskStop"), _tool_schema("TaskStop"), handler)


def _task_monitor_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        goal = _require_string(input.get("goal"), "goal")
        task = await readTaskMeta(task_id)
        if not task:
            return {"content": f"Task not found: {task_id}", "isError": True}
        existing = next((monitor for monitor in await listSessionMonitors(context["session"]["id"]) if monitor.get("taskId") == task_id and monitor.get("status") == "active"), None)
        if existing:
            return {"content": pretty_json({"monitor_id": existing.get("id"), "task_id": existing.get("taskId"), "status": existing.get("status"), "goal": existing.get("goal"), "note": "Monitor already active for this task."})}
        summary_style = input.get("summary_style") if isinstance(input.get("summary_style"), str) and input.get("summary_style").strip() else None
        record = createMonitorRecord(
            {
                "sessionId": context["session"]["id"],
                "taskId": task_id,
                "goal": goal,
                "summaryStyle": summary_style.strip() if isinstance(summary_style, str) else None,
                "checkIntervalMs": defaultMonitorCheckIntervalMs(input.get("check_interval_ms")),
                "heartbeatIntervalMs": defaultMonitorHeartbeatIntervalMs(input.get("heartbeat_interval_ms")),
            }
        )
        if summary_style:
            record["summaryStyle"] = summary_style.strip()
        await saveMonitor(record)
        _append_unique_metadata_list(context["session"], "monitorIds", record["id"])
        await maybe_await(context.get("persistSession", lambda: None)())
        context.get("outputJson", lambda *_: None)("task.registered", {"session_id": context["session"]["id"], "task_id": f"monitor:{record['id']}", "kind": "monitor", "status": "running", "title": f"Monitor {goal}"})
        context.get("outputJson", lambda *_: None)("monitor.created", {"session_id": context["session"]["id"], "monitor_id": record["id"], "task_id": task_id, "goal": goal, "status": record["status"]})
        execution = {
            "id": record["id"],
            "kind": "task",
            "title": f"Monitoring task {task_id}",
            "status": "running",
            "cwd": task.get("cwd"),
            "command": task.get("command"),
            "startedAt": record.get("createdAt"),
            "metadata": _without_none_values({"monitor_id": record["id"], "summary_style": record.get("summaryStyle")}),
        }
        return {
            "content": pretty_json(
                {
                    "monitor_id": record["id"],
                    "task_id": task_id,
                    "status": record["status"],
                    "goal": goal,
                    "check_interval_ms": record["checkIntervalMs"],
                    "heartbeat_interval_ms": record["heartbeatIntervalMs"],
                    "execution": execution,
                }
            )
        }

    return ToolDefinition("TaskMonitorStart", _tool_description("TaskMonitorStart"), _tool_schema("TaskMonitorStart"), handler)


def _task_monitor_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        monitor_id = _require_string(input.get("monitor_id"), "monitor_id")
        monitor = await loadMonitor(monitor_id)
        if not monitor or monitor.get("sessionId") != context["session"]["id"]:
            return {"content": f"Monitor not found: {monitor_id}", "isError": True}
        monitor.update({"status": "stopped", "updatedAt": now_iso(), "stoppedAt": now_iso()})
        await saveMonitor(monitor)
        context.get("outputJson", lambda *_: None)(
            "task.failed",
            {
                "session_id": context["session"]["id"],
                "task_id": f"monitor:{monitor['id']}",
                "kind": "monitor",
                "title": f"Monitor {monitor.get('goal')}",
                "summary": "Monitor stopped.",
            },
        )
        context.get("outputJson", lambda *_: None)("monitor.status_changed", {"session_id": context["session"]["id"], "monitor_id": monitor["id"], "task_id": monitor.get("taskId"), "status": monitor["status"]})
        return {"content": pretty_json({"monitor_id": monitor["id"], "task_id": monitor.get("taskId"), "status": monitor["status"]})}

    return ToolDefinition("TaskMonitorStop", _tool_description("TaskMonitorStop"), _tool_schema("TaskMonitorStop"), handler)


def _task_health_check_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        supervisor = context.get("daemon")
        if not supervisor:
            raise RuntimeError("Health checks require the local task supervisor daemon.")
        title = _require_string(input.get("title"), "title")
        mode = _require_string(input.get("mode"), "mode")
        started = await supervisor["startHealthCheck"](
            {
                "sessionId": context["session"]["id"],
                "cwd": context["session"]["cwd"],
                "title": title,
                "mode": "command" if mode == "command" else "http",
                "command": input.get("command") if isinstance(input.get("command"), str) else None,
                "url": input.get("url") if isinstance(input.get("url"), str) else None,
                "intervalMs": input.get("interval_ms") if isinstance(input.get("interval_ms"), (int, float)) else None,
                "timeoutMs": input.get("timeout_ms") if isinstance(input.get("timeout_ms"), (int, float)) else None,
                "failureThreshold": input.get("failure_threshold") if isinstance(input.get("failure_threshold"), (int, float)) else None,
                "backoffMultiplier": input.get("backoff_multiplier") if isinstance(input.get("backoff_multiplier"), (int, float)) else None,
                "maxIntervalMs": input.get("max_interval_ms") if isinstance(input.get("max_interval_ms"), (int, float)) else None,
                "expectedStatusCodes": [value for value in input.get("expected_status_codes", []) if isinstance(value, (int, float))] if isinstance(input.get("expected_status_codes"), list) else None,
                "expectedBodySubstring": input.get("expected_body_substring") if isinstance(input.get("expected_body_substring"), str) and input.get("expected_body_substring").strip() else None,
            }
        )
        task_id = started.get("taskId") or started.get("task_id")
        _append_unique_metadata_list(context["session"], "taskIds", task_id)
        await maybe_await(context.get("persistSession", lambda: None)())
        context.get("outputJson", lambda *_: None)("task.registered", {"session_id": context["session"]["id"], "task_id": task_id, "kind": "health_check", "status": "running", "title": title, "owner": "daemon"})
        return {"content": pretty_json({"task_id": task_id, "status": "running", "kind": "health_check", "title": title})}

    return ToolDefinition("TaskHealthCheckStart", _tool_description("TaskHealthCheckStart"), _tool_schema("TaskHealthCheckStart"), handler)


def _task_health_check_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        supervisor = context.get("daemon")
        if not supervisor:
            raise RuntimeError("Health checks require the local task supervisor daemon.")
        task_id = _require_string(input.get("task_id"), "task_id")
        stopped = await supervisor["stopTask"](task_id)
        if not stopped:
            raise RuntimeError(f"Task not found or not stoppable: {task_id}")
        context.get("outputJson", lambda *_: None)("task.failed", {"session_id": context["session"]["id"], "task_id": task_id, "kind": "health_check", "status": "stopped"})
        return {"content": pretty_json({"task_id": task_id, "stopped": True})}

    return ToolDefinition("TaskHealthCheckStop", _tool_description("TaskHealthCheckStop"), _tool_schema("TaskHealthCheckStop"), handler)


def _mcp_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        _throw_if_tool_cancelled(context)
        if not runtime:
            return {
                "content": pretty_json(
                    {
                        "servers": [
                            {
                                "name": server.get("name"),
                                "transport": server.get("transport"),
                                "connected": False,
                                "state": "disconnected",
                                "tools": 0,
                                "resources": 0,
                                "capabilities": {"tools": 0, "resources": 0, "channelsSupported": False},
                                "channelRegistration": {"requested": False, "status": "not_requested"},
                            }
                            for server in context["config"].get("mcpConfig", [])
                        ],
                        "tools": [],
                    }
                )
            }
        diagnostics = runtime["getDiagnostics"]() if callable(runtime.get("getDiagnostics")) else runtime.get("diagnostics", {})
        _throw_if_tool_cancelled(context)
        tools = [
            {"server": tool.get("server"), "name": tool.get("name"), "runtime_name": tool.get("runtimeName") or tool.get("runtime_name"), "description": tool.get("description")}
            for tool in runtime["listTools"]()
        ]
        _throw_if_tool_cancelled(context)
        return {"content": pretty_json({"diagnostics": diagnostics, "servers": runtime["listServerStatus"](), "tools": tools})}

    return ToolDefinition("Mcp", _tool_description("Mcp"), _tool_schema("Mcp"), handler)


def _list_mcp_resources_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        server = input.get("server") if isinstance(input.get("server"), str) else None
        if runtime:
            _throw_if_tool_cancelled(context)
            resources = await runtime["listResources"](server)
            _throw_if_tool_cancelled(context)
            return {"content": pretty_json(resources)}
        configs = context["config"].get("mcpConfig", [])
        return {"content": pretty_json([{"server": item.get("name"), "name": item.get("name"), "uri": item.get("commandOrUrl"), "description": f"{item.get('transport')} server (not connected)"} for item in configs if not server or item.get("name") == server])}

    return ToolDefinition("ListMcpResources", _tool_description("ListMcpResources"), _tool_schema("ListMcpResources"), handler)


def _read_mcp_resource_tool(runtime: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        server = _require_string(input.get("server"), "server")
        uri = _require_string(input.get("uri"), "uri")
        if runtime:
            _throw_if_tool_cancelled(context)
            resource = await runtime["readResource"](server, uri)
            _throw_if_tool_cancelled(context)
            return {"content": pretty_json(resource)}
        config = next((entry for entry in context["config"].get("mcpConfig", []) if entry.get("name") == server), None)
        if not config:
            return {"content": f"MCP server not found: {server}", "isError": True}
        if config.get("transport") in ("http", "sse"):
            _throw_if_tool_cancelled(context)
            response = await fetch(uri, headers=config.get("headers"), signal=context.get("abortSignal"))
            _throw_if_tool_cancelled(context)
            text = await response.text()
            shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="ReadMcpResource", family="web", text=text, strategy="head")
            return {"content": pretty_json({"contents": [{"uri": uri, "mimeType": _header_value(response.headers, "content-type"), "text": shaped["text"], "manifest": shaped["view"]}]})}
        return {"content": f"MCP server not found: {server}", "isError": True}

    return ToolDefinition("ReadMcpResource", _tool_description("ReadMcpResource"), _tool_schema("ReadMcpResource"), handler)


def _enter_worktree_tool(controller: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        requested_name = input.get("name").strip() if isinstance(input.get("name"), str) and input.get("name").strip() else f"tess-{int(__import__('time').time() * 1000)}"
        await _ensure_tool_permission(context, "EnterWorktree", "worktree", f"Create git worktree for {requested_name}")
        metadata = context["session"].setdefault("metadata", {})
        create_worktree = _controller_method(controller, "createWorktree")
        if callable(create_worktree):
            result = await maybe_await(create_worktree(requested_name))
            worktree = result.get("worktree") if isinstance(result, dict) else None
        else:
            worktree = await _create_git_worktree(context["session"]["cwd"], requested_name)
            result = {"worktree": worktree, "snapshot": None}
        if not worktree:
            return {"content": "Workspace controller unavailable.", "isError": True}
        target_path = worktree["targetPath"]
        branch_name = worktree["branchName"]
        metadata["worktreePath"] = target_path
        await maybe_await(context.get("updateCwd", lambda next_cwd: context["session"].__setitem__("cwd", next_cwd))(target_path))
        context["session"]["cwd"] = target_path
        context["cwd"] = target_path
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        if snapshot:
            _apply_workspace_snapshot_metadata(context["session"], snapshot)
        execution = createWorkspaceExecutionDescriptor({"action": "enter", "workspace": snapshot, "branch": branch_name, "created": True}) if snapshot else None
        result_payload = _without_none_values({"cwd": target_path, "branch": branch_name, "created": True, "workspace": snapshot, "execution": execution})
        result = {"content": pretty_json(result_payload)}
        if execution:
            result["meta"] = {"execution": execution}
        return result

    return ToolDefinition("EnterWorktree", _tool_description("EnterWorktree"), _tool_schema("EnterWorktree"), handler)


def _exit_worktree_tool(controller: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        metadata = context["session"].setdefault("metadata", {})
        original = metadata.get("originalCwd")
        await maybe_await(context.get("updateCwd", lambda next_cwd: context["session"].__setitem__("cwd", next_cwd))(original))
        context["session"]["cwd"] = original
        context["cwd"] = original
        exit_worktree = _controller_method(controller, "exitWorktree")
        snapshot = await maybe_await(exit_worktree()) if callable(exit_worktree) else None
        if snapshot:
            _apply_workspace_snapshot_metadata(context["session"], snapshot)
        execution = createWorkspaceExecutionDescriptor({"action": "exit", "workspace": snapshot}) if snapshot else None
        result_payload = _without_none_values({"cwd": original, "workspace": snapshot, "execution": execution})
        result = {"content": pretty_json(result_payload)}
        if execution:
            result["meta"] = {"execution": execution}
        return result

    return ToolDefinition("ExitWorktree", _tool_description("ExitWorktree"), _tool_schema("ExitWorktree"), handler)


def _controller_method(controller: Any, name: str) -> Any:
    if not controller:
        return None
    if isinstance(controller, dict):
        return controller.get(name)
    return getattr(controller, name, None)


def _append_unique_metadata_list(session: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    values = list(session.setdefault("metadata", {}).get(key) or [])
    if value not in values:
        values.append(value)
    session.setdefault("metadata", {})[key] = values


def _stopped_task_execution_descriptor(task_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return _without_none_values(
        {
            "id": task_id,
            "kind": "task",
            "title": f"Stopped background task {task_id}",
            "status": "terminated",
            "cwd": meta.get("cwd"),
            "command": meta.get("command"),
            "startedAt": meta.get("startedAt"),
            "finishedAt": meta.get("finishedAt") or meta.get("stoppedAt") or now_iso(),
            "metadata": _without_none_values({"pid": meta.get("pid")}),
        }
    )


def _spawn_background_task_runner(task_id: str, command: str, cwd: str, output_path: str) -> subprocess.Popen[Any]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_core.background_task_runner",
            "--task-id",
            task_id,
            "--command",
            command,
            "--cwd",
            cwd,
            "--output-path",
            output_path,
        ],
        cwd=cwd,
        env=_background_runner_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _background_runner_env() -> dict[str, str]:
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package_root if not env.get("PYTHONPATH") else f"{package_root}{os.pathsep}{env['PYTHONPATH']}"
    return env


def _track_background_runner(runner: subprocess.Popen[Any]) -> None:
    _BACKGROUND_RUNNERS[:] = [process for process in _BACKGROUND_RUNNERS if process.poll() is None]
    _BACKGROUND_RUNNERS.append(runner)


async def _wait_for_tracked_background_runner(pid: int, timeout: float = 2) -> None:
    for process in list(_BACKGROUND_RUNNERS):
        if process.pid != pid:
            continue
        if process.poll() is None:
            try:
                await asyncio.to_thread(process.wait, timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        break
    _BACKGROUND_RUNNERS[:] = [process for process in _BACKGROUND_RUNNERS if process.poll() is None]


async def _communicate_shell_process(proc: asyncio.subprocess.Process, timeout_ms: int, context: dict[str, Any]) -> tuple[bytes, bytes, bool]:
    communicate = asyncio.create_task(proc.communicate())
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    timed_out = False
    while not communicate.done():
        if _signal_aborted(context.get("abortSignal")):
            _terminate_asyncio_process(proc)
            await _finish_terminated_process(proc, communicate)
            raise _create_tool_abort_error()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            timed_out = True
            _terminate_asyncio_process(proc)
            break
        await asyncio.wait({communicate}, timeout=min(0.05, remaining))
    stdout, stderr = await _finish_terminated_process(proc, communicate)
    return stdout, stderr, timed_out


async def _finish_terminated_process(proc: asyncio.subprocess.Process, communicate: asyncio.Task[tuple[bytes, bytes]]) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(asyncio.shield(communicate), 2)
    except asyncio.TimeoutError:
        _kill_asyncio_process(proc)
        return await communicate


def _terminate_asyncio_process(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


def _kill_asyncio_process(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _resolve_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "cmd.exe" if os.name == "nt" else "/bin/sh"


def _throw_if_tool_cancelled(context: dict[str, Any]) -> None:
    if _signal_aborted(context.get("abortSignal")):
        raise _create_tool_abort_error()


def _create_tool_abort_error() -> RuntimeError:
    error = RuntimeError("Tool execution cancelled")
    error.name = "AbortError"  # type: ignore[attr-defined]
    return error


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))


def _without_none_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _apply_workspace_snapshot_metadata(session: dict[str, Any], snapshot: dict[str, Any]) -> None:
    metadata = session.setdefault("metadata", {})
    for key in ("workspaceId", "workspaceName", "workspaceRoot", "gitRoot", "gitBranch", "prNumber", "prUrl"):
        metadata[key] = snapshot.get(key)
    metadata["workspaceMode"] = snapshot.get("mode")


def parseAllowedPrompts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and item.get("tool") == "Bash" and isinstance(item.get("prompt"), str) and item["prompt"].strip():
            out.append({"tool": "Bash", "prompt": item["prompt"].strip()})
    return out


def _file_change_payload(tool: str, file_path: str, previous: str | None, next_text: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "type": "file_change",
        "tool": tool,
        "action": "create" if previous is None else "update",
        "filePath": file_path,
        "bytes": len(next_text.encode("utf-8")),
        "note": (options or {}).get("note"),
        "diff": _build_unified_diff(file_path, previous or "", next_text),
    }
    return _without_none_values(payload)


def _build_unified_diff(_file_path: str, before_text: str, after_text: str) -> str:
    before_lines = before_text.replace("\r\n", "\n").split("\n")
    after_lines = after_text.replace("\r\n", "\n").split("\n")
    operations = _build_line_diff_operations(before_lines, after_lines)
    changed_indexes = [index for index, operation in enumerate(operations) if operation["type"] != "equal"]
    if not changed_indexes:
        return "(no changes)"

    keep_indexes = _build_diff_context_indexes(operations, 3)
    max_line_num = 0
    before_cursor = 0
    after_cursor = 0
    for operation in operations:
        if operation["type"] == "equal":
            before_cursor += 1
            after_cursor += 1
        elif operation["type"] == "remove":
            before_cursor += 1
        else:
            after_cursor += 1
        max_line_num = max(max_line_num, before_cursor, after_cursor)
    num_width = max(3, len(str(max_line_num)))

    rendered = []
    before_line = 0
    after_line = 0
    previous_kept_index = -1
    for index, operation in enumerate(operations):
        if operation["type"] == "equal":
            before_line += 1
            after_line += 1
        elif operation["type"] == "remove":
            before_line += 1
        else:
            after_line += 1

        if index not in keep_indexes:
            continue
        if previous_kept_index != -1 and index > previous_kept_index + 1:
            rendered.append("...")
        line_num = before_line if operation["type"] == "remove" else after_line
        marker = "+" if operation["type"] == "add" else "-" if operation["type"] == "remove" else " "
        rendered.append(f"{str(line_num).rjust(num_width)} {marker} {operation['line']}")
        previous_kept_index = index
    return "\n".join(rendered)


def _build_line_diff_operations(before_lines: list[str], after_lines: list[str]) -> list[dict[str, str]]:
    if len(before_lines) * len(after_lines) > 1_000_000:
        return [{"type": "remove", "line": line} for line in before_lines] + [{"type": "add", "line": line} for line in after_lines]

    rows = len(before_lines)
    columns = len(after_lines)
    dp = [[0 for _ in range(columns + 1)] for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for column in range(columns - 1, -1, -1):
            if before_lines[row] == after_lines[column]:
                dp[row][column] = dp[row + 1][column + 1] + 1
            else:
                dp[row][column] = max(dp[row + 1][column], dp[row][column + 1])

    operations: list[dict[str, str]] = []
    row = 0
    column = 0
    while row < rows and column < columns:
        if before_lines[row] == after_lines[column]:
            operations.append({"type": "equal", "line": before_lines[row]})
            row += 1
            column += 1
            continue
        if dp[row + 1][column] >= dp[row][column + 1]:
            operations.append({"type": "remove", "line": before_lines[row]})
            row += 1
        else:
            operations.append({"type": "add", "line": after_lines[column]})
            column += 1
    while row < rows:
        operations.append({"type": "remove", "line": before_lines[row]})
        row += 1
    while column < columns:
        operations.append({"type": "add", "line": after_lines[column]})
        column += 1
    return operations


def _build_diff_context_indexes(operations: list[dict[str, str]], context_size: int) -> set[int]:
    indexes: set[int] = set()
    for index, operation in enumerate(operations):
        if operation["type"] == "equal":
            continue
        for cursor in range(max(0, index - context_size), min(len(operations) - 1, index + context_size) + 1):
            indexes.add(cursor)
    return indexes


async def searchWeb(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    duck = await searchDuckDuckGo(query, abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    return duck if duck else await searchBing(query, abortSignal)


async def searchDuckDuckGo(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
    response = await fetch(url, headers=_web_search_headers(), signal=abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if not response.ok:
        return []
    raw = await response.text()
    if "anomaly-modal__mask" in raw or "anomaly-modal__modal" in raw:
        return []
    return parseDuckDuckGoResults(raw)


async def searchBing(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    url = f"https://www.bing.com/search?{urlencode({'q': query})}"
    response = await fetch(url, headers=_web_search_headers(), signal=abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
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


def _require_number(value: Any, field: str, fallback: float) -> float:
    if value is None:
        return fallback
    if not isinstance(value, (int, float)):
        raise ValueError(f"Expected {field} to be a number")
    return value


def _require_optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError("Expected number")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Expected {field} to be an array")
    return value


def _normalize_todo_item(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Invalid todo item")
    content = _require_string(value.get("content"), "content")
    status = _require_string(value.get("status"), "status")
    active_form = value.get("activeForm")
    if not isinstance(active_form, str) or not active_form.strip():
        active_form = _derive_todo_active_form(content)
    return {"content": content, "status": status, "activeForm": active_form}


def _derive_todo_active_form(content: str) -> str:
    normalized = content.strip().lower()
    if not normalized:
        return "work"
    match = re.match(r"^[a-z0-9]+", normalized, re.I)
    return match.group(0) if match else "work"


def _extract_latest_assistant_text(session: dict[str, Any]) -> str | None:
    for message in reversed(session.get("messages", [])):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text
            continue
        if isinstance(content, list):
            text = "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text").strip()
            if text:
                return text
    return None


def _header_value(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    if hasattr(headers, "get"):
        return headers.get(name) or headers.get(name.lower()) or headers.get(name.title())
    return None


async def _resolve_ripgrep_binary() -> str | None:
    packaged = _packaged_ripgrep_path()
    if packaged and os.path.exists(packaged):
        return packaged
    return shutil.which("rg")


def _packaged_ripgrep_path() -> str | None:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("x86_64", "amd64") else machine
    if sys.platform == "darwin":
        key = f"{arch}-darwin"
        binary = "rg"
    elif sys.platform.startswith("linux"):
        key = f"{arch}-linux"
        binary = "rg"
    elif sys.platform == "win32":
        key = f"{arch}-win32"
        binary = "rg.exe"
    else:
        return None
    repo_root = Path(__file__).resolve().parents[1]
    return str(repo_root / "package" / "vendor" / "ripgrep" / key / binary)


async def _exec_ripgrep_allowing_no_matches(rg_path: str, args: list[str], *, cwd: str) -> tuple[str, str]:
    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([rg_path, *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    result = await asyncio.to_thread(run)
    if result.returncode in (0, 1):
        return result.stdout, result.stderr
    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ripgrep failed with exit code {result.returncode}")


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
    if answer == approve_label:
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


def _parse_permission_mode_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value in PERMISSION_MODES else None


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
    raw = 120000 if value is None else float(value)
    timeout = raw * 1000 if 0 < raw <= 600 else raw
    if re.search(r"\b(?:npm|pnpm|yarn|bun)\s+install\b", command) and timeout < 300000:
        timeout = 300000
    return int(min(timeout, 600000))


async def _create_git_worktree(cwd: str, requested_name: str) -> dict[str, str] | None:
    from .workspace import createGitWorktree

    return await createGitWorktree(cwd=cwd, requestedName=requested_name)
