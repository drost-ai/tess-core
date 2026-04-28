from __future__ import annotations

from tests.support.agent_core_case import *


class RuntimeStateProjectionTests(AgentCoreTestCase):
    def test_runtime_state_matches_typed_event_projection_edges(self):
        state = core.createInitialRuntimeState()
        activated = core.createRuntimeEvent(
            {
                "event": "session.activated",
                "createdAt": "t-1",
                "data": {
                    "session_id": "s-typed",
                    "cwd": "/tmp/typed",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "permission_mode": "acceptEdits",
                    "workspace_name": "typed workspace",
                    "remote_session_id": "remote-typed",
                    "bridge_id": "bridge-typed",
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(activated["sessionId"], "s-typed")
        self.assertEqual(
            activated["payload"],
            {
                "cwd": "/tmp/typed",
                "provider": "openai",
                "model": "gpt-5.4",
                "permissionMode": "acceptEdits",
                "workspaceName": "typed workspace",
                "gitRoot": None,
                "gitBranch": None,
                "remoteSessionId": "remote-typed",
                "bridgeId": "bridge-typed",
                "updatedAt": None,
            },
        )
        state = core.reduceRuntimeState(state, activated)
        self.assertEqual(state["session"]["activeSessionId"], "s-typed")
        self.assertEqual(state["session"]["summary"]["id"], "s-typed")
        self.assertEqual(state["bridge"]["bridgeId"], "bridge-typed")
        empty_data_precedence = core.createRuntimeEvent({"event": "message.user", "data": {}, "payload": {"text": "from payload"}})
        self.assertEqual(empty_data_precedence["payload"], {"text": ""})
        normalized_activation = core.createRuntimeEvent(
            {
                "event": "session.activated",
                "data": {"session_id": "s-normalized", "cwd": 123, "provider": "unknown", "model": None, "permission_mode": "bad", "workspace_name": "", "bridge_id": ""},
            }
        )
        self.assertEqual(normalized_activation["payload"]["cwd"], "")
        self.assertEqual(normalized_activation["payload"]["provider"], "openai")
        self.assertEqual(normalized_activation["payload"]["model"], "")
        self.assertEqual(normalized_activation["payload"]["permissionMode"], "default")
        self.assertIsNone(normalized_activation["payload"]["workspaceName"])
        self.assertIsNone(normalized_activation["payload"]["bridgeId"])

        user_message = core.createRuntimeEvent({"event": "message.user", "data": {"text": "hello", "session_id": "s-typed", "ignored_extra": "drop me"}})
        self.assertEqual(user_message["payload"], {"text": "hello"})
        self.assertEqual(core.createRuntimeEvent({"event": "message.user", "data": {"text": 123}})["payload"], {"text": ""})
        assistant_partial = core.createRuntimeEvent(
            {
                "event": "message.assistant.partial",
                "data": {"text": "hello", "text_delta": " world", "model": "ignored", "ignored_extra": "drop me"},
            }
        )
        self.assertEqual(assistant_partial["payload"], {"text": "hello", "textDelta": " world"})
        transcript_repaired = core.createRuntimeEvent(
            {
                "event": "transcript.repaired",
                "data": {
                    "truncated_from": 4,
                    "removed_count": 2,
                    "missing_tool_use_ids": ["tool-1", 2],
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(
            transcript_repaired["payload"],
            {"truncatedFrom": 4, "removedCount": 2, "missingToolUseIds": ["tool-1", "2"]},
        )

        turn_started = core.createRuntimeEvent(
            {
                "event": "turn.started",
                "createdAt": "t-0",
                "data": {"session_id": "s-typed", "prompt": "go", "model": "gpt-5.4", "agent_name": "main", "ignored_extra": "drop me"},
            }
        )
        self.assertEqual(turn_started["payload"], {"prompt": "go", "model": "gpt-5.4", "agentName": "main"})
        blank_turn_fields = core.createRuntimeEvent(
            {"event": "turn.completed", "data": {"stop_reason": "", "message": "", "model": ""}}
        )
        self.assertEqual(blank_turn_fields["payload"], {"stopReason": None, "message": None, "model": None})
        self.assertEqual(core.createRuntimeEvent({"event": "turn.cancelling", "data": {"session_id": "s-typed"}})["payload"], {})
        self.assertEqual(core.createRuntimeEvent({"event": "status.updated", "data": {"message": "ready", "detail": ""}})["payload"], {"message": "ready", "detail": None})
        self.assertEqual(core.createRuntimeEvent({"event": "status.cleared", "data": {"session_id": "s-typed"}})["payload"], {})

        provider_failed = core.createRuntimeEvent(
            {
                "event": "provider.failed",
                "createdAt": "t-0p",
                "data": {
                    "provider": "openai",
                    "provider_label": "OpenAI",
                    "category": "timeout",
                    "phase": "stream_read",
                    "message": "timed out",
                    "retryable": True,
                    "attempt": 2,
                    "status": 504,
                    "code": "gateway_timeout",
                    "error_type": "TimeoutError",
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(
            provider_failed["payload"],
            {
                "provider": "openai",
                "providerLabel": "OpenAI",
                "category": "timeout",
                "phase": "stream_read",
                "message": "timed out",
                "retryable": True,
                "attempt": 2,
                "status": 504,
                "code": "gateway_timeout",
                "errorType": "TimeoutError",
            },
        )

        compaction_completed = core.createRuntimeEvent(
            {
                "event": "compaction.completed",
                "createdAt": "t-0c",
                "data": {
                    "generation": 3,
                    "trigger": "threshold",
                    "compacted_message_count": 4,
                    "working_context_token_estimate": 1200,
                    "verification_verdict": "accept",
                    "checkpoint_path": "/tmp/checkpoint.json",
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(compaction_completed["payload"]["generation"], 3)
        self.assertEqual(compaction_completed["payload"]["compactedMessageCount"], 4)
        self.assertEqual(compaction_completed["payload"]["verificationVerdict"], "accept")
        self.assertNotIn("ignoredExtra", compaction_completed["payload"])

        context_selected = core.createRuntimeEvent(
            {
                "event": "context_relief.selected",
                "createdAt": "t-0r",
                "data": {
                    "mode": "microcompact",
                    "pressure_level": "high",
                    "planned_modes": ["microcompact", "bad_mode"],
                    "reactive_attempt": 1,
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(context_selected["payload"]["mode"], "microcompact")
        self.assertEqual(context_selected["payload"]["plannedModes"], ["microcompact", "none"])
        context_applied = core.createRuntimeEvent(
            {
                "event": "context_relief.applied",
                "createdAt": "t-0ra",
                "data": {
                    "mode": "api_microcompact",
                    "modes": [],
                    "tokens_saved_estimate": 42,
                    "dropped_message_count": 3,
                    "compacted_message_count": 2,
                    "detail": "trimmed",
                    "pressure_level": "not-a-pressure",
                    "reactive_attempt": 0,
                    "reinjection_summary": "summary",
                    "reinjection_section_count": math.inf,
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(context_applied["payload"]["modes"], [])
        self.assertIsNone(context_applied["payload"]["reinjectionSectionCount"])
        state = core.reduceRuntimeState(state, context_selected)
        state = core.reduceRuntimeState(state, context_applied)
        self.assertEqual(state["compaction"]["selectedMode"], "api_microcompact")
        self.assertEqual(state["compaction"]["appliedModes"], [])
        self.assertEqual(state["compaction"]["pressureLevel"], "high")
        self.assertEqual(state["compaction"]["reactiveAttempt"], 0)
        self.assertEqual(state["compaction"]["lastReinjectionSummary"], "summary")

        task_snapshot = core.createRuntimeEvent(
            {
                "event": "task.snapshot",
                "createdAt": "t-0t",
                "data": {
                    "total": 1,
                    "active": 1,
                    "backgrounded": 1,
                    "awaiting_input": 0,
                    "failed": 0,
                    "labels": ["Build"],
                    "tasks": [
                        {
                            "id": "task-typed",
                            "kind": "background_command",
                            "status": "backgrounded",
                            "title": "Build",
                            "session_id": "s-typed",
                            "foreground_mode": "background",
                            "retrieve_only": False,
                            "ignored_extra": "drop me",
                        }
                    ],
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(task_snapshot["payload"]["awaitingInput"], 0)
        self.assertEqual(task_snapshot["payload"]["tasks"][0]["sessionId"], "s-typed")
        self.assertNotIn("ignoredExtra", task_snapshot["payload"])
        self.assertNotIn("ignoredExtra", task_snapshot["payload"]["tasks"][0])
        task_completed = core.createRuntimeEvent({"event": "task.completed", "data": {"task_id": "task-typed", "status": "completed", "summary": "done"}})
        self.assertNotIn("status", task_completed["payload"])

        daemon_snapshot = core.createRuntimeEvent(
            {
                "event": "daemon.snapshot",
                "data": {
                    "phase": "running",
                    "pid": 123,
                    "socket_path": "/tmp/daemon.sock",
                    "active_tasks": 2,
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(daemon_snapshot["payload"]["socketPath"], "/tmp/daemon.sock")
        self.assertNotIn("ignoredExtra", daemon_snapshot["payload"])

        budget = core.createRuntimeEvent(
            {
                "event": "budget.exceeded",
                "data": {"estimated_cost_usd": 1.25, "max_budget_usd": 1.0, "model": "gpt-5.4", "ignored_extra": "drop me"},
            }
        )
        self.assertEqual(budget["payload"], {"estimatedCostUsd": 1.25, "maxBudgetUsd": 1.0, "model": "gpt-5.4"})
        non_finite_budget = core.createRuntimeEvent(
            {
                "event": "budget.exceeded",
                "data": {"estimated_cost_usd": math.inf, "max_budget_usd": math.nan},
            }
        )
        self.assertEqual(non_finite_budget["payload"], {"estimatedCostUsd": None, "maxBudgetUsd": None, "model": None})

        self.assertNotIn("status", state)
        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "status.updated", "createdAt": "t0", "data": {"message": "Working", "detail": "runtime"}}))
        self.assertNotIn("status", state)
        self.assertEqual(state["turn"]["liveStatus"]["message"], "Working")
        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "status.cleared", "createdAt": "t0b", "data": {}}))
        self.assertNotIn("status", state)
        self.assertIsNone(state["turn"]["liveStatus"])
        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "turn.failed", "createdAt": "t0c", "data": {"message": "custom failure"}}))
        self.assertEqual(state["turn"]["lastOutcome"]["message"], "Turn failed. Review the error above.")

        store = core.createRuntimeStateStore(core.createInitialRuntimeState())
        subscriber_calls = []
        unsubscribe = store["subscribe"](lambda: subscriber_calls.append(store["getState"]()["turn"]["status"]))
        store["dispatch"](core.createRuntimeEvent({"event": "turn.started", "createdAt": "t0d", "data": {"prompt": "go"}}))
        unsubscribe()
        self.assertEqual(subscriber_calls, ["running"])

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "task.registered", "createdAt": "t1", "data": {"task_id": "health-1", "kind": "health_check", "status": "running", "title": "API health"}}))
        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "health_check.unhealthy", "createdAt": "t2", "data": {"task_id": "health-1", "title": "API health", "error": "500"}}))
        self.assertEqual(state["tasks"]["tasks"][0]["status"], "degraded")
        self.assertEqual(state["tasks"]["failedCount"], 1)
        self.assertIn("500", state["tasks"]["tasks"][0]["summary"])

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "health_check.recovered", "createdAt": "t3", "data": {"task_id": "health-1", "title": "API health"}}))
        self.assertEqual(state["tasks"]["tasks"][0]["status"], "running")
        self.assertEqual(state["tasks"]["failedCount"], 0)

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "collaboration.assistant_update", "createdAt": "t4", "data": {"source_kind": "monitor", "source_id": "health-1", "status": "running", "label": "API health", "text": "API recovered."}}))
        self.assertEqual(state["collaboration"]["recentAssistantUpdates"][0]["sourceKind"], "monitor")
        self.assertEqual(state["collaboration"]["recentAssistantUpdates"][0]["text"], "API recovered.")

        monitor_status = core.createRuntimeEvent(
            {
                "event": "monitor.status_changed",
                "createdAt": "t4b",
                "data": {
                    "monitor_id": "monitor-1",
                    "task_id": "task-1",
                    "status": "active",
                    "task_status": "running",
                    "exit_code": 0,
                    "session_id": "s-typed",
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(
            monitor_status["payload"],
            {"monitorId": "monitor-1", "taskId": "task-1", "status": "active", "taskStatus": "running", "exitCode": 0},
        )

        collaboration_snapshot = core.createRuntimeEvent(
            {
                "event": "collaboration.snapshot",
                "createdAt": "t4c",
                "data": {
                    "active_count": 1,
                    "awaiting_input_count": 0,
                    "background_count": 1,
                    "remote_status": "active",
                    "remote_title": "Remote QA",
                    "bridge_status": "active",
                    "pending_bridge_messages": 2,
                    "labels": ["Remote QA"],
                    "workers": [
                        {
                            "id": "remote-1",
                            "kind": "remote",
                            "status": "active",
                            "label": "Remote QA",
                            "remote_session_id": "remote-1",
                            "bridge_id": "bridge-1",
                            "ignored_extra": "drop me",
                        }
                    ],
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(collaboration_snapshot["payload"]["activeCount"], 1)
        self.assertEqual(collaboration_snapshot["payload"]["pendingBridgeMessages"], 2)
        self.assertNotIn("ignoredExtra", collaboration_snapshot["payload"])
        self.assertEqual(collaboration_snapshot["payload"]["workers"][0]["remoteSessionId"], "remote-1")
        self.assertNotIn("ignoredExtra", collaboration_snapshot["payload"]["workers"][0])

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "bridge.message", "createdAt": "t5", "data": {"bridge_id": "bridge-1", "remote_session_id": "remote-1", "text": "continue"}}))
        self.assertTrue(state["bridge"]["attached"])
        self.assertEqual(state["bridge"]["lastEventName"], "bridge.message")
        self.assertEqual(state["collaboration"]["pendingBridgeMessages"], 1)

        remote_status = core.createRuntimeEvent(
            {
                "event": "remote.status_changed",
                "createdAt": "t5b",
                "data": {
                    "remote_session_id": "remote-1",
                    "session_id": "s-typed",
                    "status": "awaiting_input",
                    "title": "Remote QA",
                    "bridge_id": "bridge-1",
                    "bridge_status": "active",
                    "unread_bridge_messages": 4,
                    "summary": "Remote needs input.",
                    "change_kind": "changed",
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(remote_status["kind"], "remote.status_changed")
        self.assertEqual(remote_status["payload"]["unreadBridgeMessages"], 4)
        self.assertNotIn("ignoredExtra", remote_status["payload"])
        state = core.reduceRuntimeState(state, remote_status)
        self.assertEqual(state["collaboration"]["remoteStatus"], "awaiting_input")
        self.assertEqual(state["collaboration"]["remoteTitle"], "Remote QA")
        self.assertEqual(state["collaboration"]["bridgeStatus"], "active")
        self.assertEqual(state["collaboration"]["pendingBridgeMessages"], 4)

        bridge_status = core.createRuntimeEvent(
            {
                "event": "bridge.status_changed",
                "createdAt": "t5c",
                "data": {
                    "bridge_id": "bridge-1",
                    "remote_session_id": "remote-1",
                    "status": "active",
                    "pending_messages": 3,
                    "last_event_name": "bridge.message.queued",
                    "summary": "Remote queued input.",
                    "change_kind": "changed",
                    "ignored_extra": "drop me",
                },
            }
        )
        self.assertEqual(bridge_status["kind"], "bridge.status_changed")
        self.assertEqual(bridge_status["payload"]["pendingMessages"], 3)
        self.assertNotIn("ignoredExtra", bridge_status["payload"])
        state = core.reduceRuntimeState(state, bridge_status)
        self.assertTrue(state["bridge"]["attached"])
        self.assertEqual(state["bridge"]["lastEventName"], "bridge.message.queued")
        self.assertEqual(state["collaboration"]["bridgeStatus"], "active")
        self.assertEqual(state["collaboration"]["pendingBridgeMessages"], 3)

        state = core.reduceRuntimeState(state, core.createRuntimeEvent({"event": "bridge.closed", "createdAt": "t6", "data": {"bridge_id": "bridge-1", "remote_session_id": "remote-1"}}))
        self.assertFalse(state["bridge"]["attached"])
        self.assertEqual(state["bridge"]["lastEventName"], "bridge.closed")
        self.assertEqual(state["collaboration"]["bridgeStatus"], "closed")

        bridge_message = core.createRuntimeEvent(
            {
                "event": "bridge.message",
                "createdAt": "t7",
                "data": {
                    "bridge_id": "bridge-1",
                    "remote_session_id": "remote-1",
                    "message_id": "msg-1",
                    "session_id": "s1",
                    "source": "remote",
                    "text": "hello",
                },
            }
        )
        self.assertEqual(bridge_message["kind"], "bridge.message")
        self.assertEqual(bridge_message["payload"], {"bridgeId": "bridge-1", "remoteSessionId": "remote-1", "text": "hello", "source": "remote"})
        malformed_bridge_message = core.createRuntimeEvent(
            {
                "event": "bridge.message",
                "data": {"bridge_id": 1, "remote_session_id": 2, "text": 3, "source": 4},
            }
        )
        self.assertEqual(
            malformed_bridge_message["payload"],
            {"bridgeId": None, "remoteSessionId": None, "text": None, "source": None},
        )
        malformed_generic = core.createRuntimeEvent(
            {"event": "mcp.connection", "data": {"channel_name": 123, "ignored_extra": "drop me"}}
        )
        self.assertEqual(malformed_generic["payload"], {"eventName": "mcp.connection", "channelName": None})
        malformed_bridge_generic = core.createRuntimeEvent(
            {"event": "bridge.closed", "data": {"bridge_id": 123, "remote_session_id": False}}
        )
        self.assertEqual(
            malformed_bridge_generic["payload"],
            {"eventName": "bridge.closed", "bridgeId": None, "remoteSessionId": None},
        )
        malformed_remote_generic = core.createRuntimeEvent(
            {"event": "remote.session_created", "data": {"remote_session_id": 123}}
        )
        self.assertEqual(
            malformed_remote_generic["payload"],
            {"eventName": "remote.session_created", "remoteSessionId": None},
        )

        state = core.reduceRuntimeState(
            state,
            core.createRuntimeEvent(
                {
                    "event": "subagent.status_changed",
                    "createdAt": "t8",
                    "data": {"agent_id": "agent-1", "status": "running", "label": "Delegate"},
                }
            ),
        )
        state = core.reduceRuntimeState(
            state,
            core.createRuntimeEvent(
                {
                    "event": "monitor.status_changed",
                    "createdAt": "t9",
                    "data": {"monitor_id": "monitor-1", "status": "active"},
                }
            ),
        )
        self.assertNotIn("recentEvents", state["collaboration"])
        self.assertNotIn("lastStatus", state["collaboration"])
