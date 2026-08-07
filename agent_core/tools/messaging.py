"""SendMessage builtin: parent↔child mailbox messaging as a tool call.

A subagent addresses "parent"; the parent addresses a child agent id.
expects_reply blocks inside the tool call until the counterpart's reply
record lands (correlated via metadata.replyTo) or the timeout knob runs
out, returning the reply text or a visible non-answer. Child→child
addressing is rejected until a routing story exists.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .._utils import maybe_await, pretty_json
from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult
from ..storage import loadChildAgent
from ..storage import mutateTaskMeta
from ..tasks.communication import (
    answer_task_question,
    create_task_question,
    mark_task_message_delivered,
    read_task_mailbox_messages,
    read_task_questions,
    send_task_message,
    skip_task_question,
)
from .definitions import ToolDefinition
from .schemas import builtin_tool


def _send_message_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        to = str(input.get("to") or "").strip()
        message = str(input.get("message") or "").strip()
        if not to or not message:
            return _error("SendMessage requires both `to` and a non-empty `message`.")
        reply_to = input.get("reply_to") if isinstance(input.get("reply_to"), str) and input.get("reply_to") else None
        expects_reply = bool(input.get("expects_reply"))
        # Call-time import: config package imports tools at module load.
        from ..config._knobs import knobValue

        timeout_ms = int(input.get("timeout_ms") or knobValue("comms.reply_timeout_ms"))
        session = context["session"]
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        own_task_id = metadata.get("subagentTaskId")
        is_child = isinstance(own_task_id, str) and bool(own_task_id)

        if to == "parent":
            if not is_child:
                return _error('This session has no parent; SendMessage(to="parent") is only available to subagents.')
            record = await send_task_message(
                own_task_id,
                {
                    "direction": "to_parent",
                    "sender": "child",
                    "kind": "reply" if reply_to else ("question" if expects_reply else "status"),
                    "content": message,
                    "metadata": {"replyTo": reply_to} if reply_to else {},
                },
            )
            if not expects_reply:
                return _queued(record)
            # A blocking child question: tracked as an open-question record so
            # the task flips to awaiting_input and the parent-side reply
            # invariant machinery sees it.
            await create_task_question(
                own_task_id,
                {
                    "questionId": record["messageId"],
                    "childSessionId": session.get("id"),
                    "question": message,
                    "metadata": {"messageId": record["messageId"]},
                },
            )
            reply = await _await_reply(own_task_id, "to_child", record["messageId"], timeout_ms, context)
            if reply is None:
                await skip_task_question(own_task_id, record["messageId"], "unanswered:child_timeout")
            await _restore_running_status(own_task_id)
            return _reply_result(record, reply, timeout_ms)

        if is_child:
            return _error('Child-to-child messaging is not supported yet; address "parent" instead.')
        child = await loadChildAgent(to)
        task_id = (child or {}).get("taskId")
        if not isinstance(task_id, str) or not task_id:
            return _error(f"Unknown agent id: {to}. Use the agent_id returned by the Agent tool.")
        if reply_to:
            # Answering resolves the open-question record (which queues the
            # to_child answer itself) so the runtime backstop never
            # double-answers; a reply_to with no pending question falls back
            # to a plain correlated message.
            pending = [q for q in await read_task_questions(task_id) if q.get("questionId") == reply_to and q.get("status") == "pending"]
            if pending:
                await answer_task_question(task_id, reply_to, message)
                return _queued({"messageId": reply_to, "taskId": task_id})
        payload = {
            "taskId": task_id,
            "direction": "to_child",
            "sender": "parent",
            "kind": "reply" if reply_to else "guidance",
            "content": message,
            "metadata": {"replyTo": reply_to} if reply_to else {},
        }
        daemon = context.get("daemon")
        if daemon and callable(daemon.get("sendTaskMessage")):
            record = await maybe_await(daemon["sendTaskMessage"](payload))
        else:
            record = await send_task_message(task_id, payload)
        if not expects_reply:
            return _queued(record)
        reply = await _await_reply(task_id, "to_parent", record["messageId"], timeout_ms, context)
        return _reply_result(record, reply, timeout_ms)

    return builtin_tool("SendMessage", handler)


async def _await_reply(
    task_id: str,
    direction: str,
    correlation_id: str,
    timeout_ms: int,
    context: ToolContext,
) -> dict[str, Any] | None:
    from ..config._knobs import knobValue

    poll_seconds = int(knobValue("comms.reply_poll_ms")) / 1000
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        signal = context.get("abortSignal")
        if signal is not None and getattr(signal, "aborted", False):
            return None
        for record in await read_task_mailbox_messages(task_id):
            record_direction = record.get("direction") or "to_child"
            if record_direction != direction:
                continue
            record_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if correlation_id not in (record_metadata.get("replyTo"), record_metadata.get("questionId")):
                continue
            if record.get("status") == "queued":
                # Claim it here so the boundary drain does not re-inject the
                # same reply into the transcript.
                await mark_task_message_delivered(task_id, str(record.get("messageId")))
            return record
        await asyncio.sleep(poll_seconds)
    return None


async def _restore_running_status(task_id: str) -> None:
    def _restore(meta: dict[str, Any] | None) -> dict[str, Any] | None:
        if not meta or meta.get("status") != "awaiting_input":
            return meta
        return {**meta, "status": "running", "waitingReason": None}

    await mutateTaskMeta(task_id, _restore)


def _queued(record: dict[str, Any]) -> ToolResult:
    return {
        "content": pretty_json(
            {"status": "queued", "message_id": record.get("messageId"), "task_id": record.get("taskId")}
        )
    }


def _reply_result(record: dict[str, Any], reply: dict[str, Any] | None, timeout_ms: int) -> ToolResult:
    if reply is None:
        return {
            "content": pretty_json(
                {
                    "status": "no_reply",
                    "message_id": record.get("messageId"),
                    "detail": f"No reply received within {timeout_ms}ms. The message stays queued for the recipient.",
                }
            )
        }
    return {
        "content": pretty_json(
            {
                "status": "reply_received",
                "message_id": record.get("messageId"),
                "reply_message_id": reply.get("messageId"),
                "reply": reply.get("content"),
            }
        )
    }


def _error(message: str) -> ToolResult:
    return {"content": json.dumps({"status": "error", "detail": message}), "isError": True}
