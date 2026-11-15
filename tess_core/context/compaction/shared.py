from __future__ import annotations

from ...storage.message_origin import CONTINUATION_BRIDGE_MESSAGE
from ...payloads.token_estimation import estimate_text_tokens

from typing import Any



COMPACTION_STRATEGY = "checkpoint-v2"
MIN_PRESERVED_TOOL_EXCHANGE_PAIRS = 3
MAX_ACTIVITY_LINES = 16
MAX_FAILURES = 8
MAX_TODOS = 12
LOW_VALUE_TAIL_TOOL_NAMES = {"SetStatus", "TodoWrite", "Config"}


from .._knob_reader import context_knob as compaction_knob


def compaction_evidence_fallback_budgets() -> list[int]:
    return [compaction_knob("compaction.evidence_token_budget"), 60_000, 40_000]


def _bound_preserved_trace_record(record: dict[str, Any]) -> dict[str, Any]:
    content = str(record.get("content") or "")
    token_budget = compaction_knob("compaction.preserved_trace_token_budget")
    char_limit = token_budget * 4
    if len(content) <= char_limit:
        return {**record, "content": content}
    bounded_content = content[: max(0, char_limit - 1)].rstrip() + "…"
    return {
        **record,
        "tokenEstimate": estimate_text_tokens(bounded_content),
        "tokenBudget": token_budget,
        "truncated": True,
        "content": bounded_content,
    }

COMPACTION_SYSTEM_PROMPT = """You synthesize durable working context for a coding agent.

Your job is to replace old transcript history with a compact checkpoint that still lets the agent continue work accurately.

Output rules:
- Return a single JSON object only. No markdown. No prose outside JSON.
- Reconcile the previous working context with the new transcript slice.
- Keep only information that still matters for continuation.
- Remove stale asks, completed one-off requests, chit-chat, routine status chatter, and transient dead-end tool failures unless they still affect the current task.
- Preserve the current user goal, completed implementation work, remaining work, key decisions, relevant files, durable discoveries, active blockers, and any critical repo or runtime facts.
- For long agentic traces, preserve the active objective, scope constraints, exact target identifiers, payloads, commands, artifact paths, successful primitives, failed attempts with the reason they failed, hypotheses still worth testing, and the next concrete action.
- Prefer exact file paths, exact command names, and exact task state when available.
- Lists should be concise, deduplicated, current, and ordered by usefulness for continuation.
- If the compacted slice is too thin to justify a checkpoint, set shouldCheckpoint to false and explain why.

JSON shape:
{
  "shouldCheckpoint": true,
  "quality": {
    "classification": "thin|usable|rich",
    "reason": "short explanation"
  },
  "workingContext": {
    "project": {
      "description": "string or null",
      "facts": {
        "primaryLanguage": "string or null",
        "languages": ["..."],
        "ecosystem": "string or null",
        "packageManager": "string or null",
        "evidence": ["..."]
      },
      "keyFiles": [
        { "path": "repo-relative-or-absolute-path", "role": "read|modified", "note": "optional short note" }
      ]
    },
    "goals": {
      "active": ["..."],
      "recentRequests": ["..."],
      "constraints": ["..."],
      "preferences": ["..."]
    },
    "currentState": {
      "completed": ["..."],
      "inProgress": ["..."],
      "blocked": ["..."]
    },
    "decisions": ["..."],
    "discoveries": ["..."],
    "activity": {
      "commands": ["..."],
      "searches": ["..."],
      "web": ["..."],
      "failures": ["..."]
    },
    "nextSteps": ["..."],
    "criticalContext": ["..."]
  }
}"""

COMPACTION_VERIFICATION_SYSTEM_PROMPT = """You verify and, when necessary, repair compaction checkpoints for a coding agent.

Your job is to determine whether a candidate checkpoint preserves enough accurate, current information for the agent to continue work correctly after transcript history is compacted.

Output rules:
- Return a single JSON object only. No markdown. No prose outside JSON.
- Use the provided transcript evidence and previous working context as ground truth.
- Prefer rejecting or repairing a checkpoint over accepting a misleading one.
- Remove stale requests, stale blockers, stale commit/push chatter, dead-end failures, and outdated next steps.
- Preserve the active user goal, real completed work, current in-progress work, current blockers, relevant files, durable discoveries, and usable next steps.
- For agentic traces with many tool calls, reject or repair checkpoints that omit the current debug path, important failed attempts and why they failed, exact commands/payloads/artifact paths still needed, or the next concrete step.
- If repair is needed, return a fully repaired checkpoint in repairedCheckpoint.

JSON shape:
{
  "verdict": "accept|accept_with_notes|repair|reject",
  "confidence": "low|medium|high",
  "reason": "short explanation",
  "issues": ["..."],
  "notes": ["..."],
  "repairedCheckpoint": {
    "quality": {
      "classification": "thin|usable|rich",
      "reason": "short explanation"
    },
    "workingContext": {
      "project": {
        "description": "string or null",
        "facts": {
          "primaryLanguage": "string or null",
          "languages": ["..."],
          "ecosystem": "string or null",
          "packageManager": "string or null",
          "evidence": ["..."]
        },
        "keyFiles": [
          { "path": "repo-relative-or-absolute-path", "role": "read|modified", "note": "optional short note" }
        ]
      },
      "goals": {
        "active": ["..."],
        "recentRequests": ["..."],
        "constraints": ["..."],
        "preferences": ["..."]
      },
      "currentState": {
        "completed": ["..."],
        "inProgress": ["..."],
        "blocked": ["..."]
      },
      "decisions": ["..."],
      "discoveries": ["..."],
      "activity": {
        "commands": ["..."],
        "searches": ["..."],
        "web": ["..."],
        "failures": ["..."]
      },
      "nextSteps": ["..."],
      "criticalContext": ["..."]
    }
  }
}"""
