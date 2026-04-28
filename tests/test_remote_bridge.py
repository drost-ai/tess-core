from __future__ import annotations

from tests.support.agent_core_case import *


class RemoteBridgeTests(AgentCoreTestCase):
    def test_bridge_runtime_polls_inbox_and_records_received_event(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            remote = run(core.createRemoteSession({"session": session, "description": "Remote inbox QA", "channelServers": ["local"]}))
            bridge_events = []
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda event, data: bridge_events.append((event, data))}))
            message = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "please replay", "source": "remote"}))

            delivered = run(bridge_runtime["pollInbox"]())
            self.assertEqual([item["id"] for item in delivered], [message["id"]])
            self.assertTrue(any(event == "bridge.message" and data["message_id"] == message["id"] and data["text"] == "please replay" for event, data in bridge_events))
            bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge["pendingMessages"], 0)
            self.assertEqual(bridge["messageCount"], 1)
            self.assertEqual(bridge["lastEventName"], "bridge.message.received")
            event_records = run(core.loadBridgeEvents(bridge["id"]))
            self.assertEqual(
                [item["event"] for item in event_records],
                ["bridge.started", "bridge.message.queued", "bridge.message.received"],
            )
            self.assertEqual(event_records[-1]["data"], {"message_id": message["id"], "source": "remote", "text": "please replay"})
            remote_after_receive = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(remote_after_receive["lastEventName"], "bridge.message.received")
            self.assertEqual(remote_after_receive["status"], "awaiting_input")
            self.assertEqual(remote_after_receive["bridgeStatus"], "active")
            self.assertIn("Bridge message: please replay", remote_after_receive["lastEventSummary"])
            self.assertIn("bridge.message.received", run(core.formatBridgeTail(bridge["id"])))

            run(bridge_runtime["recordEvent"]("session.completed", {"summary": "finished"}))
            completed_remote = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(completed_remote["status"], "completed")
            self.assertEqual(completed_remote["bridgeStatus"], "active")
            self.assertEqual(completed_remote["lastEventSummary"], "session.completed: finished")

            self.assertEqual(run(bridge_runtime["pollInbox"]()), [])
            bridge_after_repoll = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge_after_repoll["messageCount"], 1)
            run(bridge_runtime["close"]())
            closed_remote = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(closed_remote["status"], "idle")
            self.assertEqual(closed_remote["bridgeStatus"], "closed")
            closed_events = run(core.loadBridgeEvents(bridge["id"]))
            self.assertEqual(
                [item["event"] for item in closed_events],
                ["bridge.started", "bridge.message.queued", "bridge.message.received", "session.completed"],
            )
        finally:
            self.restore_home(root, previous)

    def test_bridge_runtime_reconnects_and_replays_only_pending_inbox_after_restart(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            remote = run(core.createRemoteSession({"session": session, "description": "Remote restart QA", "channelServers": ["local"]}))
            initial_events = []
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda event, data: initial_events.append((event, data))}))
            first = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "already delivered", "source": "remote"}))
            run(bridge_runtime["pollInbox"]())
            second = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "deliver after restart", "source": "remote"}))
            run(bridge_runtime["close"]())

            replay_events = []
            restarted = run(core.createBridgeRuntime({"session": session, "bridgeId": bridge_runtime["bridge"]["id"], "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda event, data: replay_events.append((event, data))}))
            self.assertEqual(replay_events[0][0], "bridge.reconnected")
            delivered = run(restarted["pollInbox"]())
            self.assertEqual([message["id"] for message in delivered], [second["id"]])
            self.assertFalse(any(event == "bridge.message" and data["message_id"] == first["id"] for event, data in replay_events))
            self.assertTrue(any(event == "bridge.message" and data["message_id"] == second["id"] for event, data in replay_events))
            bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge["status"], "active")
            self.assertEqual(bridge["messageCount"], 2)
            self.assertEqual(bridge["pendingMessages"], 0)
            diagnostics = run(restarted["diagnostics"]())
            self.assertEqual(diagnostics["status"], "active")
            self.assertEqual(diagnostics["inboxCount"], 2)
            self.assertEqual(diagnostics["messageCount"], 2)
            self.assertTrue(any(event["event"] == "bridge.reconnected" for event in diagnostics["events"]))
            with self.assertRaisesRegex(RuntimeError, "Bridge not found for reconnect"):
                run(core.createBridgeRuntime({"session": session, "bridgeId": "missing-bridge", "startPolling": False, "emit": lambda *_: None}))
            run(restarted["close"]())
        finally:
            self.restore_home(root, previous)

    def test_bridge_runtime_records_poll_failure_diagnostics_and_retries_pending_message(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            remote = run(core.createRemoteSession({"session": session, "description": "Remote failure QA", "channelServers": ["local"]}))
            emitted = []
            fail = {"enabled": True}

            def emit(event, data):
                emitted.append((event, data))
                if event == "bridge.message" and fail["enabled"]:
                    raise RuntimeError("emit failed")

            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": emit}))
            message = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "retry me", "source": "remote"}))
            with self.assertRaisesRegex(RuntimeError, "emit failed"):
                run(bridge_runtime["pollInbox"]())
            failed_bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(failed_bridge["status"], "failed")
            self.assertEqual(failed_bridge["lastEventName"], "bridge.failed")
            self.assertEqual(failed_bridge["lastFailurePhase"], "poll")
            self.assertIn("emit failed", failed_bridge["lastFailureMessage"])
            failed_remote = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(failed_remote["status"], "failed")
            self.assertEqual(failed_remote["bridgeStatus"], "failed")
            diagnostics = run(bridge_runtime["getDiagnostics"]())
            self.assertEqual(diagnostics["status"], "failed")
            self.assertTrue(any(event["event"] == "bridge.failed" and event["data"]["phase"] == "poll" for event in diagnostics["events"]))

            fail["enabled"] = False
            delivered = run(bridge_runtime["pollInbox"]())
            self.assertEqual([item["id"] for item in delivered], [message["id"]])
            recovered_bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(recovered_bridge["status"], "active")
            self.assertEqual(recovered_bridge["pendingMessages"], 0)
            self.assertEqual(recovered_bridge["messageCount"], 1)
            run(bridge_runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_bridge_tail_and_truncation_match_reference_shape(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            remote = run(core.createRemoteSession({"session": session, "description": "Remote diagnostics QA", "channelServers": ["local"]}))
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda *_: None}))
            run(bridge_runtime["recordEvent"]("bridge.diagnostic", {"summary": "diagnostic", "count": 2}))
            tail = run(core.formatBridgeTail(bridge_runtime["bridge"]["id"]))
            self.assertIn('bridge.diagnostic  {"summary":"diagnostic","count":2}', tail)

            long_text = "x" * 120
            run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": long_text, "source": "remote"}))
            remote_after_queue = run(core.loadRemoteSession(remote["id"]))
            self.assertTrue(remote_after_queue["lastEventSummary"].endswith("\u2026"))
            self.assertEqual(len(remote_after_queue["lastEventSummary"]), 96)
            run(bridge_runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_remote_bridge_persistence_omits_undefined_optional_fields(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")

            remote = run(core.createRemoteSession({"session": session, "channelServers": []}))
            persisted_remote = run(core.loadRemoteSession(remote["id"]))
            for key in ("description", "workspaceName", "gitRoot", "gitBranch", "prNumber", "prUrl"):
                self.assertNotIn(key, persisted_remote)

            session["metadata"]["originalCwd"] = str(workspace.parent)
            session["metadata"]["workspaceName"] = "typed-workspace"
            metadata_remote = run(core.createRemoteSession({"session": session, "channelServers": []}))
            persisted_metadata_remote = run(core.loadRemoteSession(metadata_remote["id"]))
            self.assertEqual(persisted_metadata_remote["originalCwd"], str(workspace.parent))
            self.assertEqual(persisted_metadata_remote["workspaceName"], "typed-workspace")
            self.assertEqual(persisted_metadata_remote["title"], "typed-workspace")

            raw_remote = run(core.createRemoteSession({"session": {"id": "raw-remote", "cwd": str(workspace), "metadata": {}}, "channelServers": []}))
            persisted_raw_remote = run(core.loadRemoteSession(raw_remote["id"]))
            self.assertNotIn("originalCwd", persisted_raw_remote)
            empty_name_remote = run(core.createRemoteSession({"session": {"id": "empty-name", "cwd": str(workspace), "name": "", "metadata": {"workspaceName": "fallback-name"}}, "channelServers": []}))
            self.assertEqual(run(core.loadRemoteSession(empty_name_remote["id"]))["title"], "")
            empty_workspace = {
                "id": "remote-empty-workspace",
                "sessionId": session["id"],
                "title": "Empty Workspace",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "status": "created",
                "cwd": str(workspace),
                "workspaceName": "",
                "channelServers": [],
            }
            self.assertEqual(core.formatRemoteSessionCandidate(empty_workspace)[10:28], " " * 18)

            events = []
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "startPolling": False, "emit": lambda event, data: events.append((event, data))}))
            bridge_id = bridge_runtime["bridge"]["id"]
            persisted_bridge = run(core.loadBridge(bridge_id))
            self.assertNotIn("remoteSessionId", persisted_bridge)
            self.assertNotIn("name", persisted_bridge)
            started_events = run(core.loadBridgeEvents(bridge_id))
            self.assertEqual(started_events[0]["event"], "bridge.started")
            self.assertNotIn("remote_session_id", started_events[0]["data"])
            self.assertNotIn("name", started_events[0]["data"])

            message = run(core.sendBridgeMessage({"bridgeId": bridge_id, "text": "local handoff"}))
            inbox = run(core.loadBridgeInbox(bridge_id))
            self.assertEqual(inbox[0]["id"], message["id"])
            self.assertNotIn("remoteSessionId", inbox[0])
            default_source = run(core.sendBridgeMessage({"bridgeId": bridge_id, "text": "default source", "source": None}))
            self.assertEqual(default_source["source"], "bridge")
            empty_name_bridge = run(core.createBridgeRuntime({"session": session, "name": "", "startPolling": False, "emit": lambda *_: None}))
            bridge_rows = run(core.listActiveBridgeRows())
            empty_name_row = next(row for row in bridge_rows if row.startswith(empty_name_bridge["bridge"]["id"][:8]))
            self.assertEqual(empty_name_row[10:28], " " * 18)
            run(empty_name_bridge["close"]())
            run(bridge_runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_remote_teleport_interactive_selection_matches_reference_flow(self):
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            alpha = run(core.createRemoteSession({"session": session, "description": "Alpha remote", "channelServers": ["local"]}))
            beta = run(core.createRemoteSession({"session": session, "description": "Beta remote", "channelServers": ["local"]}))
            alpha["updatedAt"] = "2026-01-01T00:00:01Z"
            beta["updatedAt"] = "2026-01-01T00:00:02Z"
            run(core.saveRemoteSession(alpha))
            run(core.saveRemoteSession(beta))

            stdout = TtyBuffer()
            with mock.patch.object(sys, "stdin", TtyBuffer()), mock.patch.object(sys, "stdout", stdout), mock.patch(
                "builtins.input",
                return_value="2",
            ):
                selected = run(core.resolveTeleportSession({"teleport": True, "options": {}}))
            self.assertEqual(selected["remote"]["id"], alpha["id"])
            self.assertEqual(selected["sessionId"], alpha["sessionId"])
            self.assertIn("Select a remote session to resume:", stdout.getvalue())
            self.assertIn(core.formatRemoteSessionCandidate(beta), stdout.getvalue())

            with mock.patch.object(sys, "stdin", TtyBuffer()), mock.patch.object(sys, "stdout", TtyBuffer()), mock.patch(
                "builtins.input",
                return_value="Beta",
            ):
                selected_by_query = run(core.resolveTeleportSession({"teleport": "remote", "options": {}}))
            self.assertEqual(selected_by_query["remote"]["id"], beta["id"])

            with mock.patch.object(sys, "stdin", TtyBuffer()), mock.patch.object(sys, "stdout", TtyBuffer()), mock.patch(
                "builtins.input",
                return_value="1.0",
            ):
                selected_by_float_index = run(core.resolveTeleportSession({"teleport": True, "options": {}}))
            self.assertEqual(selected_by_float_index["remote"]["id"], beta["id"])

            with self.assertRaisesRegex(RuntimeError, 'Multiple remote sessions matched "remote"'):
                run(core.resolveTeleportSession({"teleport": "remote", "options": {"print": True}}))
        finally:
            self.restore_home(root, previous)
