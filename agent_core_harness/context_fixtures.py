from __future__ import annotations

import json
import re
from typing import Any


def build_context_messages(profile: str, count: int, workspace: str) -> list[dict[str, Any]]:
    if profile == "tool_heavy":
        return _tool_heavy_messages(max(6, count), workspace)
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": f"{profile} context message {index} for {workspace}/app.py and pytest"} for index in range(count)]


def retrieved_memory_payload() -> dict[str, Any]:
    return {
        "records": [
            {"kind": "workflow", "source": "compaction", "text": "Use pytest for focused parity validation."},
            {"kind": "repo_fact", "source": "compaction", "text": "Continuation lives in agent_core/context/continuation."},
            {"kind": "constraint", "source": "compaction", "text": "Preserve active plan and todo state."},
            {"kind": "environment", "source": "manual", "text": "Workspace uses AGENT_CORE_HOME for test isolation."},
        ],
        "tokenEstimate": 650,
    }


class ScriptedCompactionClient:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def complete(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(_client_call_summary(args))
        if self.mode == "accept":
            payload = _accept_payload() if len(self.calls) == 1 else _verification_payload("accept", "checkpoint preserves current state")
        elif self.mode == "repair":
            payload = _repair_payload(len(self.calls))
        elif self.mode == "reject":
            payload = _accept_payload() if len(self.calls) == 1 else _verification_payload("reject", "checkpoint omitted active next step")
        else:
            payload = _accept_payload()
        return {"text": json.dumps(payload), "usage": {"input_tokens": 11, "output_tokens": 7}, "model": args["model"]}


class ContextLimitFallbackClient:
    def __init__(self, core_module: Any) -> None:
        self.core = core_module
        self.calls: list[dict[str, Any]] = []

    async def complete(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(_client_call_summary(args))
        if len(self.calls) == 1:
            raise self.core.create_provider_request_error(
                self.core.create_provider_failure_record(
                    {
                        "provider": "openai",
                        "providerLabel": "OpenAI",
                        "phase": "request",
                        "category": "client_request",
                        "retryable": False,
                        "attempt": 1,
                        "hadAssistantOutput": False,
                        "hadToolUse": False,
                        "rawMessage": "input token context_length exceeded",
                    }
                )
            )
        return {"text": json.dumps(_accept_payload("fallback after reduced evidence")), "usage": {}, "model": args["model"]}


def _tool_heavy_messages(count: int, workspace: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index in range(count):
        tool_id = f"read-{index}"
        path = f"{workspace}/file{index % 2}.py"
        messages.extend(
            [
                {"role": "user", "content": f"inspect file {index}"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": path, "offset": 1}}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps({"file": {"filePath": path, "content": "x" * 4000, "startLine": 1, "totalLines": 400, "numLines": 80}}),
                        }
                    ],
                },
            ]
        )
    messages.append({"role": "user", "content": "continue from latest file inspection"})
    return messages


def _accept_payload(next_step: str = "Persist checkpoint artifacts") -> dict[str, Any]:
    return {
        "shouldCheckpoint": True,
        "quality": {"classification": "rich", "reason": "captures current work"},
        "workingContext": working_context("Run full context-management scenario", next_step),
    }


def _repair_payload(call_count: int) -> dict[str, Any]:
    if call_count == 1:
        return {
            "shouldCheckpoint": True,
            "quality": {"classification": "usable", "reason": "needs correction"},
            "workingContext": working_context("Complete Slice 7", "stale next step"),
        }
    if call_count == 2:
        return {
            "verdict": "repair",
            "confidence": "medium",
            "reason": "next step was stale",
            "issues": ["stale next step"],
            "notes": [],
            "repairedCheckpoint": {"quality": {"classification": "usable", "reason": "repaired"}, "workingContext": working_context("Complete Slice 7", "run focused context harness tests")},
        }
    return _verification_payload("accept", "repair is accurate", notes=["repair accepted"])


def _verification_payload(verdict: str, reason: str, notes: list[str] | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "confidence": "high", "reason": reason, "issues": [] if verdict == "accept" else ["missing continuation detail"], "notes": notes or ["verified"]}


def working_context(goal: str, next_step: str) -> dict[str, Any]:
    return {
        "project": {
            "description": "Python port of agent-core",
            "facts": {"primaryLanguage": "Python", "languages": ["Python"], "ecosystem": "unittest", "packageManager": None, "evidence": ["tests/test_agent_core.py"]},
            "keyFiles": [{"path": "agent_core/context/compaction/orchestration.py", "role": "modified", "note": "checkpoint-v2 pipeline"}],
        },
        "goals": {"active": [goal], "recentRequests": ["Slice 7 context pack"], "constraints": ["Perform exactly like TypeScript"], "preferences": ["Detailed commits"]},
        "currentState": {"completed": ["Committed and pushed previous slice"], "inProgress": ["Implementing context scenario harness"], "blocked": []},
        "decisions": ["Use fixed Codex Spark compaction model selection"],
        "discoveries": ["Continuation relief must preserve durable memory reinjection"],
        "activity": {"commands": ["python3 -m unittest discover tests"], "searches": ["grep context relief"], "web": [], "failures": []},
        "nextSteps": [next_step],
        "criticalContext": ["Verification must accept before artifacts are persisted"],
    }


def _client_call_summary(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "")
    return {
        "purpose": (args.get("model") or {}).get("purpose"),
        "model": (args.get("model") or {}).get("model"),
        "hasEvidence": "Compacted transcript evidence:" in prompt,
        "hasCandidate": "Candidate checkpoint:" in prompt,
        "promptPreview": _stable_prompt_preview(prompt),
    }


def _stable_prompt_preview(prompt: str) -> str:
    normalized = re.sub(r'"sessionId":\s*"[^"]+"', '"sessionId": "<session-id>"', prompt)
    normalized = re.sub(r'"cwd":\s*"[^"]+"', '"cwd": "<workspace>"', normalized)
    return "\n".join(normalized.splitlines()[:5])[:220]
