from __future__ import annotations

import os
from .._utils import now_iso
from ..context.compaction import run_compaction
from ..context.durable_memory import synchronize_manual_project_memory
from ..storage import (
    compaction_checkpoint_path,
    compaction_manifest_path,
    get_project_memory_path,
    list_session_monitors,
    list_sessions,
    load_compaction_manifest,
    load_session,
)
from ..tasks.monitor_transactions import commit_monitor_mutation as _commit_monitor_mutation
from ..tasks.monitor_transitions import (
    monitor_resume_note as _monitor_resume_note,
    monitor_stop_note as _monitor_stop_note,
    resume_monitor_record as _resume_monitor_record,
    stop_monitor_record as _stop_monitor_record,
)
from .common import (
    _call,
    _call_value,
    _emit_runtime,
    _error,
    _handled,
    _info,
    _persist,
    _resolve_monitor,
    _session_matches,
    _split_subcommand,
)
from .common import format_monitor_record
from .diagnostics import _handle_debug, format_session_candidate
from typing import Any


async def build_monitors_report(session: dict[str, Any]) -> str:
    monitors = await list_session_monitors(session.get("id"))
    if not monitors:
        return "No background monitors are active for this session."
    return "\n\n".join(format_monitor_record(monitor) for monitor in monitors[:12])


