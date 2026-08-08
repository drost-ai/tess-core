from __future__ import annotations

import json
import os
import shutil
from typing import Any

from .._utils import now_iso
from .agents import _is_related_child_agent
from ..platform_support import collectDoctorReport
from ..storage import (
    compactionCheckpointPath,
    compactionManifestPath,
    ensureBaseLayout,
    getStateRoot,
    listChildAgents,
    listSessionMonitors,
    loadBridge,
    loadBridgeEvents,
    loadBridgeInbox,
    loadCompactionManifest,
    loadRemoteSession,
    loadSession,
    readTaskMeta,
    sessionPath,
    sessionTranscriptLogPath,
)


async def createDebugBundle(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_id = args.get("sessionId") or args.get("session_id") or (args.get("session") or {}).get("id")
    if not session_id:
        raise RuntimeError("sessionId is required.")
    cwd = args.get("cwd") or (args.get("session") or {}).get("cwd") or os.getcwd()
    await ensureBaseLayout(cwd)
    session = await loadSession(session_id) or args.get("session")
    if not session:
        raise RuntimeError(f"Session not found: {session_id}")

    bundle_dir = os.path.abspath(
        args.get("outputDir")
        or args.get("outputPath")
        or os.path.join(getStateRoot(), "logs", "debug-bundles", f"{_sanitize_timestamp(now_iso())}-{str(session_id)[:8]}")
    )
    os.makedirs(bundle_dir, exist_ok=True)

    related_agents = [record for record in await listChildAgents() if _is_related_child_agent(session, record)]
    monitors = await listSessionMonitors(session["id"])
    remote = await loadRemoteSession(session.get("metadata", {}).get("remoteSessionId")) if session.get("metadata", {}).get("remoteSessionId") else None
    bridge = await loadBridge(session.get("metadata", {}).get("bridgeId")) if session.get("metadata", {}).get("bridgeId") else None
    bridge_inbox = await loadBridgeInbox(bridge["id"]) if bridge else []
    bridge_events = await loadBridgeEvents(bridge["id"]) if bridge else []
    doctor = await collectDoctorReport(session.get("cwd") or cwd)
    compaction_manifest = await loadCompactionManifest(session["id"])
    task_ids = _unique_strings(
        [
            *[record.get("taskId") for record in related_agents if record.get("taskId")],
            *[record.get("taskId") for record in monitors if record.get("taskId")],
        ]
    )
    tasks = {task_id: await readTaskMeta(task_id) for task_id in task_ids}

    included_files: list[str] = []
    await _write_json_artifact(os.path.join(bundle_dir, "session", "session.json"), session, included_files)
    await _copy_if_exists(sessionPath(session["id"]), os.path.join(bundle_dir, "session", "session.source.json"), included_files)
    await _copy_if_exists(
        sessionTranscriptLogPath(session["id"]),
        os.path.join(bundle_dir, "session", "full-transcript-with-tools.jsonl"),
        included_files,
    )
    await _write_json_artifact(os.path.join(bundle_dir, "runtime", "child-agents.json"), related_agents, included_files)
    await _write_json_artifact(os.path.join(bundle_dir, "runtime", "monitors.json"), monitors, included_files)
    await _write_json_artifact(os.path.join(bundle_dir, "runtime", "tasks.json"), tasks, included_files)
    await _write_json_artifact(os.path.join(bundle_dir, "runtime", "doctor.json"), doctor, included_files)

    if remote:
        await _write_json_artifact(os.path.join(bundle_dir, "remote", "remote-session.json"), remote, included_files)
    if bridge:
        await _write_json_artifact(os.path.join(bundle_dir, "remote", "bridge.json"), bridge, included_files)
        await _write_json_artifact(os.path.join(bundle_dir, "remote", "bridge.inbox.json"), bridge_inbox, included_files)
        await _write_json_artifact(os.path.join(bundle_dir, "remote", "bridge.events.json"), bridge_events, included_files)

    if compaction_manifest:
        await _write_json_artifact(os.path.join(bundle_dir, "compaction", "manifest.json"), compaction_manifest, included_files)
        await _copy_if_exists(compactionManifestPath(session["id"]), os.path.join(bundle_dir, "compaction", "manifest.source.json"), included_files)
        state = session.get("metadata", {}).get("compactionState")
        artifacts = state.get("artifacts") if isinstance(state, dict) and isinstance(state.get("artifacts"), dict) else {}
        generation = state.get("latestCompactionGeneration") if isinstance(state, dict) else None
        await _copy_if_exists(artifacts.get("summary"), os.path.join(bundle_dir, "compaction", "summary.md"), included_files)
        await _copy_if_exists(artifacts.get("state"), os.path.join(bundle_dir, "compaction", "state.json"), included_files)
        await _copy_if_exists(artifacts.get("workingContext"), os.path.join(bundle_dir, "compaction", "working-context.json"), included_files)
        await _copy_if_exists(artifacts.get("activity"), os.path.join(bundle_dir, "compaction", "activity.md"), included_files)
        await _copy_if_exists(artifacts.get("checkpoint"), os.path.join(bundle_dir, "compaction", "checkpoint.json"), included_files)
        if isinstance(generation, int):
            await _copy_if_exists(
                compactionCheckpointPath(session["id"], generation),
                os.path.join(bundle_dir, "compaction", "checkpoint.source.json"),
                included_files,
            )

    created_at = now_iso()
    manifest = {
        "sessionId": session["id"],
        "bundleDir": bundle_dir,
        "manifestPath": os.path.join(bundle_dir, "manifest.json"),
        "createdAt": created_at,
        "includedFiles": included_files,
        "summary": {
            "childAgentCount": len(related_agents),
            "monitorCount": len(monitors),
            "hasCompaction": bool(compaction_manifest),
            "hasRemote": bool(remote),
            "hasBridge": bool(bridge),
            "hasTranscriptLog": any(file_path.endswith("full-transcript-with-tools.jsonl") for file_path in included_files),
            "taskCount": len(task_ids),
        },
    }
    await _write_json_artifact(manifest["manifestPath"], manifest, included_files, track=False)
    return {
        **manifest,
        "path": bundle_dir,
        "files": included_files,
    }



def _sanitize_timestamp(value: str) -> str:
    return value.replace(":", "-").replace(".", "-")


def _unique_strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


async def _write_json_artifact(file_path: str, value: Any, included_files: list[str], track: bool = True) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    if track:
        included_files.append(file_path)


async def _copy_if_exists(source_path: str | None, target_path: str, included_files: list[str]) -> None:
    if not source_path:
        return
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        shutil.copyfile(source_path, target_path)
        included_files.append(target_path)
    except FileNotFoundError:
        return
