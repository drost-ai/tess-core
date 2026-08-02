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
                import agent_core.task_communication as communication

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
