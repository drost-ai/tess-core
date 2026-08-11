from __future__ import annotations


from ..daemon import connect_task_supervisor_daemon_client
from ..storage import (
    list_session_task_records,
    load_session,
    load_task_record,
)
from .common import (
    _compact_tokens,
    _error,
    _handled,
    _info,
    _maybe_await,
    _persist,
    _resolve_task_record,
    _short_id,
    _split_subcommand,
    squash_whitespace,
    truncate_ascii,
)
from typing import Any


async def build_tasks_report(session: dict[str, Any], arg_text: str = "", args: dict[str, Any] | None = None) -> str:
    records = await list_session_task_records(session.get("id"))
    parts = arg_text.split()
    if not parts:
        return format_task_list_report(records)
    if parts[0] == "inspect" and len(parts) > 1:
        record = _resolve_task_record(records, parts[1]) or await load_task_record(parts[1])
        return format_task_inspect_report(record) if record else f"Task not found: {parts[1]}"
    if parts[0] == "result" and len(parts) > 1:
        record = _resolve_task_record(records, parts[1]) or await load_task_record(parts[1])
        if not record:
            return f"Task not found: {parts[1]}"
        output, truncated = await _read_task_output(record, args)
        return format_task_result_report(record, output, truncated)
    if parts[0] == "log" and len(parts) > 1:
        record = _resolve_task_record(records, parts[1]) or {"id": parts[1], "title": parts[1]}
        output, truncated = await _read_task_output(record, args)
        return "\n".join(
            [
                f"{record.get('id')} - {record.get('title')}",
                "",
                *(["[truncated to recent output]"] if truncated else []),
                output or "(no output yet)",
            ]
        )
    return "Usage: /tasks [inspect|log|result|attach|stop|retry <id>]"


