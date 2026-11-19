"""Whether a Gemini call was served from cache, and if not, why not.

The Interactions API caches implicitly: there is nothing to mark and nothing to ask
for. A call hits the cache when it begins with what an earlier call sent, so the one
thing the runtime controls is that prefix. Each call leaves a small snapshot (hashes
and counts, never content) and the next call is compared with it: the same model,
system instruction, tools and generation settings, and steps that extend the earlier
steps. With that and the cached-token count Gemini reports, a miss can be named.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

GOOGLE_CACHE_SNAPSHOT_VERSION = 1
# After this long without a call the cached prefix has probably been dropped. Google
# does not publish the lifetime of an implicit cache entry; this is a working figure.
IDLE_GAP_MS = 300_000


def minimum_cacheable_tokens(model: str | None) -> int:
    """The smallest prompt Gemini caches: 2,048 tokens on 2.5 models, 4,096 on later ones."""
    return 2_048 if str(model or "").strip().lower().startswith("gemini-2.") else 4_096


def google_cache_snapshot(params: dict[str, Any], previous_step_count: Any = None) -> dict[str, Any]:
    """Hashes of one request's cacheable parts. ``prefixHash`` covers its first
    ``previous_step_count`` steps, to compare with the previous call's ``inputHash``."""
    steps = params.get("input") if isinstance(params.get("input"), list) else []
    wanted = previous_step_count if isinstance(previous_step_count, int) and not isinstance(previous_step_count, bool) else -1
    rolling = hashlib.sha256()
    prefix_hash = rolling.hexdigest() if wanted == 0 else None
    for index, step in enumerate(steps, start=1):
        rolling.update(_canonical(step))
        rolling.update(b"\x1e")
        if index == wanted:
            prefix_hash = rolling.hexdigest()
    generation = {key: value for key, value in (params.get("generation_config") or {}).items() if key != "max_output_tokens"}
    return {
        "version": GOOGLE_CACHE_SNAPSHOT_VERSION,
        "modelHash": _digest(params.get("model")),
        "systemHash": _digest(params.get("system_instruction")),
        "toolsHash": _digest(params.get("tools")),
        "configHash": _digest(generation),
        "inputCount": len(steps),
        "inputHash": rolling.hexdigest(),
        "prefixHash": prefix_hash,
    }


def diagnose_google_cache(previous: Any, current: dict[str, Any], usage: dict[str, Any], *, model: str | None, now: datetime | None = None) -> str:
    """One reason for what the cache did on this call, from the closed set the cache event carries."""
    if not isinstance(previous, dict) or previous.get("version") != GOOGLE_CACHE_SNAPSHOT_VERSION:
        return "warmup_no_previous_state"
    for field, reason in (
        ("modelHash", "model_changed"),
        ("systemHash", "system_changed"),
        ("toolsHash", "tools_changed"),
        ("configHash", "thinking_or_output_config_changed"),
    ):
        if previous.get(field) != current.get(field):
            return reason
    if current.get("prefixHash") != previous.get("inputHash"):
        shrank = int(current.get("inputCount") or 0) < int(previous.get("inputCount") or 0)
        rewritten = shrank or previous.get("compactions") != current.get("compactions")
        return "compaction_or_relief_rewrite" if rewritten else "message_prefix_changed"
    cached = _count(usage.get("cache_read_input_tokens"))
    if cached > 0:
        return "cache_read_observed"
    if "cache_read_input_tokens" not in usage:
        return "no_cache_usage_reported"
    if cached + _count(usage.get("input_tokens")) < minimum_cacheable_tokens(model):
        return "below_cache_minimum"
    if _elapsed_ms(previous.get("at"), now) >= IDLE_GAP_MS:
        return "idle_gap_may_have_expired"
    return "unchanged_short_gap_likely_server_side"


def google_cache_event(session_id: Any, usage: dict[str, Any], reason: str) -> dict[str, Any]:
    """The ``provider.prompt_cache`` event for one observed Gemini call."""
    return {
        "session_id": session_id,
        "phase": "observed",
        "mode": "auto",
        "ttl": "provider_default",
        "used_breakpoints": 0,
        "placements": ["automatic"],
        "cache_read_input_tokens": _count(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "cache_creation_ttl_detail_complete": True,
        "local_reason": reason,
        "state_status": "recorded",
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def _elapsed_ms(earlier: Any, now: datetime | None) -> int:
    try:
        then = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds() * 1000))