async def _handle_memory(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    cwd = session.get("cwd") or os.getcwd()
    memory_path = get_project_memory_path(cwd)
    command, payload = _split_subcommand(arg_text)
    if not command or command == "show":
        records = await synchronize_manual_project_memory(cwd)
        try:
            with open(memory_path, "r", encoding="utf-8") as handle:
                content = handle.read().strip()
        except FileNotFoundError:
            content = ""
        return _info(hooks, "Memory", "\n".join([f"path     {memory_path}", f"records  {len(records)}", "", content or "No memory stored for this project yet."]))
    if command == "clear":
        try:
            os.remove(memory_path)
        except FileNotFoundError:
            pass
        records = await synchronize_manual_project_memory(cwd)
        return _info(hooks, "Memory", f"Cleared project memory at {memory_path}\nrecords  {len(records)}")
    if command not in {"set", "append"} or not payload:
        return _error(hooks, "Usage: /memory [show|append TEXT|set TEXT|clear]")
    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
    if command == "set":
        next_content = f"{payload}\n"
    else:
        try:
            with open(memory_path, "r", encoding="utf-8") as handle:
                previous = handle.read().strip()
        except FileNotFoundError:
            previous = ""
        next_content = f"{previous}\n{payload}\n" if previous else f"{payload}\n"
    with open(memory_path, "w", encoding="utf-8") as handle:
        handle.write(next_content)
    records = await synchronize_manual_project_memory(cwd)
    verb = "Replaced" if command == "set" else "Appended to"
    return _info(hooks, "Memory", f"{verb} project memory at {memory_path}\nrecords  {len(records)}")


def _handle_extensions(hooks: dict[str, Any]) -> dict[str, Any]:
    report = _call_value(hooks, "getExtensionsReport")
    if report is None:
        return _error(hooks, "/extensions is not available in this runtime.")
    return _info(hooks, "Extensions", str(report))


async def _handle_monitor(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    parts = arg_text.split(None, 1)
    if len(parts) != 2 or parts[0] not in {"stop", "resume"}:
        return _error(hooks, "Usage: /monitor <stop|resume> <monitor-id>")
    monitors = await list_session_monitors(session.get("id"))
    monitor = _resolve_monitor(monitors, parts[1])
    if not monitor:
        return _error(hooks, f"No monitor matched: {parts[1]}")
    if parts[0] == "stop":
        committed = await _commit_monitor_mutation(
            monitorId=monitor["id"],
            expectedSessionId=session["id"],
            mutate=lambda latest: _slash_monitor_mutation(latest, "stop"),
        )
        verb = "Stopped"
    else:
        committed = await _commit_monitor_mutation(
            monitorId=monitor["id"],
            expectedSessionId=session["id"],
            mutate=lambda latest: _slash_monitor_mutation(latest, "resume"),
        )
        verb = "Resumed"
    transition = {
        **committed["value"],
        "record": committed["current"],
        "previousStatus": committed["previous"]["status"],
        "changed": committed["changed"],
    }
    if parts[0] == "stop":
        note = _monitor_stop_note(transition)
    else:
        note = _monitor_resume_note(transition)
    record = transition["record"]
    if transition["changed"]:
        _call(hooks, "refresh")
        await _persist(hooks)
        return _info(hooks, "Monitor", f"{verb} monitor {record['id']}")
    return _info(hooks, "Monitor", note or f"Monitor {record['id']} unchanged.")


def _slash_monitor_mutation(latest: dict[str, Any], operation: str) -> dict[str, Any]:
    transition = (
        _stop_monitor_record(latest, now_iso())
        if operation == "stop"
        else _resume_monitor_record(latest, now_iso())
    )
    return {
        "record": transition["record"],
        "changed": transition["changed"],
        "transition": transition["previousStatus"] != transition["record"]["status"],
        "transitionLabel": (
            f"{operation}:{transition['previousStatus']}"
            f"->{transition['record']['status']}"
        ),
        "value": transition,
    }


async def _handle_resume(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    candidates = [candidate for candidate in await list_sessions() if candidate.get("cwd") == session.get("cwd") and candidate.get("id") != session.get("id")]
    if not arg_text:
        if not candidates:
            return _info(hooks, "Resume", "No other saved sessions found for this workspace.")
        return _info(hooks, "Resume", "\n".join(format_session_candidate(candidate) for candidate in candidates[:10]))
    matches = [candidate for candidate in candidates if _session_matches(candidate, arg_text)]
    if not matches:
        return _error(hooks, f"No saved session in this workspace matched: {arg_text}")
    if len(matches) > 1:
        return _info(hooks, "Resume", "\n".join(format_session_candidate(candidate) for candidate in matches[:10]))
    request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
    if not callable(request_resume):
        return _error(hooks, "/resume switching is only available in the full-screen TUI.")
    await _persist(hooks)
    selected = await load_session(matches[0]["id"]) or matches[0]
    request_resume(selected)
    return _handled(f"resuming {selected.get('id')}")


async def _handle_compact(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    subcommand, payload = _split_subcommand(arg_text)
    if subcommand in {"status", "artifacts", "state"}:
        return _info(hooks, "Compact", await build_compaction_status_report(session))
    if subcommand == "debug":
        return await _handle_debug(payload, session, config, hooks, title="Compact Debug")
    if subcommand and subcommand not in {"run", "force"}:
        return _error(hooks, "Usage: /compact [status|debug]")
    if _call_value(hooks, "isCompacting"):
        return _info(hooks, "Compact", "Compaction is already running.")
    compaction_state = (session.get("metadata") or {}).get("compactionState") or {}
    token_measurement = compaction_state.get("workingContextTokenMeasurement") or {}
    _emit_runtime(
        hooks,
        "compaction.started",
        {
            "session_id": session.get("id"),
            "trigger": "manual",
            "current_token_estimate": compaction_state.get("workingContextTokenEstimate"),
            "current_token_source": token_measurement.get("source"),
        },
    )
    runner = hooks.get("run_compaction") if isinstance(hooks, dict) else None
    runner = runner if callable(runner) else run_compaction
    payload = {"session": session, "config": config, "trigger": "manual", "force": True}
    if args.get("modelClient") is not None:
        payload["modelClient"] = args["modelClient"]
    try:
        outcome = runner(payload)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
    except Exception as exc:
        provider_failure = getattr(exc, "record", None)
        provider_failure = provider_failure if isinstance(provider_failure, dict) else {}
        _emit_runtime(
            hooks,
            "compaction.failed",
            {
                "session_id": session.get("id"),
                "trigger": "manual",
                "current_token_estimate": compaction_state.get("workingContextTokenEstimate"),
                "current_token_source": token_measurement.get("source"),
                "error": provider_failure.get("message") or str(exc),
                "provider": provider_failure.get("provider"),
                "provider_label": provider_failure.get("providerLabel"),
                "category": provider_failure.get("category"),
                "phase": provider_failure.get("phase"),
                "retryable": provider_failure.get("retryable"),
                "attempt": provider_failure.get("attempt"),
                "status": provider_failure.get("status"),
                "code": provider_failure.get("code"),
                "error_type": provider_failure.get("errorType"),
                "raw_error": provider_failure.get("rawMessage"),
            },
        )
        return _error(hooks, str(exc))
    if outcome.get("status") == "skipped":
        _emit_runtime(
            hooks,
            "compaction.skipped",
            {
                "session_id": session.get("id"),
                "trigger": "manual",
                "current_token_estimate": compaction_state.get("workingContextTokenEstimate"),
                "current_token_source": token_measurement.get("source"),
                "reason": outcome.get("reason"),
                "detail": outcome.get("detail"),
                "previous_cutoff": outcome.get("previousCutoff"),
                "candidate_cutoff": outcome.get("candidateCutoff"),
                "quality_score": outcome.get("qualityScore"),
                "quality_classification": outcome.get("qualityClassification"),
            },
        )
        lines = ["Checkpoint not created.", f"reason          {outcome.get('reason')}", f"detail          {outcome.get('detail')}"]
        if compaction_state:
            artifacts = compaction_state.get("artifacts") or {}
            lines.extend(
                [
                    f"latest gen      {compaction_state.get('latestCompactionGeneration')}",
                    f"summary         {artifacts.get('summary')}",
                    f"working ctx     {artifacts.get('workingContext')}",
                ]
            )
        return _info(hooks, "Compact", "\n".join(lines))
    _call(hooks, "refresh")
    await _persist(hooks)
    artifacts = outcome.get("artifacts") or {}
    compaction_model = outcome.get("compactionModel") or {}
    verification_model = outcome.get("verificationModel") or {}
    verification = outcome.get("verification") or {}
    working_measurement = outcome.get("workingContextTokenMeasurement") or {}
    _emit_runtime(
        hooks,
        "compaction.completed",
        {
            "session_id": session.get("id"),
            "generation": outcome.get("generation"),
            "trigger": outcome.get("trigger"),
            "compacted_through_message_index": outcome.get("compactedThroughMessageIndex"),
            "compacted_message_count": outcome.get("compactedMessageCount"),
            "recent_message_count": outcome.get("recentMessageCount"),
            "raw_token_estimate": outcome.get("rawTokenEstimate"),
            "working_context_token_estimate": outcome.get("workingContextTokenEstimate"),
            "working_context_token_source": working_measurement.get("source"),
            "quality_classification": outcome.get("qualityClassification"),
            "compaction_provider": compaction_model.get("provider"),
            "compaction_model": compaction_model.get("model"),
            "verification_provider": verification_model.get("provider"),
            "verification_model": verification_model.get("model"),
            "verification_verdict": verification.get("verdict"),
            "verification_confidence": verification.get("confidence"),
            "summary_path": artifacts.get("summary"),
            "state_path": artifacts.get("state"),
            "working_context_path": artifacts.get("workingContext"),
            "checkpoint_path": artifacts.get("checkpoint"),
        },
    )
    return _info(
        hooks,
        "Compact",
        "\n".join(
            [
                "Compacted working context.",
                "Archive preserved.",
                f"generation      {outcome.get('generation')}",
                f"compacted msgs  {outcome.get('compactedMessageCount')}",
                f"recent msgs     {outcome.get('recentMessageCount')}",
                f"working ctx     {outcome.get('workingContextTokenEstimate')}",
                f"quality         {outcome.get('qualityClassification')}",
                f"summary         {artifacts.get('summary', 'n/a')}",
                f"state           {artifacts.get('state', 'n/a')}",
                f"working ctx     {artifacts.get('workingContext', 'n/a')}",
                f"checkpoint      {artifacts.get('checkpoint', 'n/a')}",
                "Future prompts will use the compacted artifacts plus recent raw turns.",
            ]
        ),
    )


async def build_compaction_status_report(session: dict[str, Any]) -> str:
    metadata = session.get("metadata") or {}
    state = metadata.get("compactionState") or {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    measurement = state.get("workingContextTokenMeasurement") or {}
    manifest = await load_compaction_manifest(session.get("id")) if session.get("id") else None
    generation = state.get("latestCompactionGeneration")
    lines = [
        f"session        {session.get('id') or 'n/a'}",
        f"compactions    {metadata.get('compactions', 0)}",
        f"latest gen     {generation if generation is not None else 'n/a'}",
        f"working ctx    {state.get('workingContextTokenEstimate', 'n/a')}",
        f"ctx source     {measurement.get('source', 'n/a')}",
        f"compacted to   {state.get('compactedThroughMessageIndex', 'n/a')}",
        f"manifest       {compaction_manifest_path(session.get('id')) if session.get('id') and manifest else 'none'}",
    ]
    if manifest:
        lines.extend(
            [
                f"manifest gen   {manifest.get('latestGeneration') or manifest.get('generation') or 'n/a'}",
                f"manifest ok    {manifest.get('status') or manifest.get('qualityClassification') or 'n/a'}",
            ]
        )
    for label, key in [
        ("summary", "summary"),
        ("state", "state"),
        ("working ctx", "workingContext"),
        ("activity", "activity"),
        ("checkpoint", "checkpoint"),
    ]:
        if artifacts.get(key):
            lines.append(f"{label:<14} {artifacts.get(key)}")
    if session.get("id") and isinstance(generation, int):
        lines.append(f"checkpoint src {compaction_checkpoint_path(session['id'], generation)}")
    if not state and not manifest:
        lines.append("No compaction checkpoint recorded for this session.")
    return "\n".join(lines)
