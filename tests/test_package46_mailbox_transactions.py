from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

from tests.support.agent_core_case import *


class Package46MailboxTransactionTests(AgentCoreTestCase):
    def test_concurrent_delivery_commits_one_session_projection(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise() -> tuple[list[list[dict[str, Any]]], dict[str, Any], list[dict[str, Any]]]:
                session = core.createEmptySession(
                    cwd=str(workspace),
                    provider="openai",
                    model="gpt-5.4",
                    systemPrompt="mailbox transaction",
                    permissionMode="default",
                    sessionId="package46-delivery-child",
                )
                session["metadata"]["subagentTaskId"] = "package46-delivery-task"
                session["messages"].append({"role": "user", "content": "active task turn"})
                await core.sendTaskMessage(
                    "package46-delivery-task",
                    {
                        "messageId": "delivery-once",
                        "childSessionId": session["id"],
                        "content": "transactional guidance",
                    },
                )
                deliveries = await asyncio.gather(
                    core.deliverQueuedTaskMessagesToSession(
                        session, {"sessionPersistence": True}
                    ),
                    core.deliverQueuedTaskMessagesToSession(
                        session, {"sessionPersistence": True}
                    ),
                )
                restored = await core.loadSession(session["id"])
                mailbox = await core.readTaskMailboxMessages("package46-delivery-task")
                return deliveries, restored, mailbox

            deliveries, restored, mailbox = run(exercise())
            self.assertEqual(sorted(len(delivery) for delivery in deliveries), [0, 1])
            self.assertEqual(len(restored["messages"]), 2)
            self.assertIn("transactional guidance", restored["messages"][1]["content"])
            self.assertEqual(mailbox[0]["status"], "delivered")
        finally:
            self.restore_home(root, previous)

    def test_failed_session_commit_leaves_mailbox_queued_and_live_session_clean(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise() -> tuple[dict[str, Any], list[dict[str, Any]], int]:
                import agent_core.tasks.communication as communication

                session = core.createEmptySession(
                    cwd=str(workspace),
                    provider="openai",
                    model="gpt-5.4",
                    systemPrompt="mailbox rollback",
                    permissionMode="default",
                    sessionId="package46-failed-delivery-child",
                )
                session["metadata"]["subagentTaskId"] = "package46-failed-delivery-task"
                session["messages"].append({"role": "user", "content": "active task turn"})
                await core.sendTaskMessage(
                    "package46-failed-delivery-task",
                    {
                        "messageId": "retry-delivery",
                        "childSessionId": session["id"],
                        "content": "must survive failed persist",
                    },
                )

                async def fail_persist(*_args, **_kwargs):
                    raise OSError("injected session write failure")

                with mock.patch.object(communication, "persistSession", fail_persist):
                    with self.assertRaises(OSError):
                        await core.deliverQueuedTaskMessagesToSession(
                            session, {"sessionPersistence": True}
                        )
                queued = await core.readTaskMailboxMessages("package46-failed-delivery-task")
                live_count_after_failure = len(session["messages"])
                delivered = await core.deliverQueuedTaskMessagesToSession(
                    session, {"sessionPersistence": True}
                )
                return queued[0], delivered, live_count_after_failure

            queued, delivered, live_count = run(exercise())
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(live_count, 1)
            self.assertEqual([item["messageId"] for item in delivered], ["retry-delivery"])
        finally:
            self.restore_home(root, previous)


class MailboxDirectionTests(AgentCoreTestCase):
    def test_to_parent_messages_deliver_to_parent_not_child(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                parent = core.createEmptySession(
                    cwd=str(workspace), provider="openai", model="gpt-5.4",
                    systemPrompt="parent", permissionMode="default", sessionId="dir-parent",
                )
                parent["metadata"]["taskIds"] = ["dir-task"]
                child = core.createEmptySession(
                    cwd=str(workspace), provider="openai", model="gpt-5.4",
                    systemPrompt="child", permissionMode="default", sessionId="dir-child",
                )
                child["metadata"]["subagentTaskId"] = "dir-task"
                await core.writeTaskMeta("dir-task", {
                    "type": "subagent", "taskId": "dir-task", "id": "dir-task",
                    "sessionId": child["id"], "session_id": child["id"],
                    "parent_session_id": parent["id"], "status": "running",
                    "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-01T00:00:00Z",
                })
                up = await core.sendTaskMessage("dir-task", {
                    "direction": "to_parent", "content": "progress: half done", "kind": "status",
                })
                self.assertEqual(up["direction"], "to_parent")
                self.assertEqual(up["sender"], "child")
                self.assertEqual(up["parentSessionId"], parent["id"])

                child_drained = await core.deliverQueuedTaskMessagesToSession(child, {"sessionPersistence": True})
                self.assertEqual(child_drained, [])
                self.assertEqual(len(child["messages"]), 0)

                parent_drained = await core.deliverQueuedTaskMessagesToSession(parent, {"sessionPersistence": True})
                self.assertEqual([m["messageId"] for m in parent_drained], [up["messageId"]])
                self.assertEqual(len(parent["messages"]), 1)
                body = parent["messages"][0]["content"]
                text = body if isinstance(body, str) else body[0]["text"]
                self.assertIn("[CHILD MESSAGE]", text)
                self.assertIn("task_id: dir-task", text)
                self.assertIn("progress: half done", text)

                again = await core.deliverQueuedTaskMessagesToSession(parent, {"sessionPersistence": True})
                self.assertEqual(again, [])
                self.assertEqual(len(parent["messages"]), 1)
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_legacy_records_without_direction_still_deliver_to_child(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                child = core.createEmptySession(
                    cwd=str(workspace), provider="openai", model="gpt-5.4",
                    systemPrompt="child", permissionMode="default", sessionId="legacy-child",
                )
                child["metadata"]["subagentTaskId"] = "legacy-task"
                from agent_core.tasks.communication import task_mailbox_path

                await core.appendJsonLine(
                    task_mailbox_path("legacy-task"),
                    {
                        "messageId": "legacy-1", "taskId": "legacy-task",
                        "childSessionId": child["id"], "sender": "parent",
                        "kind": "guidance", "content": "old-style record",
                        "status": "queued", "createdAt": "2026-01-01T00:00:00Z",
                        "deliveredAt": None, "metadata": {},
                    },
                )
                drained = await core.deliverQueuedTaskMessagesToSession(child, {"sessionPersistence": True})
                self.assertEqual([m["messageId"] for m in drained], ["legacy-1"])
            run(exercise())
        finally:
            self.restore_home(root, previous)

    def test_direction_validation_is_loud(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            async def exercise():
                await core.writeTaskMeta("val-task", {
                    "type": "subagent", "taskId": "val-task", "id": "val-task",
                    "sessionId": "val-child", "session_id": "val-child",
                    "status": "running", "cwd": str(workspace),
                    "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
                })
                with self.assertRaises(ValueError):
                    await core.sendTaskMessage("val-task", {"direction": "sideways", "content": "x"})
                with self.assertRaises(ValueError):
                    await core.sendTaskMessage("val-task", {"direction": "to_parent", "content": "x"})
            run(exercise())
        finally:
            self.restore_home(root, previous)
