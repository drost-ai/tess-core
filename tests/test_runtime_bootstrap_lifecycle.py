from __future__ import annotations

from tests.support.agent_core_case import *

import agent_core.collaboration.remote as collaboration_remote_module


class RuntimeBootstrapLifecycleTests(AgentCoreTestCase):
    def test_bootstrap_runtime_closes_owned_local_runtimes_in_reference_order(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            config["mcpConfig"] = [{"name": "fake", "transport": "stdio", "commandOrUrl": "fake"}]
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            close_order = []

            def close_for(name):
                async def close():
                    close_order.append(name)

                return close

            async def fake_mcp(_args):
                return {"createToolDefinitions": lambda: [], "close": close_for("mcp")}

            async def fake_bridge(args):
                return {
                    "bridge": {
                        "id": "bridge-close",
                        "sessionId": args["session"]["id"],
                        "remoteSessionId": args.get("remoteSessionId"),
                        "status": "active",
                    },
                    "close": close_for("bridge"),
                }

            async def fake_monitors(_args):
                return {"close": close_for("monitors"), "setForegroundBusy": lambda _busy: None, "setSession": lambda *_args: None}

            async def fake_tasks(_args):
                return {"close": close_for("tasks"), "setForegroundBusy": lambda _busy: None, "setSession": lambda *_args: None}

            async def fake_collaboration(_args):
                return {"close": close_for("collaboration"), "setForegroundBusy": lambda _busy: None, "setSession": lambda *_args: None}

            with mock.patch.object(mcp_module, "createMcpRuntime", fake_mcp), mock.patch.object(
                collaboration_remote_module,
                "createBridgeRuntime",
                fake_bridge,
            ), mock.patch.object(runtime_module, "createMonitorRuntime", fake_monitors), mock.patch.object(
                runtime_module,
                "createTaskRuntime",
                fake_tasks,
            ), mock.patch.object(runtime_module, "createCollaborationRuntime", fake_collaboration):
                boot = run(core.bootstrapRuntime({"session": session, "config": config, "enableBridge": True, "daemonEnabled": False}))
                run(boot["close"]())

            self.assertEqual(close_order, ["collaboration", "tasks", "monitors", "bridge", "mcp"])
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_runtime_closes_daemon_before_owned_bridge_and_mcp(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            config["mcpConfig"] = [{"name": "fake", "transport": "stdio", "commandOrUrl": "fake"}]
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            close_order = []

            def close_for(name):
                async def close():
                    close_order.append(name)

                return close

            async def fake_mcp(_args):
                return {"createToolDefinitions": lambda: [], "close": close_for("mcp")}

            async def fake_bridge(args):
                return {
                    "bridge": {"id": "bridge-daemon", "sessionId": args["session"]["id"], "status": "active"},
                    "close": close_for("bridge"),
                }

            async def get_status():
                return {
                    "phase": "running",
                    "epoch": "epoch-close",
                    "pid": 123,
                    "socketPath": str(workspace / "daemon.sock"),
                    "supervisorId": "sup",
                    "attachedClients": 1,
                    "watchedSessions": 1,
                    "activeTasks": 0,
                    "updatedAt": "2026-01-01T00:00:00Z",
                }

            daemon = {
                "subscribe": lambda _session_id, _listener: lambda: close_order.append("unsubscribe"),
                "getStatus": get_status,
                "close": close_for("daemon"),
            }

            with mock.patch.object(mcp_module, "createMcpRuntime", fake_mcp), mock.patch.object(
                collaboration_remote_module,
                "createBridgeRuntime",
                fake_bridge,
            ):
                boot = run(core.bootstrapRuntime({"session": session, "config": config, "daemonClient": daemon, "enableBridge": True}))
                run(boot["close"]())

            self.assertEqual(close_order, ["unsubscribe", "daemon", "bridge", "mcp"])
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_runtime_rebinds_active_session_in_reference_order(self):
        class FakeWorkspace:
            def __init__(self, order):
                self.order = order

            def setSession(self, _session):
                self.order.append("workspace")

            def refresh(self):
                self.order.append("workspace.refresh")

        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            other = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            order = []

            def runtime_for(name):
                return {"setSession": lambda *_args: order.append(name), "setForegroundBusy": lambda _busy: None}

            boot = run(
                core.bootstrapRuntime(
                    {
                        "session": session,
                        "config": config,
                        "daemonEnabled": False,
                        "workspaceController": FakeWorkspace(order),
                        "monitorRuntime": runtime_for("monitors"),
                        "taskRuntime": runtime_for("tasks"),
                        "collaborationRuntime": runtime_for("collaboration"),
                    }
                )
            )
            unsubscribe = boot["subscribeEvents"](
                lambda envelope: order.append("session.activated") if envelope["event"] == "session.activated" else None
            )
            boot["setActiveSession"](other, config)
            unsubscribe()
            run(boot["close"]())

            self.assertEqual(order, ["monitors", "tasks", "collaboration", "workspace", "workspace.refresh", "session.activated"])
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_runtime_daemon_mode_subscribes_and_rebinds_sessions(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            subscriptions = []
            unsubscriptions = []
            closed = []

            def subscribe(session_id, listener):
                subscriptions.append((session_id, listener))
                return lambda: unsubscriptions.append(session_id)

            async def get_status():
                return {"phase": "running", "epoch": "epoch-1", "pid": 123, "socketPath": str(workspace / "daemon.sock"), "supervisorId": "sup", "attachedClients": 1, "watchedSessions": 1, "activeTasks": 0, "updatedAt": "2026-01-01T00:00:00Z"}

            async def close():
                closed.append(True)

            daemon = {"subscribe": subscribe, "getStatus": get_status, "close": close}
            boot = run(core.bootstrapRuntime({"session": session, "config": config, "daemonClient": daemon}))
            self.assertIsNone(boot["monitors"])
            self.assertIsNone(boot["tasks"])
            self.assertIsNone(boot["collaboration"])
            self.assertEqual(boot["getState"]()["daemon"]["phase"], "running")
            self.assertEqual(boot["getState"]()["daemon"]["activeTasks"], 0)
            self.assertEqual(subscriptions[0][0], session["id"])

            envelopes = []
            unsubscribe_events = boot["subscribeEvents"](envelopes.append)
            subscriptions[-1][1]({"event": "task.snapshot", "data": {"session_id": session["id"], "total": 1, "active": 1, "backgrounded": 1, "awaiting_input": 0, "failed": 0, "labels": ["Build"], "tasks": [{"id": "task-daemon", "kind": "background_command", "status": "backgrounded", "title": "Build"}]}})
            self.assertEqual(envelopes[-1]["typedEvent"]["kind"], "task.snapshot")
            self.assertEqual(boot["getTaskSnapshot"]()["total"], 1)

            other = core.createEmptySession(cwd=str(workspace), provider="openai", providerProfileName=config["providerProfileName"], model="gpt-5.4", effort="medium", systemPrompt="s", permissionMode="default")
            boot["setActiveSession"](other, config)
            for _ in range(20):
                if len(subscriptions) >= 2:
                    break
                time.sleep(0.05)
            self.assertIn(session["id"], unsubscriptions)
            self.assertEqual(subscriptions[-1][0], other["id"])
            unsubscribe_events()
            run(boot["close"]())
            self.assertIn(other["id"], unsubscriptions)
            self.assertEqual(closed, [True])
        finally:
            self.restore_home(root, previous)
