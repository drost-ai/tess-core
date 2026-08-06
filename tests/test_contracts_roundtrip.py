from __future__ import annotations

import json
from pathlib import Path

import agent_core as core
from agent_core.contracts import (
    ChildAgentRecord,
    MonitorRecord,
    SessionRecord,
    TaskMeta,
    validate,
)
from tests.support.agent_core_case import AgentCoreTestCase, run


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _compacted_session() -> dict:
    session = core.createEmptySession(
        cwd="/tmp/workspace",
        provider="openai",
        providerProfileName="codex",
        model="gpt-5.4",
        effort="high",
        systemPrompt="You are a coding agent.",
        permissionMode="bypassPermissions",
    )
    session["messages"] = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "ls"}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool_1", "is_error": False, "content": "files"}],
        },
        {"role": "user", "content": "continue"},
    ]
    session["metadata"]["compactions"] = 1
    session["metadata"]["compactionState"] = {
        "latestCompactionId": "c0e8d3f2-1111-2222-3333-444455556666",
        "latestCompactionGeneration": 1,
        "latestCompactionAt": "2026-06-10T00:00:00.000Z",
        "compactedThroughMessageIndex": 4,
        "compactedMessageCount": 4,
        "recentMessageCount": 1,
        "archiveMessageCount": 5,
        "rawTokenEstimate": 1200,
        "compactedContextTokenEstimate": 300,
        "workingContextTokenEstimate": 400,
        "workingContextTokenMeasurement": None,
        "lastTrigger": "auto_hard_limit",
        "qualityClassification": "usable",
        "compactionModel": "gpt-5.3-codex-spark",
        "verificationModel": "gpt-5.3-codex-spark",
        "verification": {"verdict": "accept", "confidence": "high", "reason": "ok", "issues": [], "notes": ""},
        "artifacts": {"summary": "/tmp/artifacts/summary.md", "workingContext": "/tmp/artifacts/working-context.json"},
    }
    return session


def _forked_session() -> dict:
    session = core.createEmptySession(
        cwd="/tmp/workspace",
        provider="anthropic",
        model="claude-sonnet-4-6",
        systemPrompt="prompt",
        permissionMode="default",
    )
    session["messages"] = [{"role": "user", "content": "hello"}]
    session["metadata"]["forkedFromSessionId"] = "11111111-2222-3333-4444-555566667777"
    return session


def _subagent_parent_session() -> dict:
    session = core.createEmptySession(
        cwd="/tmp/workspace",
        provider="anthropic",
        model="claude-opus-4-6",
        systemPrompt="prompt",
        permissionMode="acceptEdits",
    )
    session["metadata"]["childSessionIds"] = ["child-session-1"]
    session["metadata"]["childAgentIds"] = ["agent-abc"]
    session["metadata"]["backgroundSessionIds"] = ["child-session-1"]
    session["metadata"]["taskIds"] = ["task-1"]
    session["metadata"]["depth"] = 0
    return session


def _dual_spelling_task_meta() -> dict:
    return {
        "type": "subagent",
        "taskId": "task-dual",
        "task_id": "task-dual",
        "id": "task-dual",
        "agent_id": "agent-abc",
        "sessionId": "child-session-1",
        "session_id": "child-session-1",
        "parent_session_id": "parent-session-1",
        "parent_task_id": None,
        "command": "python -m agent_core.tasks.runners.subagent",
        "cwd": "/tmp/workspace",
        "pid": 4242,
        "startedAt": "2026-06-10T00:00:00.000Z",
        "updatedAt": "2026-06-10T00:00:01.000Z",
        "status": "running",
        "outputFile": "/tmp/output.log",
        "title": "subagent",
        "description": "subagent",
        "attachableSessionId": "child-session-1",
        "retryable": False,
        "stoppable": True,
        "detailStatus": "worker_running",
        "owner": "daemon",
        "supervisorId": "sup-1",
    }


def _mid_finalize_child_agent() -> dict:
    return {
        "agentId": "agent-abc",
        "sessionId": "child-session-1",
        "parentSessionId": "parent-session-1",
        "name": "verify the fix",
        "agentType": "reviewer",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "mode": "default",
        "description": "verify the fix",
        "runInBackground": True,
        "status": "running",
        "startedAt": "2026-06-10T00:00:00.000Z",
        "owner": "daemon",
        "attachableSessionId": "child-session-1",
        "taskId": "task-dual",
        "pid": 4242,
        "retryable": False,
        "stoppable": True,
        "lastUpdatedAt": "2026-06-10T00:00:01.000Z",
        "lastSummary": "Running in the background.",
    }


