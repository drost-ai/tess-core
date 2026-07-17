from __future__ import annotations

import copy
import json
import math
import os
import stat
import time
import uuid
from typing import Any

from ._utils import now_iso
from .prompt_cache_state_schema import isPromptCacheStateV1
from .providers.prompt_cache_diagnostics import (
    MAX_PROMPT_CACHE_STATE_BYTES,
    MAX_RESPONSE_ID_BYTES,
    diagnosePromptCache,
)
from .runtime_identifiers import parseSessionId
from .runtime_paths import RuntimePaths, runWithRuntimePaths
from .storage import withRecordTransaction


_local_dispatch_sequence = 0
_PROTECTED_UNAVAILABLE = {"future_schema", "session_mismatch", "unsafe_path", "changed_during_read", "io_error"}


def promptCacheStatePath(sessionId: str, paths: RuntimePaths) -> str:
    return os.path.join(paths.sessionsDir, parseSessionId(sessionId), "prompt-cache-state.json")


def createPromptCacheDispatchOrder(args: dict[str, Any]) -> dict[str, Any]:
    global _local_dispatch_sequence
    _local_dispatch_sequence = (_local_dispatch_sequence + 1) % ((1 << 53) - 1)
    return {
        "runtimeGeneration": _positive_integer(args.get("runtimeGeneration"), 1),
        "messageCount": _nonnegative_integer(args.get("messageCount")),
        "reactiveAttempt": _nonnegative_integer(args.get("reactiveAttempt")),
        "startedAtMs": _nonnegative_integer(args.get("nowMs", int(time.time() * 1000))),
        "localSequence": _local_dispatch_sequence,
        "nonce": str(uuid.uuid4()),
    }


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_PROMPT_CACHE_STATE_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def comparePromptCacheDispatchOrder(left: dict[str, Any], right: dict[str, Any]) -> int:
    for key in ("runtimeGeneration", "messageCount", "reactiveAttempt", "startedAtMs", "localSequence"):
        if left.get(key) != right.get(key):
            return -1 if left.get(key, -1) < right.get(key, -1) else 1
    left_nonce, right_nonce = str(left.get("nonce") or ""), str(right.get("nonce") or "")
    return -1 if left_nonce < right_nonce else 1 if left_nonce > right_nonce else 0


async def readPromptCacheState(args: dict[str, Any]) -> dict[str, Any]:
    session_id, paths = args.get("sessionId"), args.get("paths")
    try:
        path = promptCacheStatePath(session_id, paths)
    except Exception:
        return {"status": "unavailable", "reason": "unsafe_path"}
    if not _safe_parent(path, paths.sessionsDir):
        return {"status": "unavailable", "reason": "unsafe_path"}
    try:
        before_path = os.lstat(path)
    except FileNotFoundError:
        return {"status": "missing", "reason": "missing"}
    except OSError:
        return {"status": "unavailable", "reason": "io_error"}
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        return {"status": "unavailable", "reason": "unsafe_path"}
    if before_path.st_size > MAX_PROMPT_CACHE_STATE_BYTES:
        return {"status": "unavailable", "reason": "oversized"}
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before_fd = os.fstat(descriptor)
        if not stat.S_ISREG(before_fd.st_mode) or _identity(before_path) != _identity(before_fd):
            return {"status": "unavailable", "reason": "changed_during_read"}
        raw = _read_bounded(descriptor)
        after_fd = os.fstat(descriptor)
        after_path = os.lstat(path)
        if len(raw) > MAX_PROMPT_CACHE_STATE_BYTES:
            return {"status": "unavailable", "reason": "oversized"}
        if (
            stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _identity(after_path) != _identity(after_fd)
            or _identity(before_fd) != _identity(after_fd)
            or before_fd.st_size != after_fd.st_size
            or before_fd.st_mtime_ns != after_fd.st_mtime_ns
        ):
            return {"status": "unavailable", "reason": "changed_during_read"}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "unavailable", "reason": "malformed"}
        schema = parsed.get("schemaVersion") if isinstance(parsed, dict) else None
        if isinstance(schema, (int, float)) and not isinstance(schema, bool) and schema > 1:
            return {"status": "unavailable", "reason": "future_schema"}
        if not isPromptCacheStateV1(parsed):
            return {"status": "unavailable", "reason": "invalid_schema"}
        if parsed["sessionId"] != session_id:
            return {"status": "unavailable", "reason": "session_mismatch"}
        return {"status": "valid", "state": parsed}
    except OSError:
        return {"status": "unavailable", "reason": "io_error"}
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def commitPromptCacheObservation(candidate: dict[str, Any]) -> dict[str, Any]:
    async def transact() -> dict[str, Any]:
        async with withRecordTransaction("prompt-cache-state", candidate["sessionId"]):
            current = await readPromptCacheState(candidate)
            if current["status"] == "unavailable":
                if current.get("reason") in _PROTECTED_UNAVAILABLE:
                    return {"status": "unavailable", "reason": current.get("reason")}
                await _quarantine_invalid_state(candidate["sessionId"], candidate["paths"])
                current = {"status": "missing", "reason": "missing"}
            current_state = current.get("state")
            if current_state and comparePromptCacheDispatchOrder(current_state["dispatchOrder"], candidate["dispatchOrder"]) >= 0:
                return {"status": "stale", "state": current_state, "reason": "newer_dispatch_committed"}
            local = diagnosePromptCache({
                "previous": current_state.get("request") if current_state else None,
                "current": candidate["snapshot"],
                "usage": candidate.get("usage") or {},
                "previousCommittedAt": current_state.get("committedAt") if current_state else None,
                "ttl": candidate["ttl"],
                "compatibility": "compatible" if candidate["snapshot"].get("routeClass") == "compatible" else None,
                "evictionPending": bool((current_state or {}).get("eviction", {}).get("pending")),
            })
            usage = candidate.get("usage") or {}
            state = {
                "schemaVersion": 1,
                "sessionId": candidate["sessionId"],
                "revision": int((current_state or {}).get("revision") or 0) + 1,
                "committedAt": now_iso(),
                "dispatchOrder": copy.deepcopy(candidate["dispatchOrder"]),
                "request": copy.deepcopy(candidate["snapshot"]),
                "placement": {
                    "mode": candidate["plan"]["mode"],
                    "ttl": candidate["ttl"],
                    "scope": candidate["scope"],
                    "placements": list(candidate["plan"]["placements"]),
                },
                "response": {
                    "cacheReadInputTokens": _usage_number(usage.get("cache_read_input_tokens")),
                    "cacheCreationInputTokens": _usage_number(usage.get("cache_creation_input_tokens")),
                    "cacheCreation5mInputTokens": _usage_number(usage.get("cache_creation_5m_input_tokens")),
                    "cacheCreation1hInputTokens": _usage_number(usage.get("cache_creation_1h_input_tokens")),
                },
                "diagnosis": {"localReason": local["reason"], "confidence": local["confidence"]},
            }
            response_id = _bounded_response_id(candidate.get("responseId"))
            if response_id:
                state["response"]["id"] = response_id
            if candidate.get("providerDiagnosis"):
                state["response"]["providerDiagnosis"] = candidate["providerDiagnosis"]
            if len(_serialized(state)) > MAX_PROMPT_CACHE_STATE_BYTES:
                return {"status": "unavailable", "reason": "state_size_limit"}
            await _write_private_state(promptCacheStatePath(candidate["sessionId"], candidate["paths"]), state, candidate["paths"])
            return {"status": "committed", "state": state}

    try:
        return await runWithRuntimePaths(candidate["paths"], transact)
    except Exception:
        return {"status": "unavailable", "reason": "state_write_failed"}


