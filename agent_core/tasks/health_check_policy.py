from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from ..sandbox import prepareSandboxedArgv
from ..shell_command_policy import classifyReadOnlyShellCommand
from ..tools.permissions import _ensure_tool_permission, _get_effective_permission_mode


HEALTH_CHECK_LIMITS = {
    "minIntervalMs": 1_000,
    "maxIntervalMs": 24 * 60 * 60 * 1_000,
    "minTimeoutMs": 100,
    "maxTimeoutMs": 60_000,
    "maxBackoffIntervalMs": 7 * 24 * 60 * 60 * 1_000,
    "maxFailureThreshold": 100,
    "maxBackoffMultiplier": 16,
    "defaultOutputLimitBytes": 128 * 1024,
    "defaultBodyLimitBytes": 256 * 1024,
    "minAcquisitionLimitBytes": 1_024,
    "maxAcquisitionLimitBytes": 1024 * 1024,
    "maxCommandBytes": 16 * 1024,
    "maxUrlBytes": 8 * 1024,
    "maxTitleBytes": 512,
    "maxExpectedSubstringBytes": 8 * 1024,
    "maxExpectedStatusCodes": 32,
    "maxRedirects": 3,
}


async def normalizeHealthCheckSpec(
    raw: dict[str, Any],
    fallbackCwd: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    legacy = bool((options or {}).get("legacy"))
    title = _bounded_string(raw.get("title"), "title", HEALTH_CHECK_LIMITS["maxTitleBytes"])
    mode = raw.get("mode")
    if mode not in {"command", "http"}:
        _fail("mode must be command or http")
    cwd = os.path.realpath(os.path.abspath(str(raw.get("cwd") or fallbackCwd)))
    interval = _bounded_integer(raw.get("intervalMs"), "intervalMs", 1_000, 86_400_000, 10_000)
    timeout = _bounded_integer(raw.get("timeoutMs"), "timeoutMs", 100, 60_000, 10_000)
    threshold = _bounded_integer(raw.get("failureThreshold"), "failureThreshold", 1, 100, 2)
    multiplier = _bounded_number(raw.get("backoffMultiplier"), "backoffMultiplier", 1, 16, 2)
    max_interval = _bounded_integer(
        raw.get("maxIntervalMs"),
        "maxIntervalMs",
        interval,
        HEALTH_CHECK_LIMITS["maxBackoffIntervalMs"],
        min(HEALTH_CHECK_LIMITS["maxBackoffIntervalMs"], interval * 4),
    )
    output_limit = _bounded_integer(
        raw.get("outputLimitBytes"),
        "outputLimitBytes",
        1_024,
        1024 * 1024,
        HEALTH_CHECK_LIMITS["defaultOutputLimitBytes"],
    )
    body_limit = _bounded_integer(
        raw.get("bodyLimitBytes"),
        "bodyLimitBytes",
        1_024,
        1024 * 1024,
        HEALTH_CHECK_LIMITS["defaultBodyLimitBytes"],
    )
    policy = raw.get("expectedBodyPolicy")
    if policy not in {"contains", "ignore"}:
        if legacy:
            policy = "contains" if isinstance(raw.get("expectedBodySubstring"), str) and raw["expectedBodySubstring"].strip() else "ignore"
        elif mode == "command":
            policy = "ignore"
        else:
            _fail("expectedBodyPolicy must explicitly be ignore or contains for HTTP checks")
    substring = (
        _bounded_string(
            raw.get("expectedBodySubstring"),
            "expectedBodySubstring",
            HEALTH_CHECK_LIMITS["maxExpectedSubstringBytes"],
        )
        if policy == "contains"
        else None
    )
    base = {
        "title": title,
        "mode": mode,
        "cwd": cwd,
        "intervalMs": interval,
        "timeoutMs": timeout,
        "failureThreshold": threshold,
        "backoffMultiplier": multiplier,
        "maxIntervalMs": max_interval,
        "outputLimitBytes": output_limit,
        "bodyLimitBytes": body_limit,
        "expectedBodyPolicy": policy,
    }
    if substring is not None:
        base["expectedBodySubstring"] = substring
    if mode == "command":
        if raw.get("url") not in {None, ""}:
            _fail("command checks cannot include url")
        base["command"] = _bounded_string(
            raw.get("command"), "command", HEALTH_CHECK_LIMITS["maxCommandBytes"]
        )
        return base
    if raw.get("command") not in {None, ""}:
        _fail("HTTP checks cannot include command")
    base["url"] = _normalize_url(raw.get("url"))
    base["expectedStatusCodes"] = _normalize_statuses(raw.get("expectedStatusCodes"), legacy)
    return base


async def authorizeHealthCheckSpec(
    context: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    if spec["mode"] == "command":
        await _ensure_tool_permission(
            context,
            "TaskHealthCheckStart",
            "shell",
            f"Schedule recurring shell health check\n{spec['command']}",
            always_ask=True,
            command=spec["command"],
        )
    else:
        await _ensure_tool_permission(
            context,
            "TaskHealthCheckStart",
            "network",
            f"Schedule recurring HTTP health check\n{spec['url']}",
            always_ask=True,
            url=spec["url"],
        )
    permission_mode = await _get_effective_permission_mode(context)
    environment = _health_environment(os.environ)
    sandbox: dict[str, Any]
    shell_path = None
    shell_policy = None
    allowed_origins = None
    if spec["mode"] == "command":
        shell_path = os.path.realpath(os.environ.get("SHELL") or ("cmd.exe" if os.name == "nt" else "/bin/sh"))
        base_argv = [shell_path, "-lc", spec["command"]]
        launch = prepareSandboxedArgv(
            base_argv,
            config=context.get("config") or {},
            session=context.get("session"),
            cwd=spec["cwd"],
            emit=context.get("outputJson"),
        )
        wrapped = launch["argv"]
        prefix = wrapped[: -len(base_argv)] if len(wrapped) >= len(base_argv) and wrapped[-len(base_argv) :] == base_argv else []
        sandbox = _sandbox_snapshot(launch, prefix)
        classification = classifyReadOnlyShellCommand(spec["command"])
        shell_policy = {
            "safe": bool(classification.get("safe")),
            "reason": str(classification.get("reason") or "unclassified"),
        }
        if isinstance(classification.get("argv"), list):
            shell_policy["argv"] = list(classification["argv"])
    else:
        _assert_network_allowed(context)
        sandbox = {
            "mode": "disabled",
            "backend": "none",
            "filesystem": _filesystem_authority(context),
            "network": True,
        }
        parsed = urlparse(spec["url"])
        allowed_origins = [f"{parsed.scheme}://{parsed.netloc}"]
    now = _now_iso()
    policy: dict[str, Any] = {
        "schemaVersion": 1,
        "permissionMode": permission_mode,
        "permissionSource": "approved",
        "authorizedAt": now,
        "sandbox": sandbox,
        "environment": environment,
        "maxRedirects": HEALTH_CHECK_LIMITS["maxRedirects"],
    }
    if shell_path:
        policy["shellPath"] = shell_path
        policy["shellPolicy"] = shell_policy
    if allowed_origins:
        policy["allowedOrigins"] = allowed_origins
    fingerprint_input = {
        "sessionId": context["session"]["id"],
        "cwd": spec["cwd"],
        "specDigest": healthCheckSpecDigest(spec),
        "permission": policy,
        "effectiveAuthority": (context["session"].get("metadata") or {}).get("effectiveAgentAuthority"),
        "allowedRoots": sorted(context.get("config", {}).get("allowedRoots") or []),
    }
    policy["authorityFingerprint"] = _digest(fingerprint_input)
    assertHealthCheckExecutionPolicy(spec, policy)
    return policy


def healthCheckSpecDigest(spec: dict[str, Any]) -> str:
    return _digest(spec)


def assertHealthCheckExecutionPolicy(spec: dict[str, Any], policy: dict[str, Any]) -> None:
    sandbox = policy.get("sandbox") if isinstance(policy.get("sandbox"), dict) else {}
    environment = policy.get("environment")
    valid = (
        policy.get("schemaVersion") == 1
        and policy.get("permissionMode") in {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}
        and isinstance(policy.get("permissionSource"), str)
        and bool(policy.get("permissionSource"))
        and isinstance(policy.get("authorizedAt"), str)
        and isinstance(policy.get("authorityFingerprint"), str)
        and len(policy["authorityFingerprint"]) == 64
        and sandbox.get("mode") in {"disabled", "wrapped"}
        and sandbox.get("backend") in {"seatbelt", "bubblewrap", "landlock", "none"}
        and sandbox.get("filesystem") in {"read-only", "workspace-write", "full"}
        and isinstance(sandbox.get("network"), bool)
        and isinstance(policy.get("maxRedirects"), int)
        and 0 <= policy["maxRedirects"] <= HEALTH_CHECK_LIMITS["maxRedirects"]
        and _safe_environment(environment)
    )
    if not valid:
        raise ValueError("Health-check execution policy is malformed.")
    prefix = sandbox.get("argvPrefix")
    if sandbox["mode"] == "wrapped":
        if not isinstance(prefix, list) or not prefix or len(prefix) > 256 or any(not isinstance(item, str) or len(item.encode()) > 16 * 1024 for item in prefix):
            raise ValueError("Health-check sandbox wrapper policy is malformed.")
    elif prefix is not None:
        raise ValueError("Disabled health-check sandbox policy cannot carry a wrapper.")
    if spec["mode"] == "command":
        if not isinstance(policy.get("shellPath"), str) or not policy["shellPath"] or not isinstance(policy.get("shellPolicy"), dict) or "allowedOrigins" in policy:
            raise ValueError("Command health-check execution policy is incomplete.")
    else:
        parsed = urlparse(spec["url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if "shellPath" in policy or "shellPolicy" in policy or not sandbox["network"] or policy.get("allowedOrigins") != [origin]:
            raise ValueError("HTTP health-check network authority does not cover exactly its approved origin.")


def buildHealthCheckEnvironment(policy: dict[str, Any]) -> dict[str, str]:
    return dict(policy["environment"])


def _sandbox_snapshot(launch: dict[str, Any], prefix: list[str]) -> dict[str, Any]:
    backend = launch.get("backend") or {}
    policy = launch.get("policy") or {}
    wrapped = bool(prefix)
    result = {
        "mode": "wrapped" if wrapped else "disabled",
        "backend": backend.get("name") or "none",
        "filesystem": _filesystem_from_policy(policy),
        "network": bool(policy.get("allowNetwork")),
    }
    if wrapped:
        result["argvPrefix"] = prefix
    return result


def _filesystem_from_policy(policy: dict[str, Any]) -> str:
    writable = [os.path.realpath(str(path)) for path in policy.get("writableRoots") or []]
    if os.path.realpath(os.path.abspath(os.sep)) in writable:
        return "full"
    return "workspace-write" if writable else "read-only"


def _filesystem_authority(context: dict[str, Any]) -> str:
    authority = ((context.get("session", {}).get("metadata") or {}).get("effectiveAgentAuthority") or {}).get("sandbox") or {}
    value = authority.get("filesystem")
    if value in {"read-only", "workspace-write", "full"}:
        return value
    config = context.get("config", {}).get("sandbox") or {}
    return _filesystem_from_policy(config)


def _assert_network_allowed(context: dict[str, Any]) -> None:
    metadata = context.get("session", {}).get("metadata") or {}
    authority = metadata.get("effectiveAgentAuthority") if isinstance(metadata, dict) else {}
    sandbox = authority.get("sandbox") if isinstance(authority, dict) else {}
    configured = context.get("config", {}).get("sandbox") or {}
    allowed = sandbox.get("network")
    if allowed is None:
        allowed = configured.get("network", configured.get("allowNetwork", True))
    if allowed is False:
        raise PermissionError("HTTP health checks are disabled by the effective network policy.")


def _health_environment(source: dict[str, str]) -> dict[str, str]:
    exact = {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TEMP", "TERM", "TMP", "TMPDIR", "TZ", "USER"}
    return {key: source[key] for key in sorted(source) if key in exact or key.startswith("LC_")}


def _safe_environment(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > 32:
        return False
    exact = {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TEMP", "TERM", "TMP", "TMPDIR", "TZ", "USER"}
    return all(isinstance(key, str) and (key in exact or key.startswith("LC_")) and isinstance(entry, str) and len(entry.encode()) <= 64 * 1024 for key, entry in value.items())


def _normalize_url(value: Any) -> str:
    raw = _bounded_string(value, "url", HEALTH_CHECK_LIMITS["maxUrlBytes"])
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        _fail("url must be HTTP(S) and must not contain credentials")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, ""))


def _normalize_statuses(value: Any, legacy: bool) -> list[int]:
    if value is None and legacy:
        return [200]
    if not isinstance(value, list) or not value or len(value) > HEALTH_CHECK_LIMITS["maxExpectedStatusCodes"]:
        _fail(f"expectedStatusCodes must contain 1-{HEALTH_CHECK_LIMITS['maxExpectedStatusCodes']} codes")
    if any(isinstance(item, bool) or not isinstance(item, int) or not 100 <= item <= 599 for item in value):
        _fail("expectedStatusCodes entries must be integers from 100 through 599")
    return sorted(set(value))


def _bounded_string(value: Any, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result.encode()) > max_bytes:
        _fail(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return result


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail(f"{field} must be an integer from {minimum} through {maximum}")
    return value


def _bounded_number(value: Any, field: str, minimum: float, maximum: float, fallback: float) -> float:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        _fail(f"{field} must be a finite number from {minimum} through {maximum}")
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value) if value[key] is not None}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _now_iso() -> str:
    from .._utils import now_iso

    return now_iso()


def _fail(message: str):
    raise ValueError(f"Invalid health check: {message}.")


normalize_health_check_spec = normalizeHealthCheckSpec
authorize_health_check_spec = authorizeHealthCheckSpec
health_check_spec_digest = healthCheckSpecDigest
