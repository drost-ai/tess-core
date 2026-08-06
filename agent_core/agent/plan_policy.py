from __future__ import annotations

from .cancellation import _safe_emit
from .shared import PLAN_SAFE_TOOL_NAMES

from collections.abc import Callable
from typing import Any, Literal

from .._utils import maybe_await
from ..tools.definitions import ToolDefinition


OperatorPolicyDecision = Literal["allow", "deny", "unset"]


async def resolve_plan_mode_mcp_verdict(
    *,
    tool: ToolDefinition,
    repository_identity: str,
    resolve_operator_policy: Callable[..., Any] | None,
) -> dict[str, str]:
    """Resolve a generated MCP call using trusted provenance, never model text."""

    identity = tool.mcp
    if not isinstance(identity, dict):
        return {"decision": "block", "reason": "unresolved_target"}
    server = identity.get("server")
    tool_name = identity.get("name")
    if not _valid_identity_part(server) or not _valid_identity_part(tool_name):
        return {"decision": "block", "reason": "unresolved_target"}

    operator_decision: OperatorPolicyDecision = "unset"
    if callable(resolve_operator_policy):
        try:
            candidate = await maybe_await(
                resolve_operator_policy(
                    repository_identity=repository_identity,
                    server_key=server,
                    tool_name=tool_name,
                )
            )
            if candidate in ("allow", "deny", "unset"):
                operator_decision = candidate
        except Exception:
            # A failed authority lookup cannot manufacture an allow or deny.
            operator_decision = "unset"

    if operator_decision == "deny":
        return {"decision": "block", "reason": "operator_deny"}
    if operator_decision == "allow":
        return {"decision": "allow", "reason": "operator_allow"}
    annotations = identity.get("annotations")
    if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
        return {"decision": "allow", "reason": "read_only_hint"}
    return {"decision": "block", "reason": "not_read_only"}


def bounded_mcp_identity(tool: ToolDefinition) -> tuple[str, str]:
    identity = tool.mcp if isinstance(tool.mcp, dict) else {}
    return _bounded_text(identity.get("server"), "unknown"), _bounded_text(identity.get("name"), "unknown")


def _valid_identity_part(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= 512


def _bounded_text(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    sanitized = "".join(character for character in value.strip() if character >= " " and character != "\x7f")
    return sanitized.encode("utf-8")[:256].decode("utf-8", errors="ignore") or fallback


async def plan_mode_denial(
    *,
    tool: Any,
    tool_use: dict[str, Any],
    runtime: dict[str, Any],
    config: dict[str, Any],
    session: dict[str, Any],
) -> str | None:
    """Denial message if the tool is unavailable while plan approval is required."""
    is_mcp_candidate = bool(tool and getattr(tool, "mcp", None) is not None) or str(tool_use["name"]).startswith("mcp__")
    if is_mcp_candidate:
        if tool is None:
            verdict = {"decision": "block", "reason": "unresolved_target"}
            server, mcp_tool_name = "unknown", "unknown"
        else:
            verdict = await resolve_plan_mode_mcp_verdict(
                tool=tool,
                repository_identity=str(
                    runtime.get("repositoryIdentity")
                    or config.get("repositoryIdentity")
                    or session.get("cwd")
                    or ""
                ),
                resolve_operator_policy=runtime.get("resolveMcpOperatorPolicy")
                or runtime.get("resolve_mcp_operator_policy"),
            )
            server, mcp_tool_name = bounded_mcp_identity(tool)
        event_name = "plan.mcp_tool_allowed" if verdict["decision"] == "allow" else "plan.mcp_tool_blocked"
        _safe_emit(
            runtime,
            event_name,
            {"server": server, "tool": mcp_tool_name, "reason": verdict["reason"]},
        )
        if verdict["decision"] != "allow":
            return (
                f"MCP tool {server}/{mcp_tool_name} is blocked while plan approval is required "
                f"({verdict['reason']})."
            )
        return None
    if tool_use["name"] not in PLAN_SAFE_TOOL_NAMES:
        return f"{tool_use['name']} is not available while plan approval is still required."
    return None
