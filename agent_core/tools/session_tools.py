from __future__ import annotations

from ..contracts.context import ToolContext

import re
import uuid
from typing import Any

from .._utils import maybe_await, now_iso, pretty_json
from ..storage import savePlanApproval, writePlanFile
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .schemas import _tool_description, _tool_schema
from .validation import _require_list, _require_string


def _todo_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        todos = [_normalize_todo_item(item) for item in _require_list(input.get("todos"), "todos")]
        context["session"]["todos"] = todos
        await maybe_await(context.get("persistSession", lambda: None)())
        return {"content": pretty_json({"updated": len(todos), "todos": todos})}

    return ToolDefinition("TodoWrite", _tool_description("TodoWrite"), _tool_schema("TodoWrite"), handler)


def _set_status_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        message = _require_string(input.get("message"), "message").strip()
        detail = input.get("detail") if isinstance(input.get("detail"), str) and input.get("detail").strip() else None
        context.get("outputJson", lambda *_: None)("status.updated", {"message": message, "detail": detail, "session_id": context["session"]["id"]})
        return {"content": pretty_json({"ok": True, "message": message, "detail": detail})}

    return ToolDefinition("SetStatus", _tool_description("SetStatus"), _tool_schema("SetStatus"), handler)


def _ask_user_question_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
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
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
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
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
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


def parseAllowedPrompts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and item.get("tool") == "Bash" and isinstance(item.get("prompt"), str) and item["prompt"].strip():
            out.append({"tool": "Bash", "prompt": item["prompt"].strip()})
    return out


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