async def markPromptCacheEvictionIntent(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("finalizationKey")
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 256:
        return {"status": "unavailable", "reason": "invalid_finalization_key"}

    async def transact() -> dict[str, Any]:
        async with withRecordTransaction("prompt-cache-state", args["sessionId"]):
            current = await readPromptCacheState(args)
            state = current.get("state")
            if not state:
                return {"status": "unavailable", "reason": current.get("reason", "missing")}
            eviction = state.get("eviction") or {}
            if eviction.get("pending") and eviction.get("finalizationKey") == key:
                return {"status": "already_recorded", "state": state}
            state = copy.deepcopy(state)
            state["revision"] += 1
            state["eviction"] = {
                "pending": True,
                "requestedAt": now_iso(),
                "reason": "terminal_session_completion",
                "finalizationKey": key,
            }
            await _write_private_state(promptCacheStatePath(args["sessionId"], args["paths"]), state, args["paths"])
            return {"status": "recorded", "state": state}

    try:
        return await runWithRuntimePaths(args["paths"], transact)
    except Exception:
        return {"status": "unavailable", "reason": "state_write_failed"}


async def _write_private_state(path: str, value: dict[str, Any], paths: RuntimePaths) -> None:
    if not _safe_parent(path, paths.sessionsDir):
        raise OSError("unsafe prompt-cache state path")
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    temporary = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        payload = _serialized(value)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


async def _quarantine_invalid_state(session_id: str, paths: RuntimePaths) -> None:
    source = promptCacheStatePath(session_id, paths)
    try:
        os.replace(source, f"{source}.invalid-{int(time.time() * 1000)}-{uuid.uuid4()}")
    except FileNotFoundError:
        pass


def _safe_parent(path: str, sessions_dir: str) -> bool:
    parent = os.path.dirname(path)
    try:
        root_real = os.path.realpath(sessions_dir)
        parent_real = os.path.realpath(parent)
        return os.path.commonpath((root_real, parent_real)) == root_real
    except (OSError, ValueError):
        return False


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _serialized(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _bounded_response_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value and len(value.encode("utf-8")) <= MAX_RESPONSE_ID_BYTES else None


def _usage_number(value: Any) -> int:
    return int(value) if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else 0


def _nonnegative_integer(value: Any) -> int:
    return value if not isinstance(value, bool) and isinstance(value, int) and value >= 0 else -1


def _positive_integer(value: Any, fallback: int) -> int:
    return value if not isinstance(value, bool) and isinstance(value, int) and value > 0 else fallback


prompt_cache_state_path = promptCacheStatePath
create_prompt_cache_dispatch_order = createPromptCacheDispatchOrder
compare_prompt_cache_dispatch_order = comparePromptCacheDispatchOrder
read_prompt_cache_state = readPromptCacheState
commit_prompt_cache_observation = commitPromptCacheObservation
mark_prompt_cache_eviction_intent = markPromptCacheEvictionIntent
