from __future__ import annotations

import uuid

import agent_core.subagents.continuation as continuation_module
import agent_core.subagents.groups as groups_module
import agent_core.tasks.subagent_parts.finalizers as finalizers_module
from tests.support.agent_core_case import *


class SubagentParentReconciliationTests(AgentCoreTestCase):
    def test_background_finalizer_reconciles_once_and_parent_next_turn_consumes(self):
        root, home, workspace, previous = self.temp_context()
        previous_env = {name: os.environ.get(name) for name in ("TESS_CHILD_AGENT_ID", "TESS_CHILD_TASK_ID", "TESS_CHILD_SESSION_ID")}
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child["metadata"].update(
                {
                    "parentSessionId": parent["id"],
                    "agentId": "agent-parent-reconcile",
                    "agentName": "Report worker",
                    "parentToolUseId": "tool-use-parent",
                    "parentTurnId": "turn-parent",
                    "launchGroupId": "turn-parent",
                }
            )
            os.environ["TESS_CHILD_AGENT_ID"] = "agent-parent-reconcile"
            os.environ["TESS_CHILD_TASK_ID"] = "task-parent-reconcile"
            os.environ["TESS_CHILD_SESSION_ID"] = child["id"]

            run(core.finalizeBackgroundSubagentSuccessFromEnv(child, {"text": "Child found the result.", "stopReason": "end_turn", "usage": {"input_tokens": 2}}))
            run(core.finalizeBackgroundSubagentSuccessFromEnv(child, {"text": "Child found the result.", "stopReason": "end_turn", "usage": {"input_tokens": 2}}))

            records = run(core.readSubagentReconciliations(parent["id"]))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["terminalStatus"], "completed")
            self.assertEqual(records[0]["summary"], "Child found the result.")
            self.assertEqual(records[0]["launchGroupId"], "turn-parent")

            captured = {}

            async def send(request):
                captured["request"] = request
                return {
                    "model": request["model"],
                    "content": [{"type": "text", "text": "Parent saw it."}],
                    "usage": {},
                    "stopReason": "end_turn",
                }

            result = run(
                core.runAgentPrompt(
                    {
                        "session": parent,
                        "config": config,
                        "prompt": "Synthesize now.",
                        "runtime": bind_test_runtime(parent, {"tools": [], "interactive": False, "emitEvent": lambda *_: None}),
                        "providerOverride": {"send": send},
                    }
                )
            )
            self.assertEqual(result["text"], "Parent saw it.")
            request_messages = captured["request"]["messages"]
            self.assertIn("[Background subagent updates]", request_messages[0]["content"])
            self.assertIn("Child found the result.", request_messages[0]["content"])
            self.assertEqual(request_messages[1]["content"], "Synthesize now.")
            consumed = run(core.readSubagentReconciliations(parent["id"]))
            self.assertEqual(len(consumed), 1)
            self.assertIsNotNone(consumed[0].get("consumedAt"))
            self.assertEqual(consumed[0].get("parentMessageIndex"), 0)
        finally:
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            self.restore_home(root, previous)

    def test_failed_child_reconciles_reason_without_raw_log_payload(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            record = run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={
                        "agentId": "agent-failed-reconcile",
                        "sessionId": "child-failed-reconcile",
                        "parentSessionId": "parent-failed-reconcile",
                        "name": "Failing worker",
                        "status": "failed",
                        "completionReason": "worker_failed",
                        "resultSummary": "Traceback line 1\nTraceback line 2",
                    },
                    taskMeta={
                        "task_id": "task-failed-reconcile",
                        "status": "failed",
                        "error": "Worker failed because the provider crashed.",
                        "outputFile": core.taskOutputPath("task-failed-reconcile"),
                    },
                    source="test",
                )
            )
            self.assertEqual(record["terminalStatus"], "failed")
            self.assertEqual(record["terminalReason"], "worker_failed")
            self.assertIn("Traceback line 1", record["summary"])
            rendered = core.renderSubagentReconciliationMessage([record])
            self.assertIn("Failed: Failing worker", rendered)
            self.assertIn("Output: task-output://task-failed-reconcile/final", rendered)
            self.assertNotIn("full-transcript-with-tools", rendered)
        finally:
            self.restore_home(root, previous)

    def test_reconciliation_record_shape_is_pinned(self):
        # Characterization pin for audit B6: this freezes the current parent-visible
        # record shape while the cross-runtime contract is still being finalized.
        record = core.buildSubagentReconciliationRecord(
            childRecord={
                "agentId": "agent-pin",
                "sessionId": "child-pin",
                "parentSessionId": "parent-pin",
                "parentAgentId": "agent-parent",
                "parentToolUseId": "tool-parent",
                "launchGroupId": "launch-pin",
                "name": "Pinned worker",
                "status": "completed",
                "completionReason": "worker_completed",
                "structuredResultStatus": "valid",
            },
            taskMeta={
                "task_id": "task-pin",
                "status": "completed",
                "outputFile": "/tmp/task-pin/output.txt",
                "completedAt": "2026-07-05T00:00:00Z",
            },
            result={"text": "Pinned summary.", "stopReason": "end_turn", "usage": {"input_tokens": 3}},
            source="test",
        )
        idempotency_key = "subagent-reconciliation:parent-pin:agent-pin:task-pin:completed:v1"
        self.assertEqual(
            record,
            {
                "id": f"rec-{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)}",
                "idempotencyKey": idempotency_key,
                "parentSessionId": "parent-pin",
                "parentAgentId": "agent-parent",
                "childSessionId": "child-pin",
                "childAgentId": "agent-pin",
                "taskId": "task-pin",
                "parentToolUseId": "tool-parent",
                "launchGroupId": "launch-pin",
                "label": "Pinned worker",
                "terminalStatus": "completed",
                "terminalReason": "end_turn",
                "summary": "Pinned summary.",
                "outputRef": "/tmp/task-pin/output.txt",
                "taskOutputRef": "task-output://task-pin/final",
                "structuredResultStatus": "valid",
                "usage": {"input_tokens": 3},
                "childCompletedAt": "2026-07-05T00:00:00Z",
                "source": "test",
            },
        )

    def test_subagent_limit_reconciliation_never_reports_completed(self):
        cases = {
            "max_turns": "failed",
            "subagent_max_turns_exceeded": "failed",
            "max_tool_calls": "failed",
            "subagent_max_tool_calls_exceeded": "failed",
            "timeout_ms": "timed_out",
            "max_budget_usd": "failed",
        }
        for stop_reason, expected_terminal in cases.items():
            with self.subTest(stop_reason=stop_reason):
                status = core.mapStopReasonToStatus(stop_reason)
                record = core.buildSubagentReconciliationRecord(
                    childRecord={
                        "agentId": f"agent-{stop_reason}",
                        "sessionId": f"child-{stop_reason}",
                        "parentSessionId": "parent-limit",
                        "name": "Limited worker",
                        "status": status,
                        "completionReason": core.mapStopReasonToCompletionReason(stop_reason),
                    },
                    taskMeta={
                        "task_id": f"task-{stop_reason}",
                        "status": status,
                        "completedAt": "2026-07-05T00:00:00Z",
                    },
                    result={"text": "Limit reached.", "stopReason": stop_reason},
                    source="test",
                )
                self.assertIsNotNone(record)
                self.assertEqual(record["terminalStatus"], expected_terminal)
                self.assertNotEqual(record["terminalStatus"], "completed")
                self.assertEqual(record["summary"], "Limit reached.")

    def test_max_depth_policy_is_configurable(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default", "subagents": {"maxDepth": 1}}, str(workspace)))
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            root_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            child_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            child_parent["metadata"]["depth"] = 1
            captured = {}

            async def run_subagent(args, _context):
                captured.update(args)
                return {"text": "ok", "usage": {}, "toolCount": 0, "stopReason": "end_turn"}

            allowed = json.loads(
                run(
                    agent.handler(
                        {"description": "allowed", "prompt": "go"},
                        {"session": root_parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "runSubagent": run_subagent},
                    )
                )["content"]
            )
            self.assertEqual(allowed["status"], "completed")
            self.assertEqual(captured["childSession"]["metadata"]["depth"], 1)

            blocked_events = []
            blocked = run(
                agent.handler(
                    {"description": "blocked", "prompt": "go"},
                    {"session": child_parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: blocked_events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None, "runSubagent": run_subagent},
                )
            )
            self.assertTrue(blocked["isError"])
            self.assertIn("child depth 2 exceeds max 1", blocked["content"])
            self.assertEqual(blocked_events[0][0], "subagent.depth_blocked")
        finally:
            self.restore_home(root, previous)

    def test_parent_continuation_readiness_supports_batch_policies(self):
        records = [
            {"id": "a", "terminalStatus": "completed"},
            {"id": "b", "terminalStatus": "failed"},
        ]
        self.assertFalse(core.evaluateParentContinuationReadiness(records, {"waitFor": "manual"})["ready"])
        self.assertTrue(core.evaluateParentContinuationReadiness(records, {"waitFor": "any"})["ready"])
        self.assertTrue(core.evaluateParentContinuationReadiness(records, {"waitFor": "all"})["ready"])
        self.assertTrue(core.evaluateParentContinuationReadiness(records, {"waitFor": "quorum", "quorumCount": 2})["ready"])
        self.assertFalse(core.evaluateParentContinuationReadiness(records[:1], {"waitFor": "quorum", "quorumCount": 2})["ready"])

    def test_group_readiness_waits_for_all_children_before_continuation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(parent))
            run(
                groups_module._upsert_subagent_launch_group(
                    parent["id"],
                    {"id": "group-all", "label": "Batch", "childAgentIds": ["agent-a", "agent-b"], "taskIds": ["task-a", "task-b"]},
                )
            )
            run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-a", "sessionId": "child-a", "parentSessionId": parent["id"], "name": "A", "status": "completed"},
                    taskMeta={"task_id": "task-a", "status": "completed", "completedAt": "2026-07-05T00:00:00Z"},
                    result={"text": "A done.", "stopReason": "end_turn"},
                    source="test",
                )
            )
            launched = []

            async def starter(args):
                launched.append(args)
                return {"taskId": f"continuation-{len(launched)}"}

            first = run(
                continuation_module._maybe_start_ready_parent_continuations(
                    parent["id"],
                    launchGroupId="group-all",
                    policy={"enabled": True, "waitFor": "all"},
                    starter=starter,
                )
            )
            self.assertEqual(first[0]["reason"], "all")
            self.assertFalse(first[0]["ready"])
            self.assertEqual(launched, [])

            run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-b", "sessionId": "child-b", "parentSessionId": parent["id"], "name": "B", "status": "failed"},
                    taskMeta={"task_id": "task-b", "status": "failed", "completedAt": "2026-07-05T00:00:01Z", "error": "B failed safely."},
                    result={"text": "B failed safely.", "stopReason": "max_budget_usd"},
                    source="test",
                )
            )
            second = run(
                continuation_module._maybe_start_ready_parent_continuations(
                    parent["id"],
                    launchGroupId="group-all",
                    policy={"enabled": True, "waitFor": "all"},
                    starter=starter,
                )
            )
            self.assertEqual(second[0]["started"], True)
            self.assertEqual(second[0]["continuationTaskId"], "continuation-1")
            self.assertEqual(len(launched), 1)
            self.assertEqual(launched[0]["prompt"], core.defaultParentContinuationPrompt())

            third = run(
                continuation_module._maybe_start_ready_parent_continuations(
                    parent["id"],
                    launchGroupId="group-all",
                    policy={"enabled": True, "waitFor": "all"},
                    starter=starter,
                )
            )
            self.assertEqual(third[0]["reason"], "already_requested")
            self.assertEqual(len(launched), 1)
        finally:
            self.restore_home(root, previous)

    def test_group_readiness_counts_consumed_terminal_reconciliations(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(parent))
            run(
                groups_module._upsert_subagent_launch_group(
                    parent["id"],
                    {"id": "group-consumed", "label": "Batch", "childAgentIds": ["agent-a", "agent-b"], "taskIds": ["task-a", "task-b"]},
                )
            )
            first_record = run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-a", "sessionId": "child-a", "parentSessionId": parent["id"], "name": "A", "status": "completed"},
                    taskMeta={"task_id": "task-a", "status": "completed", "completedAt": "2026-07-05T00:00:00Z"},
                    result={"text": "A done.", "stopReason": "end_turn"},
                    source="test",
                )
            )
            run(core.markSubagentReconciliationsConsumed(parent["id"], [first_record["id"]], parentMessageIndex=0, parentTurnId="turn-after-a"))
            run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-b", "sessionId": "child-b", "parentSessionId": parent["id"], "name": "B", "status": "completed"},
                    taskMeta={"task_id": "task-b", "status": "completed", "completedAt": "2026-07-05T00:00:01Z"},
                    result={"text": "B done.", "stopReason": "end_turn"},
                    source="test",
                )
            )
            launched = []

            async def starter(args):
                launched.append(args)
                return {"taskId": "continuation-consumed"}

            outcome = run(
                continuation_module._maybe_start_ready_parent_continuations(
                    parent["id"],
                    launchGroupId="group-consumed",
                    policy={"enabled": True, "waitFor": "all"},
                    starter=starter,
                )
            )
            self.assertEqual(outcome[0]["terminal"], 2)
            self.assertTrue(outcome[0]["started"])
            self.assertEqual(outcome[0]["continuationTaskId"], "continuation-consumed")
            self.assertEqual(len(launched), 1)
        finally:
            self.restore_home(root, previous)

    def test_unavailable_continuation_starter_does_not_burn_recovery_attempt(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.saveSession(parent))
            run(
                groups_module._upsert_subagent_launch_group(
                    parent["id"],
                    {"id": "group-no-supervisor", "label": "Batch", "childAgentIds": ["agent-a"], "taskIds": ["task-a"]},
                )
            )
            run(
                core.createSubagentReconciliationForTerminalChild(
                    childRecord={"agentId": "agent-a", "sessionId": "child-a", "parentSessionId": parent["id"], "name": "A", "status": "completed"},
                    taskMeta={"task_id": "task-a", "status": "completed", "completedAt": "2026-07-05T00:00:00Z"},
                    result={"text": "A done.", "stopReason": "end_turn"},
                    source="test",
                )
            )

            async def no_starter():
                return None

            with mock.patch.object(continuation_module, "_resolve_continuation_starter", no_starter):
                outcome = run(
                    continuation_module._maybe_start_ready_parent_continuations(
                        parent["id"],
                        launchGroupId="group-no-supervisor",
                        policy={"enabled": True, "waitFor": "all"},
                    )
                )
            self.assertEqual(outcome[0]["reason"], "supervisor_unavailable")
            groups = run(groups_module._read_subagent_launch_groups(parent["id"]))
            self.assertIsNone(groups[0].get("continuationRequestedAt"))
            self.assertEqual(groups[0].get("continuationAttemptCount"), None)
        finally:
            self.restore_home(root, previous)

    def test_finalizer_updates_launch_group_and_invokes_parent_continuation_policy(self):
        root, home, workspace, previous = self.temp_context()
        previous_env = {name: os.environ.get(name) for name in ("TESS_CHILD_AGENT_ID", "TESS_CHILD_TASK_ID", "TESS_CHILD_SESSION_ID")}
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            child = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child["metadata"].update(
                {
                    "parentSessionId": "parent-finalizer-group",
                    "agentId": "agent-finalizer-group",
                    "agentName": "Group worker",
                    "launchGroupId": "group-finalizer",
                    "parentContinuation": {"enabled": True, "waitFor": "all"},
                }
            )
            os.environ["TESS_CHILD_AGENT_ID"] = "agent-finalizer-group"
            os.environ["TESS_CHILD_TASK_ID"] = "task-finalizer-group"
            os.environ["TESS_CHILD_SESSION_ID"] = child["id"]
            with mock.patch.object(finalizers_module, "_maybe_start_ready_parent_continuations", new=mock.AsyncMock()) as maybe_continue:
                run(core.finalizeBackgroundSubagentSuccessFromEnv(child, {"text": "Group worker done.", "stopReason": "end_turn"}))
            groups = run(groups_module._read_subagent_launch_groups("parent-finalizer-group"))
            self.assertEqual(groups[0]["id"], "group-finalizer")
            self.assertEqual(groups[0]["childAgentIds"], ["agent-finalizer-group"])
            self.assertEqual(groups[0]["taskIds"], ["task-finalizer-group"])
            maybe_continue.assert_awaited_once_with(
                "parent-finalizer-group",
                launchGroupId="group-finalizer",
                policy={"enabled": True, "waitFor": "all"},
            )
        finally:
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            self.restore_home(root, previous)
