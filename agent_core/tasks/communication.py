from __future__ import annotations

import os
import uuid
from typing import Any, Mapping

from .._utils import now_iso
from ..storage import (
    append_json_line,
    load_session,
    mutate_task_meta,
    read_json_file,
    read_task_meta,
    save_session,
    task_dir,
    write_json_file,
)
from ..storage.json_io import mutate_json_lines, read_json_lines
from .mailbox_closure import append_message_unless_closed, is_mailbox_closure
from ..session.persistence import persist_session
from ..storage.message_origin import synthetic_user_message as _synthetic_user_message


def task_mailbox_path(task_id: str) -> str:
    return os.path.join(task_dir(task_id), "mailbox.jsonl")


def task_events_path(task_id: str) -> str:
    return os.path.join(task_dir(task_id), "events.jsonl")


def task_questions_path(task_id: str) -> str:
    return os.path.join(task_dir(task_id), "questions.jsonl")


async def _read_json_lines(path: str) -> list[dict[str, Any]]:
    return await read_json_lines(path)


async def append_task_event(task_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
    record = {
        "eventId": event.get("eventId") or f"evt-{uuid.uuid4()}",
        "taskId": task_id,
        "createdAt": event.get("createdAt") or now_iso(),
        **dict(event),
    }
    await append_json_line(task_events_path(task_id), record)
    return record


async def read_task_events(task_id: str) -> list[dict[str, Any]]:
    return await _read_json_lines(task_events_path(task_id))


async def send_task_message(task_id: str, message: Mapping[str, Any]) -> dict[str, Any]:
    meta = await read_task_meta(task_id)
    direction = message.get("direction") or "to_child"
    if direction not in {"to_child", "to_parent"}:
        raise ValueError(f"Unknown task message direction: {direction!r}")
    child_session_id = message.get("childSessionId") or message.get("child_session_id") or (meta or {}).get("session_id") or (meta or {}).get("sessionId")
    if not isinstance(child_session_id, str) or not child_session_id:
        raise ValueError("Task message requires a child session id.")
    parent_session_id = message.get("parentSessionId") or message.get("parent_session_id") or (meta or {}).get("parent_session_id") or (meta or {}).get("parentSessionId")
    if direction == "to_parent" and (not isinstance(parent_session_id, str) or not parent_session_id):
        raise ValueError("A to_parent task message requires a parent session id.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Task message requires non-empty content.")
    now = now_iso()
    record = {
        "messageId": message.get("messageId") or message.get("message_id") or f"msg-{uuid.uuid4()}",
        "taskId": task_id,
        "direction": direction,
        "childSessionId": child_session_id,
        **({"parentSessionId": parent_session_id} if isinstance(parent_session_id, str) and parent_session_id else {}),
        "sender": message.get("sender") or ("child" if direction == "to_parent" else "parent"),
        "kind": message.get("kind") or "guidance",
        "content": content.strip(),
        "status": "queued",
        "createdAt": message.get("createdAt") or now,
        "deliveredAt": None,
        "metadata": dict(message.get("metadata") or {}),
    }
    if direction == "to_child":
        # A child that stopped for good closed its mailbox; a message to it
        # must fail here rather than wait for a reader that will never come.
        await append_message_unless_closed(task_mailbox_path(task_id), task_id, record)
    else:
        await append_json_line(task_mailbox_path(task_id), record)
    await append_task_event(
        task_id,
        {
            "type": "message.queued",
            "messageId": record["messageId"],
            "direction": direction,
            "sender": record["sender"],
            "kind": record["kind"],
            "childSessionId": child_session_id,
        },
    )
    return record


async def read_task_mailbox_messages(task_id: str) -> list[dict[str, Any]]:
    messages: dict[str, dict[str, Any]] = {}
    for record in await _read_json_lines(task_mailbox_path(task_id)):
        message_id = record.get("messageId")
        if isinstance(message_id, str) and not is_mailbox_closure(record):
            messages[message_id] = record
    return sorted(messages.values(), key=lambda item: str(item.get("createdAt") or ""))


async def read_undelivered_task_messages(task_id: str, child_session_id: str | None = None) -> list[dict[str, Any]]:
    messages = [message for message in await read_task_mailbox_messages(task_id) if message.get("status") == "queued"]
    if child_session_id:
        messages = [message for message in messages if message.get("childSessionId") == child_session_id]
    return messages


async def requeue_task_message(task_id: str, message_id: str) -> dict[str, Any] | None:
    """Undo a delivery claim whose consumer never committed the result.

    A reply claimed inside a tool call that is then cancelled would
    otherwise be lost entirely: delivered on disk, absent from the
    transcript. Requeueing lets the level-triggered sweep and the next
    drain surface it."""
    def _requeue(records: list[dict[str, Any]]) -> dict[str, Any]:
        messages = _latest_records(records, "messageId")
        updated: dict[str, Any] | None = None
        for index, message in enumerate(messages):
            if message.get("messageId") == message_id and message.get("status") == "delivered":
                updated = {**message, "status": "queued", "deliveredAt": None}
                messages[index] = updated
                break
        return {"records": messages, "result": updated, "changed": updated is not None}

    updated = await mutate_json_lines(task_mailbox_path(task_id), _requeue)
    if updated:
        await append_task_event(task_id, {"type": "message.requeued", "messageId": message_id})
    return updated


async def mark_task_message_delivered(task_id: str, message_id: str) -> dict[str, Any] | None:
    delivered_at = now_iso()
    def _mark(records: list[dict[str, Any]]) -> dict[str, Any]:
        messages = _latest_records(records, "messageId")
        updated: dict[str, Any] | None = None
        for index, message in enumerate(messages):
            if message.get("messageId") == message_id:
                updated = {**message, "status": "delivered", "deliveredAt": delivered_at}
                messages[index] = updated
                break
        return {"records": messages, "result": updated, "changed": updated is not None}

    updated = await mutate_json_lines(task_mailbox_path(task_id), _mark)
    if updated:
        await append_task_event(task_id, {"type": "message.delivered", "messageId": message_id, "deliveredAt": delivered_at})
    return updated


def _latest_records(records: list[dict[str, Any]], id_field: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for record in records:
        record_id = record.get(id_field)
        if isinstance(record_id, str):
            latest[record_id] = record
        else:
            anonymous.append(record)
    return sorted(
        [*anonymous, *latest.values()],
        key=lambda item: str(item.get("createdAt") or ""),
    )


def _session_task_message_ids(session: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for message in session.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        metadata = message.get("metadata")
        origin = metadata.get("agentCoreOrigin") if isinstance(metadata, Mapping) else None
        lineage = origin.get("lineage") if isinstance(origin, Mapping) else None
        message_id = lineage.get("messageId") if isinstance(lineage, Mapping) else None
        if isinstance(message_id, str) and message_id:
            found.add(message_id)
    return found


def _message_matches_direction(message: Mapping[str, Any], session_id: str | None, direction: str) -> bool:
    if direction == "to_parent":
        if message.get("direction") != "to_parent":
            return False
        recipient = message.get("parentSessionId")
        return not session_id or not isinstance(recipient, str) or recipient == session_id
    if message.get("direction") not in (None, "to_child"):
        return False
    return not session_id or message.get("childSessionId") == session_id


async def _deliver_mailbox_transaction(
    task_id: str,
    session: dict[str, Any],
    config: dict[str, Any] | None,
    direction: str = "to_child",
) -> list[dict[str, Any]]:
    """Commit mailbox delivery and its session projection under one record lock.

    The session is persisted before the queued record becomes delivered.  A
    crash in that narrow boundary is reconciled by the message lineage check,
    so retry marks the record delivered without appending a duplicate.
    """

    child_session_id = session.get("id")
    delivered_at = now_iso()

    async def _commit(records: list[dict[str, Any]]) -> dict[str, Any]:
        messages = _latest_records(records, "messageId")
        existing = _session_task_message_ids(session)
        claimed: list[dict[str, Any]] = []
        appended = 0
        for index, message in enumerate(messages):
            if message.get("status") != "queued" or not _message_matches_direction(message, child_session_id, direction):
                continue
            message_id = message.get("messageId")
            updated = {**message, "status": "delivered", "deliveredAt": delivered_at}
            messages[index] = updated
            claimed.append(updated)
            if message_id not in existing:
                session.setdefault("messages", []).append(
                    _synthetic_user_message(
                        await _render_injected_message(message, session),
                        actor="task",
                        kind="task_notification",
                        lineage={
                            "taskId": str(message.get("taskId") or ""),
                            "messageId": str(message_id or ""),
                        },
                    )
                )
                appended += 1
        if not claimed:
            return {"records": messages, "result": [], "changed": False}
        try:
            if appended:
                if config is not None:
                    await persist_session(session, config)
                else:
                    await save_session(session)
        except BaseException:
            if appended:
                del session["messages"][-appended:]
            raise
        return {"records": messages, "result": claimed, "changed": True}

    return await mutate_json_lines(task_mailbox_path(task_id), _commit)


async def _render_injected_message(message: Mapping[str, Any], session: Mapping[str, Any]) -> str:
    return _message_envelope(message, await _bounded_message_content(str(message.get("content") or ""), session))


def _message_envelope(message: Mapping[str, Any], content: str | None = None) -> str:
    raw_sender = message.get("sender")
    sender = "SUPERVISOR" if raw_sender == "supervisor" else "CHILD" if raw_sender == "child" else "PARENT"
    kind = message.get("kind") or "guidance"
    origin = f"\ntask_id: {message.get('taskId')}" if message.get("direction") == "to_parent" else ""
    body = content if content is not None else str(message.get("content") or "")
    return f"[{sender} MESSAGE]\nkind: {kind}\nmessage_id: {message.get('messageId')}{origin}\n\n{body}\n[/{sender} MESSAGE]"


async def _bounded_message_content(content: str, session: Mapping[str, Any]) -> str:
    """Clamp a drain-injected message to the comms inline allowance.

    The remainder must stay reachable WITHOUT re-asking the sender: the
    full text is persisted as a tool artifact and the truncation note
    carries the artifact id, so the recipient pages it with
    ReadToolArtifact."""
    # Call-time import: the config package imports tasks at module load.
    from ..config._knobs import knob_value

    limit = int(knob_value("comms.inline_result_bytes"))
    encoded = content.encode("utf-8")
    if len(encoded) <= limit:
        return content
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    try:
        from ..payloads.tools import _persist_artifact

        artifact = await _persist_artifact(
            {
                "sessionId": session.get("id"),
                "toolName": "ChildMessage",
                "family": "comms",
                "content": content,
                "contentType": "text",
                "truncated": False,
            }
        )
        note = (
            f"… [message truncated: {len(encoded):,} bytes total; read the rest with "
            f"ReadToolArtifact artifact_id={artifact['artifactId']} offset_bytes={len(clipped.encode('utf-8'))}]"
        )
    except Exception:
        note = f"… [message truncated: {len(encoded):,} bytes total; full text in the task mailbox record]"
    return f"{clipped}\n{note}"


async def deliver_queued_task_messages_to_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Drain every queued mailbox message addressed to this session.

    A child session (metadata.subagentTaskId) receives to_child messages of
    its own task; a parent session (metadata.taskIds) receives to_parent
    messages across its children's tasks. A mid-tree session can be both.
    """
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    delivered: list[dict[str, Any]] = []
    own_task_id = metadata.get("subagentTaskId")
    if isinstance(own_task_id, str) and own_task_id:
        delivered.extend(await _deliver_mailbox_transaction(own_task_id, session, config, "to_child"))
    for child_task_id in metadata.get("taskIds") or []:
        if not isinstance(child_task_id, str) or not child_task_id or child_task_id == own_task_id:
            continue
        if not os.path.exists(task_mailbox_path(child_task_id)):
            continue
        delivered.extend(await _deliver_mailbox_transaction(child_task_id, session, config, "to_parent"))
    for message in delivered:
        await append_task_event(
            str(message.get("taskId")),
            {
                "type": "message.delivered",
                "messageId": message["messageId"],
                "deliveredAt": message.get("deliveredAt"),
            },
        )
    return delivered


async def create_task_question(task_id: str, question: Mapping[str, Any]) -> dict[str, Any]:
    content = question.get("question")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Task question requires non-empty question text.")
    now = now_iso()
    record = {
        "questionId": question.get("questionId") or question.get("question_id") or f"q-{uuid.uuid4()}",
        "taskId": task_id,
        "childSessionId": question.get("childSessionId") or question.get("child_session_id"),
        "question": content.strip(),
        "options": list(question.get("options") or []),
        "status": "pending",
        "createdAt": question.get("createdAt") or now,
        "answeredAt": None,
        "answer": None,
        "metadata": dict(question.get("metadata") or {}),
    }
    await append_json_line(task_questions_path(task_id), record)
    await append_task_event(task_id, {"type": "question.created", "questionId": record["questionId"], "childSessionId": record.get("childSessionId")})

    def _mark_awaiting(meta: dict[str, Any] | None) -> dict[str, Any] | None:
        if not meta:
            return None
        return {**meta, "status": "awaiting_input", "waitingReason": "child_question", "updatedAt": now}

    await mutate_task_meta(task_id, _mark_awaiting)
    return record


async def read_task_questions(task_id: str) -> list[dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    for record in await _read_json_lines(task_questions_path(task_id)):
        question_id = record.get("questionId")
        if isinstance(question_id, str):
            questions[question_id] = record
    return sorted(questions.values(), key=lambda item: str(item.get("createdAt") or ""))


async def answer_task_question(task_id: str, question_id: str, answer: str) -> dict[str, Any] | None:
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Answer must be non-empty.")
    answered_at = now_iso()
    def _answer(records: list[dict[str, Any]]) -> dict[str, Any]:
        questions = _latest_records(records, "questionId")
        updated: dict[str, Any] | None = None
        for index, record in enumerate(questions):
            if record.get("questionId") == question_id:
                updated = {**record, "status": "answered", "answeredAt": answered_at, "answer": answer.strip()}
                questions[index] = updated
                break
        return {"records": questions, "result": updated, "changed": updated is not None}

    updated = await mutate_json_lines(task_questions_path(task_id), _answer)
    if not updated:
        return None
    await send_task_message(
        task_id,
        {
            "childSessionId": updated.get("childSessionId"),
            "sender": "supervisor",
            "kind": "answer",
            "content": f"Answer to question {question_id}:\n{answer.strip()}",
            "metadata": {"questionId": question_id},
        },
    )
    await append_task_event(task_id, {"type": "question.answered", "questionId": question_id, "answeredAt": answered_at})
    return updated


async def skip_task_question(task_id: str, question_id: str, reason: str | None = None) -> dict[str, Any] | None:
    skipped_at = now_iso()
    def _skip(records: list[dict[str, Any]]) -> dict[str, Any]:
        questions = _latest_records(records, "questionId")
        updated: dict[str, Any] | None = None
        for index, record in enumerate(questions):
            if record.get("questionId") == question_id:
                updated = {**record, "status": "skipped", "answeredAt": skipped_at, "answer": reason or ""}
                questions[index] = updated
                break
        return {"records": questions, "result": updated, "changed": updated is not None}

    updated = await mutate_json_lines(task_questions_path(task_id), _skip)
    if not updated:
        return None
    await append_task_event(task_id, {"type": "question.skipped", "questionId": question_id, "reason": reason})
    return updated


async def load_task_session(task_id: str) -> dict[str, Any] | None:
    meta = await read_task_meta(task_id)
    session_id = (meta or {}).get("session_id") or (meta or {}).get("sessionId")
    return await load_session(session_id) if isinstance(session_id, str) and session_id else None


appendTaskEvent = append_task_event
answerTaskQuestion = answer_task_question
createTaskQuestion = create_task_question
deliverQueuedTaskMessagesToSession = deliver_queued_task_messages_to_session
loadTaskSession = load_task_session
markTaskMessageDelivered = mark_task_message_delivered
readTaskEvents = read_task_events
readTaskMailboxMessages = read_task_mailbox_messages
readTaskQuestions = read_task_questions
readUndeliveredTaskMessages = read_undelivered_task_messages
sendTaskMessage = send_task_message
skipTaskQuestion = skip_task_question
taskEventsPath = task_events_path
taskMailboxPath = task_mailbox_path
taskQuestionsPath = task_questions_path


__all__ = [
    "answerTaskQuestion",
    "answer_task_question",
    "appendTaskEvent",
    "append_task_event",
    "createTaskQuestion",
    "create_task_question",
    "deliverQueuedTaskMessagesToSession",
    "deliver_queued_task_messages_to_session",
    "loadTaskSession",
    "load_task_session",
    "markTaskMessageDelivered",
    "mark_task_message_delivered",
    "readTaskEvents",
    "read_task_events",
    "readTaskMailboxMessages",
    "read_task_mailbox_messages",
    "readTaskQuestions",
    "read_task_questions",
    "readUndeliveredTaskMessages",
    "read_undelivered_task_messages",
    "sendTaskMessage",
    "send_task_message",
    "skipTaskQuestion",
    "skip_task_question",
    "taskEventsPath",
    "taskMailboxPath",
    "taskQuestionsPath",
    "task_events_path",
    "task_mailbox_path",
    "task_questions_path",
]
