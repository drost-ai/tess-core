from __future__ import annotations

from ..._utils import maybe_await
from ...storage import readTaskMeta, taskOutputPath
from .records import _task_event_payload
from .utils import _require_string
from typing import Any, Callable
from ...payloads.bounded_io import readFileTailBounded as _read_file_tail_bounded
from ..communication import (
    answer_task_question as _answer_task_question,
    read_task_events as _read_task_events,
    read_task_questions as _read_task_questions,
    send_task_message as _send_task_message,
    skip_task_question as _skip_task_question,
)


async def readTaskOutput(task_id: str, tailBytes: int | None = 12000, **kwargs: Any) -> dict[str, Any]:
    if "tail_bytes" in kwargs:
        tailBytes = kwargs["tail_bytes"]
    meta = await readTaskMeta(task_id)
    output_file = (meta or {}).get("outputFile") or taskOutputPath(task_id)
    effective_tail_bytes = 12000 if tailBytes is None else max(0, int(tailBytes))
    try:
        tail = await _read_file_tail_bounded(filePath=output_file, maxBytes=effective_tail_bytes)
    except (OSError, ValueError):
        return {"taskId": task_id, "output": "", "truncated": False, "outputFile": output_file}
    return {
        "taskId": task_id,
        "output": tail["text"],
        "truncated": tail["truncated"],
        "outputFile": output_file,
    }


def createTaskCommunicationApi(emit: Callable[[str, dict[str, Any], str | None], Any]) -> dict[str, Any]:
    async def emit_task_update(task_id: str) -> None:
        meta = await readTaskMeta(task_id)
        if meta:
            await maybe_await(emit("task.updated", _task_event_payload(meta), meta.get("parent_session_id") or meta.get("sessionId")))

    async def send_task_message(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any]:
        task_args = {**(task_args or {}), **task_kwargs}
        task_id = _require_string(task_args.get("taskId") or task_args.get("task_id"), "taskId")
        message = await _send_task_message(task_id, task_args)
        await emit_task_update(task_id)
        return message

    async def answer_task_question(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any] | None:
        task_args = {**(task_args or {}), **task_kwargs}
        task_id = _require_string(task_args.get("taskId") or task_args.get("task_id"), "taskId")
        question_id = _require_string(task_args.get("questionId") or task_args.get("question_id"), "questionId")
        answer = _require_string(task_args.get("answer"), "answer")
        result = await _answer_task_question(task_id, question_id, answer)
        await emit_task_update(task_id)
        return result

    async def skip_task_question(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, Any] | None:
        task_args = {**(task_args or {}), **task_kwargs}
        task_id = _require_string(task_args.get("taskId") or task_args.get("task_id"), "taskId")
        question_id = _require_string(task_args.get("questionId") or task_args.get("question_id"), "questionId")
        result = await _skip_task_question(task_id, question_id, task_args.get("reason"))
        await emit_task_update(task_id)
        return result

    return {
        "readTaskEvents": _read_task_events,
        "readTaskQuestions": _read_task_questions,
        "sendTaskMessage": send_task_message,
        "answerTaskQuestion": answer_task_question,
        "skipTaskQuestion": skip_task_question,
    }
