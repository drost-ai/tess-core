from __future__ import annotations

import json
from typing import Any

from ..config import ensure_provider_for_model_selection, normalize_effort_for_model
from ..providers.anthropic import send_anthropic_messages_request
from ..providers.openai import send_openai_request
from ..providers.usage_costs import merge_usage_into_session as _merge_usage_into_session
from ..runtime.secret_provider import resolve_provider_config_secrets


async def _author_monitor_update(config: dict[str, Any], session: dict[str, Any], monitor: dict[str, Any], task: dict[str, Any] | None, reason: str, output_excerpt: str, omitted_bytes: int) -> str:
    try:
        return await _author_monitor_update_with_provider(config, session, monitor, task, reason, output_excerpt, omitted_bytes)
    except Exception:
        return _build_deterministic_monitor_update(monitor, task, reason, output_excerpt, omitted_bytes)


async def _author_monitor_update_with_provider(config: dict[str, Any], session: dict[str, Any], monitor: dict[str, Any], task: dict[str, Any] | None, reason: str, output_excerpt: str, omitted_bytes: int) -> str:
    provider = ensure_provider_for_model_selection(session.get("provider") or config.get("provider"), session.get("model") or config.get("model"), config.get("provider", "openai"))
    model = session.get("model") or config.get("model")
    request = {
        "model": model,
        "effort": normalize_effort_for_model(provider, model, session.get("effort") or config.get("effort")) if model else None,
        "system": "\n".join(
            [
                "You are continuing an existing agent session.",
                "Write a concise proactive update for the user about a monitored background task.",
                "Default to 1-3 short sentences.",
                "Be concrete, state what changed, and mention completion or failure clearly.",
                "Do not mention tokens, prompts, or that you are summarizing structured data.",
                "Do not ask the user to poll again. You are the one proactively updating them.",
            ]
        ),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "reason": reason,
                        "goal": monitor.get("goal"),
                        "summary_style": monitor.get("summaryStyle"),
                        "task": {
                            "status": (task or {}).get("status") or "unknown",
                            "exit_code": (task or {}).get("exitCode"),
                            "command": (task or {}).get("command"),
                            "cwd": (task or {}).get("cwd"),
                            "finished_at": (task or {}).get("finishedAt"),
                            "error": (task or {}).get("error"),
                        },
                        "monitor": {
                            "status": monitor.get("status"),
                            "created_at": monitor.get("createdAt"),
                            "last_assistant_update_at": monitor.get("lastAssistantUpdateAt"),
                            "last_assistant_update_summary": monitor.get("lastAssistantUpdateSummary"),
                            "omitted_output_bytes": omitted_bytes,
                        },
                        "new_output_excerpt": output_excerpt or "",
                    },
                    indent=2,
                ),
            }
        ],
        "tools": [],
    }
    resolved_config = await resolve_provider_config_secrets(config)
    if provider == "openai":
        token = resolved_config.get("openaiToken")
        if not token:
            raise RuntimeError("No OpenAI token available for monitor updates.")
        response = await send_openai_request({"token": token, "baseUrl": resolved_config.get("openaiBaseUrl"), "headers": resolved_config.get("customHeaders"), "mode": resolved_config.get("openaiApiMode"), "request": request, "runtime": {}, "maxAttempts": 1})
    elif provider == "anthropic":
        response = await send_anthropic_messages_request({"request": request, "config": resolved_config, "runtime": {}, "maxAttempts": 1})
    else:
        raise RuntimeError("Monitor notification provider unsupported; use deterministic notification")
    text = "".join(str(block.get("text") or "") for block in response.get("content", []) if block.get("type") == "text").strip()
    if not text:
        raise RuntimeError("Provider returned an empty monitor update.")
    _merge_usage_into_session(session, response.get("usage") or {})
    return text


def _build_deterministic_monitor_update(monitor: dict[str, Any], task: dict[str, Any] | None, reason: str, output_excerpt: str, omitted_bytes: int) -> str:
    label = monitor.get("goal") or f"task {str(monitor.get('taskId') or '')[:8]}"
    if reason == "start":
        return f"Monitoring {label}. The background task is {(task or {}).get('status') or 'in progress'}."
    if reason == "completed":
        suffix = f" with exit code {task.get('exitCode')}" if task and isinstance(task.get("exitCode"), int) else ""
        return f"The background task for {label} completed{suffix}."
    if reason == "failed":
        suffix = f" with exit code {task.get('exitCode')}" if task and isinstance(task.get("exitCode"), int) else ""
        error = f": {task.get('error')}" if task and task.get("error") else "."
        return f"The background task for {label} failed{suffix}{error}"
    if reason == "stopped":
        return f"The background task for {label} stopped before completion."
    if reason == "heartbeat":
        return f"Still monitoring {label}. The background task is {(task or {}).get('status') or 'in progress'}."
    excerpt = f" Latest output:\n{_render_excerpt(output_excerpt, omitted_bytes)}" if output_excerpt else ""
    return f"There is new background activity for {label}.{excerpt}"


def _render_excerpt(text: str, omitted_bytes: int) -> str:
    prefix = f"[omitted {omitted_bytes} earlier bytes]\n" if omitted_bytes > 0 else ""
    return f"{prefix}{text}".strip()
