from __future__ import annotations

from tests.support.tess_core_case import *
import tess_core.agent.context as agent_context_module


class ContextContinuationTests(TessCoreTestCase):
    def test_continuation_relief_modes_match_reference_policy(self):
        session = core.create_empty_session(cwd="/tmp/workspace", provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
        session["updatedAt"] = "2026-01-01T00:00:00Z"
        messages = []
        for index in range(18):
            tool_id = f"read-{index}"
            messages.extend(
                [
                    {"role": "user", "content": f"inspect file {index}"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": f"/tmp/workspace/file{index % 2}.py", "offset": 1}}]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": json.dumps({"file": {"filePath": f"/tmp/workspace/file{index % 2}.py", "content": "x" * 4000, "startLine": 1, "totalLines": 400, "numLines": 80}}),
                            }
                        ],
                    },
                ]
            )
        messages.append({"role": "user", "content": "continue from latest file inspection"})

        pressure = core.classify_context_pressure({"session": session, "messages": messages})
        self.assertGreater(pressure["categories"]["currentTurnToolPayloadTokens"], 0)
        self.assertGreater(pressure["categories"]["repeatedFileContentTokens"], 0)

        unscheduled_relief = core.apply_context_relief_modes({"session": session, "config": {"provider": "openai"}, "prompt": "continue", "messages": messages, "retrievedMemory": {"records": [], "tokenEstimate": 0}})
        self.assertEqual(unscheduled_relief["summary"]["plannedModes"], [])
        self.assertFalse(unscheduled_relief["summary"]["applied"])

        session["updatedAt"] = "2999-01-01T00:00:00Z"
        scheduled_messages = [{"role": "user", "content": "x" * 810_000}, *messages]
        relief = core.apply_context_relief_modes({"session": session, "config": {"provider": "openai"}, "prompt": "continue", "messages": scheduled_messages, "retrievedMemory": {"records": [], "tokenEstimate": 0}})
        summary = relief["summary"]
        self.assertIn("api_microcompact", summary["plannedModes"])
        self.assertIn("api_microcompact", summary["appliedModes"])
        self.assertGreater(summary["tokensSavedEstimate"], 0)
        prompt = core.render_context_relief_prompt(summary)
        self.assertIsNone(prompt)

        reactive_relief = core.apply_context_relief_modes({"session": session, "config": {"provider": "openai"}, "prompt": "continue", "messages": messages, "retrievedMemory": {"records": [], "tokenEstimate": 0}, "reactiveAttempt": 1})
        self.assertIn("api_microcompact", reactive_relief["summary"]["plannedModes"])
        self.assertEqual(reactive_relief["summary"]["pressureLevel"], "high")

    def test_production_relief_retires_legacy_session_memory_compaction(self):
        session = core.create_empty_session(cwd="/tmp/workspace", provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
        messages = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index} about /tmp/workspace/app.py and command pytest"} for index in range(44)]
        retrieved = {
            "records": [
                {"kind": "workflow", "source": "compaction", "text": "Use pytest for focused parity validation."},
                {"kind": "repo_fact", "source": "compaction", "text": "Continuation lives in tess_core/continuation.py."},
                {"kind": "constraint", "source": "compaction", "text": "Preserve active plan and todo state."},
                {"kind": "environment", "source": "manual", "text": "Workspace uses TESS_CORE_HOME for test isolation."},
            ],
            "tokenEstimate": 650,
        }

        relief = core.apply_context_relief_modes({"session": session, "config": {"provider": "openai"}, "prompt": "recover", "messages": messages, "retrievedMemory": retrieved, "reactiveAttempt": 2})
        summary = relief["summary"]
        self.assertNotIn("session_memory_compaction", summary["plannedModes"])
        self.assertNotIn("session_memory_compaction", summary["appliedModes"])
        self.assertIn("partial_compaction", summary["appliedModes"])
        self.assertFalse(any("durable memory:" in line for line in summary["reinjectionLines"]))
        self.assertLess(len(relief["messages"]), len(messages))

        direct_legacy = core.apply_context_relief_mode(
            {
                "mode": "session_memory_compaction",
                "session": session,
                "messages": messages,
                "retrievedMemory": retrieved,
                "reactiveAttempt": 2,
            }
        )
        self.assertFalse(direct_legacy["applied"])
        self.assertEqual(direct_legacy["step"]["mode"], "none")

        historical = core.create_runtime_event(
            {
                "event": "context_relief.applied",
                "timestamp": "2026-01-01T00:00:00Z",
                "data": {
                    "session_id": session["id"],
                    "mode": "session_memory_compaction",
                    "modes": ["session_memory_compaction"],
                },
            }
        )
        self.assertEqual(historical["payload"]["mode"], "session_memory_compaction")
        self.assertEqual(historical["payload"]["modes"], ["session_memory_compaction"])

        partial = core.apply_partial_compaction(messages, 1)
        self.assertTrue(partial["applied"])
        self.assertEqual(partial["messages"][0]["role"], "user")
        self.assertIn("Partial continuation summary", partial["messages"][0]["content"])

    def test_continuation_malformed_tool_results_match_reference_edges(self):
        self.assertEqual(core.parse_structured_tool_result('{"command":"pytest","exitCode":NaN,"stderr":"bad"}'), {})
        plain = core.summarize_tool_result_for_relief('{"command":"pytest","exitCode":NaN,"stderr":"bad"}', 80)
        self.assertEqual(plain, '{"command":"pytest","exitCode":NaN,"stderr":"bad"}')

        tool_uses = {}
        compaction_state = {"seenFilePaths": set(), "seenCommands": set()}
        message = {
            "role": "user",
            "content": [
                None,
                "malformed",
                {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "/tmp/app.py", "content": "x" * 2000}},
                {
                    "type": "tool_result",
                    "tool_use_id": "read-1",
                    "content": {
                        "file": {
                            "filePath": "/tmp/app.py",
                            "content": "line\n" * 400,
                            "startLine": 1,
                            "totalLines": 400,
                        }
                    },
                },
                {"type": "tool_result", "tool_use_id": "missing", "content": ["array", "payload"], "is_error": True},
            ],
        }
        compacted = core.compact_message_for_continuation(
            message,
            tool_uses,
            compaction_state,
            {"messageTextCharLimit": 40, "toolInputCharLimit": 60, "toolResultCharLimit": 80, "lowValueToolNames": set()},
        )
        self.assertIsNotNone(compacted)
        file_summary = compacted["content"][1]["content"]
        array_summary = compacted["content"][2]["content"]
        self.assertIn('"filePath": "/tmp/app.py"', file_summary)
        self.assertEqual(array_summary, '["array","payload"]')

        pressure = core.classify_context_pressure({"session": {"metadata": {}}, "messages": [message]})
        self.assertGreater(pressure["categories"]["currentTurnToolPayloadTokens"], 0)
        self.assertGreater(pressure["categories"]["repeatedFileContentTokens"], 0)

    def test_context_relief_daemon_and_bridge_replay_preserve_fields(self):
        root, home, workspace, previous = self.temp_context()
        boot = None
        bridge = None
        try:
            config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "default"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="default")
            relief_data = {
                "session_id": session["id"],
                "mode": "api_microcompact",
                "modes": [],
                "tokens_saved_estimate": 321,
                "dropped_message_count": 4,
                "compacted_message_count": 2,
                "detail": "daemon replay",
                "pressure_level": "high",
                "reactive_attempt": 1,
                "reinjection_summary": "carry forward",
                "reinjection_section_count": 2,
            }
            replay_record = {"cursor": 12, "event": "context_relief.applied", "data": relief_data}

            def subscribe(_session_id, listener):
                listener(replay_record)
                return lambda: None

            daemon = {"subscribe": subscribe, "getStatus": lambda: None, "close": lambda: None}
            boot = run(core.bootstrap_runtime({"session": session, "config": config, "daemon": daemon}))
            state = boot["getState"]()
            self.assertEqual(state["compaction"]["selectedMode"], "api_microcompact")
            self.assertEqual(state["compaction"]["appliedModes"], [])
            self.assertEqual(state["compaction"]["pressureLevel"], "high")
            self.assertEqual(state["compaction"]["lastDetail"], "daemon replay")
            self.assertEqual(state["compaction"]["lastReinjectionSummary"], "carry forward")

            bridge = run(core.create_bridge_runtime({"session": session, "emit": lambda *_: None, "startPolling": False}))
            run(bridge["recordEvent"]("context_relief.applied", relief_data))
            bridge_events = run(core.load_bridge_events(bridge["bridge"]["id"]))
            self.assertEqual(bridge_events[-1]["data"]["modes"], [])
            typed = core.create_runtime_event({"event": bridge_events[-1]["event"], "data": bridge_events[-1]["data"]})
            self.assertEqual(typed["payload"]["tokensSavedEstimate"], 321)
            self.assertEqual(typed["payload"]["reinjectionSectionCount"], 2)
        finally:
            if bridge:
                run(bridge["close"]())
            if boot:
                run(boot["close"]())
            self.restore_home(root, previous)

    def test_agent_prompt_retries_context_window_failure_with_reactive_relief(self):
        import tess_core.agent as agent_module

        root, home, workspace, previous = self.temp_context()
        original_measure = agent_context_module.measure_request_input_tokens
        try:
            config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"] = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"older context {index}"} for index in range(22)]
            provider_requests = []
            events = []

            async def fake_measure(_args):
                return {"inputTokens": 1_000, "source": "fake"}

            async def send(request):
                provider_requests.append(request)
                if len(provider_requests) == 1:
                    raise core.create_provider_request_error(
                        core.create_provider_failure_record(
                            {
                                "provider": "openai",
                                "providerLabel": "OpenAI",
                                "phase": "request_start",
                                "category": "client_request",
                                "retryable": False,
                                "attempt": 1,
                                "hadAssistantOutput": False,
                                "hadToolUse": False,
                                "rawMessage": "maximum context length exceeded",
                            }
                        )
                    )
                return {"model": request["model"], "content": [{"type": "text", "text": "recovered"}], "usage": {}, "stopReason": "end"}

            agent_context_module.measure_request_input_tokens = fake_measure
            result = run(core.run_agent_prompt(session=session, config=config, runtime=bind_test_runtime(session, {"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": []}), prompt="continue", providerOverride={"send": send}))

            self.assertEqual(result["text"], "recovered")
            self.assertEqual(len(provider_requests), 2)
            self.assertNotIn("<context_relief>", provider_requests[1]["system"])
            self.assertNotIn("<context_relief>", json.dumps(provider_requests[1]["messages"]))
            self.assertIn("context_reduction_summary", json.dumps(provider_requests[1]["messages"]))
            self.assertTrue(any(event == "context_relief.applied" and data["reactive_attempt"] == 1 for event, data in events))
            self.assertFalse(any(event == "provider.failed" for event, _ in events))
            self.assertEqual(session["metadata"]["runtimeRestore"]["lastContextReliefReactiveAttempt"], 1)
            self.assertEqual(session["metadata"]["runtimeRestore"]["lastContextReliefKind"], "circuit_breaker")
        finally:
            agent_context_module.measure_request_input_tokens = original_measure
            self.restore_home(root, previous)

    def test_agent_prompt_cancels_while_rebuilding_reactive_relief_request(self):
        import tess_core.agent as agent_module

        root, home, workspace, previous = self.temp_context()
        original_measure = agent_context_module.measure_request_input_tokens
        try:
            config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"] = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"older context {index}"} for index in range(22)]
            signal = SimpleNamespace(aborted=False, reason=None)
            events = []
            provider_requests = []
            measure_calls = {"count": 0}

            async def fake_measure(_args):
                measure_calls["count"] += 1
                if measure_calls["count"] == 2:
                    signal.aborted = True
                    error = RuntimeError("cancelled during reactive relief rebuild")
                    error.name = "AbortError"
                    raise error
                return {"inputTokens": 1_000, "source": "fake"}

            async def send(request):
                provider_requests.append(request)
                raise core.create_provider_request_error(
                    core.create_provider_failure_record(
                        {
                            "provider": "openai",
                            "providerLabel": "OpenAI",
                            "phase": "request_start",
                            "category": "client_request",
                            "retryable": False,
                            "attempt": 1,
                            "hadAssistantOutput": False,
                            "hadToolUse": False,
                            "rawMessage": "maximum context length exceeded",
                        }
                    )
                )

            agent_context_module.measure_request_input_tokens = fake_measure
            result = run(
                core.run_agent_prompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, {"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": [], "abortSignal": signal}),
                    prompt="continue",
                    providerOverride={"send": send},
                )
            )

            self.assertEqual(result["stopReason"], "cancelled")
            self.assertEqual(len(provider_requests), 1)
            self.assertEqual(measure_calls["count"], 2)
            self.assertEqual(session["messages"], [{"role": "user" if index % 2 == 0 else "assistant", "content": f"older context {index}"} for index in range(22)])
            self.assertTrue(any(event == "turn.cancelled" for event, _ in events))
            self.assertFalse(any(event == "provider.failed" for event, _ in events))
        finally:
            agent_context_module.measure_request_input_tokens = original_measure
            self.restore_home(root, previous)
