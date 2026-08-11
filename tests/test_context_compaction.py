from __future__ import annotations

from tests.support.agent_core_case import *
import agent_core.agent.context as agent_context_module
import agent_core.agent.checkpoints as agent_checkpoints_module


class ContextCompactionTests(AgentCoreTestCase):
    def test_codex_spark_compaction_uses_codex_transport(self):
        root, home, workspace, previous = self.temp_context()
        try:
            codex_auth_path = home / "codex-auth.json"
            codex_auth_path.write_text(json.dumps({"tokens": {"access_token": "codex-access-token"}}))
            captured = {}

            async def sender(args):
                captured.update(args)
                return {"content": [{"type": "text", "text": "Spark compaction output"}], "usage": {"input_tokens": 12, "output_tokens": 4}, "model": args["request"]["model"], "stopReason": "stop"}

            def openai_factory(_options):
                return SimpleNamespace()
            abort_signal = object()
            client = testing.create_default_compaction_model_client({"openAiRequestSender": sender})
            result = run(
                client.complete(
                    {
                        "config": {
                            "providerProfileName": "zai-glm-5-1",
                            "openaiApiMode": "chat_completions",
                            "openaiBaseUrl": "https://api.z.ai/api/paas/v4",
                            "openaiToken": "compatible-backend-token",
                            "customHeaders": {"x-test-provider": "zai"},
                            "openaiCodexAuthPath": str(codex_auth_path),
                        },
                        "model": {"provider": "openai", "model": "gpt-5.3-codex-spark", "effort": "high", "selectionSource": "fixed", "purpose": "synthesis"},
                        "system": "You are the compaction synthesizer.",
                        "prompt": "Summarize this transcript.",
                        "runtime": {
                            "abortSignal": abort_signal,
                            "emitEvent": lambda *_: None,
                            "includePartialMessages": True,
                            "openaiClientFactory": openai_factory,
                        },
                    }
                )
            )
            self.assertEqual(captured["mode"], "codex")
            self.assertEqual(captured["baseUrl"], testing.DEFAULT_CODEX_BASE_URL)
            self.assertEqual(captured["token"], "codex-access-token")
            self.assertIsNone(captured["headers"])
            self.assertIs(captured["clientFactory"], openai_factory)
            self.assertIs(captured["runtime"]["abortSignal"], abort_signal)
            self.assertNotIn("emitEvent", captured["runtime"])
            self.assertNotIn("includePartialMessages", captured["runtime"])
            self.assertEqual(result["text"], "Spark compaction output")
        finally:
            self.restore_home(root, previous)

    def test_compaction_model_client_timeout_uses_provider_failure(self):
        import agent_core.context.compaction_transport as compaction_transport

        async def slow_request():
            await asyncio.sleep(0.05)
            return {}

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(compaction_transport._run_compaction_request_with_timeout(provider="openai", providerLabel="OpenAI", attempt=1, timeoutMs=1, run=slow_request))
        self.assertEqual(raised.exception.record["category"], "timeout")
        self.assertTrue(raised.exception.record["retryable"])
        self.assertEqual(raised.exception.record["phase"], "response_complete")

    def test_compaction_checkpoint_v2_artifacts_memory_and_context_bridge(self):
        root, home, workspace, previous = self.temp_context()

        class FakeCompactionClient:
            def __init__(self):
                self.calls = []

            async def complete(self, args):
                self.calls.append(args)
                if len(self.calls) == 1:
                    payload = {
                        "shouldCheckpoint": True,
                        "quality": {"classification": "rich", "reason": "captures current work"},
                        "workingContext": self.working_context("Run full Slice 4 compaction", "Persist checkpoint artifacts"),
                    }
                else:
                    payload = {"verdict": "accept", "confidence": "high", "reason": "checkpoint preserves current state", "issues": [], "notes": ["verified"]}
                return {"text": json.dumps(payload), "usage": {"input_tokens": 11, "output_tokens": 7}, "model": args["model"]}

            def working_context(self, goal, next_step):
                return {
                    "project": {
                        "description": "Python port of agent-core",
                        "facts": {"primaryLanguage": "Python", "languages": ["Python"], "ecosystem": "unittest", "packageManager": None, "evidence": ["tests/test_agent_core.py"]},
                        "keyFiles": [{"path": "agent_core/compaction.py", "role": "modified", "note": "checkpoint-v2 pipeline"}],
                    },
                    "goals": {"active": [goal], "recentRequests": ["Slice 4 compaction"], "constraints": ["Perform exactly like TypeScript"], "preferences": ["Detailed commits"]},
                    "currentState": {"completed": ["Committed and pushed previous slice"], "inProgress": ["Implementing checkpoint-v2 compaction"], "blocked": []},
                    "decisions": ["Use fixed Codex Spark compaction model selection"],
                    "discoveries": ["Python compaction scaffold was placeholder-only"],
                    "activity": {"commands": ["python3 -m unittest discover -s tests -v"], "searches": ["grep compaction.ts"], "web": [], "failures": []},
                    "nextSteps": [next_step],
                    "criticalContext": ["Verification must accept before artifacts are persisted"],
                }

        try:
            session = core.create_empty_session(cwd=str(workspace), provider="openai", providerProfileName="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
            for index in range(34):
                role = "user" if index % 2 == 0 else "assistant"
                session["messages"].append({"role": role, "content": f"{role} message {index}"})
            session["todos"] = [{"content": "Finish compaction", "activeForm": "Implementing compaction", "status": "in_progress"}]
            client = FakeCompactionClient()
            def openai_factory(_options):
                return SimpleNamespace()

            result = run(core.run_compaction({"session": session, "config": {}, "runtime": {"openaiClientFactory": openai_factory}, "trigger": "manual", "force": True, "modelClient": client}))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["qualityClassification"], "rich")
            self.assertEqual(len(client.calls), 2)
            self.assertIs(client.calls[0]["runtime"]["openaiClientFactory"], openai_factory)
            self.assertIs(client.calls[1]["runtime"]["openaiClientFactory"], openai_factory)
            self.assertIn("Compacted transcript evidence:", client.calls[0]["prompt"])
            self.assertIn("Candidate checkpoint:", client.calls[1]["prompt"])
            self.assertTrue(os.path.exists(result["artifacts"]["summary"]))
            self.assertTrue(os.path.exists(result["artifacts"]["workingContext"]))
            working = json.loads(Path(result["artifacts"]["workingContext"]).read_text())
            self.assertEqual(working["version"], "v2")
            self.assertEqual(working["compaction"]["model"], "gpt-5.3-codex-spark")
            self.assertEqual(working["sourceRanges"][0]["start"], 0)
            self.assertGreater(working["sourceRanges"][0]["end"], 0)
            self.assertEqual(session["metadata"]["compactions"], 1)
            self.assertEqual(session["metadata"]["usage"]["inputTokens"], 22)
            prompt_context = run(core.build_compaction_system_prompt_context(session))
            self.assertIn("Archived working context", prompt_context)
            self.assertIn("active_goals:", prompt_context)
            memory = run(core.load_project_memory_records(str(workspace)))
            self.assertTrue(any(record["source"] == "compaction" and "Use fixed Codex Spark compaction" in record["text"] for record in memory))

            bridged = dict(session)
            bridged["metadata"] = {"compactionState": {"compactedThroughMessageIndex": 13}}
            bridged_messages = core.build_messages_for_working_context(bridged)
            self.assertEqual(bridged_messages[0]["content"], core.CONTINUATION_BRIDGE_MESSAGE)
        finally:
            self.restore_home(root, previous)

    def test_compaction_checkpoint_v2_repair_path_persists_repaired_checkpoint(self):
        root, home, workspace, previous = self.temp_context()

        def checkpoint(next_step):
            return {
                "project": {"description": "Python port", "facts": {"primaryLanguage": "Python", "languages": ["Python"], "ecosystem": "unittest", "packageManager": None, "evidence": []}, "keyFiles": []},
                "goals": {"active": ["Complete Slice 4"], "recentRequests": [], "constraints": [], "preferences": []},
                "currentState": {"completed": [], "inProgress": ["Compaction implementation"], "blocked": []},
                "decisions": [],
                "discoveries": [],
                "activity": {"commands": [], "searches": [], "web": [], "failures": []},
                "nextSteps": [next_step],
                "criticalContext": [],
            }

        class RepairClient:
            def __init__(self):
                self.calls = []

            async def complete(self, args):
                self.calls.append(args)
                if len(self.calls) == 1:
                    payload = {
                        "shouldCheckpoint": True,
                        "quality": {"classification": "usable", "reason": "needs correction"},
                        "workingContext": checkpoint("stale next step"),
                    }
                elif len(self.calls) == 2:
                    payload = {
                        "verdict": "repair",
                        "confidence": "medium",
                        "reason": "next step was stale",
                        "issues": ["stale next step"],
                        "notes": [],
                        "repairedCheckpoint": {"quality": {"classification": "usable", "reason": "repaired"}, "workingContext": checkpoint("run focused compaction tests")},
                    }
                else:
                    payload = {"verdict": "accept", "confidence": "high", "reason": "repair is accurate", "issues": [], "notes": ["repair accepted"]}
                return {"text": json.dumps(payload), "usage": {"input_tokens": 3, "output_tokens": 2}, "model": args["model"]}

        try:
            session = core.create_empty_session(cwd=str(workspace), provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
            for index in range(34):
                session["messages"].append({"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index}"})
            client = RepairClient()

            result = run(core.run_compaction({"session": session, "config": {}, "trigger": "manual", "force": True, "modelClient": client}))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(client.calls), 3)
            working = json.loads(Path(result["artifacts"]["workingContext"]).read_text())
            self.assertEqual(working["nextSteps"], ["run focused compaction tests"])
            state = json.loads(Path(result["artifacts"]["state"]).read_text())
            self.assertEqual(state["verification"]["repairCount"], 1)
            self.assertEqual(state["verification"]["usage"]["input_tokens"], 6)
            self.assertEqual(state["verification"]["usage"]["output_tokens"], 4)
            self.assertEqual(session["metadata"]["usage"]["inputTokens"], 9)
            self.assertEqual(session["metadata"]["usage"]["outputTokens"], 6)
        finally:
            self.restore_home(root, previous)

    def test_compaction_retrigger_policy_and_safe_tool_boundaries(self):
        session = {"messages": [{"role": "user", "content": str(index)} for index in range(110)], "metadata": {"compactionState": {"archiveMessageCount": 100, "workingContextTokenEstimate": 150_000}}}
        below = core.should_auto_compact(session, currentInputTokens=199_999)
        self.assertFalse(below["shouldCompact"])
        hard = core.should_auto_compact(session, currentInputTokens=200_000)
        self.assertTrue(hard["shouldCompact"])
        self.assertEqual(hard["trigger"], "auto_hard_limit")
        session["messages"].extend({"role": "user", "content": str(index)} for index in range(20))
        message_delta_only = core.should_auto_compact(session, currentInputTokens=160_000)
        self.assertFalse(message_delta_only["shouldCompact"])

        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "ok"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file": "a.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{\"file\":{\"filePath\":\"/tmp/a.py\",\"content\":\"x\"}}"}]},
            {"role": "user", "content": "safe restart"},
        ]
        self.assertTrue(core.is_safe_working_set_start(messages, 2))
        self.assertFalse(core.is_safe_working_set_start(messages, 3))
        self.assertTrue(core.is_safe_working_set_start(messages, 4))
        evidence = core.build_compaction_evidence({"sourceMessages": messages[2:4], "sourceStart": 2, "cwd": "/tmp", "tokenBudget": 10_000})
        self.assertIn("assistant tool_use Read", evidence["transcript"])
        self.assertIn("user tool_result Read", evidence["transcript"])
        malformed = core.build_compaction_evidence({"sourceMessages": [{"role": "assistant", "content": {"unexpected": True}}], "sourceStart": 0, "cwd": "/tmp", "tokenBudget": 10_000})
        self.assertEqual(malformed["entryCount"], 0)
        self.assertEqual(malformed["transcript"], "")
        self.assertEqual(core.parse_maybe_json('"scalar"'), '"scalar"')
        self.assertEqual(core.parse_maybe_json('{"ok":true}'), {"ok": True})

    def test_compaction_fallback_retry_matches_provider_failure_policy(self):
        session = core.create_empty_session(cwd="/tmp/workspace", provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
        source_messages = [{"role": "user", "content": "compact this"}]

        class FatalClient:
            def __init__(self):
                self.calls = 0

            async def complete(self, _args):
                self.calls += 1
                raise RuntimeError("fatal local parser error")

        fatal = FatalClient()
        with self.assertRaises(RuntimeError):
            run(core.run_model_backed_compaction_synthesis_with_fallback({"session": session, "config": {}, "modelClient": fatal, "previousWorkingContext": None, "sourceMessages": source_messages, "sourceStart": 0}))
        self.assertEqual(fatal.calls, 1)

        class ContextLimitClient:
            def __init__(self):
                self.calls = 0

            async def complete(self, args):
                self.calls += 1
                if self.calls == 1:
                    raise core.createProviderRequestError(
                        core.createProviderFailureRecord(
                            {
                                "provider": "openai",
                                "providerLabel": "OpenAI",
                                "phase": "request",
                                "category": "client_request",
                                "retryable": False,
                                "attempt": 1,
                                "hadAssistantOutput": False,
                                "hadToolUse": False,
                                "rawMessage": "input token context_length exceeded",
                            }
                        )
                    )
                payload = {
                    "shouldCheckpoint": True,
                    "quality": {"classification": "usable", "reason": "ok after smaller budget"},
                    "workingContext": {
                        "project": {"description": "Python port", "facts": None, "keyFiles": []},
                        "goals": {"active": ["Continue"], "recentRequests": [], "constraints": [], "preferences": []},
                        "currentState": {"completed": [], "inProgress": [], "blocked": []},
                        "decisions": [],
                        "discoveries": [],
                        "activity": {"commands": [], "searches": [], "web": [], "failures": []},
                        "nextSteps": [],
                        "criticalContext": [],
                    },
                }
                return {"text": json.dumps(payload), "usage": {}, "model": args["model"]}

        context_limit = ContextLimitClient()
        result = run(core.run_model_backed_compaction_synthesis_with_fallback({"session": session, "config": {}, "modelClient": context_limit, "previousWorkingContext": None, "sourceMessages": source_messages, "sourceStart": 0}))
        self.assertEqual(result["qualityClassification"], "usable")
        self.assertEqual(context_limit.calls, 2)

    def test_durable_memory_matches_compaction_record_policy(self):
        root, home, workspace, previous = self.temp_context()
        try:
            memory_file = workspace / ".tess" / "project" / "memory.md"
            memory_file.parent.mkdir(parents=True)
            memory_file.write_text("Prefer focused parity tests")
            session = core.create_empty_session(cwd=str(workspace), provider="openai", model="gpt-5.4", effort="high", systemPrompt="s", permissionMode="default")
            working = {
                "project": {"description": "Python port of agent-core", "facts": {"evidence": ["agent_core/compaction.py"]}, "keyFiles": [{"path": "agent_core/compaction.py"}]},
                "goals": {"active": ["Complete Slice 4"], "constraints": ["Match TypeScript behavior"], "preferences": ["Detailed commits"]},
                "decisions": ["Use fixed Codex Spark compaction model selection"],
                "discoveries": ["Checkpoint-v2 needs verifier repair"],
                "criticalContext": ["Compaction artifacts drive future prompt context"],
            }

            first = run(core.update_durable_memory_from_working_context(session, working))
            second = run(core.update_durable_memory_from_working_context(session, working))

            self.assertEqual(first[0]["id"], "manual-project-memory")
            self.assertEqual(first[0]["kind"], "user_preference")
            workflow = next(record for record in second if record.get("kind") == "workflow")
            expected_id = core.durable_memory_id("workflow", "Use fixed Codex Spark compaction model selection")
            self.assertEqual(workflow["id"], expected_id)
            self.assertEqual(sum(1 for record in second if record["id"] == expected_id), 1)
            retrieved = run(core.retrieve_relevant_durable_memory({"session": session, "prompt": "fixed Codex Spark compaction"}))
            prompt = core.render_retrieved_memory_prompt(retrieved)
            self.assertIn("<retrieved_memory>", prompt)
            self.assertIn("[workflow | compaction | agent_core/compaction.py]", prompt)
        finally:
            self.restore_home(root, previous)

    def test_agent_prompt_auto_compacts_and_rebuilds_request_before_provider_send(self):
        import agent_core.agent as agent_module

        root, home, workspace, previous = self.temp_context()
        original_measure = agent_context_module.measure_request_input_tokens
        original_run_compaction = agent_checkpoints_module.run_compaction
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"] = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"old message {index}"} for index in range(30)]
            measure_calls = {"count": 0}
            compaction_calls = []
            provider_requests = []
            events = []

            async def fake_measure(_args):
                measure_calls["count"] += 1
                return {
                    "inputTokens": (
                        200_000 if measure_calls["count"] == 1 else 1_000
                    ),
                    "source": "fake",
                }

            async def fake_run_compaction(args):
                compaction_calls.append(args)
                args["session"].setdefault("metadata", {})["compactionState"] = {
                    "compactedThroughMessageIndex": 11,
                    "compactedContextTokenEstimate": 200,
                    "workingContextTokenEstimate": 1_000,
                    "archiveMessageCount": len(args["session"].get("messages", [])),
                    "artifacts": {},
                }
                return {
                    "status": "completed",
                    "generation": 1,
                    "trigger": args["trigger"],
                    "compactedThroughMessageIndex": 11,
                    "compactedMessageCount": 11,
                    "recentMessageCount": len(args["session"].get("messages", [])) - 11,
                    "rawTokenEstimate": 200_000,
                    "workingContextTokenEstimate": 1_000,
                    "qualityClassification": "usable",
                    "compactionModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"},
                    "verificationModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"},
                    "verification": {"verdict": "accept", "confidence": "high", "reason": "ok", "issues": [], "notes": []},
                    "artifacts": {"summary": "summary.md", "state": "state.json", "workingContext": "working-context.json", "checkpoint": "checkpoint.json"},
                }

            async def send(request):
                provider_requests.append(request)
                return {"model": request["model"], "content": [{"type": "text", "text": "done"}], "usage": {}, "stopReason": "end"}

            def openai_factory(_options):
                return SimpleNamespace()
            agent_context_module.measure_request_input_tokens = fake_measure
            agent_checkpoints_module.run_compaction = fake_run_compaction
            result = run(core.runAgentPrompt(session=session, config=config, runtime=bind_test_runtime(session, {"interactive": False, "askQuestion": lambda *_: "", "emitEvent": lambda event, data: events.append((event, data)), "tools": [], "openaiClientFactory": openai_factory}), prompt="continue", providerOverride={"send": send}))

            self.assertEqual(result["text"], "done")
            self.assertEqual(len(compaction_calls), 1)
            self.assertEqual(compaction_calls[0]["trigger"], "auto_hard_limit")
            self.assertIs(compaction_calls[0]["runtime"]["openaiClientFactory"], openai_factory)
            self.assertEqual(measure_calls["count"], 2)
            self.assertEqual(provider_requests[0]["messages"][0]["content"], core.CONTINUATION_BRIDGE_MESSAGE)
            self.assertTrue(any(event == "compaction.completed" for event, _ in events))
            self.assertTrue(any(event == "provider.input_tokens" and data["input_tokens"] == 1_000 for event, data in events))
            self.assertEqual(session["metadata"]["runtimeRestore"]["lastCompactionMode"], "full_checkpoint_compaction")
        finally:
            agent_context_module.measure_request_input_tokens = original_measure
            agent_checkpoints_module.run_compaction = original_run_compaction
            self.restore_home(root, previous)

    def test_raw_session_checkpoint_runs_before_request_relief(self):
        import agent_core.agent as agent_module

        root, home, workspace, previous = self.temp_context()
        original_run_compaction = agent_checkpoints_module.run_compaction
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            session["messages"] = [{"role": "user", "content": "x" * 810_000}]
            compaction_calls = []
            events = []

            async def fake_run_compaction(args):
                compaction_calls.append(args)
                args["session"].setdefault("metadata", {})["compactionState"] = {
                    "latestCompactionAt": "2026-01-01T00:00:00Z",
                    "compactedThroughMessageIndex": 1,
                    "compactedContextTokenEstimate": 500,
                    "workingContextTokenEstimate": 500,
                    "archiveMessageCount": len(args["session"].get("messages", [])),
                    "rawTokenEstimate": 202_500,
                    "artifacts": {},
                }
                return {
                    "status": "completed",
                    "generation": 1,
                    "trigger": args["trigger"],
                    "compactedThroughMessageIndex": 1,
                    "compactedMessageCount": 1,
                    "recentMessageCount": 0,
                    "rawTokenEstimate": 202_500,
                    "workingContextTokenEstimate": 500,
                    "qualityClassification": "usable",
                    "compactionModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"},
                    "verificationModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"},
                    "verification": {"verdict": "accept", "confidence": "high", "reason": "ok", "issues": [], "notes": []},
                    "artifacts": {"summary": "summary.md", "state": "state.json", "workingContext": "working-context.json", "checkpoint": "checkpoint.json"},
                }

            agent_checkpoints_module.run_compaction = fake_run_compaction
            result = run(agent_context_module.maybeAutoCheckpointRawSession({"session": session, "config": config, "runtime": {"emitEvent": lambda event, data: events.append((event, data))}}))

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(compaction_calls), 1)
            self.assertEqual(compaction_calls[0]["trigger"], "auto_raw_hard_limit")
            self.assertTrue(any(event == "context.raw_pressure" and data["raw_token_estimate"] >= 200_000 for event, data in events))
            decision = next(data for event, data in events if event == "compaction.decision")
            self.assertTrue(decision["should_checkpoint"])
            self.assertEqual(decision["trigger"], "auto_raw_hard_limit")
            started = next(data for event, data in events if event == "compaction.started")
            self.assertEqual(started["current_token_source"], "raw_session_estimate")
        finally:
            agent_checkpoints_module.run_compaction = original_run_compaction
            self.restore_home(root, previous)


