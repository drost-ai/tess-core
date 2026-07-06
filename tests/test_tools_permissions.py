from __future__ import annotations

from tests.support.agent_core_case import *


class ToolPermissionTests(AgentCoreTestCase):
    def test_tool_permission_modes_for_edits_and_interactive_switch(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            write = next(tool for tool in core.createBuiltinTools() if tool.name == "Write")

            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "Deny", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}
            with self.assertRaises(PermissionError):
                run(write.handler({"file_path": str(workspace / "blocked.txt"), "content": "blocked"}, context))

            session["permissionMode"] = "plan"
            config["permissionMode"] = "plan"
            with self.assertRaises(PermissionError) as plan_ctx:
                run(write.handler({"file_path": str(workspace / "plan.txt"), "content": "blocked"}, context))
            self.assertIn("plan mode", str(plan_ctx.exception))

            session["permissionMode"] = "acceptEdits"
            config["permissionMode"] = "acceptEdits"
            run(write.handler({"file_path": str(workspace / "accepted.txt"), "content": "ok"}, context))
            self.assertEqual((workspace / "accepted.txt").read_text(), "ok")

            yes_events = []
            yes_session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            yes_config = {**config, "permissionMode": "default"}
            yes_context = {
                **context,
                "session": yes_session,
                "config": yes_config,
                "interactive": True,
                "askQuestion": lambda _prompt, options=None: "yes",
                "outputJson": lambda event, data: yes_events.append((event, data)),
            }
            with self.assertRaises(PermissionError) as yes_ctx:
                run(write.handler({"file_path": str(workspace / "yes-is-not-label.txt"), "content": "nope"}, yes_context))
            self.assertIn("Permission denied for Write", str(yes_ctx.exception))
            self.assertFalse((workspace / "yes-is-not-label.txt").exists())
            self.assertTrue(any(data.get("source") == "prompt" and data.get("decision") == "deny" for event, data in yes_events if event == "permission.decided"))

            events = []
            persisted = {"count": 0}
            session["permissionMode"] = "default"
            config["permissionMode"] = "default"
            interactive_context = {
                **context,
                "interactive": True,
                "askQuestion": lambda _prompt, options=None: "Switch to acceptEdits",
                "outputJson": lambda event, data: events.append((event, data)),
                "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1),
            }
            run(write.handler({"file_path": str(workspace / "switched.txt"), "content": "yes"}, interactive_context))
            self.assertEqual(session["permissionMode"], "acceptEdits")
            self.assertEqual(config["permissionMode"], "acceptEdits")
            self.assertEqual(persisted["count"], 1)
            self.assertTrue(any(event == "permission.requested" for event, _ in events))
            self.assertTrue(any(data.get("source") == "prompt-switch" for event, data in events if event == "permission.decided"))
            self.assertEqual((workspace / "switched.txt").read_text(), "yes")
        finally:
            self.restore_home(root, previous)

    def test_permission_resolution_matrix_matches_reference_edges(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            context = {"session": session, "config": config, "interactive": False, "cwd": str(workspace)}
            run(core.saveAutoMode(str(workspace), {"allow": ["echo"], "deny": ["rm -rf"], "environment": {}}))

            cases = [
                ("Write", "edit", "bypassPermissions", False, None, {"decision": "allow", "source": "mode"}),
                ("Bash", "shell", "plan", False, "git status", {"decision": "deny", "source": "mode", "reason": "Bash is blocked in plan mode. Switch to /permissions default or /permissions acceptEdits to execute changes."}),
                ("Write", "edit", "default", False, None, {"decision": "ask", "source": "mode", "switchMode": "acceptEdits", "reason": "This tool changes files on disk."}),
                ("EnterWorktree", "worktree", "auto", False, None, {"decision": "deny", "source": "mode", "reason": "Worktree creation is blocked in non-interactive permission modes."}),
                ("TaskStop", "process", "dontAsk", False, None, {"decision": "deny", "source": "mode", "reason": "Stopping processes is blocked in non-interactive permission modes."}),
                ("Bash", "shell", "default", False, "git status --short", {"decision": "allow", "source": "heuristic"}),
                ("Bash", "shell", "default", False, f"{shlex.quote(sys.executable)} -c {shlex.quote('print(1)')}", {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This shell command can modify the environment or repository."}),
                ("Bash", "shell", "auto", False, "echo ok", {"decision": "allow", "source": "auto-mode"}),
                ("Bash", "shell", "auto", False, "rm -rf build", {"decision": "deny", "source": "auto-mode", "reason": "Command matched the deny policy for auto mode."}),
                ("Bash", "shell", "auto", False, "uname -a", {"decision": "deny", "source": "auto-mode", "reason": "Command is not in the allow policy for auto mode."}),
                ("Bash", "shell", "auto", True, "echo ok", {"decision": "deny", "source": "auto-mode", "reason": "Command is not in the allow policy for auto mode."}),
            ]

            for tool_name, category, mode, always_ask, command, expected in cases:
                with self.subTest(tool=tool_name, category=category, mode=mode, command=command, always_ask=always_ask):
                    self.assertEqual(run(tools_module._resolve_tool_permission(context, tool_name, category, mode, always_ask, command)), expected)
        finally:
            self.restore_home(root, previous)

    def test_auto_mode_shell_policy_allows_and_denies_commands(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "auto"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="auto")
            run(core.saveAutoMode(str(workspace), {"allow": ["echo"], "deny": [sys.executable], "environment": {}}))
            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            events = []
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "Deny", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None}

            allowed = run(bash.handler({"command": "echo auto-ok"}, context))
            allowed_payload = json.loads(allowed["content"])
            self.assertEqual(allowed_payload["exitCode"], 0)
            self.assertIn("auto-ok", allowed_payload["stdout"])
            self.assertTrue(any(data.get("source") == "auto-mode" and data.get("decision") == "allow" for event, data in events if event == "permission.decided"))

            with self.assertRaises(PermissionError) as deny_ctx:
                run(bash.handler({"command": f"{shlex.quote(sys.executable)} -c {shlex.quote('print(1)')}"}, context))
            self.assertIn("deny policy", str(deny_ctx.exception))
            self.assertTrue(any(data.get("source") == "auto-mode" and data.get("decision") == "deny" for event, data in events if event == "permission.decided"))

            with self.assertRaises(PermissionError) as unknown_ctx:
                run(bash.handler({"command": "uname -a"}, context))
            self.assertIn("not in the allow policy", str(unknown_ctx.exception))
        finally:
            self.restore_home(root, previous)

    def test_child_session_inherits_interactive_parent_permission_mode(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            child.setdefault("metadata", {})["parentSessionId"] = parent["id"]
            run(core.saveSession(parent))
            write = next(tool for tool in core.createBuiltinTools() if tool.name == "Write")
            persisted = {"count": 0}
            context = {"session": child, "config": config, "interactive": True, "askQuestion": lambda *_: "Deny", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1)}
            run(write.handler({"file_path": str(workspace / "inherited.txt"), "content": "ok"}, context))
            self.assertEqual(child["permissionMode"], "bypassPermissions")
            self.assertEqual(config["permissionMode"], "bypassPermissions")
            self.assertEqual(persisted["count"], 1)
            self.assertEqual((workspace / "inherited.txt").read_text(), "ok")
        finally:
            self.restore_home(root, previous)

    def test_agent_tool_depth_limit_precedes_input_validation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            session["metadata"]["depth"] = 2
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")
            result = run(agent.handler({}, {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}))
            self.assertTrue(result["isError"])
            self.assertEqual(result["content"], "Delegation depth limit reached.")
        finally:
            self.restore_home(root, previous)

    def test_agent_nested_depth_boundary_and_cancellation_propagation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            agent = next(tool for tool in core.createBuiltinTools() if tool.name == "Agent")

            depth_one_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            depth_one_parent["metadata"]["depth"] = 1
            captured = {}

            async def run_subagent(args, _context):
                captured.update(args)
                return {"text": "depth ok", "usage": {}, "toolCount": 0, "stopReason": "end_turn"}

            depth_one_result = json.loads(
                run(
                    agent.handler(
                        {"description": "Depth one", "prompt": "go"},
                        {
                            "session": depth_one_parent,
                            "config": config,
                            "interactive": False,
                            "askQuestion": lambda *_: "",
                            "outputJson": lambda *_: None,
                            "cwd": str(workspace),
                            "persistSession": lambda: None,
                            "runSubagent": run_subagent,
                        },
                    )
                )["content"]
            )
            self.assertEqual(depth_one_result["status"], "completed")
            self.assertEqual(captured["childSession"]["metadata"]["depth"], 2)

            depth_two_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            depth_two_parent["metadata"]["depth"] = 2
            blocked = run(agent.handler({"description": "Blocked", "prompt": "no"}, {"session": depth_two_parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "runSubagent": run_subagent}))
            self.assertTrue(blocked["isError"])
            self.assertEqual(depth_two_parent["metadata"].get("childAgentIds"), [])
            self.assertEqual(depth_two_parent["metadata"].get("childSessionIds"), [])

            signal = {"aborted": False, "reason": "parent cancelled"}
            cancelled_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            events = []

            async def cancel_subagent(_args, _context):
                self.assertIs(_context["abortSignal"], signal)
                signal["aborted"] = True
                error = RuntimeError("parent cancelled")
                error.name = "AbortError"
                raise error

            cancelled = run(
                agent.handler(
                    {"description": "Cancelled child", "prompt": "stop"},
                    {
                        "session": cancelled_parent,
                        "config": config,
                        "interactive": False,
                        "askQuestion": lambda *_: "",
                        "outputJson": lambda event, data: events.append((event, data)),
                        "cwd": str(workspace),
                        "persistSession": lambda: None,
                        "runSubagent": cancel_subagent,
                        "abortSignal": signal,
                    },
                )
            )
            cancelled_payload = json.loads(cancelled["content"])
            self.assertTrue(cancelled["isError"])
            self.assertEqual(cancelled_payload["status"], "cancelled")
            self.assertEqual(cancelled_payload["stop_reason"], "cancelled")
            self.assertEqual([event for event, _ in events], ["subagent.started", "subagent.failed"])
            self.assertEqual(events[1][1]["status"], "cancelled")
            self.assertEqual(events[1][1]["stop_reason"], "cancelled")
            cancelled_record = run(core.loadChildAgent(cancelled_payload["agent_id"]))
            self.assertEqual(cancelled_record["status"], "cancelled")
            self.assertEqual(cancelled_record["stopReason"], "cancelled")

            already_cancelled_parent = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            with self.assertRaises(RuntimeError) as abort_ctx:
                run(agent.handler({"description": "Already cancelled", "prompt": "no"}, {"session": already_cancelled_parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "runSubagent": run_subagent, "abortSignal": {"aborted": True}}))
            self.assertEqual(getattr(abort_ctx.exception, "name", None), "AbortError")
            self.assertEqual(already_cancelled_parent["metadata"]["childAgentIds"], [])
            self.assertEqual(already_cancelled_parent["metadata"]["childSessionIds"], [])
        finally:
            self.restore_home(root, previous)

    def test_agent_permission_and_stop_reason_helpers_match_reference_edges(self):
        self.assertEqual(core.sanitizePermissionMode("default"), "default")
        self.assertEqual(core.sanitizePermissionMode("bypassPermissions"), "bypassPermissions")
        self.assertEqual(core.sanitizePermissionMode("bogus"), "default")
        self.assertEqual(core.inheritParentPermissionMode("default", "bypassPermissions"), "bypassPermissions")
        self.assertEqual(core.inheritParentPermissionMode("plan", "acceptEdits"), "acceptEdits")
        self.assertEqual(core.inheritParentPermissionMode("auto", "acceptEdits"), "auto")
        self.assertEqual(core.inheritParentPermissionMode("bypassPermissions", "plan"), "plan")
        self.assertEqual(core.mapStopReasonToStatus("plan_approval_required"), "awaiting_plan_approval")
        self.assertEqual(core.mapStopReasonToStatus("plan_chat_requested"), "awaiting_input")
        self.assertEqual(core.mapStopReasonToStatus("cancelled"), "cancelled")
        self.assertEqual(core.mapStopReasonToStatus("max_tokens"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("max_output_tokens"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("length"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("max_turns"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("subagent_max_turns_exceeded"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("max_tool_calls"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("subagent_max_tool_calls_exceeded"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("timeout_ms"), "truncated")
        self.assertEqual(core.mapStopReasonToStatus("max_budget_exceeded"), "failed")
        self.assertEqual(core.mapStopReasonToStatus("max_budget_usd"), "failed")
        self.assertEqual(core.mapStopReasonToStatus("plan_rejected"), "failed")
        self.assertEqual(core.mapStopReasonToCompletionReason("max_tokens"), "max_output_tokens")
        self.assertEqual(core.mapStopReasonToCompletionReason("max_output_tokens"), "max_output_tokens")
        self.assertEqual(core.mapStopReasonToCompletionReason("length"), "max_output_tokens")
        self.assertEqual(core.mapStopReasonToCompletionReason("max_turns"), "max_turns")
        self.assertEqual(core.mapStopReasonToCompletionReason("subagent_max_turns_exceeded"), "max_turns")
        self.assertEqual(core.mapStopReasonToCompletionReason("max_tool_calls"), "max_tool_calls")
        self.assertEqual(core.mapStopReasonToCompletionReason("subagent_max_tool_calls_exceeded"), "max_tool_calls")
        self.assertEqual(core.mapStopReasonToCompletionReason("timeout_ms"), "timeout_ms")
        self.assertEqual(core.mapStopReasonToCompletionReason("max_budget_usd"), "max_budget_exceeded")
        self.assertEqual(core.mapStopReasonToCompletionReason("max_budget_exceeded"), "max_budget_exceeded")
        self.assertEqual(core.mapStopReasonToStatus("error"), "completed")
        self.assertEqual(core.mapStopReasonToStatus("failed"), "completed")
        self.assertIsNone(core.mapStopReasonToCompletionReason("error"))
        self.assertIsNone(core.mapStopReasonToCompletionReason("failed"))
