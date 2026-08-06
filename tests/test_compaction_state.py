from __future__ import annotations

import copy

from tests.support.agent_core_case import *

from agent_core.context.compaction_state import refreshCompactionStateFromManifest


class CompactionStateTests(AgentCoreTestCase):
    def _state(self, session, generation, created_at, archive_message_count=None):
        archive_count = len(session["messages"]) if archive_message_count is None else archive_message_count
        return {
            "latestCompactionId": f"checkpoint-{generation}",
            "latestCompactionGeneration": generation,
            "latestCompactionAt": created_at,
            "compactedThroughMessageIndex": max(0, archive_count - 1),
            "compactedMessageCount": max(0, archive_count - 1),
            "recentMessageCount": 1,
            "archiveMessageCount": archive_count,
            "rawTokenEstimate": generation * 1_000,
            "compactedContextTokenEstimate": generation * 100,
            "workingContextTokenEstimate": generation * 200,
            "lastTrigger": "auto_raw_hard_limit",
            "artifacts": {},
        }

    def test_session_restore_preserves_newer_live_compaction_state(self):
        import agent_core.agent as agent_module

        session = core.createEmptySession(
            cwd="/tmp/agent-core-compaction-state",
            provider="openai",
            model="test-model",
            systemPrompt="test",
            permissionMode="bypassPermissions",
        )
        session["messages"].append({"role": "user", "content": "start"})
        snapshot = copy.deepcopy(session)
        snapshot["metadata"]["compactionState"] = self._state(
            snapshot, 1, "2026-06-27T19:35:40.529Z"
        )
        snapshot["metadata"]["compactions"] = 1

        session["metadata"]["compactionState"] = self._state(
            session, 2, "2026-06-27T20:13:50.135Z"
        )
        session["metadata"]["compactions"] = 2
        session["metadata"]["runtimeRestore"] = {
            "savedAt": "2026-06-27T20:15:17.674Z",
            "restoredTaskCount": 0,
            "activeTasks": [],
            "lastCompactionMode": "full_checkpoint_compaction",
        }

        agent_module._restore_session_state(session, snapshot)

        self.assertEqual(session["metadata"]["compactionState"]["latestCompactionGeneration"], 2)
        self.assertEqual(session["metadata"]["compactions"], 2)
        self.assertEqual(
            session["metadata"]["runtimeRestore"]["lastCompactionMode"],
            "full_checkpoint_compaction",
        )

    def test_manifest_refresh_repairs_stale_in_memory_compaction_state(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                model="test-model",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            session["messages"].extend(
                [
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "working"},
                    {"role": "user", "content": "more"},
                    {"role": "assistant", "content": "done"},
                ]
            )
            session["metadata"]["compactionState"] = self._state(
                session, 1, "2026-06-27T19:35:40.529Z", 2
            )
            run(
                core.saveCompactionManifest(
                    {
                        "sessionId": session["id"],
                        "strategy": "checkpoint-v2",
                        "createdAt": "2026-06-27T19:35:40.529Z",
                        "updatedAt": "2026-06-27T20:13:50.135Z",
                        "latestGeneration": 2,
                        "latestCompactionId": "checkpoint-2",
                        "compactedThroughMessageIndex": 3,
                        "compactedMessageCount": 3,
                        "recentMessageCount": 1,
                        "archiveMessageCount": len(session["messages"]),
                        "rawTokenEstimate": 513_599,
                        "compactedContextTokenEstimate": 51_043,
                        "workingContextTokenEstimate": 80_454,
                        "lastTrigger": "auto_raw_hard_limit",
                        "artifacts": {},
                    }
                )
            )

            refreshed = run(refreshCompactionStateFromManifest(session))

            self.assertTrue(refreshed)
            self.assertEqual(session["metadata"]["compactionState"]["latestCompactionGeneration"], 2)
            self.assertEqual(
                session["metadata"]["compactionState"]["archiveMessageCount"],
                len(session["messages"]),
            )
            self.assertEqual(session["metadata"]["compactions"], 2)
        finally:
            self.restore_home(root, previous)
