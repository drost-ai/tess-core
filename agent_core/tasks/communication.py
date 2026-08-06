from __future__ import annotations

import os
import uuid
from typing import Any, Mapping

from .._utils import now_iso
from ..storage import (
    appendJsonLine,
    loadSession,
    mutateTaskMeta,
    readJsonFile,
    readTaskMeta,
    saveSession,
    taskDir,
    writeJsonFile,
)
from ..storage.json_io import mutateJsonLines, readJsonLines
from ..session.persistence import persistSession
from ..session.message_origin import syntheticUserMessage as _synthetic_user_message


def task_mailbox_path(task_id: str) -> str:
    return os.path.join(taskDir(task_id), "mailbox.jsonl")


def task_events_path(task_id: str) -> str:
    return os.path.join(taskDir(task_id), "events.jsonl")


def task_questions_path(task_id: str) -> str:
    return os.path.join(taskDir(task_id), "questions.jsonl")


async def _read_json_lines(path: str) -> list[dict[str, Any]]:
    return await readJsonLines(path)


async def append_task_event(task_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
    record = {
        "eventId": event.get("eventId") or f"evt-{uuid.uuid4()}",
        "taskId": task_id,
        "createdAt": event.get("createdAt") or now_iso(),
        **dict(event),
    }
    await appendJsonLine(task_events_path(task_id), record)
    return record


async def read_task_events(task_id: str) -> list[dict[str, Any]]:
    return await _read_json_lines(task_events_path(task_id))


async def send_task_message(task_id: str, message: Mapping[str, Any]) -> dict[str, Any]:
    meta = await readTaskMeta(task_id)
    child_session_id = message.get("childSessionId") or message.get("child_session_id") or (meta or {}).get("session_id") or (meta or {}).get("sessionId")
    if not isinstance(child_session_id, str) or not child_session_id:
        raise ValueError("Task message requires a child session id.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Task message requires non-empty content.")
    now = now_iso()
    record = {
        "messageId": message.get("messageId") or message.get("message_id") or f"msg-{uuid.uuid4()}",
        "taskId": task_id,
        "childSessionId": child_session_id,
        "sender": message.get("sender") or "parent",
        "kind": message.get("kind") or "guidance",
        "content": content.strip(),
        "status": "queued",
        "createdAt": message.get("createdAt") or now,
        "deliveredAt": None,
        "metadata": dict(message.get("metadata") or {}),
    }
    await appendJsonLine(task_mailbox_path(task_id), record)
    await append_task_event(
        task_id,
        {
            "type": "message.queued",
            "messageId": record["messageId"],
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
        if isinstance(message_id, str):
            messages[message_id] = record
    return sorted(messages.values(), key=lambda item: str(item.get("createdAt") or ""))


async def read_undelivered_task_messages(task_id: str, child_session_id: str | None = None) -> list[dict[str, Any]]:
    messages = [message for message in await read_task_mailbox_messages(task_id) if message.get("status") == "queued"]
    if child_session_id:
        messages = [message for message in messages if message.get("childSessionId") == child_session_id]
    return messages


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

    updated = await mutateJsonLines(task_mailbox_path(task_id), _mark)
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


async def _claim_undelivered_task_messages(
    task_id: str,
    child_session_id: str | None,
) -> list[dict[str, Any]]:
    delivered_at = now_iso()

    def _claim(records: list[dict[str, Any]]) -> dict[str, Any]:
        messages = _latest_records(records, "messageId")
        claimed: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            matches_session = not child_session_id or message.get("childSessionId") == child_session_id
            if message.get("status") == "queued" and matches_session:
                updated = {**message, "status": "delivered", "deliveredAt": delivered_at}
                messages[index] = updated
                claimed.append(updated)
        return {"records": messages, "result": claimed, "changed": bool(claimed)}

    return await mutateJsonLines(task_mailbox_path(task_id), _claim)


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


async def _deliver_mailbox_transaction(
    task_id: str,
    session: dict[str, Any],
    config: dict[str, Any] | None,
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
            matches_session = not child_session_id or message.get("childSessionId") == child_session_id
            if message.get("status") != "queued" or not matches_session:
                continue
            message_id = message.get("messageId")
            updated = {**message, "status": "delivered", "deliveredAt": delivered_at}
            messages[index] = updated
            claimed.append(updated)
            if message_id not in existing:
                session.setdefault("messages", []).append(
                    _synthetic_user_message(
                        _message_envelope(message),
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
                    await persistSession(session, config)
                else:
                    await saveSession(session)
        except BaseException:
            if appended:
                del session["messages"][-appended:]
            raise
        return {"records": messages, "result": claimed, "changed": True}

    return await mutateJsonLines(task_mailbox_path(task_id), _commit)


def _message_envelope(message: Mapping[str, Any]) -> str:
    sender = "SUPERVISOR" if message.get("sender") == "supervisor" else "PARENT"
    kind = message.get("kind") or "guidance"
    return f"[{sender} MESSAGE]\nkind: {kind}\nmessage_id: {message.get('messageId')}\n\n{message.get('content')}\n[/{sender} MESSAGE]"


async def deliver_queued_task_messages_to_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    task_id = metadata.get("subagentTaskId")
    if not isinstance(task_id, str) or not task_id:
        return []
    messages = await _deliver_mailbox_transaction(task_id, session, config)
    if not messages:
        return []
    for message in messages:
        await append_task_event(
            task_id,
            {
                "type": "message.delivered",
                "messageId": message["messageId"],
                "deliveredAt": message.get("deliveredAt"),
            },
        )
    return messages


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
    await appendJsonLine(task_questions_path(task_id), record)
    await append_task_event(task_id, {"type": "question.created", "questionId": record["questionId"], "childSessionId": record.get("childSessionId")})

    def _mark_awaiting(meta: dict[str, Any] | None) -> dict[str, Any] | None:
        if not meta:
            return None
        return {**meta, "status": "awaiting_input", "waitingReason": "child_question", "updatedAt": now}

    await mutateTaskMeta(task_id, _mark_awaiting)
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

    updated = await mutateJsonLines(task_questions_path(task_id), _answer)
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

    updated = await mutateJsonLines(task_questions_path(task_id), _skip)
    if not updated:
        return None
    await append_task_event(task_id, {"type": "question.skipped", "questionId": question_id, "reason": reason})
    return updated


async def load_task_session(task_id: str) -> dict[str, Any] | None:
    meta = await readTaskMeta(task_id)
    session_id = (meta or {}).get("session_id") or (meta or {}).get("sessionId")
    return await loadSession(session_id) if isinstance(session_id, str) and session_id else None


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
