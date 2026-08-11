from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

from typing import Any

from .._utils import maybe_await, pretty_json
from ..tasks.communication import create_task_question, read_task_questions
from ..payloads.todo_policy import require_todo_items, serialize_todo_state_summary, update_active_task_from_todos
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .schemas import builtin_tool
from .validation import _require_string


def _todo_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        todos = require_todo_items(input.get("todos"))
        context["session"]["todos"] = todos
        update_active_task_from_todos(context["session"], todos)
        await maybe_await(context.get("persist_session", lambda: None)())
        return {"content": serialize_todo_state_summary(todos)}

    return builtin_tool("TodoWrite", handler)


def _set_status_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        message = _require_string(input.get("message"), "message").strip()
        detail = input.get("detail") if isinstance(input.get("detail"), str) and input.get("detail").strip() else None
        context.get("outputJson", lambda *_: None)("status.updated", {"message": message, "detail": detail, "session_id": context["session"]["id"]})
        return {"content": pretty_json({"ok": True, "message": message, "detail": detail})}

    return builtin_tool("SetStatus", handler)


def _ask_user_question_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        session = context["session"]
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        contract = metadata.get("subagentRuntimeContract") if isinstance(metadata, dict) else {}
        communication = contract.get("communication") if isinstance(contract, dict) and isinstance(contract.get("communication"), dict) else {}
        task_id = metadata.get("subagentTaskId") if isinstance(metadata, dict) else None
        if isinstance(task_id, str) and communication.get("allowChildQuestions"):
            pending = [question for question in await read_task_questions(task_id) if question.get("status") == "pending"]
            max_pending = int(communication.get("maxPendingQuestions") or 25)
            created = []
            questions = input.get("questions") if isinstance(input.get("questions"), list) else []
            for index, raw_question in enumerate(questions):
                _throw_if_tool_cancelled(context)
                if not isinstance(raw_question, dict):
                    continue
                if len(pending) + len(created) >= max_pending:
                    break
                question = _require_string(raw_question.get("question"), f"questions[{index}].question")
                options = []
                if isinstance(raw_question.get("options"), list):
                    for option in raw_question["options"]:
                        label = option.get("label") if isinstance(option, dict) else option
                        if isinstance(label, str) and label.strip():
                            options.append(label.strip())
                created.append(
                    await create_task_question(
                        task_id,
                        {
                            "childSessionId": session.get("id"),
                            "question": question,
                            "options": options,
                        },
                    )
                )
            if created:
                await maybe_await(context.get("persist_session", lambda: None)())
            return {
                "content": pretty_json(
                    {
                        "status": "awaiting_parent",
                        "task_id": task_id,
                        "questions": [
                            {
                                "question_id": question.get("questionId"),
                                "question": question.get("question"),
                                "options": question.get("options") or [],
                            }
                            for question in created
                        ],
                    }
                )
            }
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

    return builtin_tool("AskUserQuestion", handler)


def _config_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
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

    return builtin_tool("Config", handler)


def _normalize_todo_item(value: Any) -> dict[str, str]:
    return require_todo_items([value])[0]


def _derive_todo_active_form(content: str) -> str:
    from ..payloads.todo_policy import derive_todo_active_form

    return derive_todo_active_form(content)
