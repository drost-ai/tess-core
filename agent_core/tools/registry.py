from __future__ import annotations

from typing import Any

from .agent import _agent_tool
from .code_search import _glob_tool, _grep_tool
from .definitions import ToolDefinition
from .files import _edit_tool, _read_tool, _write_tool
from .mcp import _list_mcp_resources_tool, _mcp_tool, _read_mcp_resource_tool
from .notebook import _notebook_edit_tool
from .session_tools import _ask_user_question_tool, _config_tool, _exit_plan_mode_tool, _set_status_tool, _todo_tool
from .shell import _bash_tool
from .tasks import _task_health_check_start_tool, _task_health_check_stop_tool, _task_monitor_start_tool, _task_monitor_stop_tool, _task_output_tool, _task_stop_tool
from .web import _web_fetch_tool, _web_search_tool
from .worktree import _enter_worktree_tool, _exit_worktree_tool


def createBuiltinToolContractSnapshot(options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema}
        for tool in createBuiltinTools(options)
    ]


create_builtin_tool_contract_snapshot = createBuiltinToolContractSnapshot


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
