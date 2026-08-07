from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.tools.messaging import _send_message_tool


def _context(session, workspace, daemon=None):
    return {
        "session": session,
        "config": {"sessionPersistence": False},
        "cwd": str(workspace),
        "daemon": daemon,
        "abortSignal": None,
    }


class SendMessageToolTests(AgentCoreTestCase):
    def _sessions(self, workspace):
        parent = core.createEmptySession(
            cwd=str(workspace), provider="openai", model="gpt-5.4",
            systemPrompt="parent", permissionMode="default", sessionId="sm-parent",
        )
        parent["metadata"]["taskIds"] = ["sm-task"]
        child = core.createEmptySession(
            cwd=str(workspace), provider="openai", model="gpt-5.4",
            systemPrompt="child", permissionMode="default", sessionId="sm-child",
        )
        child["metadata"]["subagentTaskId"] = "sm-task"
        return parent, child

    def _seed_task(self, workspace, parent, child):
        run(core.writeTaskMeta("sm-task", {
            "type": "subagent", "taskId": "sm-task", "id": "sm-task",
            "sessionId": child["id"], "session_id": child["id"],
            "parent_session_id": parent["id"], "status": "running",
            "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }))
        run(core.saveChildAgent({
            "agentId": "sm-agent", "sessionId": child["id"],
            "parentSessionId": parent["id"], "name": "Worker",
            "provider": "openai", "model": "gpt-5.4", "mode": "default",
            "runInBackground": True, "status": "running", "taskId": "sm-task",
            "startedAt": "2026-01-01T00:00:00Z",
        }))

    def test_child_sends_to_parent_and_parent_drains_it(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, child = self._sessions(workspace)
            self._seed_task(workspace, parent, child)
            tool = _send_message_tool()
            result = run(tool.handler({"to": "parent", "message": "halfway there"}, _context(child, workspace)))
            payload = json.loads(result["content"])
            self.assertEqual(payload["status"], "queued")
            drained = run(core.deliverQueuedTaskMessagesToSession(parent, {"sessionPersistence": False}))
            self.assertEqual(len(drained), 1)
            self.assertEqual(drained[0]["messageId"], payload["message_id"])
            self.assertEqual(drained[0]["direction"], "to_parent")
        finally:
            self.restore_home(root, previous)

    def test_child_to_child_rejected_and_parentless_parent_addressing_rejected(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, child = self._sessions(workspace)
            self._seed_task(workspace, parent, child)
            tool = _send_message_tool()
            rejected = run(tool.handler({"to": "sm-agent", "message": "hi sibling"}, _context(child, workspace)))
            self.assertTrue(rejected.get("isError"))
            self.assertIn("Child-to-child", json.loads(rejected["content"])["detail"])
            no_parent = run(tool.handler({"to": "parent", "message": "hello"}, _context(parent, workspace)))
            self.assertTrue(no_parent.get("isError"))
        finally:
            self.restore_home(root, previous)

    def test_parent_sends_to_child_by_agent_id(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, child = self._sessions(workspace)
            self._seed_task(workspace, parent, child)
            tool = _send_message_tool()
            result = run(tool.handler({"to": "sm-agent", "message": "please focus on tests"}, _context(parent, workspace)))
            payload = json.loads(result["content"])
            self.assertEqual(payload["status"], "queued")
            self.assertEqual(payload["task_id"], "sm-task")
            drained = run(core.deliverQueuedTaskMessagesToSession(child, {"sessionPersistence": False}))
            self.assertEqual([m["messageId"] for m in drained], [payload["message_id"]])
            unknown = run(tool.handler({"to": "nobody-here", "message": "hi"}, _context(parent, workspace)))
            self.assertTrue(unknown.get("isError"))
        finally:
            self.restore_home(root, previous)

    def test_expects_reply_round_trip_and_timeout(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, child = self._sessions(workspace)
            self._seed_task(workspace, parent, child)
            tool = _send_message_tool()
            os.environ["AGENT_CORE_COMMS_REPLY_POLL_MS"] = "50"
            try:
                async def round_trip():
                    async def reply_soon():
                        await asyncio.sleep(0.2)
                        pending = await core.readTaskMailboxMessages("sm-task")
                        question = next(m for m in pending if m.get("direction") == "to_child")
                        await core.sendTaskMessage("sm-task", {
                            "direction": "to_parent", "sender": "child",
                            "content": "answer: use option B",
                            "metadata": {"replyTo": question["messageId"]},
                        })

                    replier = asyncio.get_running_loop().create_task(reply_soon())
                    result = await tool.handler(
                        {"to": "sm-agent", "message": "A or B?", "expects_reply": True, "timeout_ms": 5000},
                        _context(parent, workspace),
                    )
                    await replier
                    return result

                result = run(round_trip())
                payload = json.loads(result["content"])
                self.assertEqual(payload["status"], "reply_received")
                self.assertEqual(payload["reply"], "answer: use option B")
                reply_records = [m for m in run(core.readTaskMailboxMessages("sm-task")) if m.get("direction") == "to_parent"]
                self.assertEqual(reply_records[0]["status"], "delivered")

                timeout_result = run(tool.handler(
                    {"to": "sm-agent", "message": "anyone?", "expects_reply": True, "timeout_ms": 300},
                    _context(parent, workspace),
                ))
                timeout_payload = json.loads(timeout_result["content"])
                self.assertEqual(timeout_payload["status"], "no_reply")
                self.assertIn("300ms", timeout_payload["detail"])
            finally:
                os.environ.pop("AGENT_CORE_COMMS_REPLY_POLL_MS", None)
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
