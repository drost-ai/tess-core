from __future__ import annotations

from tests.support.agent_core_case import *


class CollaborationRuntimeTests(AgentCoreTestCase):
    def test_collaboration_runtime_projects_workers_and_bridge_state(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child = {
                "agentId": "agent-collab",
                "sessionId": "child-session",
                "parentSessionId": session["id"],
                "name": "Delegate",
                "provider": "openai",
                "model": "gpt-5.4",
                "mode": "bypassPermissions",
                "runInBackground": True,
                "status": "backgrounded",
                "startedAt": "2026-01-01T00:00:00Z",
                "taskId": "task-subagent",
                "owner": "daemon",
                "attachableSessionId": "child-session",
                "lastUpdatedAt": "2026-01-01T00:00:01Z",
                "lastSummary": "Running in the background.",
                "stoppable": True,
            }
            run(core.saveChildAgent(child))
            session["metadata"]["childAgentIds"] = [child["agentId"]]
            monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-monitor", "goal": "Watch tests"})
            monitor["lastAssistantUpdateSummary"] = "Tests are still running."
            run(core.saveMonitor(monitor))
            run(
                core.writeTaskMeta(
                    "task-monitor",
                    {
                        "type": "background_command",
                        "taskId": "task-monitor",
                        "id": "task-monitor",
                        "sessionId": session["id"],
                        "command": "pytest",
                        "cwd": str(workspace),
                        "startedAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:00:01Z",
                        "status": "running",
                        "outputFile": core.taskOutputPath("task-monitor"),
                    },
                )
            )
            remote = run(core.createRemoteSession({"session": session, "description": "Remote QA", "channelServers": ["local"]}))
            session["metadata"]["remoteSessionId"] = remote["id"]
            bridge_events = []
            bridge_runtime = run(core.createBridgeRuntime({"session": session, "remoteSessionId": remote["id"], "name": "qa", "startPolling": False, "emit": lambda event, data: bridge_events.append((event, data))}))
            session["metadata"]["bridgeId"] = bridge_runtime["bridge"]["id"]
            message = run(core.sendBridgeMessage({"bridgeId": bridge_runtime["bridge"]["id"], "text": "please review", "source": "remote"}))
            self.assertEqual(message["source"], "remote")
            bridge = run(core.loadBridge(bridge_runtime["bridge"]["id"]))
            self.assertEqual(bridge["pendingMessages"], 1)
            self.assertEqual(bridge["lastEventName"], "bridge.message.queued")
            remote_after_message = run(core.loadRemoteSession(remote["id"]))
            self.assertEqual(remote_after_message["status"], "awaiting_input")
            self.assertEqual(remote_after_message["unreadBridgeMessages"], 1)
            self.assertIn("bridge.message.queued", run(core.formatBridgeTail(bridge["id"])))

            events = []
            runtime = run(core.createCollaborationRuntime({"session": session, "config": config, "emit": lambda event, data: events.append((event, data))}))
            snapshot = runtime["getSnapshot"]()
            self.assertEqual(snapshot["activeCount"], 2)
            self.assertEqual(snapshot["awaitingInputCount"], 1)
            self.assertEqual(snapshot["backgroundCount"], 2)
            self.assertEqual(snapshot["pendingBridgeMessages"], 1)
            self.assertIn("Delegate", snapshot["labels"])
            workers = {worker["kind"]: worker for worker in snapshot["workers"]}
            self.assertEqual(workers["subagent"]["status"], "backgrounded")
            self.assertEqual(workers["subagent"]["taskId"], "task-subagent")
            self.assertEqual(workers["remote"]["status"], "awaiting_input")
            self.assertTrue(any(event == "collaboration.snapshot" and data["pending_bridge_messages"] == 1 for event, data in events))
            self.assertTrue(any(event == "subagent.status_changed" and data["agent_id"] == child["agentId"] for event, data in events))
            self.assertTrue(any(event == "monitor.status_changed" and data["monitor_id"] == monitor["id"] for event, data in events))
            self.assertTrue(any(event == "remote.status_changed" and data["remote_session_id"] == remote["id"] for event, data in events))
            self.assertTrue(any(event == "bridge.status_changed" and data["pending_messages"] == 1 for event, data in events))

            state = core.createInitialRuntimeState(session)
            for event, data in events:
                state = core.reduceRuntimeState(state, {"event": event, "createdAt": "2026-01-01T00:00:02Z", "data": data})
            self.assertEqual(state["collaboration"]["backgroundCount"], 2)
            self.assertEqual(state["collaboration"]["pendingBridgeMessages"], 1)
            self.assertNotIn("recentEvents", state["collaboration"])
            self.assertNotIn("lastStatus", state["collaboration"])

            boot = run(core.bootstrapRuntime({"session": session, "config": config, "daemonEnabled": False}))
            self.assertTrue(boot["collaborationRuntime"])
            self.assertTrue(any(tool.name == "Agent" for tool in boot["tools"]))
            self.assertGreaterEqual(boot["stateStore"]["getState"]()["collaboration"]["activeCount"], 2)
            run(boot["close"]())
            run(runtime["close"]())
            run(bridge_runtime["close"]())
            closed = run(core.loadBridge(bridge["id"]))
            self.assertEqual(closed["status"], "closed")
        finally:
            self.restore_home(root, previous)

    def test_collaboration_runtime_tracks_terminal_and_awaiting_workers_incrementally(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            child_records = [
                {
                    "agentId": "agent-completed",
                    "sessionId": "child-completed",
                    "parentSessionId": session["id"],
                    "name": "Completed worker",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "mode": "bypassPermissions",
                    "runInBackground": True,
                    "status": "completed",
                    "completedAt": "2026-01-01T00:00:04Z",
                    "lastUpdatedAt": "2026-01-01T00:00:04Z",
                    "completionReason": "finished",
                    "resultSummary": "Collected final notes.",
                    "latestResultAt": "2026-01-01T00:00:04Z",
                    "stoppable": False,
                },
                {
                    "agentId": "agent-failed",
                    "sessionId": "child-failed",
                    "parentSessionId": session["id"],
                    "name": "Failed worker",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "mode": "bypassPermissions",
                    "runInBackground": True,
                    "status": "failed",
                    "completedAt": "2026-01-01T00:00:03Z",
                    "lastUpdatedAt": "2026-01-01T00:00:03Z",
                    "completionReason": "worker_failed",
                    "error": "Process exited 2",
                    "resultSummary": "Tests failed.",
                    "retryable": True,
                    "stoppable": False,
                },
                {
                    "agentId": "agent-awaiting",
                    "sessionId": "child-awaiting",
                    "parentSessionId": session["id"],
                    "name": "Awaiting plan",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "mode": "bypassPermissions",
                    "runInBackground": True,
                    "status": "awaiting_plan_approval",
                    "startedAt": "2026-01-01T00:00:02Z",
                    "lastUpdatedAt": "2026-01-01T00:00:02Z",
                    "waitingReason": "Needs plan approval.",
                    "stoppable": True,
                },
                {
                    "agentId": "agent-running",
                    "sessionId": "child-running",
                    "parentSessionId": session["id"],
                    "name": "Running worker",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "mode": "bypassPermissions",
                    "runInBackground": True,
                    "status": "running",
                    "startedAt": "2026-01-01T00:00:01Z",
                    "lastUpdatedAt": "2026-01-01T00:00:01Z",
                    "lastSummary": "Still running.",
                    "stoppable": True,
                },
            ]
            for record in child_records:
                run(core.saveChildAgent(record))
            session["metadata"]["childAgentIds"] = [record["agentId"] for record in child_records]

            events = []
            runtime = run(
                core.createCollaborationRuntime(
                    {
                        "session": session,
                        "config": config,
                        "startTicker": False,
                        "minAssistantUpdateSpacingMs": 10**15,
                        "emit": lambda event, data: events.append((event, data)),
                    }
                )
            )
            workers = {worker["id"]: worker for worker in runtime["getSnapshot"]()["workers"]}
            self.assertEqual(workers["agent-completed"]["status"], "completed")
            self.assertEqual(workers["agent-completed"]["resultSummary"], "Collected final notes.")
            self.assertEqual(workers["agent-failed"]["status"], "failed")
            self.assertEqual(workers["agent-failed"]["retryable"], True)
            self.assertEqual(workers["agent-awaiting"]["status"], "awaiting_input")
            self.assertEqual(workers["agent-awaiting"]["waitingReason"], "Needs plan approval.")
            self.assertEqual(runtime["getSnapshot"]()["activeCount"], 1)
            self.assertEqual(runtime["getSnapshot"]()["awaitingInputCount"], 1)
            self.assertTrue(
                any(event == "subagent.status_changed" and data["agent_id"] == "agent-awaiting" and data["change_kind"] == "started" for event, data in events)
            )

            running = {**child_records[-1], "status": "completed", "completedAt": "2026-01-01T00:00:05Z", "lastUpdatedAt": "2026-01-01T00:00:05Z", "completionReason": "finished", "resultSummary": "Delivered runtime notes.", "latestResultAt": "2026-01-01T00:00:05Z", "stoppable": False}
            run(core.saveChildAgent(running))
            before_count = len(events)
            run(runtime["refresh"]())
            changed = events[before_count:]
            self.assertTrue(
                any(
                    event == "subagent.status_changed"
                    and data["agent_id"] == "agent-running"
                    and data["status"] == "completed"
                    and data["change_kind"] == "changed"
                    and data["result_summary"] == "Delivered runtime notes."
                    for event, data in changed
                )
            )
            self.assertEqual(runtime["getSnapshot"]()["activeCount"], 0)
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_collaboration_set_session_refreshes_runtime_snapshot(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session_a = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            session_b = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            remote = run(core.createRemoteSession({"session": session_b, "description": "Set-session remote", "channelServers": ["local"]}))
            remote = run(core.updateRemoteSession(remote["id"], {"status": "active", "lastEventSummary": "Remote attached"}))
            session_b["metadata"]["remoteSessionId"] = remote["id"]

            events = []
            runtime = run(
                core.createCollaborationRuntime(
                    {
                        "session": session_a,
                        "config": config,
                        "startTicker": False,
                        "emit": lambda event, data: events.append((event, data)),
                    }
                )
            )
            self.assertEqual(runtime["getSnapshot"]()["workers"], [])
            runtime["setSession"](session_b, config)
            for _ in range(40):
                if runtime["getSnapshot"]().get("remoteStatus") == "active":
                    break
                time.sleep(0.05)
            snapshot = runtime["getSnapshot"]()
            self.assertEqual(snapshot["remoteStatus"], "active")
            self.assertEqual(snapshot["workers"][0]["remoteSessionId"], remote["id"])
            self.assertTrue(
                any(event == "collaboration.snapshot" and data["remote_status"] == "active" for event, data in events)
            )
            self.assertTrue(
                any(event == "remote.status_changed" and data["remote_session_id"] == remote["id"] for event, data in events)
            )
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_collaboration_runtime_authors_deterministic_assistant_update(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"].append({"role": "user", "content": "Track the delegated work."})
            child = {
                "agentId": "agent-collab-update",
                "sessionId": "child-update-session",
                "parentSessionId": session["id"],
                "name": "Delegate",
                "provider": "openai",
                "model": "gpt-5.4",
                "mode": "bypassPermissions",
                "runInBackground": True,
                "status": "backgrounded",
                "startedAt": "2026-01-01T00:00:00Z",
                "lastUpdatedAt": "2026-01-01T00:00:01Z",
            }
            run(core.saveChildAgent(child))
            session["metadata"]["childAgentIds"] = [child["agentId"]]
            events = []
            runtime = run(core.createCollaborationRuntime({"session": session, "config": config, "minAssistantUpdateSpacingMs": 0, "emit": lambda event, data: events.append((event, data))}))
            self.assertFalse(any(event == "collaboration.assistant_update" for event, _ in events))
            run(core.saveChildAgent({**child, "status": "completed", "completedAt": "2026-01-01T00:00:02Z", "lastUpdatedAt": "2026-01-01T00:00:02Z", "resultSummary": "Collected the evidence."}))
            run(runtime["refresh"]())
            update = next(data for event, data in events if event == "collaboration.assistant_update")
            self.assertEqual(update["source_kind"], "subagent")
            self.assertEqual(update["source_id"], child["agentId"])
            self.assertIn("finished its background work", update["text"])
            self.assertIn("Collected the evidence.", update["text"])
            self.assertEqual(session["messages"][-1]["role"], "assistant")
            self.assertEqual(session["messages"][-1]["content"], update["text"])
            persisted = run(core.loadSession(session["id"]))
            self.assertEqual(persisted["messages"][-1]["content"], update["text"])
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)