async def _handle_tasks(arg_text: str, session: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    records = await list_session_task_records(session.get("id"))
    if not arg_text:
        return _info(hooks, "Tasks", format_task_list_report(records))
    raw_subcommand, query = _split_subcommand(arg_text)
    subcommand = raw_subcommand.strip().lower()
    if subcommand not in {"inspect", "log", "result", "attach", "stop", "retry"} or not query:
        return _error(hooks, "Usage: /tasks [inspect|log|result|attach|stop|retry <id>]")
    record = _resolve_task_record(records, query)
    if not record:
        return _error(hooks, f"No task matched: {query}")
    if subcommand == "inspect":
        return _info(hooks, "Task", format_task_inspect_report(record))
    if subcommand == "attach":
        if not record.get("attachableSessionId"):
            return _error(hooks, f"Task {record.get('id')} is retrieve-only and cannot be attached.")
        request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
        if not callable(request_resume):
            return _error(hooks, "/tasks attach is only available in the full-screen TUI.")
        target_session = await load_session(record["attachableSessionId"])
        if not target_session:
            return _error(hooks, f"Attach target not found: {record['attachableSessionId']}")
        if target_session.get("id") == session.get("id"):
            return _info(hooks, "Task", f"Task {record.get('id')} belongs to the current session. Use /tasks result {record.get('id')} or keep watching here.")
        await _persist(hooks)
        request_resume(target_session)
        return _handled(f"resuming {target_session.get('id')}")
    if subcommand in {"log", "result"}:
        output, truncated = await _read_task_output(record, args)
        if subcommand == "result":
            return _info(hooks, "Task Result", format_task_result_report(record, output, truncated))
        return _info(
            hooks,
            "Task Log",
            "\n".join(
                [
                    f"{record.get('id')} - {record.get('title')}",
                    "",
                    *(["[truncated to recent output]"] if truncated else []),
                    output or "(no output yet)",
                ]
            ),
        )
    supervisor, should_close = await _resolve_task_supervisor(args)
    if not supervisor:
        return _error(hooks, "This task action requires the local task supervisor daemon.")
    try:
        if subcommand == "stop":
            stop_fn = supervisor.get("stopTask") if isinstance(supervisor, dict) else None
            if not callable(stop_fn):
                return _error(hooks, "Task supervisor does not support stopTask.")
            stopped = await _maybe_await(stop_fn(record["id"]))
            if not stopped:
                return _error(hooks, f"Task could not be stopped: {record['id']}")
            return _info(hooks, "Task", f"Stopped {record['id']}")
        retry_fn = supervisor.get("retryTask") if isinstance(supervisor, dict) else None
        if not callable(retry_fn):
            return _error(hooks, "Task supervisor does not support retryTask.")
        try:
            retried = await _maybe_await(retry_fn(record["id"]))
        except Exception as exc:
            return _error(hooks, str(exc))
        return _info(
            hooks,
            "Task",
            "\n".join(
                [
                    f"Retried {record['id']}",
                    f"new task     {(retried or {}).get('taskId') or (retried or {}).get('id')}",
                    *([f"output       {retried.get('outputFile')}"] if isinstance(retried, dict) and retried.get("outputFile") else []),
                ]
            ),
        )
    finally:
        await _close_task_supervisor_if_needed(supervisor, should_close)


async def _read_task_output(record: dict[str, Any], args: dict[str, Any] | None = None) -> tuple[str, bool]:
    supervisor, should_close = await _resolve_task_supervisor(args or {})
    try:
        read_output = supervisor.get("read_task_output") if isinstance(supervisor, dict) else None
        if callable(read_output):
            value = await _maybe_await(read_output(record.get("id"), 12000))
            if isinstance(value, dict):
                return str(value.get("output") or ""), bool(value.get("truncated"))
            return str(value or ""), False
    finally:
        await _close_task_supervisor_if_needed(supervisor, should_close)
    output_file = record.get("outputFile")
    if output_file:
        try:
            with open(output_file, "r", encoding="utf-8") as handle:
                return handle.read(), False
        except OSError:
            return "", False
    return "", False


def format_task_list_report(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No runtime tasks for this session."
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    summary = " - ".join(f"{status} {counts[status]}" for status in sorted(counts))
    lines = [f"summary      {summary}", ""]
    for record in records[:12]:
        lines.append(
            "\n".join(
                [
                    f"{str(record.get('id') or '').ljust(20)} {str(record.get('status') or '').ljust(12)} {record.get('kind')}",
                    f"title        {record.get('title')}",
                    *([f"family       {record.get('taskFamily')}"] if record.get("taskFamily") else []),
                    *([f"detail       {record.get('detailStatus')}"] if record.get("detailStatus") else []),
                    *([f"summary      {record.get('summary')}"] if record.get("summary") else []),
                    *([f"result       {record.get('resultSummary')}"] if record.get("resultSummary") else []),
                    f"surface      {format_task_surface(record)}",
                    *([f"owner        {record.get('owner')}"] if record.get("owner") else []),
                    f"updated      {record.get('updatedAt')}",
                ]
            )
        )
    lines.extend(["", "tip          /tasks inspect <id> - /tasks result <id> - /tasks attach <id> - /tasks log <id>"])
    return "\n".join(lines)


def format_task_result_report(record: dict[str, Any], output: str = "", truncated: bool = False) -> str:
    lines = [
        f"{record.get('id')} - {record.get('title')}",
        f"status       {record.get('status')}",
    ]
    if record.get("detailStatus"):
        lines.append(f"detail       {record.get('detailStatus')}")
    if record.get("resultSummary"):
        lines.append(f"result       {record.get('resultSummary')}")
    if record.get("summary") and record.get("summary") != record.get("resultSummary"):
        lines.append(f"summary      {record.get('summary')}")
    lines.append(f"surface      {format_task_surface(record)}")
    if record.get("attachableSessionId"):
        lines.append(f"attach       /tasks attach {record.get('id')}")
    if record.get("outputFile"):
        lines.append(f"output       {record.get('outputFile')}")
    if output.strip():
        lines.append("")
        if truncated:
            lines.append("[truncated to recent output]")
        lines.append(output.strip())
    return "\n".join(lines)


def format_task_inspect_report(record: dict[str, Any]) -> str:
    lines = [
        f"id           {record.get('id')}",
        f"kind         {record.get('kind')}",
        f"status       {record.get('status')}",
        f"title        {record.get('title')}",
    ]
    for label, key in [
        ("family", "taskFamily"),
        ("detail", "detailStatus"),
        ("summary", "summary"),
        ("result", "resultSummary"),
    ]:
        if record.get(key):
            lines.append(f"{label:<12} {record.get(key)}")
    lines.append(f"surface      {format_task_surface(record)}")
    if record.get("attachableSessionId"):
        lines.append(f"attach       /tasks attach {record.get('id')}")
    for label, key in [
        ("owner", "owner"),
        ("supervisor", "supervisorId"),
        ("cwd", "cwd"),
        ("command", "command"),
        ("output", "outputFile"),
    ]:
        if record.get(key):
            lines.append(f"{label:<12} {record.get(key)}")
    if isinstance(record.get("attachmentCount"), int):
        lines.append(f"attachments  {record.get('attachmentCount')}")
    if isinstance(record.get("outputCursor"), int):
        lines.append(f"output bytes {record.get('outputCursor')}")
    if record.get("relatedMonitorIds"):
        lines.append(f"monitors     {', '.join(record.get('relatedMonitorIds') or [])}")
    if record.get("relatedChildSessionIds"):
        lines.append(f"children     {', '.join(record.get('relatedChildSessionIds') or [])}")
    lines.append(f"updated      {record.get('updatedAt')}")
    return "\n".join(lines)


def format_task_surface(record: dict[str, Any]) -> str:
    if record.get("attachableSessionId"):
        return f"attachable via session {_short_id(record.get('attachableSessionId', ''))}"
    if record.get("retrieveOnly"):
        return "retrieve-only"
    return "inspectable"


async def _resolve_task_supervisor(args: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, bool]:
    args = args or {}
    supervisor = args.get("daemon") or args.get("taskSupervisor")
    if supervisor:
        return supervisor, False
    instance_id = _request_runtime_instance_id(args)
    if not instance_id:
        return None, False
    supervisor = await connect_task_supervisor_daemon_client({"instanceId": instance_id})
    should_close = bool(isinstance(supervisor, dict) and supervisor.get("_daemonClient"))
    return supervisor, should_close


def _request_runtime_instance_id(args: dict[str, Any]) -> str | None:
    runtime = args.get("runtime") if isinstance(args.get("runtime"), dict) else {}
    instance_id = args.get("runtimeInstanceId") or runtime.get("runtimeInstanceId")
    return instance_id if isinstance(instance_id, str) and instance_id else None


async def _close_task_supervisor_if_needed(supervisor: dict[str, Any] | None, should_close: bool) -> None:
    if not should_close or not isinstance(supervisor, dict):
        return
    close = supervisor.get("close")
    if callable(close):
        await _maybe_await(close())
