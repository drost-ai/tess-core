from __future__ import annotations

from .shared import *

def createInitialRuntimeState(session: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = session.get("metadata", {}) if session else {}
    return {
        "session": (
            {
                "activeSessionId": session.get("id"),
                "summary": summarizeSessionRecord(session),
                "lastActivatedAt": session.get("updatedAt"),
            }
            if session
            else {}
        ),
        "turn": {"status": "idle", "lastOutcome": deriveStoredTurnOutcome(session) if session else None},
        "provider": {"phase": "idle"},
        "compaction": {"status": "idle"},
        "sandbox": {},
        "mcp": {"servers": {}, "catalogGeneration": 0, "lastPlanDecision": None},
        "daemon": {"phase": "offline"},
        "tasks": {"total": 0, "tasks": [], "activeCount": 0, "backgroundedCount": 0, "awaitingInputCount": 0, "failedCount": 0, "labels": []},
        "monitors": {"snapshot": {"total": 0, "active": 0, "failed": 0, "stopped": 0, "muted": 0, "labels": []}, "activeMonitorIds": [], "recentAssistantUpdates": []},
        "bridge": {"attached": bool(metadata.get("bridgeId")), "bridgeId": metadata.get("bridgeId"), "remoteSessionId": metadata.get("remoteSessionId")},
        "collaboration": {
            "workers": [],
            "activeCount": 0,
            "awaitingInputCount": 0,
            "backgroundCount": 0,
            "remoteStatus": "active" if metadata.get("remoteControlEnabled") else None,
            "remoteTitle": metadata.get("remoteSessionTitle"),
            "labels": [],
            "recentAssistantUpdates": [],
        },
    }

def createRuntimeStateStore(initialState: dict[str, Any] | None = None) -> dict[str, Any]:
    state = copy.deepcopy(initialState or createInitialRuntimeState())
    subscribers: list[Callable[[dict[str, Any]], None]] = []

    def get_state():
        return copy.deepcopy(state)

    def dispatch(event):
        nonlocal state
        next_state = reduceRuntimeState(state, event)
        if next_state == state:
            return
        state = next_state
        for subscriber in list(subscribers):
            subscriber()

    def set_state(next_state):
        nonlocal state
        state = copy.deepcopy(next_state)
        for subscriber in list(subscribers):
            subscriber()

    def subscribe(fn):
        subscribers.append(fn)
        return lambda: subscribers.remove(fn) if fn in subscribers else None

    return {"getState": get_state, "setState": set_state, "dispatch": dispatch, "subscribe": subscribe, "applyEvent": dispatch}

def summarizeSessionRecord(session: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    return {
        "id": _field(session, "id", "sessionId", "session_id"),
        "cwd": _field(session, "cwd"),
        "name": _field(session, "name"),
        "provider": _field(session, "provider"),
        "model": _field(session, "model"),
        "permissionMode": _field(session, "permissionMode", "permission_mode"),
        "updatedAt": _field(session, "updatedAt", "updated_at"),
        "workspaceName": _field(session, "workspaceName", "workspace_name") or metadata.get("workspaceName"),
        "gitRoot": _field(session, "gitRoot", "git_root") or metadata.get("gitRoot"),
        "gitBranch": _field(session, "gitBranch", "git_branch") or metadata.get("gitBranch"),
        "remoteSessionId": _field(session, "remoteSessionId", "remote_session_id") or metadata.get("remoteSessionId"),
        "bridgeId": _field(session, "bridgeId", "bridge_id") or metadata.get("bridgeId"),
    }

def deriveStoredTurnOutcome(session: dict[str, Any]) -> dict[str, Any] | None:
    text = _extract_last_assistant_text(session)
    if not text:
        return None
    return {"kind": "completed", "message": "Awaiting your reply." if text.rstrip().endswith("?") else "Previous turn complete.", "updatedAt": session.get("updatedAt")}
