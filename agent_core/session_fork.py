from __future__ import annotations

import copy
from typing import Any

from .usage_costs import createUsageTotals
from .session_message_history import createSessionMessageList as _create_session_message_list


SESSION_FORK_METADATA_DISPOSITIONS = {
    "originalCwd": "recomputed",
    "assistantName": "recomputed",
    "assistantDescription": "recomputed",
    "worktreePath": "recomputed",
    "workspaceId": "recomputed",
    "workspaceMode": "recomputed",
    "workspaceRoot": "recomputed",
    "workspaceName": "recomputed",
    "gitRoot": "recomputed",
    "gitRepoPlanOnly": "inherited",
    "gitBranch": "recomputed",
    "prNumber": "recomputed",
    "prUrl": "recomputed",
    "forkedFromSessionId": "recomputed",
    "usage": "fresh",
    "compactions": "fresh",
    "compactionState": "fresh",
    "fullTranscriptLog": "fresh",
    "depth": "fresh",
    "parentSessionId": "fresh",
    "parentAgentId": "fresh",
    "agentId": "fresh",
    "agentName": "fresh",
    "agentType": "fresh",
    "subagentTaskId": "fresh",
    "subagentLaunchGroupId": "fresh",
    "subagentRuntimeContract": "fresh",
    "effectiveAgentAuthority": "fresh",
    "teamName": "fresh",
    "childSessionIds": "fresh",
    "childAgentIds": "fresh",
    "planMode": "fresh",
    "remoteSessionId": "fresh",
    "remoteSessionTitle": "fresh",
    "remoteControlEnabled": "fresh",
    "bridgeId": "fresh",
    "bridgeGeneration": "fresh",
    "channelServers": "fresh",
    "remoteLastEventAt": "fresh",
    "tmuxSessionName": "recomputed",
    "tmuxWindowName": "recomputed",
    "tmuxMode": "recomputed",
    "ideName": "fresh",
    "ideConnectedAt": "fresh",
    "chromeIntegrationEnabled": "fresh",
    "chromeIntegrationMode": "fresh",
    "tokenTracking": "fresh",
    "promptCacheTailState": "fresh",
    "providerFailureCount": "fresh",
    "lastProviderFailure": "fresh",
    "lastProviderRecoveryAt": "fresh",
    "monitorIds": "fresh",
    "taskIds": "fresh",
    "backgroundSessionIds": "fresh",
    "runtimeRestore": "fresh",
    "lowValueProgress": "fresh",
    "activeTask": "fresh",
    "finalStop": "fresh",
    "daemonUnavailable": "fresh",
}


def _empty_usage() -> dict[str, Any]:
    return createUsageTotals()


def createForkedSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    parent = values["parent"]
    metadata = {
        "originalCwd": values["originalCwd"],
        "assistantName": values.get("assistantName"),
        "assistantDescription": values.get("assistantDescription"),
        "worktreePath": values.get("worktreePath"),
        "forkedFromSessionId": parent["id"],
        "usage": _empty_usage(),
        "compactions": 0,
        "depth": 0,
        "childSessionIds": [],
        "childAgentIds": [],
        "planMode": {"state": "inactive"},
        "tmuxSessionName": values.get("tmuxSessionName"),
        "tmuxWindowName": values.get("tmuxWindowName"),
        "tmuxMode": values.get("tmuxMode"),
    }
    if parent.get("metadata", {}).get("gitRepoPlanOnly") is not None:
        metadata["gitRepoPlanOnly"] = parent["metadata"]["gitRepoPlanOnly"]
    metadata = {key: value for key, value in metadata.items() if value is not None}
    return {
        "id": values["id"],
        "cwd": values["cwd"],
        "createdAt": values["now"],
        "updatedAt": values["now"],
        "name": values.get("name"),
        "provider": parent["provider"],
        "providerProfileName": parent.get("providerProfileName"),
        "model": values["model"],
        "effort": values.get("effort"),
        "systemPrompt": values["systemPrompt"],
        "permissionMode": values["permissionMode"],
        "messages": _create_session_message_list(copy.deepcopy(parent.get("messages") or [])),
        "todos": [],
        "metadata": metadata,
    }


def createForkArchiveHeader(session: dict[str, Any], persistedAt: str) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    if metadata.get("fullTranscriptLog") or not metadata.get("forkedFromSessionId"):
        return None
    archived_metadata = copy.deepcopy(metadata)
    archived_metadata.pop("fullTranscriptLog", None)
    return {
        "type": "session_header",
        "schema_version": 1,
        "session_id": session["id"],
        "persisted_at": persistedAt,
        "session": {
            "id": session["id"],
            "cwd": session["cwd"],
            "createdAt": session["createdAt"],
            "updatedAt": session["updatedAt"],
            "name": session.get("name"),
            "provider": session["provider"],
            "providerProfileName": session.get("providerProfileName"),
            "model": session["model"],
            "effort": session.get("effort"),
            "systemPrompt": session["systemPrompt"],
            "permissionMode": session["permissionMode"],
            "todos": copy.deepcopy(session.get("todos") or []),
            "metadata": archived_metadata,
        },
    }


create_forked_session = createForkedSession
create_fork_archive_header = createForkArchiveHeader
