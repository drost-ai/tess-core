"""Builtin tool definitions, registry, and permission resolution.

The facade re-exports the package's public API plus the private seam
names tests exercise directly (ripgrep resolution, permission checks,
process tracking, and the web fetch entry points).
"""
from __future__ import annotations

from .agent import _agent_tool
from .cancellation import _create_tool_abort_error, _signal_aborted, _throw_if_tool_cancelled
from .code_search import _glob_tool, _grep_tool, _packaged_ripgrep_path, _resolve_ripgrep_binary
from .definitions import ToolDefinition
from .files import _edit_tool, _read_tool, _write_tool
from .mcp import _list_mcp_resources_tool, _mcp_tool, _read_mcp_resource_tool
from .notebook import _notebook_edit_tool
from .paths import _authorize_path, _display_path
from .permissions import PERMISSION_MODES, _ensure_tool_permission, _get_effective_permission_mode, _is_safe_read_only_shell_command, _matches_command_policy, _parse_permission_mode_value, _resolve_tool_permission
from .processes import _BACKGROUND_RUNNERS, _background_runner_env, _communicate_shell_process, _finish_terminated_process, _kill_asyncio_process, _resolve_shell, _spawn_background_task_runner, _terminate_asyncio_process, _track_background_runner, _wait_for_tracked_background_runner
from .registry import create_builtin_tool_contract_snapshot, create_builtin_tools
from .schemas import BUILTIN_TOOL_DESCRIPTIONS, BUILTIN_TOOL_INPUT_SCHEMAS, BUILTIN_TOOL_SPECS, builtin_tool
from .session_metadata import _append_unique_metadata_list, _apply_workspace_snapshot_metadata
from .session_tools import _ask_user_question_tool, _config_tool, _derive_todo_active_form, _normalize_todo_item, _set_status_tool, _todo_tool
from .shell import _bash_tool, _normalize_bash_timeout
from .tasks import _stopped_task_execution_descriptor, _task_health_check_start_tool, _task_health_check_stop_tool, _task_monitor_start_tool, _task_monitor_stop_tool, _task_output_tool, _task_stop_tool
from .validation import _require_list, _require_number, _require_optional_number, _require_string, _without_none_values
from .web import FetchResponse, _header_value, _safe_hostname, _strip_html, _web_fetch_tool, _web_search_headers, _web_search_tool, decode_bing_redirect_target, fetch, normalize_bing_result_url, normalize_duck_duck_go_result_url, parse_bing_results, parse_duck_duck_go_results, search_bing, search_duck_duck_go, search_web
from .worktree import _controller_method, _create_git_worktree, _enter_worktree_tool, _exit_worktree_tool

__all__ = [
    "BUILTIN_TOOL_DESCRIPTIONS",
    "BUILTIN_TOOL_INPUT_SCHEMAS",
    "FetchResponse",
    "PERMISSION_MODES",
    "ToolDefinition",
    "create_builtin_tool_contract_snapshot",
    "create_builtin_tools",
    "decode_bing_redirect_target",
    "fetch",
    "normalize_bing_result_url",
    "normalize_duck_duck_go_result_url",
    "parse_bing_results",
    "parse_duck_duck_go_results",
    "search_bing",
    "search_duck_duck_go",
    "search_web",
]
