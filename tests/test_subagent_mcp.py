from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.authority.agent import filter_tools_for_agent_authority, assert_valid_agent_authority, AgentAuthorityError
from agent_core.authority.executable_config import parse_mcp_server_config
from agent_core.authority.executable_source_policy import executable_config_digest
from agent_core.runtime.generation import create_runtime_session_binding
from agent_core.tasks.runners.subagent_components import delegation_exclusion, prepare_subagent_components


class SubagentMcpTests(AgentCoreTestCase):
    def test_descriptor_policy_is_pinned_and_invalid_values_fail_closed(self):
        raw = {"name": "bound", "transport": "stdio", "commandOrUrl": sys.executable}
        normal = parse_mcp_server_config(raw)
        pinned = parse_mcp_server_config({**raw, "delegation": {"mode": "deny", "scope": "session"}})
        self.assertEqual(pinned["delegation"], {"mode": "deny", "scope": "session"})
        self.assertNotEqual(executable_config_digest([normal]), executable_config_digest([pinned]))
        authority = {"executableSources": {"mcpDelegationVersion": 1}}
        self.assertIsNone(delegation_exclusion(normal, authority))
        self.assertEqual(delegation_exclusion(pinned, authority), "nondelegable_server")
        self.assertEqual(delegation_exclusion(normal, {"executableSources": {}}), "legacy_delegation_absent")
        for policy in ({}, {"mode": []}, {"mode": "allow", "scope": {}}, {"mode": "unknown"}, {"mode": "allow", "scope": "unknown"}, {"mode": "allow", "extra": True}):
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    parse_mcp_server_config({**raw, "delegation": policy})
                self.assertEqual(delegation_exclusion({**raw, "delegation": policy}, authority), "invalid_delegation_policy")

    def test_real_stdio_composition_refresh_filter_and_cleanup(self):
        root, home, workspace, previous = self.temp_context()
        try:
            server = workspace / "delegated_mcp.py"
            pid_file = workspace / "server.pid"
            server.write_text(textwrap.dedent(r'''
                import json, os, sys
                from pathlib import Path
                Path(sys.argv[1]).write_text(str(os.getpid()))
                changed = False
                def send(value):
                    print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    request = json.loads(line)
                    ident, method = request.get("id"), request.get("method")
                    if ident is None:
                        continue
                    if method == "initialize":
                        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "fixture", "version": "1"}}
                    elif method == "tools/list":
                        names = ["echo", "forbidden"] + (["later"] if changed else [])
                        result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names]}
                    elif method == "tools/call":
                        changed = True
                        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                        result = {"content": [{"type": "text", "text": "real child MCP"}]}
                    else:
                        result = {}
                    send({"jsonrpc": "2.0", "id": ident, "result": result})
            '''))

            async def scenario():
                authority = {"enabledTools": ["mcp__fixture__echo", "mcp__fixture__later"], "executableSources": {"mcpDelegationVersion": 1, "pluginRecords": []}}
                session = {"id": "child", "cwd": str(workspace), "metadata": {"effectiveAgentAuthority": authority}}
                descriptor = {"name": "fixture", "transport": "stdio", "commandOrUrl": sys.executable, "args": [str(server), str(pid_file)]}
                denied_pid = workspace / "denied.pid"
                denied = {**descriptor, "name": "bound", "args": [str(server), str(denied_pid)], "delegation": {"mode": "deny", "scope": "session"}}
                events = []
                async def enforce(**_):
                    return authority
                async def plugins(*_):
                    return {"tools": [], "mcpServers": []}
                factories = {
                    "enforceAuthority": enforce, "filterAuthorityTools": filter_tools_for_agent_authority,
                    "createWorkspace": lambda **_: object(), "load_plugins": plugins,
                    "createMcp": core.create_mcp_runtime, "create_builtin_tools": lambda _: [],
                    "augment_config_tools": lambda *_: None,
                }
                components = await prepare_subagent_components(
                    session=session, config={"cwd": str(workspace), "mcpConfig": [descriptor, denied]},
                    binding=create_runtime_session_binding("child", 1, 1), emit=lambda *args: events.append(args), factories=factories,
                )
                pid = int(pid_file.read_text())
                try:
                    self.assertEqual([t.name for t in components["tools"]], ["mcp__fixture__echo"])
                    self.assertFalse(denied_pid.exists())
                    self.assertIn(("mcp.delegation_excluded", {"server": "bound", "reason": "nondelegable_server"}), events)
                    result = await components["mcp"]["callTool"]("fixture", "echo", {})
                    self.assertIn("real child MCP", str(result))
                    for _ in range(100):
                        refresh = await components["refreshMcpTools"]()
                        if refresh["changed"]:
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(refresh["changed"])
                    self.assertEqual({t.name for t in components["tools"]}, {"mcp__fixture__echo", "mcp__fixture__later"})
                    self.assertNotIn("mcp__fixture__forbidden", [t.name for t in components["tools"]])
                finally:
                    await components["close"]()
                    await components["close"]()  # owned closure is idempotent
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_worker_rehydrates_pinned_mcp_and_rejects_delegation_tampering(self):
        from tests.test_resident_subagent import ResidentSubagentTests
        from agent_core.tasks.runners.subagent import _prepare_worker
        root, home, workspace, previous = self.temp_context()
        try:
            config, parent, child = ResidentSubagentTests()._seed_family(home, workspace)
            server = workspace / "pinned.py"
            server.write_text("import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n if 'id' in r:\n  result={'tools':[{'name':'echo','inputSchema':{'type':'object'}}]} if r.get('method')=='tools/list' else {}\n  print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)\n")
            descriptor = {"name": "pinned", "transport": "stdio", "commandOrUrl": sys.executable, "args": [str(server)], "delegation": {"mode": "allow", "scope": "shared"}}
            config["mcpConfig"] = [descriptor]
            config["tools"] = ["mcp__pinned__echo"]
            config["allowedTools"] = ["mcp__pinned__echo"]
            child["metadata"].pop("effectiveAgentAuthority")
            authority = run(core.capture_agent_authority(config=config, session=child, tools=[{"name": "mcp__pinned__echo"}]))
            child["metadata"]["effectiveAgentAuthority"] = authority
            run(core.save_session(child))
            record = run(core.load_child_agent("resident-agent"))
            record["effectiveAgentAuthority"] = authority
            run(core.save_child_agent(record))
            self.assertEqual(authority["executableSources"]["mcpDelegationVersion"], 1)

            async def scenario():
                session, restored, runtime, close = await _prepare_worker({"sessionId": child["id"], "agentId": "resident-agent", "taskId": "bootstrap-test"})
                try:
                    self.assertEqual(restored["mcpConfig"], [descriptor])
                    self.assertEqual([t.name for t in runtime["tools"]], ["mcp__pinned__echo"])
                    self.assertEqual([t.name for t in await runtime["refreshMcpToolRegistry"]()], ["mcp__pinned__echo"])
                finally:
                    await close()
            run(scenario())
            authority["executableSources"]["mcpConfig"][0]["delegation"]["mode"] = "deny"
            with self.assertRaisesRegex(AgentAuthorityError, "fingerprint"):
                assert_valid_agent_authority(authority)
        finally:
            self.restore_home(root, previous)

    def test_outer_cleanup_on_one_shot_and_resident_cancellation(self):
        import agent_core.tasks.runners.subagent as runner
        async def scenario():
            for entry in (runner._execute_background_subagent, runner._run_resident_subagent):
                close = mock.AsyncMock()
                prepare = mock.AsyncMock(return_value=({"id": "s"}, {}, {}, close))
                with mock.patch.object(runner, "_prepare_worker", prepare), mock.patch.object(runner, "run_agent_prompt", mock.AsyncMock(side_effect=asyncio.CancelledError)), mock.patch.object(runner, "_mark_resident_status", mock.AsyncMock()):
                    with self.assertRaises(asyncio.CancelledError):
                        await entry({"sessionId": "s", "agentId": "a", "taskId": "t"})
                close.assert_awaited_once()
        run(scenario())
