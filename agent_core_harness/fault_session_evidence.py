from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import agent_core as core

from .runtime_bindings import bind_agent_runtime


async def verifyFaultSessionIntegrity(
    cell: dict[str, Any], actual: dict[str, Any], root: Path
) -> tuple[str, dict[str, Any], list[str]]:
    """Settle an adapter verdict through the real agent loop and reload it."""

    provider = (
        "anthropic"
        if cell["providerFamily"].startswith("anthropic")
        else "openai"
    )
    session = core.createEmptySession(
        cwd=str(root),
        provider=provider,
        model="claude-sonnet-4-6" if provider == "anthropic" else "gpt-5.4",
        systemPrompt="fault matrix loop settlement",
        permissionMode="bypassPermissions",
        sessionId="fault-" + hashlib.sha256(cell["id"].encode()).hexdigest()[:24],
    )
    events: list[dict[str, Any]] = []
    signal: dict[str, Any] = {"aborted": False, "reason": None}
    runtime = bind_agent_runtime(
        session,
        emitEvent=lambda event, data: events.append(
            {"event": event, "data": copy.deepcopy(data)}
        ),
        abortSignal=signal,
    )
    config = _loop_config(root)

    async def send(_request: dict[str, Any]) -> dict[str, Any]:
        classification = actual["classification"]
        if classification == "success":
            return {
                "model": session["model"],
                "stopReason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 2},
                "content": [{"type": "text", "text": "terminal"}],
            }
        if classification == "cancelled":
            error = RuntimeError("fault-matrix cancellation")
            error.name = "AbortError"  # type: ignore[attr-defined]
            signal.update({"aborted": True, "reason": error})
            raise error
        record = core.createProviderFailureRecord(
            provider=provider,
            providerLabel="Anthropic" if provider == "anthropic" else "OpenAI",
            phase="request_start" if cell["state"] in {"S0", "S1", "S2"} else "stream_read",
            category=classification,
            retryable=bool(actual.get("retryable")),
            attempt=1,
            hadAssistantOutput=bool(actual["hadAssistantOutput"]),
            hadToolUse=bool(actual["hadToolUse"]),
            rawMessage=f"fault-matrix {cell['id']}",
        )
        raise core.createProviderRequestError(record)

    raised: BaseException | None = None
    result: dict[str, Any] | None = None
    try:
        result = await core.runAgentPrompt(
            session=session,
            config=config,
            runtime=runtime,
            prompt="stable request",
            providerOverride={"send": send},
        )
    except BaseException as exc:
        raised = exc
    _assert_settlement(cell, actual, result, raised)

    restored = await core.loadSession(session["id"])
    if not isinstance(restored, dict):
        raise RuntimeError("Fault-matrix loop session did not reload")
    repair = core.repairSessionTranscript(restored)
    if repair["repaired"]:
        raise RuntimeError(f"Fault-matrix loop persisted an invalid transcript: {cell['id']}")
    expected_messages = 2 if actual["classification"] == "success" else 0 if actual["classification"] == "cancelled" else 1
    if len(restored.get("messages") or []) != expected_messages:
        raise RuntimeError(
            f"Fault-matrix loop message count diverged for {cell['id']}: "
            f"expected {expected_messages}, got {len(restored.get('messages') or [])}"
        )
    failure = (restored.get("metadata") or {}).get("lastProviderFailure")
    if actual["classification"] not in {"success", "cancelled"} and not isinstance(failure, dict):
        raise RuntimeError(f"Fault-matrix failure ledger missing for {cell['id']}")

    event_sequence = _contract_event_sequence(events, actual["classification"])
    archive_kinds = _archive_kinds(session["id"])
    revision = restored.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise RuntimeError(f"Fault-matrix session revision missing for {cell['id']}")
    if actual["classification"] not in {"success", "cancelled"} and "provider_failure" not in archive_kinds:
        raise RuntimeError(f"Fault-matrix failure archive missing for {cell['id']}")
    if actual["classification"] == "cancelled" and "repair" not in archive_kinds:
        raise RuntimeError(f"Fault-matrix cancellation repair missing for {cell['id']}")
    label = _integrity_label(cell, actual)
    evidence = {
        "settledThrough": "runAgentPrompt",
        "persisted": True,
        "repairApplied": repair["repaired"],
        "messageCount": len(restored.get("messages") or []),
        "revision": revision,
        "failureLedgerPresent": isinstance(failure, dict),
        "archiveKinds": archive_kinds,
        "runtimeEventNames": [item["event"] for item in events],
        "digest": hashlib.sha256(
            json.dumps(restored, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    return label, evidence, event_sequence


def _loop_config(root: Path) -> dict[str, Any]:
    profile = {
        "name": "fault-matrix-loop",
        "kind": "openai",
        "apiMode": "responses",
        "maxContextLength": 100_000,
        "maxOutputTokens": 4_096,
    }
    return {
        "cwd": str(root),
        "runtimeHome": core.getStateRoot(),
        "allowedRoots": [str(root)],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": profile["name"],
        "providerProfile": profile,
        "providerProfiles": {profile["name"]: profile},
        "openaiApiMode": "responses",
        "model": "gpt-5.4",
        "effort": "medium",
        "systemPrompt": "fault matrix loop settlement",
        "tools": [],
        "allowedTools": [],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": True,
    }


def _assert_settlement(
    cell: dict[str, Any],
    actual: dict[str, Any],
    result: dict[str, Any] | None,
    raised: BaseException | None,
) -> None:
    classification = actual["classification"]
    if classification == "success" and (raised is not None or not result):
        raise RuntimeError(f"Fault-matrix success did not settle: {cell['id']}")
    if classification == "cancelled":
        if raised is not None or (result or {}).get("stopReason") != "cancelled":
            raise RuntimeError(f"Fault-matrix cancellation did not roll back: {cell['id']}")
        return
    if classification not in {"success", "cancelled"}:
        record = getattr(raised, "record", None)
        if not isinstance(record, dict) or record.get("category") != classification:
            raise RuntimeError(
                f"Fault-matrix failure did not retain classification: {cell['id']} "
                f"raised={raised!r} record={record!r}"
            )


def _contract_event_sequence(
    events: list[dict[str, Any]], classification: str
) -> list[str]:
    names = [item["event"] for item in events]
    terminal = (
        "provider.response_completed"
        if classification == "success"
        else "turn.cancelled"
        if classification == "cancelled"
        else "provider.failed"
    )
    expected = ["provider.request_preflight", terminal]
    positions = [names.index(name) for name in expected if name in names]
    if len(positions) != len(expected) or positions != sorted(positions):
        raise RuntimeError(f"Fault-matrix runtime events missing or reordered: {expected}")
    return expected


def _archive_kinds(session_id: str) -> list[str]:
    path = Path(core.sessionTranscriptLogPath(session_id))
    if not path.exists():
        return []
    return [
        item.get("type")
        for item in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    ]


def _integrity_label(cell: dict[str, Any], actual: dict[str, Any]) -> str:
    if actual["classification"] == "success":
        return "terminal_committed_usage_optional"
    state_index = int(cell["state"][1:])
    if 4 <= state_index <= 6:
        return "no_orphaned_tool_use"
    if state_index >= 3:
        return "partial_output_not_injected_canonical_no_retry"
    return "no_partial_output_revision_consistent"


verify_fault_session_integrity = verifyFaultSessionIntegrity