def _monitor_record() -> dict:
    return {
        "id": "monitor-1",
        "sessionId": "parent-session-1",
        "taskId": "task-dual",
        "createdAt": "2026-06-10T00:00:00.000Z",
        "updatedAt": "2026-06-10T00:00:02.000Z",
        "status": "active",
        "goal": "watch the build",
        "summaryStyle": None,
        "checkIntervalMs": 2000,
        "heartbeatIntervalMs": 15000,
        "lastNarratedOutputSize": 0,
        "lastObservedOutputSize": 128,
        "lastCheckAt": "2026-06-10T00:00:02.000Z",
        "lastObservedTaskStatus": "running",
        "muted": False,
    }


class ContractsRoundTripTests(AgentCoreTestCase):
    """Persisted records pass through annotated load/save paths byte-identically."""

    def _roundtrip(self, save, load, path_for, record, schema, label, key):
        root, home, workspace, previous = self.temp_context()
        try:
            self.assertEqual(validate(record, schema, strict=True), [], label)
            run(save(record))
            path = path_for(record[key])
            first = _read_bytes(path)
            loaded = run(load(record[key]))
            self.assertIsNotNone(loaded, label)
            run(save(loaded))
            second = _read_bytes(path)
            self.assertEqual(first, second, f"{label} round-trip changed bytes")
            self.assertEqual(validate(loaded, schema, strict=True), [], label)
        finally:
            self.restore_home(root, previous)

    def test_compacted_session_roundtrip(self):
        self._roundtrip(
            core.saveSession, core.loadSession, core.sessionPath,
            _compacted_session(), SessionRecord, "compacted session", "id",
        )

    def test_forked_session_roundtrip(self):
        self._roundtrip(
            core.saveSession, core.loadSession, core.sessionPath,
            _forked_session(), SessionRecord, "forked session", "id",
        )

    def test_subagent_parent_session_roundtrip(self):
        self._roundtrip(
            core.saveSession, core.loadSession, core.sessionPath,
            _subagent_parent_session(), SessionRecord, "subagent parent session", "id",
        )

    def test_dual_spelling_task_meta_roundtrip(self):
        record = _dual_spelling_task_meta()
        root, home, workspace, previous = self.temp_context()
        try:
            self.assertEqual(validate(record, TaskMeta, strict=True), [])
            run(core.writeTaskMeta(record["taskId"], record))
            path = core.taskMetaPath(record["taskId"])
            first = _read_bytes(path)
            loaded = run(core.readTaskMeta(record["taskId"]))
            run(core.writeTaskMeta(record["taskId"], loaded))
            self.assertEqual(first, _read_bytes(path), "task meta round-trip changed bytes")
            self.assertEqual(loaded.get("taskId"), loaded.get("task_id"))
        finally:
            self.restore_home(root, previous)

    def test_mid_finalize_child_agent_roundtrip(self):
        self._roundtrip(
            core.saveChildAgent, core.loadChildAgent, core.childAgentPath,
            _mid_finalize_child_agent(), ChildAgentRecord, "child agent", "agentId",
        )

    def test_monitor_record_roundtrip(self):
        self._roundtrip(
            core.saveMonitor, core.loadMonitor, core.monitorPath,
            _monitor_record(), MonitorRecord, "monitor", "id",
        )

    def test_session_with_transcript_archive_loads_and_validates(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = _compacted_session()
            run(core.saveSession(session))
            run(
                core.appendSessionTranscriptLogEntries(
                    session["id"],
                    [
                        {
                            "type": "message",
                            "message_index": 0,
                            "role": "user",
                            "content": "start",
                            "persisted_at": "2026-06-10T00:00:00.000Z",
                        }
                    ],
                )
            )
            loaded = run(core.loadSession(session["id"]))
            self.assertIn("fullTranscriptLog", loaded["metadata"])
            self.assertEqual(validate(loaded, SessionRecord, strict=True), [])
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    import unittest

    unittest.main()
