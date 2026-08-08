from __future__ import annotations

import agent_core.authority.mcp_origin_policy as mcp_origin_policy_module

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
            session["metadata"]["monitorIds"] = [monitor["id"]]
            stopped = json.loads(run(tools["TaskMonitorStop"].handler({"monitor_id": monitor["id"]}, context))["content"])
            self.assertEqual(
                {key: stopped[key] for key in ("monitor_id", "task_id", "status", "previous_status", "changed")},
                {"monitor_id": monitor["id"], "task_id": "task-1", "status": "stopped", "previous_status": "active", "changed": True},
            )
            self.assertEqual(stopped["revision"], 1)
            self.assertTrue(stopped["transition_id"].startswith(f"monitor:{monitor['id']}:r1:"))
            self.assertEqual([event for event, _ in events], ["monitor.status_changed"])

            with self.assertRaises(RuntimeError):
                run(tools["TaskHealthCheckStart"].handler({"title": "health", "mode": "http"}, context))
            health_calls = []

            async def start_health(payload):
                health_calls.append(("start", payload))
                await core.saveTaskRecord(
                    {
                        "id": "health-1",
                        "sessionId": session["id"],
                        "sourceId": "health-1",
                        "kind": "health_check",
                        "status": "running",
                        "title": "health",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "startedAt": "2026-01-01T00:00:00Z",
                    }
                )
                await core.writeTaskMeta(
                    "health-1",
                    {
                        "type": "health_check",
                        "taskId": "health-1",
                        "sessionId": session["id"],
                        "status": "running",
                    },
                )
                return {"taskId": "health-1"}

            async def stop_health(task_id):
                health_calls.append(("stop", task_id))
                return True

            health_context = {**context, "daemon": {"startHealthCheck": start_health, "stopTask": stop_health}}
            with self.assertRaises(ValueError):
                run(tools["TaskHealthCheckStart"].handler({"title": "health", "mode": "http", "url": "http://localhost", "expected_status_codes": [200, "bad"], "expected_body_policy": "contains", "expected_body_substring": "ok"}, health_context))
            started = json.loads(run(tools["TaskHealthCheckStart"].handler({"title": "health", "mode": "http", "url": "http://localhost", "expected_status_codes": [200], "expected_body_policy": "contains", "expected_body_substring": "ok"}, health_context))["content"])
            self.assertEqual(started["task_id"], "health-1")
            self.assertEqual(health_calls[0][1]["spec"]["expectedStatusCodes"], [200])
            self.assertEqual(health_calls[0][1]["spec"]["expectedBodySubstring"], "ok")
            self.assertEqual(len(health_calls[0][1]["specDigest"]), 64)
            stopped_health = json.loads(run(tools["TaskHealthCheckStop"].handler({"task_id": "health-1"}, health_context))["content"])
            self.assertEqual(
                stopped_health,
                {
                    "task_id": "health-1",
                    "previous_status": "running",
                    "status": "stopped",
                    "changed": True,
                    "stopped": True,
                    "note": "Task stopped by deliberate request.",
                },
            )

            config["mcpConfig"] = [{"name": "http", "transport": "http", "commandOrUrl": "http://mcp", "headers": {"authorization": "Bearer token"}}, {"name": "stdio", "transport": "stdio", "commandOrUrl": "python"}]
            with mock.patch.object(
                mcp_origin_policy_module,
                "_fetch_manual",
                return_value=mcp_origin_policy_module.McpResourceResponse(
                    200,
                    b"resource body",
                    {"content-type": "text/plain"},
                ),
            ):
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
            self.assertEqual(rg_calls[-1][1][0:5], ["--files", "--sort", "path", "-g", "*.py"])
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
