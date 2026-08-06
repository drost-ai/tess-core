from __future__ import annotations

import agent_core.subagents.continuation as continuation_module
import agent_core.subagents.groups as groups_module
from tests.support.agent_core_case import *


class SubagentContinuationEdgeTests(AgentCoreTestCase):
    def _parent_session(self, home, workspace):
        config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
        parent = core.createEmptySession(
            cwd=str(workspace),
            provider=config["provider"],
            providerProfileName=config["providerProfileName"],
            model=config["model"],
            effort=config["effort"],
            systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        run(core.saveSession(parent))
        return parent

    def test_ready_group_with_only_consumed_reconciliations_satisfies_intent(self):
        root, home, workspace, previous = self.temp_context()
        try:
            parent = self._parent_session(home, workspace)
            run(groups_module._upsert_subagent_launch_group(parent["id"], {"id": "group-seen", "label": "Batch", "childAgentIds": ["agent-a"], "taskIds": ["task-a"]}))
            record = run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-a", "sessionId": "child-a", "parentSessionId": parent["id"], "name": "A", "status": "completed"},
                    taskMeta={"task_id": "task-a", "status": "completed", "completedAt": "2026-07-05T00:00:00Z"},
                    result={"text": "A done.", "stopReason": "end_turn"},
                    source="test",
                )
            )
            run(core.markSubagentReconciliationsConsumed(parent["id"], [record["id"]], parentMessageIndex=0, parentTurnId="turn-after-a"))
            launched = []

            async def starter(args):
                launched.append(args)
                return {"taskId": "should-not-launch"}

            outcome = run(
                continuation_module._maybe_start_ready_parent_continuations(
                    parent["id"],
                    launchGroupId="group-seen",
                    policy={"enabled": True, "waitFor": "all"},
                    starter=starter,
                )
            )
            self.assertEqual(outcome[0]["reason"], "satisfied_intent")
            self.assertEqual(outcome[0]["terminal"], 1)
            self.assertEqual(launched, [])
        finally:
            self.restore_home(root, previous)

    def test_legacy_no_group_continuation_ignores_consumed_reconciliations(self):
        root, home, workspace, previous = self.temp_context()
        try:
            parent = self._parent_session(home, workspace)
            record = run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-a", "sessionId": "child-a", "parentSessionId": parent["id"], "name": "A", "status": "completed"},
                    taskMeta={"task_id": "task-a", "status": "completed", "completedAt": "2026-07-05T00:00:00Z"},
                    result={"text": "A done.", "stopReason": "end_turn"},
                    source="test",
                )
            )
            run(core.markSubagentReconciliationsConsumed(parent["id"], [record["id"]], parentMessageIndex=0, parentTurnId="turn-after-a"))

            outcome = run(core.requestParentContinuationForReconciliations(parent["id"], waitFor="all"))
            self.assertFalse(outcome["requested"])
            self.assertFalse(outcome["ready"])
            self.assertEqual(outcome["total"], 0)
            self.assertEqual(outcome["terminal"], 0)
        finally:
            self.restore_home(root, previous)
