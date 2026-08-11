from __future__ import annotations

from tests.support.agent_core_case import *

from tests.test_mcp_interop_quickwins import _stdio_error_config, _write_error_fixture

from agent_core.authority.permission_rules import clearPermissionRulesCache
from agent_core.tools.permissions import _resolve_tool_permission


class McpPermissionResolutionTests(AgentCoreTestCase):
    def test_mcp_category_resolution_by_mode(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            context = {"session": session, "config": config, "interactive": False, "cwd": str(workspace)}
            cases = [
                ("bypassPermissions", {"decision": "allow", "source": "mode"}),
                ("default", {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This MCP server tool runs outside the built-in toolset."}),
                ("acceptEdits", {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This MCP server tool runs outside the built-in toolset."}),
                ("auto", {"decision": "deny", "source": "mode", "reason": "MCP tools require an explicit allow permission rule in non-interactive permission modes."}),
                ("dontAsk", {"decision": "deny", "source": "mode", "reason": "MCP tools require an explicit allow permission rule in non-interactive permission modes."}),
            ]
            for mode, expected in cases:
                with self.subTest(mode=mode):
                    self.assertEqual(
                        run(_resolve_tool_permission(context, "mcp__srv__tool", "mcp", mode, False, None)),
                        expected,
                    )
        finally:
            self.restore_home(root, previous)

    def test_mcp_rules_match_tool_and_server_prefix(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            cases = [
                ({"allow": ["mcp__srv__tool"]}, "default", "allow", "rule:inline"),
                ({"allow": ["mcp__srv"]}, "default", "allow", "rule:inline"),
                ({"allow": ["mcp__srv"]}, "auto", "allow", "rule:inline"),
                ({"ask": ["mcp__srv__tool"]}, "default", "ask", "rule:inline"),
                # bypassPermissions outranks ask rules (engine-wide semantics);
                # only deny rules survive bypass.
                ({"ask": ["mcp__srv__tool"]}, "bypassPermissions", "allow", "mode"),
                ({"deny": ["mcp__srv"]}, "bypassPermissions", "deny", "rule:inline"),
                ({"allow": ["mcp__other"]}, "default", "ask", "mode"),
                # Argument patterns have no MCP matching language: they must
                # not weaken deny/ask (fire on the tool alone) and may only
                # narrow allow (never match).
                ({"deny": ["mcp__srv__tool(secret)"]}, "bypassPermissions", "deny", "rule:inline"),
                ({"deny": ["mcp__srv(anything)"]}, "default", "deny", "rule:inline"),
                ({"ask": ["mcp__srv__tool(x)"]}, "default", "ask", "rule:inline"),
                ({"allow": ["mcp__srv__tool(x)"]}, "default", "ask", "mode"),
            ]
            for permissions, mode, decision, source in cases:
                with self.subTest(permissions=permissions, mode=mode):
                    clearPermissionRulesCache()
                    scoped_config = {**config, "inlineSettings": json.dumps({"permissions": permissions})}
                    context = {"session": session, "config": scoped_config, "interactive": False, "cwd": str(workspace)}
                    resolved = run(_resolve_tool_permission(context, "mcp__srv__tool", "mcp", mode, False, None))
                    self.assertEqual(resolved["decision"], decision)
                    self.assertEqual(resolved["source"], source)
        finally:
            self.restore_home(root, previous)


class McpPermissionGateIntegrationTests(AgentCoreTestCase):
    def _runtime_and_tool(self, workspace: Path):
        fixture = _write_error_fixture(workspace)
        runtime = run(
            core.createMcpRuntime(
                {"cwd": str(workspace), "servers": [_stdio_error_config(fixture)]}
            )
        )
        pid_tool = next(
            tool for tool in runtime["createToolDefinitions"]() if tool.name == "mcp__errors__pid"
        )
        return runtime, pid_tool

    def _context(self, workspace: Path, home: Path, mode: str, **overrides):
        config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": mode}, str(workspace)))
        session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=mode)
        return {
            "session": session,
            "config": config,
            "interactive": False,
            "askQuestion": lambda *_: "",
            "outputJson": lambda *_: None,
            "cwd": str(workspace),
            "persistSession": lambda: None,
            **overrides,
        }

    def test_mcp_tool_handler_is_permission_gated(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            clearPermissionRulesCache()
            runtime, pid_tool = self._runtime_and_tool(workspace)

            # Default mode, non-interactive: blocked before the server runs.
            with self.assertRaises(PermissionError):
                run(pid_tool.handler({}, self._context(workspace, home, "default")))

            # Missing session/config context: fail closed, never dispatch.
            with self.assertRaises(PermissionError):
                run(pid_tool.handler({}, {}))

            # bypassPermissions: allowed by mode.
            result = run(pid_tool.handler({}, self._context(workspace, home, "bypassPermissions")))
            self.assertFalse(result.get("isError"))

            # Interactive approval in default mode.
            events = []
            approve_context = self._context(
                workspace,
                home,
                "default",
                interactive=True,
                askQuestion=lambda _prompt, options=None, _detail=None: "Approve once",
                outputJson=lambda event, data: events.append((event, data)),
            )
            result = run(pid_tool.handler({}, approve_context))
            self.assertFalse(result.get("isError"))
            self.assertTrue(any(event == "permission.requested" for event, _ in events))

            # An inline allow rule naming the runtime tool permits unprompted.
            allow_context = self._context(workspace, home, "default")
            allow_context["config"] = {
                **allow_context["config"],
                "inlineSettings": json.dumps({"permissions": {"allow": ["mcp__errors__pid"]}}),
            }
            decisions = []
            allow_context["outputJson"] = lambda event, data: decisions.append((event, data))
            result = run(pid_tool.handler({}, allow_context))
            self.assertFalse(result.get("isError"))
            self.assertTrue(
                any(
                    data.get("decision") == "allow" and data.get("source") == "rule:inline"
                    for event, data in decisions
                    if event == "permission.decided"
                )
            )

            # A deny rule blocks even bypassPermissions.
            clearPermissionRulesCache()
            deny_context = self._context(workspace, home, "bypassPermissions")
            deny_context["config"] = {
                **deny_context["config"],
                "inlineSettings": json.dumps({"permissions": {"deny": ["mcp__errors"]}}),
            }
            with self.assertRaises(PermissionError) as denied:
                run(pid_tool.handler({}, deny_context))
            self.assertIn("mcp__errors", str(denied.exception))
        finally:
            clearPermissionRulesCache()
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)
