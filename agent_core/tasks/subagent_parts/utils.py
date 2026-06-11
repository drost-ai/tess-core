from __future__ import annotations

from .shared import *


def _subagent_started_payload(prepared: dict[str, Any], args: dict[str, Any], background: bool) -> dict[str, Any]:
    context = args["context"]
    child_record = prepared["childRecord"]
    child_session = prepared["childSession"]
    return {
        "agent_id": prepared["agentId"],
        "session_id": child_session["id"],
        "parent_session_id": context["session"]["id"],
        "attachable_session_id": child_session["id"],
        "agent_type": child_record.get("agentType"),
        "label": child_record.get("name") or child_record.get("agentType") or prepared["agentId"],
        "description": args.get("description"),
        "status": "running",
        "run_in_background": background,
        "runtime_contract": prepared.get("runtimeContractSummary"),
        "worktree_path": prepared.get("worktreePath"),
    }


async def _create_subagent_worktree(context: dict[str, Any], agent_id: str, child_name: str) -> str:
    suffix = _slugify(child_name) or agent_id[-8:]
    result = await createGitWorktree({"cwd": context["session"]["cwd"], "requestedName": suffix})
    return result["targetPath"]


def _output(context: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    context.get("outputJson", lambda *_: None)(event, _without_none_values(data))


def _without_none_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none_values(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none_values(item) for item in value]
    return value


def _dedupe(values: list[Any]) -> list[str]:
    out = []
    for value in values:
        if isinstance(value, str) and value not in out:
            out.append(value)
    return out


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected {name} to be a non-empty string")
    return value


def _slugify(value: str) -> str:
    return __import__("re").sub(r"(^-+|-+$)", "", __import__("re").sub(r"[^a-z0-9]+", "-", value.lower()))[:24]


def _throw_if_subagent_cancelled(context: dict[str, Any]) -> None:
    signal = context.get("abortSignal")
    if not _signal_aborted(signal):
        return
    reason = _signal_reason(signal)
    if isinstance(reason, BaseException):
        raise reason
    error = RuntimeError(str(reason or "Subagent cancelled"))
    error.name = "AbortError"  # type: ignore[attr-defined]
    raise error


def _is_subagent_abort_error(error: BaseException, signal: Any = None) -> bool:
    if _signal_aborted(signal):
        return True
    if error.__class__.__name__ == "AbortError":
        return True
    return getattr(error, "name", None) == "AbortError"


def _signal_aborted(signal: Any) -> bool:
    if not signal:
        return False
    if isinstance(signal, dict):
        return bool(signal.get("aborted"))
    return bool(getattr(signal, "aborted", False))


def _signal_reason(signal: Any) -> Any:
    if not signal:
        return None
    if isinstance(signal, dict):
        return signal.get("reason")
    return getattr(signal, "reason", None)


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)
