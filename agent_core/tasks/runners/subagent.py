from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ...agent import runAgentPrompt
from ...authority.agent import assertMatchingPersistedChildAuthorities, enforceSessionAgentAuthority
from ...runtime.agent_bindings import createAgentRuntimeBindings
from ...config import resolveConfig
from ...storage import configureRuntimeHome, loadChildAgent, loadSession, loadTaskRecord, mutateChildAgent, mutateTaskMeta, saveSession, saveTaskRecord, withRecordTransaction
from ..communication import append_task_event, read_undelivered_task_messages, send_task_message
from ...subagents import finalizeBackgroundSubagentFailureFromEnv, finalizeBackgroundSubagentSuccessFromEnv
from ...subagents.finalizers import _background_structured_result_fields
from ...tools import createBuiltinTools
from ...runtime.generation import createRuntimeSessionBinding
from ..._utils import now_iso


async def run_background_subagent(args: dict[str, Any]) -> dict[str, Any]:
    session, result, payload = await _execute_background_subagent(args)
    await finalizeBackgroundSubagentSuccessFromEnv(session, result)
    await saveSession(session)
    return payload


def _create_trace_emitter(task_id: str):
    """Compact activity trace for live visibility: appended to the task's
    events.jsonl, forwarded by the daemon as subagent.trace events."""

    def emit(event: str, data: dict[str, Any]) -> None:
        if event == "tool.started":
            record = {"tool": data.get("name"), "input_preview": _preview(data.get("input"), 160)}
        elif event == "tool.completed":
            record = {"tool": data.get("name"), "error": bool(data.get("error")), "output_chars": len(str(data.get("output") or ""))}
        elif event == "message.assistant":
            record = {"text_preview": _preview(data.get("text"), 200)}
        else:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(append_task_event(task_id, {"type": "trace", "event": event, **record}))

    return emit


