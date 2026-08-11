from __future__ import annotations

import asyncio
import os
import unittest

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run


class StaleFullSaveTests(AgentCoreTestCase):
    def test_stale_full_session_save_preserves_concurrent_metadata_mutation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="prompt",
                permissionMode="default",
            )
            session["messages"].append({"role": "user", "content": "hello"})
            run(core.save_session(session))
            stale_snapshot = run(core.load_session(session["id"]))

            run(
                core.mutate_session_record(
                    session["id"],
                    lambda record: {
                        **record,
                        "metadata": {
                            **record.get("metadata", {}),
                            "taskIds": [*(record.get("metadata", {}).get("taskIds") or []), "task-new"],
                            "childAgentIds": [*(record.get("metadata", {}).get("childAgentIds") or []), "agent-new"],
                        },
                    },
                )
            )
            run(core.save_session(stale_snapshot))

            loaded = run(core.load_session(session["id"]))
            self.assertIn("task-new", loaded["metadata"]["taskIds"])
            self.assertIn("agent-new", loaded["metadata"]["childAgentIds"])
        finally:
            self.restore_home(root, previous)

    def test_stale_child_agent_save_does_not_rewind_terminal_state(self):
        root, home, workspace, previous = self.temp_context()
        try:
            base = {
                "agentId": "agent-fast",
                "sessionId": "child-fast",
                "parentSessionId": "parent-fast",
                "name": "fast",
                "description": "fast",
                "status": "queued",
                "startedAt": "2026-06-10T00:00:00.000Z",
            }
            run(core.save_child_agent(base))
            stale_snapshot = run(core.load_child_agent("agent-fast"))
            run(
                core.mutate_child_agent(
                    "agent-fast",
                    lambda current: {
                        **current,
                        "status": "completed",
                        "taskId": "task-fast",
                        "completedAt": "2026-06-10T00:00:01.000Z",
                        "resultSummary": "done",
                        "latestResultAt": "2026-06-10T00:00:01.000Z",
                        "stoppable": False,
                    },
                )
            )
            run(
                core.save_child_agent(
                    {
                        **stale_snapshot,
                        "status": "backgrounded",
                        "taskId": "task-fast",
                        "pid": 1234,
                        "owner": "daemon",
                        "lastSummary": "Running in the background.",
                    }
                )
            )

            loaded = run(core.load_child_agent("agent-fast"))
            self.assertEqual(loaded["status"], "completed")
            self.assertEqual(loaded["resultSummary"], "done")
            self.assertEqual(loaded["taskId"], "task-fast")
            self.assertEqual(loaded["pid"], 1234)
        finally:
            self.restore_home(root, previous)

    def test_concurrent_project_memory_mutations_all_survive(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def writer(index: int) -> None:
                await core.mutate_project_memory_records(str(workspace), lambda records: [*records, _memory_record(index, workspace)])

            async def main() -> None:
                await asyncio.gather(*(writer(index) for index in range(12)))

            run(main())
            records = run(core.load_project_memory_records(str(workspace)))
            self.assertEqual({record["id"] for record in records}, {f"memory-{index}" for index in range(12)})
        finally:
            self.restore_home(root, previous)

    def test_empty_manual_memory_sync_does_not_create_records_file(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = core.get_project_memory_records_path(str(workspace))

            records = run(core.synchronize_manual_project_memory(str(workspace)))

            self.assertEqual([], records)
            self.assertFalse(os.path.exists(path))
        finally:
            self.restore_home(root, previous)


def _memory_record(index: int, workspace) -> dict:
    return {
        "id": f"memory-{index}",
        "workspaceRoot": str(workspace),
        "createdAt": "2026-06-10T00:00:00.000Z",
        "updatedAt": "2026-06-10T00:00:00.000Z",
        "kind": "project_fact",
        "source": "test",
        "text": f"memory {index}",
        "confidence": "high",
    }


if __name__ == "__main__":
    unittest.main()
