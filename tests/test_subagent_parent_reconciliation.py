from __future__ import annotations

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
                        "runtime": {"tools": [], "interactive": False, "emitEvent": lambda *_: None},
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