def _preview(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text[:limit]


async def _prepare_worker(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Any]:
    runtime_home = os.environ.get("AGENT_CORE_HOME")
    if runtime_home:
        configureRuntimeHome(runtime_home)
    session = await loadSession(args["sessionId"])
    if not session:
        raise RuntimeError(f"Subagent session not found: {args['sessionId']}")
    child_record = await loadChildAgent(args["agentId"])
    assertMatchingPersistedChildAuthorities(
        (session.get("metadata") or {}).get("effectiveAgentAuthority"),
        (child_record or {}).get("effectiveAgentAuthority"),
    )
    config = await resolveConfig(_session_config_options(session, runtime_home), session["cwd"])
    _apply_session_overrides(config, session)
    await enforceSessionAgentAuthority(session=session, config=config)
    runtime = createAgentRuntimeBindings({
        "emitEvent": _create_trace_emitter(args["taskId"]),
        "tools": createBuiltinTools(),
        "daemon": {"state": "unavailable", "reason": {"code": "subagent.daemon_unavailable", "message": "No task daemon is attached to this background subagent runner."}},
        "refreshMcpToolRegistry": lambda: None,
        "sessionBinding": createRuntimeSessionBinding(session["id"], 1, 1),
        "questionPolicy": {"interactive": False, "askQuestion": lambda *_args: ""},
    })
    return session, config, runtime


async def _execute_background_subagent(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    session, config, runtime = await _prepare_worker(args)
    result = await runAgentPrompt({"session": session, "config": config, "prompt": "", "runtime": runtime})
    payload = {"ok": True, "session_id": session["id"], "agent_id": args["agentId"], "task_id": args["taskId"], "result": {"text": result.get("text"), "stopReason": result.get("stopReason"), "toolCount": result.get("toolCount"), "usage": result.get("usage")}}
    return session, result, payload


async def _run_resident_subagent(args: dict[str, Any]) -> None:
    """Resident child: run the initial turn, then live on the mailbox.

    Each turn's result is delivered to the parent as a result-kind
    to_parent message (the daemon mailbox watch turns it into a parent
    wake), while the task record stays non-terminal: awaiting_input while
    idle, running during turns. The process exits only when stopped or
    killed — the supervisor's stop machinery and the containment ladder
    own termination, so no terminal finalizer runs here.
    """
    # Call-time import: the config package imports tasks at module load.
    from ...config._knobs import knobValue

    session, config, runtime = await _prepare_worker(args)
    task_id, agent_id = args["taskId"], args["agentId"]
    await _mark_resident_status(task_id, agent_id, "running", None)
    result = await runAgentPrompt({"session": session, "config": config, "prompt": "", "runtime": runtime})
    await saveSession(session)
    await _publish_resident_turn_result(task_id, agent_id, session, result, trigger_ids=[])
    turn_index = 1
    print(json.dumps({"ok": True, "resident": True, "turn": turn_index, "session_id": session["id"], "agent_id": agent_id, "task_id": task_id, "result": {"text": result.get("text"), "stopReason": result.get("stopReason")}}), flush=True)
    while True:
        await asyncio.sleep(int(knobValue("subagents.resident_poll_ms")) / 1000)
        queued = [
            message
            for message in await read_undelivered_task_messages(task_id, session["id"])
            if message.get("direction") in (None, "to_child")
        ]
        if not queued:
            continue
        trigger_ids = [str(message.get("messageId")) for message in queued if message.get("messageId")]
        await _mark_resident_status(task_id, agent_id, "running", None)
        try:
            # The turn's boundary drain claims and injects the queued
            # messages; an empty prompt makes them the turn's substance.
            result = await runAgentPrompt({"session": session, "config": config, "prompt": "", "runtime": runtime})
        except Exception as exc:  # stay resident: the parent decides what happens next
            await send_task_message(task_id, {"direction": "to_parent", "sender": "child", "kind": "result", "content": f"[turn failed] {exc}", "metadata": _reply_metadata(trigger_ids)})
            await _mark_resident_status(task_id, agent_id, "awaiting_input", "resident_idle")
            continue
        await saveSession(session)
        await _publish_resident_turn_result(task_id, agent_id, session, result, trigger_ids=trigger_ids)
        turn_index += 1
        print(json.dumps({"ok": True, "resident": True, "turn": turn_index, "result": {"text": (result.get("text") or "")[:400], "stopReason": result.get("stopReason")}}), flush=True)


def _reply_metadata(trigger_ids: list[str]) -> dict[str, Any]:
    """Correlate a turn's result with the messages that triggered it, so a
    parent blocked in SendMessage(expects_reply) is released by it."""
    if not trigger_ids:
        return {}
    return {"replyTo": trigger_ids[-1], "repliesTo": trigger_ids}


async def _publish_resident_turn_result(task_id: str, agent_id: str, session: dict[str, Any], result: dict[str, Any], *, trigger_ids: list[str]) -> None:
    text = (result.get("text") or "").strip() or "(no text)"
    summary = text[:280]
    now = now_iso()
    await send_task_message(task_id, {"direction": "to_parent", "sender": "child", "kind": "result", "content": text, "metadata": _reply_metadata(trigger_ids)})
    # Structured results are part of the advertised contract in resident mode
    # too (the default): evaluate the latest turn's text against the child's
    # responseSchema, not only in the one-shot finalizer.
    structured_fields = await _background_structured_result_fields(task_id, session, text)

    def _idle_child(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if not current:
            return current
        return {
            **current,
            "status": "awaiting_input",
            "waitingReason": "resident_idle",
            "completedAt": None,
            "completionReason": None,
            "resultSummary": summary,
            "latestResultAt": now,
            "stoppable": True,
            "retryable": False,
            "lastUpdatedAt": now,
            "lastSummary": summary,
            **structured_fields,
        }

    await mutateChildAgent(agent_id, _idle_child)
    await _mark_resident_status(task_id, agent_id, "awaiting_input", "resident_idle", summary=summary)


_TERMINAL_RESIDENT_META_STATUSES = {"completed", "failed", "terminated", "stopped", "cancelled", "truncated"}


async def _mark_resident_status(task_id: str, agent_id: str, status: str, waiting_reason: str | None, summary: str | None = None) -> None:
    now = now_iso()

    def _mark(meta: dict[str, Any] | None) -> dict[str, Any] | None:
        # First-terminal-wins applies to the meta too: a stop or failure
        # committed between this turn's read and write must not be revived
        # into awaiting_input by the resident's idle stamp.
        if not meta or meta.get("status") in _TERMINAL_RESIDENT_META_STATUSES:
            return meta
        next_meta = {**meta, "status": status, "waitingReason": waiting_reason, "updatedAt": now, "stoppable": True}
        next_meta.pop("completedAt", None)
        next_meta.pop("finishedAt", None)
        if summary is not None:
            next_meta["resultSummary"] = summary
            next_meta["summary"] = summary
        return next_meta

    await mutateTaskMeta(task_id, _mark)
    async with withRecordTransaction("task-lifecycle", task_id):
        record = await loadTaskRecord(task_id)
        if record and record.get("status") not in {"completed", "failed", "stopped"}:
            # Keep the shared record store aligned so capacity governance and
            # the boot sweep see the resident's real state; terminal records are
            # first-terminal-wins and never regress here.
            next_record = {
                **record,
                "status": status,
                "detailStatus": waiting_reason or status,
                "updatedAt": now,
            }
            if summary is not None:
                next_record["summary"] = summary
                next_record["resultSummary"] = summary
            next_record.pop("completedAt", None)
            await saveTaskRecord(next_record)


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-core Python background subagent runner")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--task-id", required=True)
    parsed = parser.parse_args()
    worker_args = {"sessionId": parsed.session_id, "agentId": parsed.agent_id, "taskId": parsed.task_id}
    try:
        if _resident_enabled_for(parsed.session_id):
            asyncio.run(_run_resident_subagent(worker_args))
            return
        session, result, payload = asyncio.run(_execute_background_subagent(worker_args))
        print(json.dumps(payload, indent=2), flush=True)
        asyncio.run(finalizeBackgroundSubagentSuccessFromEnv(session, result))
        asyncio.run(saveSession(session))
    except Exception as exc:
        asyncio.run(finalizeBackgroundSubagentFailureFromEnv(exc))
        print(json.dumps({"ok": False, "error": str(exc), "session_id": parsed.session_id, "agent_id": parsed.agent_id, "task_id": parsed.task_id}, indent=2), file=sys.stderr)
        raise


def _resident_enabled_for(session_id: str) -> bool:
    runtime_home = os.environ.get("AGENT_CORE_HOME")
    if runtime_home:
        configureRuntimeHome(runtime_home)

    async def read() -> bool:
        session = await loadSession(session_id)
        contract = ((session or {}).get("metadata") or {}).get("subagentRuntimeContract")
        if isinstance(contract, dict) and contract.get("resident") is False:
            return False
        return True

    return asyncio.run(read())


def _apply_session_overrides(config: dict[str, Any], session: dict[str, Any]) -> None:
    for key in ("provider", "providerProfileName", "model", "effort", "permissionMode", "systemPrompt"):
        if session.get(key) is not None:
            config[key] = session[key]
    config["providerKind"] = config.get("provider")


def _session_config_options(session: dict[str, Any], runtime_home: str | None) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    settings = (metadata.get("agentCoreSettingsPath") or metadata.get("settingsPath")) if isinstance(metadata, dict) else None
    options = {
        "runtimeHome": runtime_home,
        "provider": session.get("providerProfileName") or session.get("provider"),
        "model": session.get("model"),
        "settingSources": ",".join(
            ((metadata.get("effectiveAgentAuthority") or {}).get("executableSources") or {}).get("settingsSources")
            or ["user"]
        ),
    }
    if settings:
        options["settings"] = settings
    return options


if __name__ == "__main__":
    main()
