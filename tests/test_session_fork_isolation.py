from __future__ import annotations

import json
from pathlib import Path

import tess_core._facade as core
from tess_core.session.fork import SESSION_FORK_METADATA_DISPOSITIONS
from tests.support.tess_core_case import TessCoreTestCase, run


PARENT_ID = "60000000-0000-4000-8000-000000000001"
CHILD_ID = "60000000-0000-4000-8000-000000000002"


class SessionForkIsolationTests(TessCoreTestCase):
    def test_fork_allowlist_creates_fresh_state_and_recoverable_archive(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolve_config({"runtimeHome": str(home)}, str(workspace)))
            parent = core.create_empty_session(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config.get("providerProfileName"),
                model=config["model"],
                effort=config.get("effort"),
                systemPrompt=config["systemPrompt"],
                permissionMode=config["permissionMode"],
                sessionId=PARENT_ID,
                originalCwd=str(workspace),
            )
            parent["revision"] = 88
            parent["messages"] = [
                {"role": "user", "content": "parent message 1"},
                {"role": "assistant", "content": "parent reply"},
                {"role": "user", "content": "parent message 2"},
            ]
            parent["todos"] = [{"id": "parent-todo", "content": "must not cross", "status": "pending"}]
            parent["metadata"].update(
                {
                    "workspaceId": "hostile-parent-workspace-id",
                    "compactionState": {"generation": 5},
                    "fullTranscriptLog": {"path": "/parent/archive", "archivedMessageCount": 2},
                    "taskIds": ["parent-task"],
                    "monitorIds": ["parent-monitor"],
                    "childSessionIds": ["parent-child"],
                    "childAgentIds": ["parent-agent"],
                    "remoteSessionId": "parent-remote",
                    "bridgeId": "parent-bridge",
                    "runtimeRestore": {"savedAt": "old"},
                    "effectiveAgentAuthority": {"version": 1},
                    "providerFailureCount": 3,
                    "promptCacheTailState": {"anchorMessageIndex": 2},
                }
            )
            run(core.save_session(parent))
            parent_path = Path(core.session_path(PARENT_ID))
            parent_before = parent_path.read_bytes()

            child = run(
                core.resolve_session(
                    {"resume": PARENT_ID, "forkSession": True, "sessionId": CHILD_ID},
                    config,
                    {
                        "cwd": str(workspace),
                        "originalCwd": str(workspace),
                        "tmuxSessionName": "current-tmux",
                        "tmuxWindowName": "current-window",
                        "tmuxMode": "current-mode",
                    },
                )
            )
            self.assertEqual(child["id"], CHILD_ID)
            self.assertNotIn("revision", child)
            self.assertEqual(child["messages"], parent["messages"])
            child["messages"][0]["content"] = "child-private-copy"
            self.assertEqual(parent["messages"][0]["content"], "parent message 1")
            child["messages"][0]["content"] = "parent message 1"
            self.assertEqual(child["todos"], [])
            self._assert_fresh(child)
            self.assertEqual(child["metadata"]["tmuxSessionName"], "current-tmux")

            run(core.persist_session(child, config))
            run(core.persist_session(child, config))
            archive_path = Path(core.session_transcript_log_path(CHILD_ID))
            archive = [json.loads(line) for line in archive_path.read_text().splitlines() if line]
            self.assertEqual(sum(entry.get("type") == "session_header" for entry in archive), 1)
            self.assertEqual(
                [entry["message_index"] for entry in archive if entry.get("type") == "message"],
                [0, 1, 2],
            )
            self.assertTrue(all(entry.get("session_id") == CHILD_ID for entry in archive))

            Path(core.session_path(CHILD_ID)).unlink()
            recovered = run(core.load_session(CHILD_ID))
            self.assertEqual(recovered["messages"], parent["messages"])
            self.assertEqual(recovered["metadata"]["forkedFromSessionId"], PARENT_ID)
            self._assert_fresh(recovered, allow_archive=True)
            self.assertEqual(parent_path.read_bytes(), parent_before)
            self.assertFalse(Path(core.session_transcript_log_path(PARENT_ID)).exists())
        finally:
            self.restore_home(root, previous)

    def test_every_session_metadata_field_has_explicit_disposition(self):
        values = list(SESSION_FORK_METADATA_DISPOSITIONS.values())
        self.assertGreater(len(values), 50)
        self.assertTrue(all(value in {"inherited", "recomputed", "fresh"} for value in values))
        self.assertEqual(SESSION_FORK_METADATA_DISPOSITIONS["fullTranscriptLog"], "fresh")
        self.assertEqual(SESSION_FORK_METADATA_DISPOSITIONS["compactionState"], "fresh")
        self.assertEqual(SESSION_FORK_METADATA_DISPOSITIONS["bridgeId"], "fresh")

    def _assert_fresh(self, session, *, allow_archive=False):
        metadata = session["metadata"]
        self.assertEqual(metadata["usage"], {
            "turns": 0,
            "toolCalls": 0,
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheCreationInputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheCreation5mInputTokens": 0,
            "cacheCreation1hInputTokens": 0,
            "cacheCreationTtlDetailComplete": True,
        })
        self.assertEqual(metadata["compactions"], 0)
        self.assertEqual(metadata["depth"], 0)
        self.assertEqual(metadata["childSessionIds"], [])
        self.assertEqual(metadata["childAgentIds"], [])
        self.assertNotIn("planMode", metadata)
        for field in (
            "compactionState", "parentSessionId", "agentId", "subagentTaskId",
            "effectiveAgentAuthority", "remoteSessionId", "bridgeId", "monitorIds",
            "taskIds", "runtimeRestore", "promptCacheTailState", "providerFailureCount",
        ):
            self.assertNotIn(field, metadata, field)
        if not allow_archive:
            self.assertNotIn("fullTranscriptLog", metadata)
