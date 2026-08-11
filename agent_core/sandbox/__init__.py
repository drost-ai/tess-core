from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Callable


SandboxMode = str
SandboxPolicy = dict[str, Any]


class SandboxUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxBackend:
    name: str
    enforcement: str
    reason: str | None = None

    def wrap(self, argv: list[str], policy: SandboxPolicy) -> list[str]:
        return list(argv)


class SeatbeltSandboxBackend(SandboxBackend):
    def __init__(self) -> None:
        super().__init__("seatbelt", "os")

    def wrap(self, argv: list[str], policy: SandboxPolicy) -> list[str]:
        profile = _seatbelt_profile(policy)
        return ["sandbox-exec", "-p", profile, *argv]


class BubblewrapSandboxBackend(SandboxBackend):
    def __init__(self) -> None:
        super().__init__("bubblewrap", "os")

    def wrap(self, argv: list[str], policy: SandboxPolicy) -> list[str]:
        wrapped = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/"]
        for root in policy.get("writableRoots") or []:
            wrapped.extend(["--bind", root, root])
        if not policy.get("allowNetwork"):
            wrapped.append("--unshare-net")
        wrapped.extend(["--", *argv])
        return wrapped


def normalizeSandboxConfig(value: Any = None, cwd: str | None = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = _normalize_mode(raw.get("mode"))
    config: dict[str, Any] = {"mode": mode}
    if isinstance(raw.get("backend"), str):
        config["backend"] = raw["backend"].strip().lower()
    if "writableRoots" in raw:
        config["writableRoots"] = _resolve_root_list(cwd, raw.get("writableRoots"))
    if "readableRoots" in raw:
        config["readableRoots"] = _resolve_root_list(cwd, raw.get("readableRoots"))
    if "allowNetwork" in raw:
        config["allowNetwork"] = bool(raw.get("allowNetwork"))
    if "allowSubprocess" in raw:
        config["allowSubprocess"] = bool(raw.get("allowSubprocess"))
    return config


def resolveSandboxPolicy(
    config: dict[str, Any] | None = None,
    *,
    session: dict[str, Any] | None = None,
    cwd: str | None = None,
    extraWritableRoots: list[str] | tuple[str, ...] | None = None,
) -> SandboxPolicy:
    sandbox = _sandbox_config(config)
    root = os.path.abspath(cwd or (session or {}).get("cwd") or (config or {}).get("cwd") or os.getcwd())
    writable = sandbox.get("writableRoots")
    readable = sandbox.get("readableRoots")
    writable_roots = _dedupe_roots([*(writable if isinstance(writable, list) else [root]), *(extraWritableRoots or [])], root)
    readable_roots = _dedupe_roots(readable if isinstance(readable, list) else [root], root)
    return {
        "mode": _normalize_mode(sandbox.get("mode")),
        "backend": sandbox.get("backend"),
        "writableRoots": writable_roots,
        "readableRoots": readable_roots,
        "allowNetwork": bool(sandbox.get("allowNetwork", False)),
        "allowSubprocess": bool(sandbox.get("allowSubprocess", True)),
    }


def resolveSandboxBackend(
    config: dict[str, Any] | None = None,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> SandboxBackend:
    sandbox = _sandbox_config(config)
    mode = _normalize_mode(sandbox.get("mode"))
    if mode == "off":
        return SandboxBackend("none", "none", "sandbox mode is off")
    requested = str(sandbox.get("backend") or "").strip().lower()
    system = (platform or sys.platform or _platform.system()).lower()
    find = which or shutil.which
    if requested:
        return _resolve_requested_backend(requested, find)
    if system.startswith("darwin") and find("sandbox-exec"):
        return SeatbeltSandboxBackend()
    if system.startswith("linux") and find("bwrap"):
        return BubblewrapSandboxBackend()
    return SandboxBackend("none", "none", "no supported sandbox backend is available")


def prepareSandboxedArgv(
    argv: list[str],
    *,
    config: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    cwd: str | None = None,
    extraWritableRoots: list[str] | tuple[str, ...] | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    policy = resolveSandboxPolicy(config, session=session, cwd=cwd, extraWritableRoots=extraWritableRoots)
    mode = policy["mode"]
    backend = resolveSandboxBackend({"sandbox": policy})
    if mode == "off":
        return {"argv": list(argv), "policy": policy, "backend": _backend_record(backend)}
    if mode == "os-required" and backend.enforcement != "os":
        # Anything below mandatory OS enforcement (including an advisory
        # backend) is unavailable to a caller that demanded isolation:
        # refuse loudly, never degrade silently.
        raise SandboxUnavailableError(
            f"Sandbox enforcement required but unavailable: {backend.reason or backend.name or 'no backend'}"
        )
    if backend.enforcement == "none":
        _emit(
            emit,
            "sandbox.downgraded",
            {
                "requested": mode,
                "actual": "none",
                "backend": backend.name,
                "reason": backend.reason,
                "writableRoots": policy["writableRoots"],
                "allowNetwork": policy["allowNetwork"],
            },
        )
        return {"argv": list(argv), "policy": policy, "backend": _backend_record(backend)}
    wrapped = backend.wrap(list(argv), policy)
    if backend.enforcement == "os":
        _emit(
            emit,
            "sandbox.applied",
            {
                "backend": backend.name,
                "enforcement": backend.enforcement,
                "writableRoots": policy["writableRoots"],
                "allowNetwork": policy["allowNetwork"],
            },
        )
    else:
        _emit(
            emit,
            "sandbox.downgraded",
            {
                "requested": mode,
                "actual": backend.enforcement,
                "backend": backend.name,
                "reason": backend.reason,
                "writableRoots": policy["writableRoots"],
                "allowNetwork": policy["allowNetwork"],
            },
        )
    return {"argv": wrapped, "policy": policy, "backend": _backend_record(backend)}


def _sandbox_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {"mode": "off"}
    nested = config.get("sandbox")
    raw = nested if isinstance(nested, dict) else config
    return raw if isinstance(raw, dict) else {"mode": "off"}


def _resolve_requested_backend(name: str, which: Callable[[str], str | None]) -> SandboxBackend:
    if name in {"none", "off"}:
        return SandboxBackend("none", "none", "sandbox backend forced to none")
    if name == "seatbelt":
        return SeatbeltSandboxBackend() if which("sandbox-exec") else SandboxBackend("none", "none", "sandbox-exec is not available")
    if name in {"bubblewrap", "bwrap"}:
        return BubblewrapSandboxBackend() if which("bwrap") else SandboxBackend("none", "none", "bwrap is not available")
    if name == "landlock":
        return SandboxBackend("none", "none", "the landlock launcher shim was removed because it enforced nothing; use bubblewrap")
    return SandboxBackend("none", "none", f"unknown sandbox backend: {name}")


def _normalize_mode(value: Any) -> SandboxMode:
    raw = str(value or "").strip().lower()
    return raw if raw in {"off", "auto", "os-required"} else "off"


def _resolve_root_list(cwd: str | None, value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    root = os.path.abspath(cwd or os.getcwd())
    return _dedupe_roots([str(item) for item in value if isinstance(item, str) and item.strip()], root)


def _dedupe_roots(values: list[str] | tuple[str, ...], cwd: str) -> list[str]:
    seen: set[str] = set()
    roots: list[str] = []
    for value in values:
        path = os.path.expanduser(os.path.expandvars(str(value)))
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        path = os.path.realpath(path)
        if path not in seen:
            seen.add(path)
            roots.append(path)
    return roots


def _seatbelt_profile(policy: SandboxPolicy) -> str:
    lines = ["(version 1)", "(deny default)", "(allow process*)", "(allow file-read*)"]
    write_roots = policy.get("writableRoots") or []
    if write_roots:
        paths = " ".join(f"(subpath {json.dumps(path)})" for path in write_roots)
        lines.append(f"(allow file-write* {paths})")
    if policy.get("allowNetwork"):
        lines.append("(allow network*)")
    return "\n".join(lines)


def _backend_record(backend: SandboxBackend) -> dict[str, Any]:
    return {"name": backend.name, "enforcement": backend.enforcement, "reason": backend.reason}


def _emit(emit: Callable[[str, dict[str, Any]], Any] | None, event: str, data: dict[str, Any]) -> None:
    if emit:
        emit(event, data)


__all__ = [
    "BubblewrapSandboxBackend",
    "SandboxBackend",
    "SandboxMode",
    "SandboxPolicy",
    "SandboxUnavailableError",
    "SeatbeltSandboxBackend",
    "normalizeSandboxConfig",
    "prepareSandboxedArgv",
    "resolveSandboxBackend",
    "resolveSandboxPolicy",
]
