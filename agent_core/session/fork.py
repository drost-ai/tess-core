from __future__ import annotations

import copy
from typing import Any

from ..contracts.records import SessionRecord
from ..providers.usage_costs import createUsageTotals
from ..storage.message_history import create_session_message_list as _create_session_message_list


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


def create_forked_session(
    *,
    parent: SessionRecord,
    id: str,
    cwd: str,
    now: str,
    model: str,
    system_prompt: str,
    permission_mode: str,
    original_cwd: str,
    name: str | None = None,
    effort: str | None = None,
    assistant_name: str | None = None,
    assistant_description: str | None = None,
    worktree_path: str | None = None,
    tmux_session_name: str | None = None,
    tmux_window_name: str | None = None,
    tmux_mode: str | None = None,
) -> SessionRecord:
    metadata = {
        "originalCwd": original_cwd,
        "assistantName": assistant_name,
        "assistantDescription": assistant_description,
        "worktreePath": worktree_path,
        "forkedFromSessionId": parent["id"],
        "usage": _empty_usage(),
        "compactions": 0,
        "depth": 0,
        "childSessionIds": [],
        "childAgentIds": [],
        "tmuxSessionName": tmux_session_name,
        "tmuxWindowName": tmux_window_name,
        "tmuxMode": tmux_mode,
    }
    metadata = {key: value for key, value in metadata.items() if value is not None}
    return {
        "id": id,
        "cwd": cwd,
        "createdAt": now,
        "updatedAt": now,
        "name": name,
        "provider": parent["provider"],
        "providerProfileName": parent.get("providerProfileName"),
        "model": model,
        "effort": effort,
        "systemPrompt": system_prompt,
        "permissionMode": permission_mode,
        "messages": _create_session_message_list(copy.deepcopy(parent.get("messages") or [])),
        "todos": [],
        "metadata": metadata,
    }


def create_fork_archive_header(session: dict[str, Any], persistedAt: str) -> dict[str, Any] | None:
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


