from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse


_VALID_COMPATIBILITY = {"auto", "official", "claude_code", "compatible"}
_VALID_SCOPES = {"ephemeral", "global", "bedrock"}


def resolve_prompt_cache_policy(
    *,
    config: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    request = request or {}
    options = options or {}
    profile = config.get("providerProfile") if isinstance(config.get("providerProfile"), dict) else {}
    raw: dict[str, Any] = {}
    for source in (
        profile.get("promptCache"),
        profile.get("prompt_cache"),
        config.get("promptCache"),
        config.get("prompt_cache"),
        request.get("promptCache"),
        request.get("prompt_cache"),
        options.get("promptCache"),
        options.get("prompt_cache"),
    ):
        if isinstance(source, dict):
            raw.update(source)
    enabled = _bool_setting(raw.get("enabled"), True)
    mode = _normalize_choice(raw.get("mode"), {"auto", "explicit", "off"}, "auto")
    ttl = _normalize_choice(raw.get("ttl"), {"provider_default", "5m", "1h", "24h"}, "provider_default")
    compatibility = _normalize_choice(raw.get("compatibility"), _VALID_COMPATIBILITY, "auto")
    if compatibility == "auto":
        compatibility = _resolve_anthropic_compatibility(config, profile)
    explicit = _bool_setting(raw.get("explicit"), None)
    suppressed = bool(
        enabled
        and mode != "off"
        and str(config.get("provider") or "anthropic").lower() == "anthropic"
        and compatibility == "claude_code"
        and explicit is not True
    )
    return {
        "enabled": enabled and mode != "off",
        "mode": mode,
        "ttl": ttl,
        "affinity": _bool_setting(raw.get("affinity"), True),
        "isolation": str(raw.get("isolation") or "").strip() or None,
        "retention": _normalize_choice(raw.get("retention"), {"provider_default", "in_memory", "24h"}, "provider_default"),
        "explicit": explicit,
        "compatibility": compatibility,
        "messageTailBreakpoints": _bool_setting(
            raw.get("messageTailBreakpoints", raw.get("message_tail_breakpoints")), None
        ),
        "toolDefinitionBreakpoint": _bool_setting(
            raw.get("toolDefinitionBreakpoint", raw.get("tool_definition_breakpoint")), None
        ),
        "providerDiagnostics": bool(
            _bool_setting(raw.get("providerDiagnostics", raw.get("provider_diagnostics")), False)
        ),
        "scope": _normalize_choice(raw.get("scope"), _VALID_SCOPES, "ephemeral"),
        "evictOnComplete": bool(
            _bool_setting(raw.get("evictOnComplete", raw.get("evict_on_complete")), False)
        ),
        "messageTailPreviousAnchor": raw.get("messageTailPreviousAnchor"),
        "dispatchContext": raw.get("dispatchContext") if isinstance(raw.get("dispatchContext"), dict) else None,
        "compatibilitySuppressedReason": (
            "Anthropic cache controls suppressed for Claude Code compatible provider profile."
            if suppressed
            else None
        ),
    }


def anthropic_explicit_cache_enabled(config: dict[str, Any], request: dict[str, Any], options: dict[str, Any] | None = None) -> bool:
    policy = resolve_prompt_cache_policy(config=config, request=request, options=options)
    if not policy["enabled"] or policy.get("compatibilitySuppressedReason"):
        return False
    if policy["explicit"] is False:
        return False
    return policy["mode"] in {"auto", "explicit"}


def anthropic_automatic_cache_enabled(config: dict[str, Any], request: dict[str, Any], options: dict[str, Any] | None = None) -> bool:
    policy = resolve_prompt_cache_policy(config=config, request=request, options=options)
    if (
        not policy["enabled"]
        or policy["mode"] != "auto"
        or policy.get("compatibilitySuppressedReason")
    ):
        return False
    return True


def anthropic_message_tail_cache_enabled(policy: dict[str, Any] | None) -> bool:
    if not _anthropic_explicit_policy_enabled(policy):
        return False
    explicit = (policy or {}).get("messageTailBreakpoints")
    if isinstance(explicit, bool):
        return explicit
    return (policy or {}).get("compatibility") in {"official", "claude_code"}


def anthropic_tool_definition_cache_enabled(policy: dict[str, Any] | None) -> bool:
    if not _anthropic_explicit_policy_enabled(policy):
        return False
    explicit = (policy or {}).get("toolDefinitionBreakpoint")
    if isinstance(explicit, bool):
        return explicit
    return (policy or {}).get("compatibility") in {"official", "claude_code"}


def anthropic_cache_control(config: dict[str, Any], request: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, str]:
    policy = resolve_prompt_cache_policy(config=config, request=request, options=options)
    control = {"type": "ephemeral"}
    if policy.get("ttl") in {"5m", "1h"}:
        control["ttl"] = str(policy["ttl"])
    return control


def provider_affinity_key(
    *,
    provider: str,
    base_url: str | None = None,
    config: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
) -> str:
    request = request or {}
    config = config or {}
    raw_session = (
        request.get("sessionId")
        or request.get("session_id")
        or config.get("sessionId")
        or config.get("session_id")
        or ""
    )
    raw_profile = config.get("providerProfileName") or config.get("profileName") or config.get("provider") or provider
    source = f"{provider}:{base_url or ''}:{raw_profile}:{raw_session or request.get('model') or ''}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"tess-core-{digest}"


def open_ai_prompt_cache_request_options(
    *,
    mode: str,
    base_url: str | None,
    config: dict[str, Any] | None,
    request: dict[str, Any],
    request_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = resolve_prompt_cache_policy(config=config, request=request, options=request_options)
    if not policy["enabled"]:
        return dict(request_options or {})
    out = dict(request_options or {})
    key = provider_affinity_key(provider="openai", base_url=base_url, config=config, request=request)
    if _supports_openai_prompt_cache_key(mode, base_url):
        out.setdefault("prompt_cache_key", key)
    if _supports_openai_prompt_cache_retention(mode, base_url) and policy.get("retention") in {"in_memory", "24h"}:
        out.setdefault("prompt_cache_retention", policy["retention"])
    headers = dict(out.get("extra_headers") or {})
    if _is_fireworks_base_url(base_url):
        headers.setdefault("x-session-affinity", key)
        isolation = policy.get("isolation")
        if isolation:
            headers.setdefault("x-prompt-cache-isolation-key", _isolation_key(str(isolation), key))
    if _is_xai_base_url(base_url):
        headers.setdefault("x-grok-conv-id", key)
    if headers:
        out["extra_headers"] = headers
    return out


def normalize_cache_usage_fields(usage: dict[str, Any], raw_usage: Any) -> None:
    cached = _first_int(
        _get_path(raw_usage, ("prompt_tokens_details", "cached_tokens")),
        _get_path(raw_usage, ("input_tokens_details", "cached_tokens")),
        _get_path(raw_usage, ("cached_tokens",)),
        _get_path(raw_usage, ("prompt_cache_hit_tokens",)),
    )
    miss = _first_int(
        _get_path(raw_usage, ("prompt_cache_miss_tokens",)),
        _get_path(raw_usage, ("prompt_cache_creation_tokens",)),
    )
    if cached is not None:
        usage["cache_read_input_tokens"] = cached
        usage["cache_hit_input_tokens"] = cached
    if miss is not None:
        usage["cache_miss_input_tokens"] = miss


def is_kimi_anthropic_coding_backend(config: dict[str, Any] | None) -> bool:
    base = str((config or {}).get("anthropicBaseUrl") or "").strip().rstrip("/").lower()
    return "api.kimi.com/coding" in base


def _anthropic_explicit_policy_enabled(policy: dict[str, Any] | None) -> bool:
    return bool(
        policy
        and policy.get("enabled")
        and not policy.get("compatibilitySuppressedReason")
        and policy.get("explicit") is not False
        and policy.get("mode") in {"auto", "explicit"}
    )


def _resolve_anthropic_compatibility(
    config: dict[str, Any], profile: dict[str, Any]
) -> str:
    base_url = str(
        profile.get("baseUrl")
        or profile.get("base_url")
        or config.get("anthropicBaseUrl")
        or ""
    ).strip()
    profile_name = str(
        config.get("providerProfileName") or profile.get("name") or ""
    )
    auth = profile.get("auth") if isinstance(profile.get("auth"), dict) else {}
    if not base_url:
        return "official"
    if auth.get("kind") == "anthropic_auth_token" or "claude-code" in profile_name.lower():
        return "claude_code"
    host = _hostname(base_url)
    if "kimi" in host or "moonshot" in host:
        return "claude_code"
    return "compatible"


def _bool_setting(value: Any, default: bool | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_choice(value: Any, choices: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in choices else default


def _get_path(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            if value is not None:
                return int(value)
        except Exception:
            continue
    return None


def _hostname(base_url: str | None) -> str:
    if not base_url or not str(base_url).strip():
        return "api.openai.com"
    try:
        return (urlparse(str(base_url)).hostname or "").lower()
    except Exception:
        return ""


def _supports_openai_prompt_cache_key(mode: str, base_url: str | None) -> bool:
    host = _hostname(base_url)
    return mode in {"responses", "codex"} and host in {"api.openai.com", "chatgpt.com", "api.x.ai"}


def _supports_openai_prompt_cache_retention(mode: str, base_url: str | None) -> bool:
    return mode in {"responses", "codex"} and _hostname(base_url) in {"api.openai.com", "chatgpt.com"}


def _is_fireworks_base_url(base_url: str | None) -> bool:
    return "fireworks.ai" in _hostname(base_url)


def _is_xai_base_url(base_url: str | None) -> bool:
    return _hostname(base_url) == "api.x.ai"


def _isolation_key(scope: str, affinity_key: str) -> str:
    if scope == "profile":
        return affinity_key.rsplit("-", 1)[0]
    if scope == "session":
        return affinity_key
    return hashlib.sha256(f"{scope}:{affinity_key}".encode("utf-8")).hexdigest()[:32]
