from __future__ import annotations

from typing import Any, Callable


def activeSessionPayload(session: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    return {
        "session_id": session.get("id"),
        "cwd": session.get("cwd"),
        "provider": session.get("provider"),
        "model": session.get("model"),
        "permission_mode": session.get("permissionMode") or config.get("permissionMode"),
        "workspace_name": metadata.get("workspaceName"),
        "git_root": metadata.get("gitRoot"),
        "git_branch": metadata.get("gitBranch"),
        "remote_session_id": metadata.get("remoteSessionId"),
        "bridge_id": metadata.get("bridgeId"),
        "updated_at": session.get("updatedAt"),
    }


def createSessionRuntimeController(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    active_session = args.get("session") or {}
    active_config = args.get("config") or {}
    emit: Callable[[str, dict[str, Any]], Any] = args.get("emitRuntimeEvent") or args.get("emit") or (lambda *_: None)

    def get_active_session() -> dict[str, Any]:
        return active_session

    def get_active_config() -> dict[str, Any]:
        return active_config

    def set_active_session(session: dict[str, Any], config: dict[str, Any]) -> None:
        nonlocal active_session, active_config
        active_session = session
        active_config = config
        emit("session.activated", activeSessionPayload(active_session, active_config))

    def start_turn(turn: dict[str, Any] | None = None, **turn_kwargs: Any) -> None:
        turn = {**(turn or {}), **turn_kwargs}
        emit(
            "turn.started",
            {
                "session_id": active_session.get("id"),
                "prompt": turn.get("prompt"),
                "model": turn.get("model") or active_session.get("model"),
                "agent_name": turn.get("agentName") or turn.get("agent_name"),
            },
        )

    def set_turn_cancelling() -> None:
        emit("turn.cancelling", {"session_id": active_session.get("id")})

    def finish_turn(turn: dict[str, Any] | None = None, **turn_kwargs: Any) -> None:
        turn = {**(turn or {}), **turn_kwargs}
        kind = turn.get("kind") or "completed"
        emit(
            "turn.cancelled" if kind == "cancelled" else "turn.completed",
            {
                "session_id": active_session.get("id"),
                "stop_reason": turn.get("stopReason") or turn.get("stop_reason"),
                "message": turn.get("message"),
                "model": turn.get("model") or active_session.get("model"),
            },
        )

    def fail_turn(error: BaseException | str) -> None:
        emit("turn.failed", {"session_id": active_session.get("id"), "message": str(error)})

    controller = {
        "session": active_session,
        "runtime": args.get("runtime"),
        "getActiveSession": get_active_session,
        "getActiveConfig": get_active_config,
        "setActiveSession": set_active_session,
        "startTurn": start_turn,
        "setTurnCancelling": set_turn_cancelling,
        "finishTurn": finish_turn,
        "failTurn": fail_turn,
        "close": lambda: None,
    }
    if active_session:
        set_active_session(active_session, active_config)
    return controller
