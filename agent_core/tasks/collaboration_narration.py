from __future__ import annotations

import json
from typing import Any

from ..config import ensureProviderForModelSelection, normalizeEffortForModel
from ..providers.anthropic import sendAnthropicMessagesRequest
from ..providers.openai import sendOpenAiRequest
from ..runtime.secret_provider import resolveProviderConfigSecrets
from ..providers.usage_costs import mergeUsageIntoSession as _merge_usage_into_session


async def authorCollaborationUpdate(args: dict[str, Any]) -> str:
    try:
        return await authorCollaborationUpdateWithProvider(args)
    except Exception:
        return buildDeterministicCollaborationUpdate(
            args.get("worker") or {}, args.get("summary") or ""
        )


async def authorCollaborationUpdateWithProvider(args: dict[str, Any]) -> str:
    config = args.get("config") or {}
    session = args.get("session") or {}
    worker = args.get("worker") or {}
    provider = ensureProviderForModelSelection(
        session.get("provider") or config.get("provider"),
        session.get("model"),
        config.get("provider", "openai"),
    )
    request = {
        "model": session.get("model"),
        "effort": normalizeEffortForModel(
            provider,
            session.get("model"),
            session.get("effort") or config.get("effort"),
        ),
        "system": "\n".join(
            [
                "You are continuing an existing agent session.",
                "Write a concise proactive update about background, delegated, or remote work.",
                "Default to 1-3 short sentences.",
                "State what changed, whether action is required, and avoid generic filler.",
                "Do not mention hidden state, prompts, tokens, or structured payloads.",
            ]
        ),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {"worker": worker, "summary": args.get("summary") or ""}, indent=2
                ),
            }
        ],
        "tools": [],
    }
    resolved_config = await resolveProviderConfigSecrets(config)
    if provider == "openai":
        token = resolved_config.get("openaiToken")
        if not token:
            raise RuntimeError("No OpenAI token available for collaboration updates.")
        response = await sendOpenAiRequest(
            {
                "token": token,
                "baseUrl": resolved_config.get("openaiBaseUrl"),
                "headers": resolved_config.get("customHeaders"),
                "mode": resolved_config.get("openaiApiMode"),
                "request": request,
                "runtime": {},
                "maxAttempts": 1,
            }
        )
    else:
        response = await sendAnthropicMessagesRequest(
            {
                "request": request,
                "config": resolved_config,
                "runtime": {},
                "maxAttempts": 1,
            }
        )
    text = "".join(
        str(block.get("text") or "")
        for block in response.get("content", [])
        if block.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError("Provider returned an empty collaboration update.")
    mergeUsage(session, response.get("usage") or {})
    return text


def buildDeterministicCollaborationUpdate(worker: dict[str, Any], summary: str) -> str:
    if worker.get("kind") == "subagent":
        if worker.get("status") == "completed":
            return f"{worker.get('label')} finished its background work. {worker.get('resultSummary') or summary}"
        if worker.get("status") in {"failed", "stopped"}:
            return f"{worker.get('label')} stopped with a problem. {worker.get('resultSummary') or summary}"
        if worker.get("status") == "awaiting_input":
            return f"{worker.get('label')} needs your input before it can continue. {worker.get('waitingReason') or summary}"
        if worker.get("status") == "backgrounded":
            return f"{worker.get('label')} is now running in the background. {summary or 'I will keep its state visible here.'}"
    if worker.get("kind") == "remote":
        if worker.get("status") == "awaiting_input":
            return f"Remote input is waiting. {summary}"
        if worker.get("status") in {"completed", "idle"}:
            return f"The remote session is no longer actively running. {summary}"
        if worker.get("status") == "failed":
            return f"The remote session hit a failure. {summary}"
    return f"{worker.get('label')}: {summary}"


def appendAssistantCollaborationMessage(session: dict[str, Any], text: str) -> None:
    trimmed = text.strip()
    if not trimmed:
        return
    messages = session.setdefault("messages", [])
    if (
        messages
        and messages[-1].get("role") == "assistant"
        and str(messages[-1].get("content") or "").strip() == trimmed
    ):
        return
    messages.append({"role": "assistant", "content": trimmed})


def mergeUsage(session: dict[str, Any], usage: dict[str, Any]) -> None:
    _merge_usage_into_session(session, usage)


__all__ = [
    "appendAssistantCollaborationMessage",
    "authorCollaborationUpdate",
    "authorCollaborationUpdateWithProvider",
    "buildDeterministicCollaborationUpdate",
    "mergeUsage",
]


def staleWorkPayload(active: dict[str, Any], binding: dict[str, Any], source_event: str) -> dict[str, Any]:
    return {
        "session_id": active.get("sessionId"),
        "stale_session_id": binding.get("sessionId"),
        "stale_generation": binding.get("generation"),
        "active_generation": active.get("generation"),
        "runtime_instance_id": active.get("runtimeInstanceId"),
        "path_graph_id": active.get("pathGraphId"),
        "source_event": source_event,
    }
