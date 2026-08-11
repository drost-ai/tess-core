from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

from typing import Any

from .._utils import maybe_await, pretty_json
from ..payloads.execution_descriptors import create_workspace_execution_descriptor
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission
from .schemas import builtin_tool
from .session_metadata import _apply_workspace_snapshot_metadata
from .validation import _without_none_values


def _enter_worktree_tool(controller: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
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
        execution = create_workspace_execution_descriptor({"action": "enter", "workspace": snapshot, "branch": branch_name, "created": True}) if snapshot else None
        result_payload = _without_none_values({"cwd": target_path, "branch": branch_name, "created": True, "workspace": snapshot, "execution": execution})
        result = {"content": pretty_json(result_payload)}
        if execution:
            result["meta"] = {"execution": execution}
        return result

    return builtin_tool("EnterWorktree", handler)


def _exit_worktree_tool(controller: Any) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        metadata = context["session"].setdefault("metadata", {})
        original = metadata.get("originalCwd")
        await maybe_await(context.get("updateCwd", lambda next_cwd: context["session"].__setitem__("cwd", next_cwd))(original))
        context["session"]["cwd"] = original
        context["cwd"] = original
        exit_worktree = _controller_method(controller, "exitWorktree")
        snapshot = await maybe_await(exit_worktree()) if callable(exit_worktree) else None
        if snapshot:
            _apply_workspace_snapshot_metadata(context["session"], snapshot)
        execution = create_workspace_execution_descriptor({"action": "exit", "workspace": snapshot}) if snapshot else None
        result_payload = _without_none_values({"cwd": original, "workspace": snapshot, "execution": execution})
        result = {"content": pretty_json(result_payload)}
        if execution:
            result["meta"] = {"execution": execution}
        return result

    return builtin_tool("ExitWorktree", handler)


def _controller_method(controller: Any, name: str) -> Any:
    if not controller:
        return None
    if isinstance(controller, dict):
        return controller.get(name)
    return getattr(controller, name, None)


async def _create_git_worktree(cwd: str, requested_name: str) -> dict[str, str] | None:
    from ..runtime.workspace import create_git_worktree

    return await create_git_worktree(cwd=cwd, name=requested_name)
