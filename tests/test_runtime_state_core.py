from __future__ import annotations

from tests.support.agent_core_case import *


class RuntimeStateCoreTests(AgentCoreTestCase):
    def test_runtime_state_reduces_provider_task_daemon_events(self):
        state = core.createInitialRuntimeState()
        state = core.reduceRuntimeState(state, {"event": "session.activated", "createdAt": "2026-01-01T00:00:00Z", "data": {"id": "s1", "cwd": "/tmp/w", "provider": "openai", "model": "gpt-5.4", "permissionMode": "default"}})
        self.assertEqual(state["session"]["activeSessionId"], "s1")
        state = core.reduceRuntimeState(state, {"event": "provider.input_tokens", "createdAt": "t1", "data": {"provider": "openai", "model": "gpt-5.4", "input_tokens": 123, "source": "estimate"}})
        self.assertEqual(state["provider"]["inputTokens"], 123)
        state = core.reduceRuntimeState(state, {"event": "daemon.snapshot", "createdAt": "t2", "data": {"pid": 42, "supervisorId": "sup", "activeTasks": 1}})
        self.assertEqual(state["daemon"]["phase"], "running")
        state = core.reduceRuntimeState(state, {"event": "task.registered", "createdAt": "t3", "data": {"task_id": "task-1", "kind": "background_command", "status": "running", "title": "test task"}})
        self.assertEqual(state["tasks"]["activeCount"], 1)
        state = core.reduceRuntimeState(state, {"event": "task.completed", "createdAt": "t4", "data": {"task_id": "task-1", "summary": "done"}})
        self.assertEqual(state["tasks"]["activeCount"], 0)
        self.assertEqual(state["tasks"]["tasks"][0]["status"], "completed")
