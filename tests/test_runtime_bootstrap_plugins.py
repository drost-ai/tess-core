from __future__ import annotations

from tests.support.agent_core_case import *


class RuntimeBootstrapPluginTests(AgentCoreTestCase):
    def test_bootstrap_runtime_exposes_typed_event_and_turn_lifecycle_surface(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            boot = run(core.bootstrapRuntime({"session": session, "config": config}))
            self.assertEqual(boot["getState"]()["session"]["activeSessionId"], session["id"])
            envelopes = []
            unsubscribe = boot["subscribeEvents"](envelopes.append)
            boot["startTurn"]({"prompt": "build", "agentName": "main"})
            self.assertEqual(boot["getState"]()["turn"]["status"], "running")
            self.assertEqual(envelopes[-1]["typedEvent"]["kind"], "turn.started")
            boot["finishTurn"]({"kind": "completed", "stopReason": "stop", "message": "done"})
            self.assertEqual(boot["getState"]()["turn"]["lastOutcome"]["message"], "done")
            other = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="acceptEdits")
            before_switch_events = len(envelopes)
            boot["setActiveSession"](other, {**config, "permissionMode": "acceptEdits"})
            self.assertEqual(boot["getState"]()["session"]["activeSessionId"], other["id"])
            self.assertTrue(
                any(
                    envelope["typedEvent"]["kind"] == "session.activated"
                    for envelope in envelopes[before_switch_events:]
                )
            )
            unsubscribe()
            run(boot["close"]())
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_runtime_composes_local_runtimes_bridge_and_dynamic_tools(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            default_tools = core.getDefaultToolList()
            config["tools"] = list(default_tools)
            config["allowedTools"] = list(default_tools)
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            remote = run(core.createRemoteSession({"session": session, "description": "Bridge parity", "channelServers": ["local"]}))
            session["metadata"]["remoteControlEnabled"] = True
            session["metadata"]["remoteSessionId"] = remote["id"]
            dynamic_tool = SimpleNamespace(name="RemoteEcho")
            mcp_runtime = {"createToolDefinitions": lambda: [dynamic_tool], "close": lambda: None}

            boot = run(core.bootstrapRuntime({"session": session, "config": config, "mcpRuntime": mcp_runtime, "enableBridge": True, "bridgeName": "qa", "bridgeStartPolling": False}))
            tool_names = [getattr(tool, "name", None) or tool.get("name") for tool in boot["tools"]]
            self.assertIn("RemoteEcho", tool_names)
            self.assertIn("RemoteEcho", config["tools"])
            self.assertIn("RemoteEcho", config["allowedTools"])
            self.assertTrue(boot["monitors"])
            self.assertTrue(boot["tasks"])
            self.assertTrue(boot["collaboration"])
            self.assertTrue(boot["bridge"])
            state = boot["getState"]()
            self.assertTrue(state["bridge"]["attached"])
            self.assertEqual(state["bridge"]["bridgeId"], boot["bridge"]["bridge"]["id"])
            self.assertEqual(boot["getMonitorSnapshot"](), state["monitors"]["snapshot"])
            self.assertEqual(boot["getTaskSnapshot"]()["total"], state["tasks"]["total"])
            run(boot["close"]())
            closed_bridge = run(core.loadBridge(boot["bridge"]["bridge"]["id"]))
            self.assertEqual(closed_bridge["status"], "closed")
        finally:
            self.restore_home(root, previous)

    def test_plugin_runtime_loads_manifest_entry_commands_and_mcp_servers(self):
        root, home, workspace, previous = self.temp_context()
        try:
            plugin_dir = workspace / "alpha-plugin"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "alpha",
                        "version": "1.2.3",
                        "entry": "plugin_entry.py",
                        "capabilities": {"tools": True, "commands": True, "mcpServers": True},
                        "commands": [
                            {
                                "name": "hello",
                                "description": "Say hello",
                                "command": "printf 'cmd:%s:%s' \"{{args}}\" \"{{session_id}}\"",
                            }
                        ],
                        "mcpServers": [
                            {
                                "name": "alpha-mcp",
                                "transport": "http",
                                "commandOrUrl": "http://alpha.invalid/rpc",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "plugin_entry.py").write_text(
                textwrap.dedent(
                    """
                    from agent_core.tools import ToolDefinition


                    async def echo_handler(input, context):
                        return {"content": "entry:" + str(input.get("value", ""))}


                    def default(context):
                        return {
                            "tools": [
                                ToolDefinition(
                                    "PluginEcho",
                                    "Echo from plugin",
                                    {"type": "object", "properties": {"value": {"type": "string"}}},
                                    echo_handler,
                                )
                            ],
                            "commands": [
                                {
                                    "name": "entrycmd",
                                    "usage": "/entrycmd",
                                    "description": "Entry command",
                                    "handler": lambda payload: "entry handled " + payload.get("argText", ""),
                                }
                            ],
                            "mcpServers": [
                                {
                                    "name": "entry-mcp",
                                    "transport": "http",
                                    "commandOrUrl": "http://entry.invalid/rpc",
                                }
                            ],
                        }
                    """
                ),
                encoding="utf-8",
            )

            config = run(
                core.resolveConfig(
                    {"provider": "openai", "runtimeHome": str(home), "permissionMode": "default", "pluginDir": [str(plugin_dir)]},
                    str(workspace),
                )
            )
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )

            plugin_runtime = run(core.loadPluginRuntime(config))
            self.assertEqual(plugin_runtime["errors"], [])
            self.assertEqual([plugin["id"] for plugin in plugin_runtime["plugins"]], ["alpha"])
            self.assertEqual([tool.name for tool in plugin_runtime["tools"]], ["PluginEcho"])
            self.assertEqual([command["name"] for command in plugin_runtime["commands"]], ["hello", "entrycmd"])
            self.assertEqual({server["name"] for server in plugin_runtime["mcpServers"]}, {"alpha-mcp", "entry-mcp"})
            self.assertTrue(all(server.get("updatedAt") for server in plugin_runtime["mcpServers"]))
            self.assertEqual(plugin_runtime["plugins"][0]["capabilities"]["provided"], {"tools": 1, "commands": 2, "mcpServers": 2})
            self.assertEqual([diagnostic for diagnostic in plugin_runtime["diagnostics"] if diagnostic["severity"] == "error"], [])

            tool_result = run(plugin_runtime["tools"][0].handler({"value": "ok"}, {"session": session, "config": config}))
            self.assertEqual(tool_result["content"], "entry:ok")

            infos = []
            errors = []
            hooks = {"info": lambda title, message: infos.append((title, message)), "error": errors.append}
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                shell_result = run(
                    core.handleSlashCommand(
                        {
                            "command": "/hello world",
                            "session": session,
                            "config": config,
                            "hooks": hooks,
                            "runtimeCommands": plugin_runtime["commands"],
                        }
                    )
                )
            self.assertEqual(shell_result, {"outcome": "handled"})
            self.assertEqual(errors, [])
            self.assertEqual(infos[-1], ("Plugin alpha", f"cmd:world:{session['id']}"))

            entry_result = run(
                core.handleSlashCommand(
                    {
                        "command": "/entrycmd payload",
                        "session": session,
                        "config": config,
                        "hooks": hooks,
                        "runtimeCommands": plugin_runtime["commands"],
                    }
                )
            )
            self.assertEqual(entry_result["message"], "entry handled payload")
        finally:
            self.restore_home(root, previous)

    def test_plugin_runtime_installed_invalid_source_and_explicit_conflict_resolution(self):
        root, home, workspace, previous = self.temp_context()
        try:
            explicit_dir = workspace / "shared-plugin"
            explicit_dir.mkdir()
            (explicit_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "shared",
                        "version": "2.0.0",
                        "commands": [{"name": "explicit", "description": "Explicit wins", "command": "printf explicit"}],
                    }
                ),
                encoding="utf-8",
            )
            run(
                core.savePlugins(
                    "project",
                    str(workspace),
                    [
                        {
                            "enabled": True,
                            "source": str(workspace / "missing-ghost-plugin"),
                            "manifest": {
                                "id": "ghost",
                                "version": "1.0.0",
                                "commands": [{"name": "ghost", "description": "Manifest only", "command": "printf ghost"}],
                                "mcpServers": [{"name": "ghost-mcp", "transport": "http", "commandOrUrl": "http://ghost.invalid/rpc"}],
                            },
                        },
                        {
                            "enabled": True,
                            "source": str(workspace / "missing-shared-plugin"),
                            "manifest": {
                                "id": "shared",
                                "version": "1.0.0",
                                "commands": [{"name": "installed", "description": "Installed loses", "command": "printf installed"}],
                            },
                        },
                    ],
                )
            )
            run(core.approvePluginSource(cwd=str(workspace), scope="project"))
            config = run(
                core.resolveConfig(
                    {"provider": "openai", "runtimeHome": str(home), "permissionMode": "default", "pluginDir": [str(explicit_dir)]},
                    str(workspace),
                )
            )

            plugin_runtime = run(core.loadPluginRuntime(config))
            self.assertEqual(plugin_runtime["errors"], [])
            self.assertEqual([plugin["id"] for plugin in plugin_runtime["plugins"]], ["ghost", "shared"])
            commands = [command["name"] for command in plugin_runtime["commands"]]
            self.assertIn("ghost", commands)
            self.assertIn("explicit", commands)
            self.assertNotIn("installed", commands)
            ghost = next(plugin for plugin in plugin_runtime["plugins"] if plugin["id"] == "ghost")
            shared = next(plugin for plugin in plugin_runtime["plugins"] if plugin["id"] == "shared")
            self.assertIsNone(ghost["source"])
            self.assertEqual(shared["source"], str(explicit_dir))
            self.assertEqual([server["name"] for server in plugin_runtime["mcpServers"]], ["ghost-mcp"])
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_runtime_composes_plugin_tools_commands_and_mcp_servers(self):
        root, home, workspace, previous = self.temp_context()
        try:
            plugin_dir = workspace / "bootstrap-plugin"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "bootstrapper",
                        "version": "1.0.0",
                        "entry": "plugin_entry.py",
                        "commands": [{"name": "ship", "description": "Ship it", "command": "printf shipped"}],
                        "mcpServers": [
                            {"name": "plugin-mcp", "transport": "http", "commandOrUrl": "http://plugin.invalid/rpc"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "plugin_entry.py").write_text(
                textwrap.dedent(
                    """
                    from agent_core.tools import ToolDefinition


                    async def handler(input, context):
                        return {"content": "plugin tool"}


                    def default(context):
                        return {
                            "tools": [
                                ToolDefinition(
                                    "PluginTool",
                                    "Plugin tool",
                                    {"type": "object", "properties": {}},
                                    handler,
                                )
                            ],
                            "mcpServers": [
                                {"name": "entry-mcp", "transport": "http", "commandOrUrl": "http://entry.invalid/rpc"}
                            ],
                        }
                    """
                ),
                encoding="utf-8",
            )

            config = run(
                core.resolveConfig(
                    {"provider": "openai", "runtimeHome": str(home), "permissionMode": "default", "pluginDir": [str(plugin_dir)]},
                    str(workspace),
                )
            )
            default_tools = core.getDefaultToolList()
            config["tools"] = list(default_tools)
            config["allowedTools"] = list(default_tools)
            config["channels"] = ["local"]
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            captured = {}

            async def fake_mcp(args):
                captured.update(args)
                return {"createToolDefinitions": lambda: [SimpleNamespace(name="McpFromPlugin")], "close": lambda: None}

            with mock.patch.object(runtime_module, "createMcpRuntime", fake_mcp):
                boot = run(core.bootstrapRuntime({"session": session, "config": config}))
                tool_names = [getattr(tool, "name", None) or tool.get("name") for tool in boot["tools"]]
                command_names = [command["name"] for command in boot["slashCommands"]]
                server_names = [server["name"] for server in captured["servers"]]
                self.assertEqual(captured["sessionId"], session["id"])
                self.assertEqual(captured["registeredChannels"], ["local"])
                self.assertEqual(server_names, ["plugin-mcp", "entry-mcp"])
                self.assertIn("McpFromPlugin", tool_names)
                self.assertIn("PluginTool", tool_names)
                self.assertIn("PluginTool", config["tools"])
                self.assertIn("PluginTool", config["allowedTools"])
                self.assertEqual(command_names, ["ship"])
                self.assertEqual([plugin["id"] for plugin in boot["plugins"]["plugins"]], ["bootstrapper"])
                run(boot["close"]())
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_runtime_composes_daemon_mcp_workspace_plugin_and_bridge(self):
        root, home, workspace, previous = self.temp_context()
        try:
            plugin_dir = workspace / "daemon-bootstrap-plugin"
            plugin_dir.mkdir()
            (plugin_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "daemon-bootstrapper",
                        "version": "1.0.0",
                        "commands": [{"name": "remote", "description": "Remote command", "command": "printf remote"}],
                        "mcpServers": [{"name": "plugin-daemon-mcp", "transport": "http", "commandOrUrl": "http://plugin.invalid/rpc"}],
                    }
                ),
                encoding="utf-8",
            )
            config = run(
                core.resolveConfig(
                    {"provider": "openai", "runtimeHome": str(home), "permissionMode": "default", "pluginDir": [str(plugin_dir)]},
                    str(workspace),
                )
            )
            config["mcpConfig"] = [{"name": "config-mcp", "transport": "http", "commandOrUrl": "http://config.invalid/rpc"}]
            config["channels"] = ["local", "remote"]
            default_tools = core.getDefaultToolList()
            config["tools"] = list(default_tools)
            config["allowedTools"] = list(default_tools)
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            remote = run(core.createRemoteSession({"session": session, "description": "Daemon bridge", "channelServers": ["local"]}))
            session["metadata"]["remoteControlEnabled"] = True
            session["metadata"]["remoteSessionId"] = remote["id"]
            session["metadata"]["bridgeId"] = "bridge-daemon-compose"
            captured = {"subscriptions": [], "unsubscribed": []}
            close_order = []

            async def fake_mcp(args):
                captured["mcp"] = args
                return {"createToolDefinitions": lambda: [SimpleNamespace(name="McpDaemonTool")], "close": lambda: close_order.append("mcp")}

            async def fake_bridge(args):
                captured["bridge"] = args
                args["emit"](
                    "bridge.started",
                    {
                        "bridge_id": "bridge-daemon-compose",
                        "session_id": args["session"]["id"],
                        "remote_session_id": args.get("remoteSessionId"),
                        "name": args.get("name"),
                    },
                )
                return {
                    "bridge": {
                        "id": "bridge-daemon-compose",
                        "sessionId": args["session"]["id"],
                        "remoteSessionId": args.get("remoteSessionId"),
                        "status": "active",
                    },
                    "close": lambda: close_order.append("bridge"),
                }

            async def get_status():
                return {
                    "phase": "running",
                    "epoch": "epoch-compose",
                    "pid": 456,
                    "socketPath": str(workspace / "daemon.sock"),
                    "supervisorId": "sup-compose",
                    "attachedClients": 2,
                    "watchedSessions": 1,
                    "activeTasks": 3,
                    "updatedAt": "2026-01-01T00:00:00Z",
                }

            daemon = {
                "subscribe": lambda session_id, _listener: captured["subscriptions"].append(session_id) or (lambda: captured["unsubscribed"].append(session_id)),
                "getStatus": get_status,
                "close": lambda: close_order.append("daemon"),
            }

            with mock.patch.object(runtime_module, "createMcpRuntime", fake_mcp), mock.patch.object(
                runtime_module,
                "createBridgeRuntime",
                fake_bridge,
            ):
                boot = run(core.bootstrapRuntime({"session": session, "config": config, "daemonClient": daemon, "enableBridge": True, "bridgeName": "qa", "bridgeStartPolling": False}))
                self.assertIsNotNone(boot["workspace"])
                self.assertIsNone(boot["monitorRuntime"])
                self.assertIsNone(boot["taskRuntime"])
                self.assertIsNone(boot["collaborationRuntime"])
                self.assertEqual(captured["subscriptions"], [session["id"]])
                self.assertEqual(captured["mcp"]["sessionId"], session["id"])
                self.assertEqual(captured["mcp"]["registeredChannels"], ["local", "remote"])
                self.assertEqual([server["name"] for server in captured["mcp"]["servers"]], ["config-mcp", "plugin-daemon-mcp"])
                self.assertEqual(captured["bridge"]["bridgeId"], "bridge-daemon-compose")
                self.assertEqual(captured["bridge"]["remoteSessionId"], remote["id"])
                self.assertEqual(captured["bridge"]["name"], "qa")
                tool_names = [getattr(tool, "name", None) or tool.get("name") for tool in boot["tools"]]
                self.assertIn("McpDaemonTool", tool_names)
                self.assertIn("McpDaemonTool", config["tools"])
                self.assertIn("McpDaemonTool", config["allowedTools"])
                self.assertEqual([command["name"] for command in boot["slashCommands"]], ["remote"])
                state = boot["getState"]()
                self.assertEqual(state["daemon"]["activeTasks"], 3)
                self.assertTrue(state["bridge"]["attached"])
                self.assertEqual(state["bridge"]["bridgeId"], "bridge-daemon-compose")
                run(boot["close"]())

            self.assertEqual(captured["unsubscribed"], [session["id"]])
            self.assertEqual(close_order, ["daemon", "bridge", "mcp"])
        finally:
            self.restore_home(root, previous)
