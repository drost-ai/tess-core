from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core import start_runtime, SessionOptions
from agent_core.runtime.shared_mcp import prepare_host_shared_mcp, _unconditionally_granted_tools
from agent_core.authority.agent import assert_valid_agent_authority
from agent_core.authority.executable_config import parse_mcp_server_config
from agent_core.mcp.shared_broker_http import _control_request
from agent_core.runtime.secrets import resolve_mcp_server_config_secrets
from agent_core.subagents import prepare_subagent
from agent_core.tasks.runners.subagent import _prepare_worker


class HostSharedMcpTests(AgentCoreTestCase):
    def test_parser_and_permission_constraints_fail_closed(self):
        descriptor = {"name": "coding", "transport": "http", "commandOrUrl": "http://127.0.0.1:1234/mcp", "sharedBroker": {"version": 1}}
        self.assertEqual(parse_mcp_server_config(descriptor)["sharedBroker"], {"version": 1})
        for marker in ({"version": True}, {"version": 2}, {"version": 1, "token": "secret"}, None):
            with self.assertRaises(ValueError):
                parse_mcp_server_config({**descriptor, "sharedBroker": marker})
        name = "mcp__coding__echo"
        base = {"mode": "bypassPermissions", "enabledTools": [name], "permissionRules": []}
        self.assertEqual(_unconditionally_granted_tools(base), {name})
        for effect in ("deny", "ask"):
            rules = [{"tool": "mcp__coding", "effect": effect, "pattern": "scoped"}]
            self.assertEqual(_unconditionally_granted_tools({**base, "permissionRules": rules}), set())
        self.assertEqual(_unconditionally_granted_tools({**base, "mode": "default"}), set())
        self.assertEqual(_unconditionally_granted_tools({**base, "hooks": [{"blocking": True}]}), set())

    def test_real_spawn_restore_worker_close_and_host_lifetime(self):
        root, home, workspace, previous = self.temp_context()
        try:
            script = workspace / "shared-server.py"
            starts = workspace / "starts"
            script.write_text("import json,sys,os\nwith open(sys.argv[1],'a') as f: f.write(str(os.getpid())+'\\n')\nfor line in sys.stdin:\n r=json.loads(line)\n if 'id' not in r: continue\n m=r.get('method')\n result={'tools':[{'name':'Echo-X','inputSchema':{'type':'object'}},{'name':'forbidden','inputSchema':{'type':'object'}}]} if m=='tools/list' else ({'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}} if m=='initialize' else {'content':[{'type':'text','text':'ok'}]})\n print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)\n")

            async def scenario():
                runtime = await start_runtime(str(home), cwd=str(workspace), daemon_enabled=False, shared_mcp_servers=("coding", "personal"))
                config = await core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace))
                config["tools"] = ["Agent", "mcp__coding__echo_x", "mcp__coding__forbidden", "ReadMcpResource"]
                config["inlineSettings"] = json.dumps({"permissions": {"deny": ["mcp__coding__forbidden(secret)", "ReadMcpResource(private/**)"]}})
                config["allowedTools"] = list(config["tools"])
                original = {"name": "coding", "transport": "stdio", "commandOrUrl": sys.executable, "args": [str(script), str(starts)]}
                personal = {**original, "name": "personal", "delegation": {"mode": "deny", "scope": "session"}}
                config["mcpConfig"] = [original, personal]
                parent = core.create_empty_session(cwd=str(workspace), provider=config["provider"], model=config["model"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
                try:
                    await prepare_host_shared_mcp(runtime, config, parent)
                    self.assertEqual(config["mcpConfig"][1], personal)
                    parent_descriptor = config["mcpConfig"][0]
                    self.assertNotIn("Bearer ", json.dumps(parent_descriptor))
                    context = {"session": parent, "config": config, "interactive": False, "cwd": str(workspace), "persist_session": lambda: None}
                    await core.save_session(parent)
                    prepared = await prepare_subagent({"context": context, "description": "shared", "prompt": "test", "runInBackground": True})
                    child = prepared["childSession"]
                    authority = child["metadata"]["effectiveAgentAuthority"]
                    assert_valid_agent_authority(authority)
                    child_descriptor = authority["executableSources"]["mcpConfig"][0]
                    self.assertNotEqual(child_descriptor["headers"], parent_descriptor["headers"])
                    self.assertNotIn("Bearer ", json.dumps(child))
                    _, restored, worker, close = await _prepare_worker({"sessionId": child["id"], "agentId": prepared["agentId"], "taskId": "shared-test"})
                    self.assertEqual(restored["mcpConfig"][0], child_descriptor)
                    self.assertIn("mcp__coding__echo_x", [tool.name for tool in worker["tools"]])
                    runtime.session_options = lambda *_: SessionOptions(provider="openai", permission_mode="bypassPermissions", mcp_servers=(original,), strict_mcp_config=True)
                    opened = await runtime.sessions.open()
                    self.assertIsNotNone(runtime._shared_mcp_broker)
                    opened_id = opened.id
                    await opened.close()
                    leases_before_reopen = len(runtime._shared_mcp_broker._leases)
                    reopened = await runtime.sessions.open(opened_id)
                    await reopened.close()
                    self.assertEqual(len(runtime._shared_mcp_broker._leases), leases_before_reopen)
                    resolved_child = await resolve_mcp_server_config_secrets(child_descriptor)
                    self.assertIn("content", await _control_request(resolved_child, "tools/call", {"name": "Echo-X", "arguments": {}}))
                    with self.assertRaises(PermissionError):
                        await _control_request(resolved_child, "tools/call", {"name": "forbidden", "arguments": {}})
                    with self.assertRaises(PermissionError):
                        await _control_request(resolved_child, "resources/read", {"uri": "private/secret"})
                    await close()
                    with self.assertRaises(PermissionError):
                        await _control_request(resolved_child, "tools/list", {})
                    resolved_parent = await resolve_mcp_server_config_secrets(parent_descriptor)
                    self.assertEqual(len((await _control_request(resolved_parent, "tools/list", {}))["tools"]), 2)
                    from agent_core.subagents.lifecycle import start_background_subagent
                    leases = len(runtime._shared_mcp_broker._leases)
                    failed_context = {**context, "daemon": {"startSubagentTask": mock.AsyncMock(side_effect=RuntimeError("launch failed"))}}
                    with self.assertRaisesRegex(RuntimeError, "launch failed"):
                        await start_background_subagent({"context": failed_context, "description": "failed", "prompt": "test", "runInBackground": True})
                    self.assertEqual(len(runtime._shared_mcp_broker._leases), leases)
                    crashed = await prepare_subagent({"context": context, "description": "crashed", "prompt": "test", "runInBackground": True})
                    crashed_descriptor = crashed["childConfig"]["mcpConfig"][0]
                    resolved_crashed = await resolve_mcp_server_config_secrets(crashed_descriptor)
                    crash_meta = {"id": "crashed-task", "taskId": "crashed-task", "cwd": str(workspace), "parent_session_id": parent["id"], "type": "subagent", "status": "running", "agent_id": crashed["agentId"], "sessionId": crashed["childSession"]["id"]}
                    await core.write_task_meta("crashed-task", crash_meta)
                    event = {"event": "task.failed", "data": {"task_id": "crashed-task", "session_id": parent["id"]}}
                    runtime.sessions.route_daemon_event(event)
                    await asyncio.gather(*list(runtime._shared_mcp_cleanup_tasks.values()))
                    self.assertIn("tools", await _control_request(resolved_crashed, "tools/list", {}))
                    # A terminal task alone cannot revoke a grant whose child
                    # linkage is missing, or belongs to a newer retry.
                    await core.write_task_meta("crashed-task", {**crash_meta, "status": "failed"})
                    for linked_task in (None, "newer-task"):
                        await core.save_child_agent({**crashed["childRecord"], "taskId": linked_task})
                        runtime.sessions.route_daemon_event(event)
                        await asyncio.gather(*list(runtime._shared_mcp_cleanup_tasks.values()))
                        self.assertIn("tools", await _control_request(resolved_crashed, "tools/list", {}))
                    await core.save_child_agent({**crashed["childRecord"], "taskId": "crashed-task"})
                    await core.write_task_meta("crashed-task", crash_meta)
                    from agent_core.tasks.supervisor_parts.finalize_child import finalize_lost_worker
                    await finalize_lost_worker(taskId="crashed-task", cause="worker_lost", source="test",
                        emit=lambda event, data, session_id: runtime.sessions.route_daemon_event({"event": event, "data": data, "sessionId": session_id}))
                    await asyncio.gather(*list(runtime._shared_mcp_cleanup_tasks.values()))
                    with self.assertRaises(PermissionError):
                        await _control_request(resolved_crashed, "tools/list", {})
                    self.assertIn("tools", await _control_request(resolved_parent, "tools/list", {}))
                    # A second host session shares the same backend, not a new stdio.
                    second = {**config, "mcpConfig": [original]}
                    await prepare_host_shared_mcp(runtime, second, {**parent, "id": "second"})
                    self.assertEqual(len(starts.read_text().splitlines()), 1)
                finally:
                    await runtime.stop()
                with self.assertRaises((OSError, PermissionError)):
                    await _control_request(resolved_parent, "tools/list", {})
            run(scenario())
        finally:
            self.restore_home(root, previous)
