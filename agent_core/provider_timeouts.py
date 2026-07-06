from __future__ import annotations

from typing import Any


def resolve_provider_timeout_policy(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, int]:
    args = {**(args or {}), **kwargs}
    profile = args.get("profile") or (args.get("config") or {}).get("providerProfile") or {}
    per_call = args.get("perCallOptions") or {}
    policy: dict[str, int] = {}
    request_start = _positive_int(per_call.get("requestStartTimeoutMs")) or _positive_int(profile.get("requestStartTimeoutMs"))
    stream_idle = _positive_int(per_call.get("streamIdleTimeoutMs")) or _positive_int(profile.get("streamIdleTimeoutMs"))
    json_response = _positive_int(per_call.get("jsonResponseTimeoutMs"))
    if request_start:
        policy["requestStartTimeoutMs"] = request_start
    if stream_idle:
        policy["streamIdleTimeoutMs"] = stream_idle
    if json_response:
        policy["jsonResponseTimeoutMs"] = json_response
    return policy


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(str(value).strip())) if str(value).strip() else 0
    except Exception:
        return None
    return parsed if parsed > 0 else None


resolveProviderTimeoutPolicy = resolve_provider_timeout_policy
