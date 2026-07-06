from __future__ import annotations

import copy
from typing import Any


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
    "Glob": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}}, "required": ["pattern"]},
    "Grep": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The regular expression to search for."},
            "path": {"type": "string", "description": "File or directory to search. Defaults to the current working directory."},
            "glob": {"type": "string", "description": "Optional glob filter, such as '*.py' or 'src/**/*.ts'."},
            "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"], "description": "Use files_with_matches for filenames, content for matching lines with line numbers, or count for match counts per file."},
            "-A": {"type": "number"},
            "-B": {"type": "number"},
            "-C": {"type": "number"},
            "context": {"type": "number", "description": "Number of context lines to include before and after each match."},
            "before_context": {"type": "number", "description": "Number of lines to include before each match."},
            "after_context": {"type": "number", "description": "Number of lines to include after each match."},
            "-n": {"type": "boolean"},
            "-i": {"type": "boolean"},
            "ignore_case": {"type": "boolean", "description": "Whether the search should be case-insensitive."},
            "type": {"type": "string", "description": "Optional ripgrep file type, such as 'py', 'ts', 'js', or 'md'."},
            "head_limit": {"type": "number", "description": "Maximum number of returned matches. Use 0 only when full output is required."},
            "offset": {"type": "number", "description": "Match offset for paginating large result sets."},
            "multiline": {"type": "boolean", "description": "Enable multiline regular-expression search."},
        },
        "required": ["pattern"],
    },
    "Bash": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}, "description": {"type": "string"}, "run_in_background": {"type": "boolean"}, "dangerouslyDisableSandbox": {"type": "boolean"}}, "required": ["command"]},
    "TodoWrite": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"], "additionalProperties": False}}}, "required": ["todos"]},
    "WebFetch": {"type": "object", "properties": {"url": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["url", "prompt"]},
    "WebSearch": {"type": "object", "properties": {"query": {"type": "string"}, "allowed_domains": {"type": "array", "items": {"type": "string"}}, "blocked_domains": {"type": "array", "items": {"type": "string"}}}, "required": ["query"]},
    "SetStatus": {"type": "object", "properties": {"message": {"type": "string"}, "detail": {"type": "string"}}, "required": ["message"], "additionalProperties": False},
    "Agent": {"type": "object", "properties": {"description": {"type": "string"}, "prompt": {"type": "string"}, "subagent_type": {"type": "string"}, "model": {"type": "string"}, "run_in_background": {"type": "boolean"}, "name": {"type": "string"}, "team_name": {"type": "string"}, "mode": {"type": "string", "enum": ["default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"]}, "isolation": {"type": "string", "enum": ["shared", "worktree", "none"]}}, "required": ["description", "prompt"]},
    "AskUserQuestion": {"type": "object", "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 4}}, "required": ["questions"]},
    "Config": {"type": "object"},
    "ExitPlanMode": {"type": "object", "properties": {"allowedPrompts": {"type": "array"}}},
    "NotebookEdit": {"type": "object", "properties": {"notebook_path": {"type": "string"}, "cell_id": {"type": "string"}, "new_source": {"type": "string"}, "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"]}, "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"]}}, "required": ["notebook_path", "new_source"]},
    "TaskOutput": {"type": "object", "properties": {"task_id": {"type": "string"}, "block": {"type": "boolean"}, "timeout": {"type": "number"}}, "required": ["task_id", "block", "timeout"]},
    "TaskStop": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    "TaskMonitorStart": {"type": "object", "properties": {"task_id": {"type": "string"}, "goal": {"type": "string"}, "summary_style": {"type": "string"}, "check_interval_ms": {"type": "number"}, "heartbeat_interval_ms": {"type": "number"}}, "required": ["task_id", "goal"]},
    "TaskMonitorStop": {"type": "object", "properties": {"monitor_id": {"type": "string"}}, "required": ["monitor_id"]},
    "TaskHealthCheckStart": {"type": "object", "properties": {"title": {"type": "string"}, "mode": {"type": "string", "enum": ["http", "command"]}, "url": {"type": "string"}, "command": {"type": "string"}, "interval_ms": {"type": "number"}, "timeout_ms": {"type": "number"}, "failure_threshold": {"type": "number"}, "backoff_multiplier": {"type": "number"}, "max_interval_ms": {"type": "number"}, "expected_status_codes": {"type": "array", "items": {"type": "number"}}, "expected_body_substring": {"type": "string"}}, "required": ["title", "mode"]},
    "TaskHealthCheckStop": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    "Mcp": {"type": "object"},
    "ListMcpResources": {"type": "object", "properties": {"server": {"type": "string"}}},
    "ReadMcpResource": {"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}}, "required": ["server", "uri"]},
    "EnterWorktree": {"type": "object", "properties": {"name": {"type": "string"}}},
    "ExitWorktree": {"type": "object"},
}

for _schema in BUILTIN_TOOL_INPUT_SCHEMAS.values():
    if _schema.get("type") == "object":
        _schema.setdefault("additionalProperties", False)


def _tool_description(name: str) -> str:
    return BUILTIN_TOOL_DESCRIPTIONS[name]


def _tool_schema(name: str) -> dict[str, Any]:
    return copy.deepcopy(BUILTIN_TOOL_INPUT_SCHEMAS[name])
