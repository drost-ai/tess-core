"""Open-question enforcement for parent turns.

The runtime guarantees every child question terminates in exactly one
reply record: open questions are re-surfaced at parent turn boundaries as
standing reminders, a parent turn that tries to end with open questions
gets a bounded continuation nudge, and when the nudges are exhausted the
runtime authors the backstop answer ("unanswered:<reason>") itself so the
blocked child always unblocks.
"""
from __future__ import annotations

from typing import Any

from ..session.message_origin import syntheticUserMessage as _synthetic_user_message
from ..tasks.communication import answer_task_question, read_task_questions
from ..storage import readTaskMeta

MAX_QUESTION_NUDGES = 3


async def read_open_child_questions(session: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    open_questions: list[dict[str, Any]] = []
    for task_id in metadata.get("taskIds") or []:
        if not isinstance(task_id, str) or not task_id:
            continue
        for question in await read_task_questions(task_id):
            if question.get("status") == "pending":
                open_questions.append(question)
    return open_questions


async def build_question_reminder(questions: list[dict[str, Any]]) -> str:
    lines = [
        "[OPEN QUESTIONS FROM CHILD AGENTS]",
        "Each blocked child stays blocked until you answer. Reply with",
        'SendMessage(to=<agent_id>, reply_to=<question_id>, message=<your answer>).',
        "",
    ]
    for question in questions:
        task_id = str(question.get("taskId") or "")
        meta = await readTaskMeta(task_id) if task_id else None
        agent_id = (meta or {}).get("agent_id") or (meta or {}).get("agentId") or task_id
        lines.append(f"- question_id: {question.get('questionId')} (agent: {agent_id})")
        lines.append(f"  {question.get('question')}")
    lines.append("[/OPEN QUESTIONS FROM CHILD AGENTS]")
    return "\n".join(lines)


async def inject_open_question_reminders(
    session: dict[str, Any],
    reminded_ids: set[str],
) -> list[dict[str, Any]]:
    """Append a standing reminder when the open-question set has news.

    Re-injecting an identical reminder at every tool boundary would bloat
    the transcript, so a fresh reminder lands only when a question id
    appears that this turn has not surfaced yet; the reminder always lists
    the full open set.
    """
    open_questions = await read_open_child_questions(session)
    fresh = [q for q in open_questions if str(q.get("questionId")) not in reminded_ids]
    if not fresh:
        return open_questions
    for question in open_questions:
        reminded_ids.add(str(question.get("questionId")))
    session.setdefault("messages", []).append(
        _synthetic_user_message(
            await build_question_reminder(open_questions),
            actor="task",
            kind="task_notification",
            lineage={"kind": "open_question_reminder"},
        )
    )
    return open_questions


async def resolve_open_questions_with_backstop(
    session: dict[str, Any],
    reason: str = "unanswered:parent_turn_exhausted",
) -> list[dict[str, Any]]:
    """Runtime-authored replies for every remaining open question."""
    resolved: list[dict[str, Any]] = []
    for question in await read_open_child_questions(session):
        task_id = str(question.get("taskId") or "")
        question_id = str(question.get("questionId") or "")
        if not task_id or not question_id:
            continue
        answered = await answer_task_question(task_id, question_id, reason)
        if answered:
            resolved.append(answered)
    return resolved
