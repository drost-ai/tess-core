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

    def test_parent_expects_reply_is_direction_gated_to_queued(self):
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
                # expects_reply is direction-gated to child->parent: a parent
                # never blocks on a child. The message still queues and the
                # child's reply arrives as a wake-bearing child message.
                self.assertEqual(payload["status"], "queued")
                self.assertIn("expects_reply is ignored", payload["note"])
                queued = [m for m in run(core.readTaskMailboxMessages("sm-task")) if m.get("direction") == "to_child"]
                self.assertEqual(queued[0]["status"], "queued")
            finally:
                os.environ.pop("AGENT_CORE_COMMS_REPLY_POLL_MS", None)
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()


class CommsInlineBudgetTests(AgentCoreTestCase):
    def test_large_sendmessage_result_returns_inline_not_artifact(self):
        from agent_core.payloads.agent_results import finalizeToolDispatchResult

        root, _home, workspace, previous = self.temp_context()
        try:
            big_reply = json.dumps({"status": "reply_received", "reply": "x" * 20_000})
            completed = run(finalizeToolDispatchResult(
                config={"providerProfile": {}},
                sessionId="comms-budget-session",
                toolUse={"id": "tool-1", "name": "SendMessage", "input": {}},
                outcome={"kind": "returned", "result": {"content": big_reply}},
            ))
            content = completed["result"]["content"]
            self.assertNotIn("tool_result_truncated", content)
            self.assertIn("x" * 1000, content)

            huge_reply = json.dumps({"status": "reply_received", "reply": "y" * 100_000})
            spilled = run(finalizeToolDispatchResult(
                config={"providerProfile": {}},
                sessionId="comms-budget-session",
                toolUse={"id": "tool-2", "name": "SendMessage", "input": {}},
                outcome={"kind": "returned", "result": {"content": huge_reply}},
            ))
            self.assertIn("tool_result_truncated", spilled["result"]["content"])

            plain_big = run(finalizeToolDispatchResult(
                config={"providerProfile": {}},
                sessionId="comms-budget-session",
                toolUse={"id": "tool-3", "name": "Bash", "input": {}},
                outcome={"kind": "returned", "result": {"content": "z" * 30_000}},
            ))
            self.assertIn("truncated", plain_big["result"]["content"])  # non-comms keeps the class budget (24KB large cap)
        finally:
            self.restore_home(root, previous)

    def test_drain_injection_clamps_oversized_message_with_readable_artifact(self):
        import re

        from agent_core.tasks.communication import _render_injected_message

        root, _home, workspace, previous = self.temp_context()
        os.environ["AGENT_CORE_COMMS_INLINE_RESULT_BYTES"] = "4096"
        try:
            session = {"id": "clamp-session"}
            envelope = run(_render_injected_message({
                "sender": "child", "kind": "result", "messageId": "m-1",
                "direction": "to_parent", "taskId": "t-1",
                "content": "a" * 10_000,
            }, session))
            self.assertLess(len(envelope.encode("utf-8")), 5_000)
            self.assertIn("message truncated: 10,000 bytes total", envelope)
            match = re.search(r"ReadToolArtifact artifact_id=([0-9a-f-]+) offset_bytes=(\d+)", envelope)
            self.assertIsNotNone(match, envelope)
            artifact_path = core.toolArtifactDataPath("clamp-session", match.group(1), ".txt")
            self.assertEqual(Path(artifact_path).read_text(encoding="utf-8"), "a" * 10_000)

            small = run(_render_injected_message({
                "sender": "child", "kind": "result", "messageId": "m-2",
                "direction": "to_parent", "taskId": "t-1",
                "content": "short and sweet",
            }, session))
            self.assertIn("short and sweet", small)
            self.assertNotIn("truncated", small)
        finally:
            os.environ.pop("AGENT_CORE_COMMS_INLINE_RESULT_BYTES", None)
            self.restore_home(root, previous)

    def test_comms_results_skip_the_structured_summarizer(self):
        from agent_core.payloads.agent_results import finalizeToolDispatchResult

        root, _home, workspace, previous = self.temp_context()
        try:
            reply_json = json.dumps({
                "status": "reply_received", "message_id": "m-1",
                "reply_message_id": "m-2", "reply": "section one\n" * 800,
            })
            completed = run(finalizeToolDispatchResult(
                config={"providerProfile": {}},
                sessionId="comms-shape-session",
                toolUse={"id": "tool-1", "name": "SendMessage", "input": {}},
                outcome={"kind": "returned", "result": {"content": reply_json}},
            ))
            content = completed["result"]["content"]
            self.assertNotIn("structured_result_summary", content)
            self.assertIn("section one", content)
            self.assertEqual(content, reply_json)
        finally:
            self.restore_home(root, previous)
