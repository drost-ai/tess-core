from __future__ import annotations

from tests.support.agent_core_case import *
import agent_core.agent.checkpoints as agent_checkpoints_module
import agent_core.agent.context as agent_context_module

from agent_core.agent.context_budget import assess_context_headroom
from agent_core.providers.activity import ProviderActivityEmitter
from agent_core.providers.token_budget import resolveCompactionPolicy


def _working_context_fields() -> dict[str, Any]:
    return {
        "project": {"description": "package 35 fixture", "facts": None, "keyFiles": []},
        "goals": {
            "active": ["preserve sustained headroom"],
            "recentRequests": [],
            "constraints": [],
            "preferences": [],
        },
        "currentState": {"completed": [], "inProgress": ["long turn"], "blocked": []},
        "decisions": [],
        "discoveries": [],
        "activity": {"commands": [], "searches": [], "web": [], "failures": []},
        "nextSteps": ["continue"],
        "criticalContext": [],
    }


class _CompactionClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, args: dict[str, Any]) -> dict[str, Any]:
        purpose = args["model"]["purpose"]
        self.calls.append(purpose)
        if purpose == "verification":
            value = {
                "verdict": "accept",
                "confidence": "high",
                "reason": "fixture accepted",
                "issues": [],
                "notes": [],
            }
        else:
            value = {
                "shouldCheckpoint": True,
                "quality": {"classification": "rich", "reason": "fixture"},
                "workingContext": _working_context_fields(),
            }
        return {
            "text": json.dumps(value),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model": args["model"],
        }


def _tool_tail(start: int, pairs: int, *, chars: int = 8_000) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for offset in range(pairs):
        identifier = f"call-{start + offset}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": identifier,
                            "name": "Read",
                            "input": {"path": f"file-{start + offset}.txt"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": identifier,
                            "content": "x" * chars,
                        }
                    ],
                },
            ]
        )
    return messages


