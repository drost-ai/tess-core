from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import agent_core as core


_CRASH_CHILD = """
import asyncio, json, os, signal, sys
import agent_core as core
home, session_id, provider, model, state = sys.argv[1:]
core.configureRuntimeHome(home)
async def main():
    session = core.createEmptySession(cwd=home, provider=provider, model=model, systemPrompt='crash', permissionMode='default', sessionId=session_id)
    session['messages'] = [{'role': 'user', 'content': 'stable before crash'}]
    await core.persistSession(session, {'sessionPersistence': True})
    state_index = int(state[1:])
    if state_index >= 3:
        content = (
            [{'type': 'tool_use', 'id': 'volatile-tool', 'name': 'Read', 'input': {}}]
            if 4 <= state_index <= 6
            else [{'type': 'text', 'text': 'volatile partial output'}]
        )
        session['messages'].append({'role': 'assistant', 'content': content})
    print(json.dumps({
        'state': state,
        'volatileMessageCount': len(session['messages']),
        'volatileHadAssistantOutput': state_index >= 3,
        'volatileHadToolUse': 4 <= state_index <= 6,
    }), flush=True)
asyncio.run(main())
os.kill(os.getpid(), signal.SIGKILL)
"""


async def executeCrashFaultCell(
    cell: dict[str, Any], root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    digest = hashlib.sha256(cell["id"].encode("utf-8")).hexdigest()
    crash_home = root / f"crash-{digest[:16]}"
    provider = (
        "anthropic" if cell["providerFamily"].startswith("anthropic") else "openai"
    )
    model = "claude-sonnet-4-6" if provider == "anthropic" else "gpt-5.4"
    session_id = f"crash-{digest[:20]}"
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            _CRASH_CHILD,
            str(crash_home),
            session_id,
            provider,
            model,
            cell["state"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in {-signal.SIGKILL, 128 + signal.SIGKILL}:
        raise RuntimeError(
            f"Crash probe did not terminate via SIGKILL: {completed.returncode}"
        )
    control_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not control_lines:
        raise RuntimeError("Crash probe did not report its volatile pre-kill state")
    control = json.loads(control_lines[-1])
    expected_output = int(cell["state"][1:]) >= 3
    expected_tool = 4 <= int(cell["state"][1:]) <= 6
    if (
        control.get("state") != cell["state"]
        or control.get("volatileHadAssistantOutput") is not expected_output
        or control.get("volatileHadToolUse") is not expected_tool
        or control.get("volatileMessageCount") != (2 if expected_output else 1)
    ):
        raise RuntimeError(f"Crash probe volatile state diverged: {control!r}")
    async with core.scopedRuntimeHome(str(crash_home)):
        restored = await core.loadSession(session_id)
        restored_again = await core.loadSession(session_id)
    if not isinstance(restored, dict):
        raise RuntimeError("Crash probe could not reload its stable session")
    repair = core.repairSessionTranscript(restored)
    if repair["repaired"] or len(restored.get("messages") or []) != 1:
        raise RuntimeError("Crash probe recovery changed its stable transcript")
    revision = restored.get("revision")
    archived_count = (restored.get("metadata") or {}).get(
        "fullTranscriptLog", {}
    ).get("archivedMessageCount")
    if not isinstance(revision, int) or revision < 1 or archived_count != 1:
        raise RuntimeError("Crash probe did not recover a revisioned archive boundary")
    if restored_again != restored:
        raise RuntimeError("Crash probe restart recovery was not idempotent")
    restored_digest = hashlib.sha256(
        json.dumps(restored, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _actual(cell), {
        "returnCode": completed.returncode,
        "signal": "SIGKILL",
        "restoredMessageCount": len(restored["messages"]),
        "repairApplied": repair["repaired"],
        "volatileState": control,
        "session": {
            "persisted": True,
            "settledThrough": "SIGKILL then two independent loadSession calls",
            "repairApplied": repair["repaired"],
            "messageCount": len(restored["messages"]),
            "revision": revision,
            "archivedMessageCount": archived_count,
            "idempotentReload": True,
            "digest": restored_digest,
        },
    }


def _actual(cell: dict[str, Any]) -> dict[str, Any]:
    state_index = int(cell["state"][1:])
    return {
        "classification": "process_terminated",
        "retry": False,
        "hadAssistantOutput": state_index >= 3,
        "hadToolUse": 4 <= state_index <= 6,
        "sessionIntegrity": "restart_reload_idempotent_stable_boundary",
        "eventSequence": [],
    }


execute_crash_fault_cell = executeCrashFaultCell
