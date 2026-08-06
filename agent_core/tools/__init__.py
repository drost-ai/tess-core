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

from .._utils import maybe_await, now_iso, pretty_json
from ..payloads.execution_descriptors import createTaskExecutionDescriptor, createWorkspaceExecutionDescriptor, summarizeOutputManifest
from ..monitors import createMonitorRecord, defaultMonitorCheckIntervalMs, defaultMonitorHeartbeatIntervalMs
from ..storage import listSessionMonitors, loadAutoMode, loadMonitor, loadSession, readTaskMeta, resolveMaybeRelative, saveMonitor, savePlanApproval, taskMetaPath, taskOutputPath, writePlanFile, writeTaskMeta
from ..payloads.tools import applyDefaultGlobExcludes, filterDefaultIgnoredGlobMatches, shapeGlobPayload, shapeGrepPayload, shapeReadPayload, shapeTextToolResult, splitToolTextLines
from .agent import _agent_tool
from .cancellation import _create_tool_abort_error, _signal_aborted, _throw_if_tool_cancelled
from .code_search import _exec_ripgrep_allowing_no_matches, _glob_tool, _grep_tool, _packaged_ripgrep_path, _resolve_ripgrep_binary
from .definitions import ToolDefinition
from .diffs import _build_diff_context_indexes, _build_line_diff_operations, _build_unified_diff, _file_change_payload
from .files import _edit_tool, _read_tool, _write_tool
from .mcp import _list_mcp_resources_tool, _mcp_tool, _read_mcp_resource_tool
from .notebook import _notebook_edit_tool
from .paths import _authorize_path, _display_path
from .permissions import PERMISSION_MODES, _ensure_tool_permission, _get_effective_permission_mode, _is_safe_read_only_shell_command, _matches_command_policy, _normalize_shell_command, _parse_permission_mode_value, _resolve_tool_permission
from .processes import _BACKGROUND_RUNNERS, _background_runner_env, _communicate_shell_process, _finish_terminated_process, _kill_asyncio_process, _resolve_shell, _spawn_background_task_runner, _terminate_asyncio_process, _track_background_runner, _wait_for_tracked_background_runner
from .registry import createBuiltinToolContractSnapshot, createBuiltinTools, create_builtin_tool_contract_snapshot, create_builtin_tools
from .schemas import BUILTIN_TOOL_DESCRIPTIONS, BUILTIN_TOOL_INPUT_SCHEMAS, BUILTIN_TOOL_ORDER, _tool_description, _tool_schema
from .session_metadata import _append_unique_metadata_list, _apply_workspace_snapshot_metadata
from .session_tools import _ask_user_question_tool, _config_tool, _derive_todo_active_form, _exit_plan_mode_tool, _extract_latest_assistant_text, _normalize_todo_item, _set_status_tool, _todo_tool, parseAllowedPrompts
from .shell import _bash_tool, _normalize_bash_timeout
from .tasks import _stopped_task_execution_descriptor, _task_health_check_start_tool, _task_health_check_stop_tool, _task_monitor_start_tool, _task_monitor_stop_tool, _task_output_tool, _task_stop_tool
from .validation import _require_list, _require_number, _require_optional_number, _require_string, _without_none_values
from .web import FetchResponse, _header_value, _safe_hostname, _strip_html, _web_fetch_tool, _web_search_headers, _web_search_tool, decodeBingRedirectTarget, fetch, normalizeBingResultUrl, normalizeDuckDuckGoResultUrl, parseBingResults, parseDuckDuckGoResults, searchBing, searchDuckDuckGo, searchWeb
from .worktree import _controller_method, _create_git_worktree, _enter_worktree_tool, _exit_worktree_tool

_PUBLIC_EXPORTS = ['Any', 'Awaitable', 'BUILTIN_TOOL_DESCRIPTIONS', 'BUILTIN_TOOL_INPUT_SCHEMAS', 'BUILTIN_TOOL_ORDER', 'Callable', 'FetchResponse', 'PERMISSION_MODES', 'Path', 'Request', 'ToolDefinition', 'annotations', 'applyDefaultGlobExcludes', 'asyncio', 'base64', 'copy', 'createBuiltinToolContractSnapshot', 'createBuiltinTools', 'createMonitorRecord', 'createTaskExecutionDescriptor', 'createWorkspaceExecutionDescriptor', 'create_builtin_tool_contract_snapshot', 'create_builtin_tools', 'dataclass', 'decodeBingRedirectTarget', 'defaultMonitorCheckIntervalMs', 'defaultMonitorHeartbeatIntervalMs', 'fetch', 'filterDefaultIgnoredGlobMatches', 'html', 'json', 'listSessionMonitors', 'loadAutoMode', 'loadMonitor', 'loadSession', 'maybe_await', 'mimetypes', 'normalizeBingResultUrl', 'normalizeDuckDuckGoResultUrl', 'now_iso', 'os', 'parseAllowedPrompts', 'parseBingResults', 'parseDuckDuckGoResults', 'parse_qs', 'platform', 'pretty_json', 're', 'readTaskMeta', 'resolveMaybeRelative', 'saveMonitor', 'savePlanApproval', 'searchBing', 'searchDuckDuckGo', 'searchWeb', 'shapeGlobPayload', 'shapeGrepPayload', 'shapeReadPayload', 'shapeTextToolResult', 'shutil', 'splitToolTextLines', 'subprocess', 'summarizeOutputManifest', 'sys', 'taskMetaPath', 'taskOutputPath', 'unquote', 'urlencode', 'urlopen', 'urlparse', 'urlunparse', 'uuid', 'writePlanFile', 'writeTaskMeta']

__all__ = _PUBLIC_EXPORTS

def __dir__():
    return sorted(_PUBLIC_EXPORTS)
