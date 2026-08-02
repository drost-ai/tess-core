from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import agent_core as core


def build_compaction_authority_states(
    session: dict[str, Any],
    paths: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    compactions_dir = Path(core.getSessionCompactionsDir(session["id"]))
    compactions_dir.mkdir(parents=True, exist_ok=True)
    summary = compactions_dir / "summary-primitive.md"
    summary.write_text(
        "# Compacted Summary\n\n- This legacy summary must never become prompt authority.\n",
        encoding="utf-8",
    )
    owned = compactions_dir / "working-context-owned.json"
    foreign = compactions_dir / "working-context-foreign.json"
    outside = paths["python_home"] / "working-context-outside.json"
    owned.write_text(
        json.dumps(_working_context_document(session, session["id"]), indent=2) + "\n",
        encoding="utf-8",
    )
    foreign.write_text(
        json.dumps(_working_context_document(session, "foreign-compaction-session"), indent=2) + "\n",
        encoding="utf-8",
    )
    outside.write_text(
        json.dumps(_working_context_document(session, session["id"]), indent=2) + "\n",
        encoding="utf-8",
    )

    def with_state(working_context: Path) -> dict[str, Any]:
        compacted = copy.deepcopy(session)
        compacted["metadata"]["compactionState"] = {
            "generation": 1,
            "compactedThroughMessageIndex": 1,
            "archiveMessageCount": len(compacted["messages"]) - 4,
            "compactedContextTokenEstimate": 321,
            "workingContextTokenEstimate": 500,
            "workingContextTokenMeasurement": {
                "inputTokens": 150_500,
                "source": "scripted",
            },
            "artifacts": {
                "summary": str(summary),
                "workingContext": str(working_context),
            },
        }
        return compacted

    return {
        "ownedWorkingContext": with_state(owned),
        "missingWorkingContext": with_state(
            compactions_dir / "working-context-missing.json"
        ),
        "foreignWorkingContext": with_state(foreign),
        "outsideWorkingContext": with_state(outside),
    }


def _working_context_document(
    session: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    timestamp = "2026-01-01T00:00:02.000Z"
    return {
        "version": "v2",
        "sessionId": session_id,
        "generatedAt": timestamp,
        "updatedAt": timestamp,
        "generation": 1,
        "sourceRanges": [
            {
                "generation": 1,
                "start": 0,
                "end": 1,
                "messageCount": 1,
                "createdAt": timestamp,
            }
        ],
        "session": {
            "cwd": session["cwd"],
            "provider": session["provider"],
            "model": session["model"],
            "effort": session.get("effort"),
            "permissionMode": session["permissionMode"],
        },
        "project": {
            "description": "Compaction primitive parity authority fixture.",
            "keyFiles": [],
        },
        "goals": {
            "active": ["Preserve checkpoint-v2 primitive parity."],
            "recentRequests": [],
            "constraints": ["Only session-owned working context is authoritative."],
            "preferences": [],
        },
        "currentState": {
            "completed": [],
            "inProgress": ["Validate compaction artifact isolation."],
            "blocked": [],
        },
        "decisions": ["Reject summary fallback and foreign artifacts."],
        "discoveries": [],
        "todos": {"open": [], "completed": []},
        "activity": {"commands": [], "searches": [], "web": [], "failures": []},
        "preservedTrace": [],
        "nextSteps": ["Compare the owned projection across runtimes."],
        "criticalContext": ["The working-context document is session-bound."],
    }