class TokenEstimatorUnificationTests(AgentCoreTestCase):
    def test_every_estimator_shares_one_message_serialization(self):
        from agent_core.context.token_accounting import _estimate_request_tokens
        from agent_core.context.continuation.tokens import estimate_messages_tokens
        from agent_core.context.compaction.utils import estimateMessageTokens as compaction_estimate
        from agent_core.payloads.token_estimation import (
            estimateMessageTokens,
            estimateTextTokens,
            stringifyValueForTokenEstimate,
        )

        messages = [
            {"role": "user", "content": "plain prompt", "actor": "human", "kind": "prompt"},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "output " * 50}],
                "actor": "tool",
                "kind": "tool_result_batch",
                "lineage": {"logicalTurnId": "turn-1"},
            },
        ]
        canonical = estimateMessageTokens(messages)
        # Origin metadata must not count: the whole-dict spaced serialization
        # measured 43.5% above the role:content form on tool results, and the
        # relief gate subtracts these numbers from each other.
        self.assertEqual(compaction_estimate(messages), canonical)
        self.assertEqual(estimate_messages_tokens(messages), canonical)
        request = {"system": "sys", "messages": messages, "tools": [{"name": "Bash"}]}
        self.assertEqual(
            _estimate_request_tokens(request),
            estimateTextTokens("sys")
            + canonical
            + estimateTextTokens(stringifyValueForTokenEstimate(request["tools"])),
        )
        stripped = [{"role": m["role"], "content": m["content"]} for m in messages]
        self.assertEqual(estimateMessageTokens(stripped), canonical)
