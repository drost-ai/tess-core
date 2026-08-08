"""Builtin tool contract data: one spec per tool.

Adding a builtin means one entry here (description, concurrency, schema),
one factory calling ``builtin_tool``, and one line in the registry list.
"""
from __future__ import annotations

import copy
from typing import Any, Awaitable, Callable

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult
from .definitions import McpToolIdentity, ToolDefinition


BUILTIN_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "Read": {
        "description": 'Read files from disk, including partial line ranges for large text files.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'file_path': {'type': 'string'},
                        'offset': {'type': 'number'},
                        'cursor': {'type': 'string',
                                   'description': 'Opaque continuation cursor returned by a previous Read page. Do '
                                                  'not combine with offset.'},
                        'limit': {'type': 'number'},
                        'pages': {'type': 'string'}},
         'required': ['file_path'],
         'additionalProperties': False},
    },
    "Write": {
        "description": 'Write full file contents to disk, creating parent directories when needed.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'file_path': {'type': 'string'}, 'content': {'type': 'string'}},
         'required': ['file_path', 'content'],
         'additionalProperties': False},
    },
    "Edit": {
        "description": 'Apply exact string replacements inside a file.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'file_path': {'type': 'string'},
                        'old_string': {'type': 'string'},
                        'new_string': {'type': 'string'},
                        'replace_all': {'type': 'boolean'}},
         'required': ['file_path', 'old_string', 'new_string'],
         'additionalProperties': False},
    },
    "Glob": {
        "description": 'List files that match a glob pattern.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'pattern': {'type': 'string'},
                        'path': {'type': 'string'},
                        'offset': {'type': 'number'},
                        'cursor': {'type': 'string',
                                   'description': 'Opaque continuation cursor returned by a previous Glob page. Do '
                                                  'not combine with offset.'},
                        'limit': {'type': 'number'}},
         'required': ['pattern'],
         'additionalProperties': False},
    },
    "Grep": {
        "description": 'Search file contents with ripgrep.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'pattern': {'type': 'string', 'description': 'The regular expression to search for.'},
                        'path': {'type': 'string',
                                 'description': 'File or directory to search. Defaults to the current working '
                                                'directory.'},
                        'glob': {'type': 'string',
                                 'description': "Optional glob filter, such as '*.py' or 'src/**/*.ts'."},
                        'output_mode': {'type': 'string',
                                        'enum': ['files_with_matches', 'content', 'count'],
                                        'description': 'Use files_with_matches for filenames, content for matching '
                                                       'lines with line numbers, or count for match counts per '
                                                       'file.'},
                        '-A': {'type': 'number'},
                        '-B': {'type': 'number'},
                        '-C': {'type': 'number'},
                        'context': {'type': 'number',
                                    'description': 'Number of context lines to include before and after each match.'},
                        'before_context': {'type': 'number',
                                           'description': 'Number of lines to include before each match.'},
                        'after_context': {'type': 'number',
                                          'description': 'Number of lines to include after each match.'},
                        '-n': {'type': 'boolean'},
                        '-i': {'type': 'boolean'},
                        'ignore_case': {'type': 'boolean',
                                        'description': 'Whether the search should be case-insensitive.'},
                        'type': {'type': 'string',
                                 'description': "Optional ripgrep file type, such as 'py', 'ts', 'js', or 'md'."},
                        'head_limit': {'type': 'number',
                                       'description': 'Maximum number of returned matches. Use 0 only when full '
                                                      'output is required.'},
                        'offset': {'type': 'number', 'description': 'Match offset for paginating large result sets.'},
                        'cursor': {'type': 'string',
                                   'description': 'Opaque continuation cursor returned by a previous Grep page. Do '
                                                  'not combine with offset.'},
                        'multiline': {'type': 'boolean',
                                      'description': 'Enable multiline regular-expression search.'}},
         'required': ['pattern'],
         'additionalProperties': False},
    },
    "Bash": {
        "description": 'Run shell commands in the current workspace. When you launch a long-running background task and promise to keep watching it, call TaskMonitorStart immediately afterward.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'command': {'type': 'string'},
                        'timeout': {'type': 'number'},
                        'description': {'type': 'string'},
                        'run_in_background': {'type': 'boolean'},
                        'dangerouslyDisableSandbox': {'type': 'boolean'}},
         'required': ['command'],
         'additionalProperties': False},
    },
    "TodoWrite": {
        "description": 'Update the session todo list.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'todos': {'type': 'array',
                                  'description': 'At most 100 items. This limit is enforced at runtime.',
                                  'items': {'type': 'object',
                                            'properties': {'content': {'type': 'string',
                                                                       'description': 'Non-empty todo text of at '
                                                                                      'most 2000 UTF-8 bytes. This '
                                                                                      'limit is enforced at '
                                                                                      'runtime.'},
                                                           'status': {'type': 'string',
                                                                      'enum': ['pending',
                                                                               'in_progress',
                                                                               'completed']},
                                                           'activeForm': {'type': 'string',
                                                                          'description': 'Present-progress label of '
                                                                                         'at most 1000 UTF-8 bytes. '
                                                                                         'This limit is enforced at '
                                                                                         'runtime.'}},
                                            'required': ['content', 'status', 'activeForm'],
                                            'additionalProperties': False}}},
         'required': ['todos'],
         'additionalProperties': False},
    },
    "WebFetch": {
        "description": 'Fetch a URL and return a text snapshot of its contents.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'url': {'type': 'string'}, 'prompt': {'type': 'string'}},
         'required': ['url', 'prompt'],
         'additionalProperties': False},
    },
    "WebSearch": {
        "description": 'Perform a lightweight web search and return ranked results.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'query': {'type': 'string'},
                        'count': {'type': 'number', 'description': 'Number of results to return, from 1 to 10.'},
                        'max_results': {'type': 'number', 'description': 'Alias for count.'},
                        'country': {'type': 'string',
                                    'description': 'Two-letter country code for providers that support it.'},
                        'freshness': {'type': 'string',
                                      'description': 'Optional freshness filter such as pd, pw, pm, or py.'},
                        'category': {'type': 'string',
                                     'description': 'Optional provider category, such as news, pdf, github, or '
                                                    'research paper.'},
                        'provider': {'type': 'string', 'enum': ['exa', 'scraper']},
                        'allowed_domains': {'type': 'array', 'items': {'type': 'string'}},
                        'blocked_domains': {'type': 'array', 'items': {'type': 'string'}}},
         'required': ['query'],
         'additionalProperties': False},
    },
    "SetStatus": {
        "description": "Update the assistant's live status line with a short progress message. Use this when changing phases or starting meaningful work. Keep messages brief and concrete, such as 'Reading plan...', 'Searching code...', 'Updating plan...', or 'Running tests...'.",
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'message': {'type': 'string'}, 'detail': {'type': 'string'}},
         'required': ['message'],
         'additionalProperties': False},
    },
    "Agent": {
        "description": 'Spawn a delegated resident subagent to work on a focused task. Always returns at spawn — the child works in the background and its result arrives later as a child message that wakes you. Keep working (or end the turn) in the meantime; never idle-poll for the child.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'description': {'type': 'string'},
                        'prompt': {'type': 'string'},
                        'subagent_type': {'type': 'string',
                                          'description': 'Named agent profile from this runtime\'s configured agents. '
                                                         'OMIT unless the runtime has agent profiles configured — most have '
                                                         'none, and any value then fails the spawn.'},
                        'model': {'type': 'string'},
                        'name': {'type': 'string'},
                        'team_name': {'type': 'string'},
                        'mode': {'type': 'string',
                                 'enum': ['default', 'acceptEdits', 'bypassPermissions', 'dontAsk', 'auto']},
                        'isolation': {'type': 'string', 'enum': ['shared', 'worktree', 'none']}},
         'required': ['description', 'prompt'],
         'additionalProperties': False},
    },
    "AskUserQuestion": {
        "description": 'Ask the interactive user one or more multiple-choice questions.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'questions': {'type': 'array', 'minItems': 1, 'maxItems': 4}},
         'required': ['questions'],
         'additionalProperties': False},
    },
    "Config": {
        "description": 'Return the active runtime configuration snapshot.',
        "concurrency": 'parallel',
        "schema": {'type': 'object', 'additionalProperties': False},
    },
    "NotebookEdit": {
        "description": 'Edit Jupyter notebook cells by ID.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'notebook_path': {'type': 'string'},
                        'cell_id': {'type': 'string'},
                        'new_source': {'type': 'string'},
                        'cell_type': {'type': 'string', 'enum': ['code', 'markdown', 'raw']},
                        'edit_mode': {'type': 'string', 'enum': ['replace', 'insert', 'delete']}},
         'required': ['notebook_path', 'new_source'],
         'additionalProperties': False},
    },
    "SendMessage": {
        "description": 'Send a message to a related agent: "parent" from a subagent, or a child agent id from the parent session. expects_reply blocks your turn until the answer — use it for quick questions only; for long-running work send without it and the reply will arrive as a message that wakes you. reply_to correlates an answer to an earlier message.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'to': {'type': 'string',
                               'description': '"parent" or a child agent id.'},
                        'message': {'type': 'string'},
                        'expects_reply': {'type': 'boolean'},
                        'reply_to': {'type': 'string',
                                     'description': 'Correlation id of the message being answered.'},
                        'timeout_ms': {'type': 'number'}},
         'required': ['to', 'message'],
         'additionalProperties': False},
    },
    "TaskOutput": {
        "description": 'Read output from a background task. This is a pull-based inspection tool, not proactive monitoring.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'task_id': {'type': 'string'}, 'block': {'type': 'boolean'}, 'timeout': {'type': 'number'}},
         'required': ['task_id', 'block', 'timeout'],
         'additionalProperties': False},
    },
    "TaskStop": {
        "description": 'Stop a running background task.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'task_id': {'type': 'string'}},
         'required': ['task_id'],
         'additionalProperties': False},
    },
    "TaskMonitorStart": {
        "description": 'Start proactive monitoring for a background task so the assistant can keep watching it and update the user without another prompt.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'task_id': {'type': 'string'},
                        'goal': {'type': 'string'},
                        'summary_style': {'type': 'string'},
                        'check_interval_ms': {'type': 'number'},
                        'heartbeat_interval_ms': {'type': 'number'}},
         'required': ['task_id', 'goal'],
         'additionalProperties': False},
    },
    "TaskMonitorStop": {
        "description": 'Stop proactive monitoring for a background task without stopping the underlying task itself.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'monitor_id': {'type': 'string'}},
         'required': ['monitor_id'],
         'additionalProperties': False},
    },
    "TaskHealthCheckStart": {
        "description": 'Start a daemon-backed periodic health check for a URL or shell command.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'title': {'type': 'string'},
                        'mode': {'type': 'string', 'enum': ['http', 'command']},
                        'url': {'type': 'string'},
                        'command': {'type': 'string'},
                        'interval_ms': {'type': 'number'},
                        'timeout_ms': {'type': 'number'},
                        'failure_threshold': {'type': 'number'},
                        'backoff_multiplier': {'type': 'number'},
                        'max_interval_ms': {'type': 'number'},
                        'expected_status_codes': {'type': 'array', 'items': {'type': 'number'}},
                        'expected_body_policy': {'type': 'string', 'enum': ['ignore', 'contains']},
                        'expected_body_substring': {'type': 'string'},
                        'output_limit_bytes': {'type': 'number'},
                        'body_limit_bytes': {'type': 'number'}},
         'required': ['title', 'mode'],
         'additionalProperties': False},
    },
    "TaskHealthCheckStop": {
        "description": 'Stop a daemon-backed health check task.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object',
         'properties': {'task_id': {'type': 'string'}},
         'required': ['task_id'],
         'additionalProperties': False},
    },
    "Mcp": {
        "description": 'Inspect the connected MCP runtime and discovered capabilities.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object', 'additionalProperties': False},
    },
    "ListMcpResources": {
        "description": 'List resources published by connected MCP servers.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'server': {'type': 'string'},
                        'offset': {'type': 'number'},
                        'cursor': {'type': 'string',
                                   'description': 'Opaque continuation cursor returned by a previous resource-list '
                                                  'page. Do not combine with offset.'},
                        'limit': {'type': 'number'}},
         'additionalProperties': False},
    },
    "ReadMcpResource": {
        "description": 'Read a resource published by a connected MCP server.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'server': {'type': 'string'},
                        'uri': {'type': 'string'},
                        'offset': {'type': 'number'},
                        'cursor': {'type': 'string',
                                   'description': 'Opaque continuation cursor returned by a previous resource page. '
                                                  'Do not combine with offset.'},
                        'limit': {'type': 'number'}},
         'required': ['server', 'uri'],
         'additionalProperties': False},
    },
    "EnterWorktree": {
        "description": 'Create and enter a git worktree for isolated work.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'additionalProperties': False},
    },
    "ExitWorktree": {
        "description": 'Return to the original workspace and optionally leave the worktree intact.',
        "concurrency": 'exclusive',
        "schema": {'type': 'object', 'additionalProperties': False},
    },
    "ReadToolArtifact": {
        "description": 'Read one bounded page from a complete tool-result artifact by artifact id. Use this operation, not Read or Bash, when a truncated tool result names an artifact.',
        "concurrency": 'parallel',
        "schema": {'type': 'object',
         'properties': {'artifact_id': {'type': 'string',
                                        'description': 'Artifact id from a truncated tool-result envelope.'},
                        'offset_bytes': {'type': 'integer',
                                         'minimum': 0,
                                         'description': 'Byte offset returned by the previous page. Defaults to '
                                                        'zero.'},
                        'max_bytes': {'type': 'integer',
                                      'minimum': 1,
                                      'maximum': 24000,
                                      'description': 'Requested page bytes. Defaults to 16384.'}},
         'required': ['artifact_id'],
         'additionalProperties': False},
    },
}


def builtin_tool(
    name: str,
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]],
    *,
    mcp: McpToolIdentity | None = None,
) -> ToolDefinition:
    spec = BUILTIN_TOOL_SPECS.get(name)
    if spec is None:
        raise KeyError(f"No builtin tool spec registered for {name!r}; add it to BUILTIN_TOOL_SPECS.")
    return ToolDefinition(
        name,
        spec["description"],
        copy.deepcopy(spec["schema"]),
        handler,
        concurrency=spec["concurrency"],
        mcp=mcp,
    )


# Compatibility projections of the spec table.
BUILTIN_TOOL_DESCRIPTIONS: dict[str, str] = {
    name: spec["description"] for name, spec in BUILTIN_TOOL_SPECS.items()
}
BUILTIN_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    name: spec["schema"] for name, spec in BUILTIN_TOOL_SPECS.items()
}
