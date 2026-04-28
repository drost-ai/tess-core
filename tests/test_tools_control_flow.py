from __future__ import annotations

from tests.support.agent_core_case import *


class ToolControlFlowTests(AgentCoreTestCase):
    def test_mcp_introspection_tool_does_not_dispatch_tool_calls(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            calls = []

            async def call_tool(*args):
                calls.append(args)
                return {"content": "called"}

            runtime = {
                "getDiagnostics": lambda: {"connectedServers": 1, "totalTools": 1, "totalResources": 0},
                "listServerStatus": lambda: [{"name": "fake", "transport": "stdio", "connected": True, "state": "connected", "tools": 1, "resources": 0}],
                "listTools": lambda: [{"server": "fake", "name": "echo", "runtimeName": "mcp__fake__echo", "description": "Echo"}],
                "callTool": call_tool,
            }
            mcp = next(tool for tool in core.createBuiltinTools({"mcpRuntime": runtime}) if tool.name == "Mcp")
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}

            payload = json.loads(run(mcp.handler({"server": "fake", "name": "echo", "arguments": {"value": "x"}}, context))["content"])
            self.assertEqual(calls, [])
            self.assertEqual(payload["diagnostics"]["totalTools"], 1)
            self.assertEqual(payload["tools"][0]["runtime_name"], "mcp__fake__echo")
        finally:
            self.restore_home(root, previous)

    def test_tool_cancellation_guards_web_question_and_mcp_paths(self):
        root, home, workspace, previous = self.temp_context()
        original_fetch = tools_module.fetch
        original_search = tools_module.searchWeb
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            config["mcpConfig"] = [{"name": "edge", "transport": "http", "commandOrUrl": "https://mcp.example.test"}]
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            tools = {tool.name: tool for tool in core.createBuiltinTools()}
            calls = []

            async def fetch_should_not_run(*args, **kwargs):
                calls.append(("fetch", args, kwargs))
                return tools_module.FetchResponse(200, "ok")

            async def search_should_not_run(*args, **kwargs):
                calls.append(("search", args, kwargs))
                return []

            tools_module.fetch = fetch_should_not_run
            tools_module.searchWeb = search_should_not_run
            context = {"session": session, "config": config, "interactive": True, "askQuestion": lambda *_: calls.append(("question", (), {})) or "Blue", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "abortSignal": {"aborted": True}}

            for tool_name, payload in (
                ("WebFetch", {"url": "https://example.test", "prompt": "summarize"}),
                ("WebSearch", {"query": "agent core"}),
                ("AskUserQuestion", {"questions": [{"question": "Pick?", "options": [{"label": "Blue"}]}]}),
                ("ReadMcpResource", {"server": "edge", "uri": "https://mcp.example.test/resource"}),
            ):
                with self.subTest(tool=tool_name):
                    with self.assertRaises(RuntimeError) as abort_ctx:
                        run(tools[tool_name].handler(payload, context))
                    self.assertEqual(getattr(abort_ctx.exception, "name", None), "AbortError")
            self.assertEqual(calls, [])
        finally:
            tools_module.fetch = original_fetch
            tools_module.searchWeb = original_search
            self.restore_home(root, previous)

    def test_runtime_mcp_resource_tools_honor_late_abort_signals(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            calls = []

            class FlipSignal:
                def __init__(self):
                    self.count = 0

                @property
                def aborted(self):
                    self.count += 1
                    return self.count >= 2

            async def list_resources(server=None):
                calls.append(("list", server))
                return [{"server": server or "edge", "uri": "memory://one", "name": "one"}]

            async def read_resource(server, uri):
                calls.append(("read", server, uri))
                return {"contents": [{"uri": uri, "text": "one"}]}

            runtime = {
                "listResources": list_resources,
                "readResource": read_resource,
                "getDiagnostics": lambda: {"connectedServers": 1},
                "listTools": lambda: [],
                "listServerStatus": lambda: [],
            }
            tools = {tool.name: tool for tool in core.createBuiltinTools({"mcpRuntime": runtime})}
            base_context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}

            with self.assertRaises(RuntimeError) as list_ctx:
                run(tools["ListMcpResources"].handler({"server": "edge"}, {**base_context, "abortSignal": FlipSignal()}))
            self.assertEqual(getattr(list_ctx.exception, "name", None), "AbortError")

            with self.assertRaises(RuntimeError) as read_ctx:
                run(tools["ReadMcpResource"].handler({"server": "edge", "uri": "memory://one"}, {**base_context, "abortSignal": FlipSignal()}))
            self.assertEqual(getattr(read_ctx.exception, "name", None), "AbortError")
            self.assertEqual(calls, [("list", "edge"), ("read", "edge", "memory://one")])
        finally:
            self.restore_home(root, previous)

    def test_exit_plan_mode_matches_reference_pending_and_approval_flows(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "ExitPlanMode")
            base_context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}

            blocked = run(tool.handler({}, base_context))
            self.assertTrue(blocked["isError"])
            self.assertIn("only available", blocked["content"])

            events = []
            persisted = {"count": 0}
            session["permissionMode"] = "plan"
            config["permissionMode"] = "plan"
            session["messages"] = [{"role": "assistant", "content": [{"type": "text", "text": "1. Build it.\n2. Test it."}]}]
            context = {**base_context, "outputJson": lambda event, data: events.append((event, data)), "persistSession": lambda: persisted.__setitem__("count", persisted["count"] + 1)}
            pending = run(tool.handler({"allowedPrompts": [{"tool": "Bash", "prompt": "  pytest  "}, {"tool": "Bash", "prompt": "   "}, {"tool": "Read", "prompt": "ignored"}]}, context))
            payload = json.loads(pending["content"])
            self.assertEqual(payload["plan"], "1. Build it.\n2. Test it.")
            self.assertTrue(payload["awaitingLeaderApproval"])
            self.assertEqual(payload["allowedPrompts"], [{"tool": "Bash", "prompt": "pytest"}])
            self.assertEqual(session["metadata"]["planMode"]["state"], "pending")
            self.assertEqual(events[0][0], "plan.requested")
            self.assertEqual(run(core.loadPlanApproval(payload["requestId"]))["status"], "pending")
            self.assertEqual(persisted["count"], 1)

            approve_session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="plan")
            approve_config = {**config, "permissionMode": "plan"}
            approve_session["messages"] = [{"role": "assistant", "content": "Approve this plan."}]
            approve_events = []
            approved = run(
                tool.handler(
                    {},
                    {
                        "session": approve_session,
                        "config": approve_config,
                        "interactive": True,
                        "askQuestion": lambda *_: "Approve plan",
                        "outputJson": lambda event, data: approve_events.append((event, data)),
                        "cwd": str(workspace),
                        "persistSession": lambda: None,
                    },
                )
            )
            approved_payload = json.loads(approved["content"])
            self.assertTrue(approved_payload["approved"])
            self.assertEqual(approved_payload["nextMode"], "acceptEdits")
            self.assertEqual(approve_session["permissionMode"], "acceptEdits")
            self.assertEqual(approve_config["permissionMode"], "acceptEdits")
            self.assertEqual([event for event, _ in approve_events], ["plan.requested", "plan.approved"])
            self.assertEqual(run(core.loadPlanApproval(approved_payload["requestId"]))["status"], "approved")
        finally:
            self.restore_home(root, previous)

    def test_monitor_health_check_mcp_resource_and_ripgrep_tool_edges(self):
        root, home, workspace, previous = self.temp_context()
        original_fetch = tools_module.fetch
        original_resolve_rg = tools_module._resolve_ripgrep_binary
        original_exec_rg = tools_module._exec_ripgrep_allowing_no_matches
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            tools = {tool.name: tool for tool in core.createBuiltinTools()}
            events = []
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persistSession": lambda: None}

            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-1", "goal": "watch parity"})
            run(core.saveMonitor(monitor))
            stopped = json.loads(run(tools["TaskMonitorStop"].handler({"monitor_id": monitor["id"]}, context))["content"])
            self.assertEqual(stopped, {"monitor_id": monitor["id"], "task_id": "task-1", "status": "stopped"})
            self.assertEqual([event for event, _ in events[-2:]], ["task.failed", "monitor.status_changed"])
            self.assertEqual(events[-2][1]["task_id"], f"monitor:{monitor['id']}")

            with self.assertRaises(RuntimeError):
                run(tools["TaskHealthCheckStart"].handler({"title": "health", "mode": "http"}, context))
            health_calls = []

            async def start_health(payload):
                health_calls.append(("start", payload))
                return {"taskId": "health-1"}

            async def stop_health(task_id):
                health_calls.append(("stop", task_id))
                return True

            health_context = {**context, "daemon": {"startHealthCheck": start_health, "stopTask": stop_health}}
            started = json.loads(run(tools["TaskHealthCheckStart"].handler({"title": "health", "mode": "http", "url": "http://localhost", "expected_status_codes": [200, "bad"], "expected_body_substring": "ok"}, health_context))["content"])
            self.assertEqual(started["task_id"], "health-1")
            self.assertEqual(health_calls[0][1]["expectedStatusCodes"], [200])
            self.assertEqual(health_calls[0][1]["expectedBodySubstring"], "ok")
            stopped_health = json.loads(run(tools["TaskHealthCheckStop"].handler({"task_id": "health-1"}, health_context))["content"])
            self.assertEqual(stopped_health, {"task_id": "health-1", "stopped": True})

            class HeaderResponse(Response):
                def __init__(self, body, status=200):
                    super().__init__(body, status)
                    self.headers = {"content-type": "text/plain"}

            async def fake_fetch(*args, **kwargs):
                return HeaderResponse("resource body")

            tools_module.fetch = fake_fetch
            config["mcpConfig"] = [{"name": "http", "transport": "http", "commandOrUrl": "http://mcp", "headers": {"authorization": "Bearer token"}}, {"name": "stdio", "transport": "stdio", "commandOrUrl": "python"}]
            resource = json.loads(run(tools["ReadMcpResource"].handler({"server": "http", "uri": "http://mcp/resource"}, context))["content"])
            self.assertEqual(resource["contents"][0]["mimeType"], "text/plain")
            self.assertEqual(resource["contents"][0]["text"], "resource body")
            stdio_resource = run(tools["ReadMcpResource"].handler({"server": "stdio", "uri": "file://resource"}, context))
            self.assertTrue(stdio_resource["isError"])
            self.assertEqual(stdio_resource["content"], "MCP server not found: stdio")

            async def missing_rg():
                return None

            tools_module._resolve_ripgrep_binary = missing_rg
            self.assertTrue(run(tools["Glob"].handler({"pattern": "*.py"}, context))["isError"])
            self.assertTrue(run(tools["Grep"].handler({"pattern": "needle"}, context))["isError"])

            rg_calls = []

            async def fake_rg():
                return "/fake/rg"

            async def fake_exec_rg(path, args, *, cwd):
                rg_calls.append((path, args, cwd))
                return "src/a.py\nnode_modules/ignored.js\n", ""

            tools_module._resolve_ripgrep_binary = fake_rg
            tools_module._exec_ripgrep_allowing_no_matches = fake_exec_rg
            glob_payload = json.loads(run(tools["Glob"].handler({"pattern": "*.py"}, context))["content"])
            self.assertEqual(glob_payload["matches"], ["src/a.py"])
            self.assertEqual(rg_calls[-1][1][0:3], ["--files", "-g", "*.py"])
            grep_payload = json.loads(run(tools["Grep"].handler({"pattern": "needle", "output_mode": "content", "glob": "*.py", "-i": True, "context": 2}, context))["content"])
            self.assertEqual(grep_payload["outputMode"], "content")
            self.assertIn("--glob", rg_calls[-1][1])
            self.assertIn("-i", rg_calls[-1][1])
            self.assertIn("-C", rg_calls[-1][1])
        finally:
            tools_module.fetch = original_fetch
            tools_module._resolve_ripgrep_binary = original_resolve_rg
            tools_module._exec_ripgrep_allowing_no_matches = original_exec_rg
            self.restore_home(root, previous)
