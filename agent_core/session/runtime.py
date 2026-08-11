from __future__ import annotations

from typing import Any, Callable

from ..contracts.records import SessionRecord


def active_session_payload(session: SessionRecord, config: dict[str, Any]) -> dict[str, Any]:
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


def create_session_runtime_controller(
    *,
    session: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    binding: Any = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    runtime: Any = None,
) -> dict[str, Any]:
    active_session = session or {}
    active_config = config or {}
    active_binding = binding
    turn_in_flight = False
    emit = emit or (lambda *_: None)

    def get_active_session() -> dict[str, Any]:
        return active_session

    def get_active_config() -> dict[str, Any]:
        return active_config

    def get_active_binding() -> Any:
        return active_binding

    def set_active_session(
        session: dict[str, Any], config: dict[str, Any], binding: Any = None
    ) -> None:
        nonlocal active_session, active_config, active_binding
        active_session = session
        active_config = config
        active_binding = binding or active_binding
        payload = active_session_payload(active_session, active_config)
        if active_binding is not None:
            payload.update(
                {
                    "generation": active_binding.get("generation"),
                    "config_revision": active_binding.get("configRevision"),
                    "runtime_instance_id": active_binding.get("runtimeInstanceId"),
                    "path_graph_id": active_binding.get("pathGraphId"),
                }
            )
        emit("session.activated", payload)

    def start_turn(turn: dict[str, Any] | None = None, **turn_kwargs: Any) -> None:
        nonlocal turn_in_flight
        turn = {**(turn or {}), **turn_kwargs}
        turn_in_flight = True
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
        nonlocal turn_in_flight
        turn = {**(turn or {}), **turn_kwargs}
        turn_in_flight = False
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
        nonlocal turn_in_flight
        turn_in_flight = False
        emit("turn.failed", {"session_id": active_session.get("id"), "message": str(error)})

    controller = {
        "session": active_session,
        "runtime": runtime,
        "getActiveSession": get_active_session,
        "getActiveConfig": get_active_config,
        "getActiveBinding": get_active_binding,
        "setActiveSession": set_active_session,
        "startTurn": start_turn,
        "setTurnCancelling": set_turn_cancelling,
        "finishTurn": finish_turn,
        "failTurn": fail_turn,
        "isTurnInFlight": lambda: turn_in_flight,
        "close": lambda: None,
    }
    if active_session:
        set_active_session(active_session, active_config)
    return controller