class Package35CompactionLivenessTests(AgentCoreTestCase):
    def test_two_budget_policy_derives_target_and_rejects_invalid_explicit_target(self):
        policy = resolveCompactionPolicy(
            {
                "maxContextLength": 400_000,
                "maxOutputTokens": 128_000,
                "compactionThresholdTokens": 200_000,
                "compactionOutputTokens": 20_000,
            }
        )
        self.assertEqual(policy["compactionThresholdTokens"], 200_000)
        self.assertEqual(policy["compactionTargetTokens"], 150_000)
        self.assertEqual(policy["compactionHeadroomTokens"], 50_000)
        self.assertEqual(policy["targetSource"], "derived")
        with self.assertRaisesRegex(ValueError, "less than"):
            resolveCompactionPolicy(
                {
                    "compactionThresholdTokens": 200_000,
                    "compactionTargetTokens": 200_000,
                }
            )

    def test_generation_three_plus_sixty_eight_message_tail_creates_four_and_five(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.5",
                effort="medium",
                systemPrompt="system",
                permissionMode="default",
            )
            session["messages"] = [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"archived-{index}",
                }
                for index in range(311)
            ]
            session["messages"].extend(_tool_tail(0, 34, chars=20_000))
            previous_context = core.build_working_context_document_from_candidate(
                {
                    "previous": None,
                    "session": session,
                    "generation": 3,
                    "createdAt": "2026-07-25T00:00:00Z",
                    "sourceRange": {
                        "generation": 3,
                        "start": 0,
                        "end": 311,
                        "messageCount": 311,
                        "createdAt": "2026-07-25T00:00:00Z",
                    },
                    "candidate": {
                        "qualityClassification": "rich",
                        "qualityReason": "fixture",
                        "workingContext": _working_context_fields(),
                        "model": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "effort": "medium",
                        },
                    },
                }
            )
            context_path = home / "generation-3-working-context.json"
            context_path.write_text(json.dumps(previous_context), encoding="utf-8")
            run(
                core.save_compaction_manifest(
                    {
                        "sessionId": session["id"],
                        "strategy": "checkpoint-v2",
                        "createdAt": "2026-07-25T00:00:00Z",
                        "updatedAt": "2026-07-25T00:00:00Z",
                        "latestGeneration": 3,
                        "latestCompactionId": "generation-3",
                        "compactedThroughMessageIndex": 311,
                        "archiveMessageCount": 311,
                        "artifacts": {"workingContext": str(context_path)},
                    }
                )
            )
            session["metadata"]["compactionState"] = {
                "latestCompactionId": "generation-3",
                "latestCompactionGeneration": 3,
                "latestCompactionAt": "2026-07-25T00:00:00Z",
                "compactedThroughMessageIndex": 311,
                "archiveMessageCount": 311,
                "artifacts": {"workingContext": str(context_path)},
            }
            client = _CompactionClient()

            generation_four = run(
                core.run_compaction(
                    {
                        "session": session,
                        "config": {"durableMemory": {"enabled": False}},
                        "trigger": "auto_hard_limit",
                        "currentInputTokens": 202_261,
                        "compactionThresholdTokens": 200_000,
                        "compactionTargetTokens": 150_000,
                        "modelClient": client,
                    }
                )
            )
            self.assertEqual(len(session["messages"]), 379)
            self.assertEqual(generation_four["generation"], 4)
            self.assertGreater(generation_four["compactedThroughMessageIndex"], 311)

            first_cutoff = generation_four["compactedThroughMessageIndex"]
            session["messages"].extend(_tool_tail(34, 34, chars=20_000))
            generation_five = run(
                core.run_compaction(
                    {
                        "session": session,
                        "config": {"durableMemory": {"enabled": False}},
                        "trigger": "auto_target_headroom",
                        "currentInputTokens": 190_000,
                        "compactionThresholdTokens": 200_000,
                        "compactionTargetTokens": 150_000,
                        "modelClient": client,
                    }
                )
            )
            self.assertEqual(generation_five["generation"], 5)
            self.assertGreater(
                generation_five["compactedThroughMessageIndex"],
                first_cutoff,
            )
            calls_after_generation_five = list(client.calls)
            no_churn = run(
                core.run_compaction(
                    {
                        "session": session,
                        "config": {"durableMemory": {"enabled": False}},
                        "trigger": "auto_target_headroom",
                        "currentInputTokens": 170_000,
                        "compactionThresholdTokens": 200_000,
                        "compactionTargetTokens": 150_000,
                        "modelClient": client,
                    }
                )
            )
            self.assertEqual(no_churn["reason"], "insufficient_incremental_relief")
            self.assertEqual(client.calls, calls_after_generation_five)
            state = json.loads(Path(generation_five["artifacts"]["state"]).read_text())
            self.assertEqual(state["previousWorkingContextGeneration"], 4)
            self.assertEqual(state["sourceRange"]["start"], first_cutoff)
        finally:
            self.restore_home(root, previous)

    def test_oversized_rebuilt_request_fails_closed_after_one_generation(self):
        root, home, workspace, previous = self.temp_context()
        original_measure = agent_context_module.measure_request_input_tokens
        original_compact = agent_checkpoints_module.run_compaction
        try:
            config = run(
                core.resolve_config(
                    {
                        "provider": "openai",
                        "runtimeHome": str(home),
                        "permissionMode": "bypassPermissions",
                    },
                    str(workspace),
                )
            )
            session = core.create_empty_session(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            session["messages"] = [
                {"role": "user" if index % 2 == 0 else "assistant", "content": f"m-{index}"}
                for index in range(40)
            ]
            compact_calls = 0
            provider_calls = 0

            async def measure(_args: dict[str, Any]) -> dict[str, Any]:
                return {
                    "inputTokens": 202_261,
                    "source": "fixture",
                    "estimatedFixedInputTokens": 20_000,
                    "estimatedMessageInputTokens": 182_261,
                }

            async def compact(args: dict[str, Any]) -> dict[str, Any]:
                nonlocal compact_calls
                compact_calls += 1
                args["session"].setdefault("metadata", {})["compactionState"] = {
                    "latestCompactionGeneration": 4,
                    "latestCompactionAt": "2026-07-25T00:00:00Z",
                    "compactedThroughMessageIndex": 20,
                    "archiveMessageCount": len(args["session"]["messages"]),
                    "artifacts": {},
                }
                return {"status": "completed", "generation": 4, "trigger": args["trigger"]}

            async def send(_request: dict[str, Any]) -> dict[str, Any]:
                nonlocal provider_calls
                provider_calls += 1
                raise AssertionError("oversized request must not dispatch")

            agent_context_module.measure_request_input_tokens = measure
            agent_checkpoints_module.run_compaction = compact
            with self.assertRaises(core.ProviderRequestError) as raised:
                run(
                    core.run_agent_prompt(
                        session=session,
                        config=config,
                        runtime=bind_test_runtime(session),
                        prompt="continue",
                        providerOverride={"send": send},
                    )
                )
            self.assertEqual(raised.exception.record["code"], "context_capacity_exhausted")
            self.assertEqual(compact_calls, 1)
            self.assertEqual(provider_calls, 0)
        finally:
            agent_context_module.measure_request_input_tokens = original_measure
            agent_checkpoints_module.run_compaction = original_compact
            self.restore_home(root, previous)

    def test_headroom_degraded_is_typed_and_requires_output_safety(self):
        policy = resolveCompactionPolicy(
            {
                "maxContextLength": 200_000,
                "maxOutputTokens": 16_384,
                "compactionThresholdTokens": 200_000,
            }
        )
        assessment = assess_context_headroom(
            measurement={
                "inputTokens": 160_000,
                "source": "fixture",
                "estimatedFixedInputTokens": 25_000,
            },
            policy=policy,
            outputBudget={
                "insufficient": False,
                "effectiveMaxOutputTokens": 16_384,
                "minUsefulOutputTokens": 256,
            },
            compactionOutcome={
                "status": "skipped",
                "reason": "insufficient_incremental_relief",
            },
        )
        self.assertEqual(assessment["status"], "headroom_degraded")
        self.assertEqual(assessment["reason"], "non_compactable_current_turn_tail")

    def test_provider_activity_is_content_free_monotonic_and_coalesced(self):
        events: list[tuple[str, dict[str, Any]]] = []
        clock = iter([0.0, 0.01, 0.02, 0.30, 0.31])
        emitter = ProviderActivityEmitter(
            emitEvent=lambda event, data: events.append((event, data)),
            sessionId="session-1",
            logicalTurnId="turn-1",
            logicalCallId="call-1",
            provider="openai",
            profile="codex",
            model="gpt-5.5",
            coalesceIntervalMs=250,
            monotonic=lambda: next(clock),
        )
        self.assertTrue(emitter.emit("request_started"))
        self.assertTrue(emitter.emit("reasoning"))
        self.assertFalse(emitter.emit("reasoning"))
        self.assertTrue(emitter.emit("reasoning"))
        self.assertTrue(emitter.emit("terminal", force=True))
        payloads = [data for event, data in events if event == "provider.activity"]
        self.assertEqual([item["monotonic_sequence"] for item in payloads], [1, 2, 3, 4])
        rendered = json.dumps(payloads)
        self.assertNotIn("thinking", rendered)
        self.assertNotIn("payload", rendered)

    def test_true_silence_has_typed_request_start_timeout(self):
        async def never_create(_params: dict[str, Any]) -> Any:
            await asyncio.Future()

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.send_openai_responses_request(
                    {
                        "token": "test",
                        "mode": "responses",
                        "request": {
                            "model": "gpt-5.5",
                            "system": "s",
                            "messages": [{"role": "user", "content": "silent"}],
                            "tools": [],
                        },
                        "runtime": {},
                        "maxAttempts": 1,
                        "requestStartTimeoutMs": 20,
                        "clientFactory": lambda _options: SimpleNamespace(
                            responses=SimpleNamespace(create=never_create)
                        ),
                    }
                )
            )
        self.assertEqual(raised.exception.record["phase"], "request_start")
        self.assertEqual(
            raised.exception.record["code"],
            "provider_request_start_timeout",
        )

    def test_productive_reasoning_without_text_renews_stream_idle(self):
        emitted: list[tuple[str, dict[str, Any]]] = []

        class ReasoningStream:
            async def __aiter__(self):
                for index in range(4):
                    await asyncio.sleep(0.01)
                    yield {
                        "type": "response.reasoning_text.delta",
                        "delta": f"private-{index}",
                    }
                yield {
                    "type": "response.completed",
                    "response": {
                        "output": [],
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                }

        async def create(_params: dict[str, Any]) -> Any:
            return ReasoningStream()

        events: list[tuple[str, dict[str, Any]]] = []
        emitter = ProviderActivityEmitter(
            emitEvent=lambda event, data: events.append((event, data)),
            sessionId="session",
            logicalTurnId="turn",
            logicalCallId="call",
            provider="openai",
            profile="test",
            model="gpt-5.5",
            coalesceIntervalMs=0,
        )
        response = run(
            testing.send_openai_responses_request(
                {
                    "token": "test",
                    "mode": "responses",
                    "request": {
                        "model": "gpt-5.5",
                        "system": "s",
                        "messages": [{"role": "user", "content": "reason"}],
                        "tools": [],
                    },
                    "runtime": {
                        "includePartialMessages": True,
                        "emitEvent": lambda event, data: emitted.append((event, data)),
                        "providerActivityEmitter": emitter,
                    },
                    "maxAttempts": 1,
                    "streamIdleTimeoutMs": 25,
                    "clientFactory": lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                }
            )
        )
        self.assertEqual(response["stopReason"], "stop")
        activity = [data for event, data in events if event == "provider.activity"]
        self.assertEqual(
            len([item for item in activity if item["activity_kind"] == "reasoning"]),
            4,
        )
        self.assertNotIn("private-", json.dumps(activity))
        raw_thinking = next(
            data
            for event, data in emitted
            if event == "message.assistant.thinking.partial"
        )
        self.assertEqual(raw_thinking["reasoning_kind"], "raw_thinking")
        self.assertFalse(raw_thinking["user_visible"])
        self.assertEqual(raw_thinking["provider_event_type"], "response.reasoning_text.delta")
