from __future__ import annotations

import hashlib
import json
from typing import Any

from .prompt_cache import resolvePromptCachePolicy
from ..prompt_cache_state import markPromptCacheEvictionIntent
from ..runtime.paths import getActiveRuntimePaths, resolveRuntimePaths


_TERMINAL_SUCCESS_REASONS = {"end_turn", "stop", "completed"}


def isPromptCacheEvictionTerminalSuccess(args: dict[str, Any]) -> bool:
    return bool(
        not args.get("signal")
        and not args.get("error")
        and args.get("stopReason") in _TERMINAL_SUCCESS_REASONS
    )


async def recordPromptCacheEvictionIntentAtFinalization(args: dict[str, Any]) -> None:
    session = args.get("session") or {}
    config = args.get("config") or {}
    runtime = args.get("runtime") or {}
    try:
        provider = session.get("provider") or config.get("provider")
        if provider != "anthropic" or not isPromptCacheEvictionTerminalSuccess(args):
            return
        policy = resolvePromptCachePolicy(config=config)
        if not policy.get("enabled") or not policy.get("evictOnComplete"):
            return
        paths = (
            resolveRuntimePaths(candidate=config.get("runtimeHome"))
            if config.get("runtimeHome")
            else getActiveRuntimePaths()
        )
        result = await markPromptCacheEvictionIntent(
            {
                "sessionId": session.get("id"),
                "paths": paths,
                "finalizationKey": _finalization_key(session, str(args.get("stopReason"))),
            }
        )
        if result["status"] == "unavailable":
            _emit(runtime, {
                "session_id": session.get("id"), "phase": "state_error", "state_status": "unavailable"
            })
        elif result["status"] == "recorded":
            _emit(runtime, {
                "session_id": session.get("id"),
                "phase": "eviction_intent",
                "mode": policy.get("mode"),
                "ttl": policy.get("ttl"),
                "scope": policy.get("scope"),
                "state_status": "recorded",
                "intent_status": "recorded",
            })
    except Exception:
        _emit(runtime, {
            "session_id": session.get("id"), "phase": "state_error", "state_status": "unavailable"
        })


def _finalization_key(session: dict[str, Any], stop_reason: str) -> str:
    encoded = json.dumps(
        {
            "schemaVersion": 1,
            "sessionId": session.get("id"),
            "stopReason": stop_reason,
            "messageCount": len(session.get("messages") or []),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _emit(runtime: dict[str, Any], payload: dict[str, Any]) -> None:
    try:
        runtime.get("emitEvent", lambda *_: None)("provider.prompt_cache", payload)
    except Exception:
        pass


is_prompt_cache_eviction_terminal_success = isPromptCacheEvictionTerminalSuccess
record_prompt_cache_eviction_intent_at_finalization = recordPromptCacheEvictionIntentAtFinalization
