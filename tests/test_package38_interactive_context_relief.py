from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, bind_test_runtime, run

from agent_core.providers.token_budget import resolve_interactive_context_policy
from agent_core.agent_loop.tool_result_budget import ToolResultBatchBudget
from agent_core.payloads.result_guard import guardToolCallResult, serializedContentBytes


def _codex_config(home: Path, workspace: Path) -> dict[str, Any]:
    profile = {
        "name": "codex",
        "kind": "openai",
        "apiMode": "codex",
        "defaultModel": "gpt-5.6-sol",
        "auth": {},
        "maxContextLength": 400_000,
        "maxOutputTokens": 128_000,
        "compactionThresholdTokens": 200_000,
        "compactionTargetTokens": 150_000,
        "compactionOutputTokens": 20_000,
    }
    return {
        "cwd": str(workspace),
        "runtimeHome": str(home),
        "allowedRoots": [str(workspace)],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "codex",
        "providerProfile": profile,
        "providerProfiles": {"codex": profile},
        "customHeaders": {},
        "openaiApiMode": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "assistantName": "Test",
        "assistantDescription": "",
        "systemPrompt": "stable identity",
        "tools": [],
        "allowedTools": [],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": False,
    }


class Package38InteractiveContextReliefTests(AgentCoreTestCase):
    def test_hundreds_of_batches_keep_the_full_later_allowance(self):
        budget = ToolResultBatchBudget(
            {"profileName": "codex", "kind": "openai", "apiMode": "codex"}
        )
        self.assertEqual(
            [budget.perResultLimit(1) for _ in range(500)],
            [24_000] * 500,
        )

    def test_removed_settings_are_inert_and_batch_default_remains(self):
        policy = resolve_interactive_context_policy(
            {"profileName": "codex", "kind": "openai", "apiMode": "codex"}
        )
        self.assertIsNone(policy["contextReliefThresholdTokens"])
        self.assertIsNone(policy["contextReliefTargetTokens"])
        self.assertEqual(policy["inlineToolResultBatchBytes"], 24_000)
        self.assertIsNone(policy["inlineToolResultTurnBytes"])
        self.assertEqual(policy["reliefSource"], "disabled")
        self.assertEqual(policy["toolBudgetSource"], "codex_inferred")

        disabled = resolve_interactive_context_policy(
            {"profileName": "anthropic", "kind": "anthropic"}
        )
        self.assertIsNone(disabled["contextReliefThresholdTokens"])
        self.assertIsNone(disabled["inlineToolResultBatchBytes"])

        historical = resolve_interactive_context_policy(
            {
                "profileName": "codex",
                "kind": "openai",
                "apiMode": "codex",
                "contextReliefThresholdTokens": "invalid but inert",
                "contextReliefTargetTokens": -1,
                "inlineToolResultBatchBytes": 20_000,
                "inlineToolResultTurnBytes": "invalid but inert",
            }
        )
        self.assertIsNone(historical["contextReliefThresholdTokens"])
        self.assertIsNone(historical["contextReliefTargetTokens"])
        self.assertIsNone(historical["inlineToolResultTurnBytes"])
        self.assertEqual(historical["inlineToolResultBatchBytes"], 20_000)
        self.assertEqual(historical["reliefSource"], "deprecated_ignored")
        self.assertTrue(historical["deprecatedTurnBudgetIgnored"])

    def test_removed_interactive_target_does_not_block_provider_dispatch(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            config = _codex_config(home, workspace)
            system_prompt = "irreducible identity " * 12_000
            config["systemPrompt"] = system_prompt
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.6-sol",
                effort="xhigh",
                systemPrompt=system_prompt,
                permissionMode="bypassPermissions",
            )
            requests: list[dict[str, Any]] = []

            async def send(request):
                requests.append(copy.deepcopy(request))
                return {
                    "model": "gpt-5.6-sol",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 30_000, "output_tokens": 10},
                    "content": [{"type": "text", "text": "dispatched"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session),
                    prompt="current request",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["text"], "dispatched")
            self.assertEqual(len(requests), 1)
        finally:
            self.restore_home(root, previous)

    def test_live_shaped_first_request_is_not_projected_or_mutated(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            config = _codex_config(home, workspace)
            system_prompt = "identity guidance " * 7_500
            config["systemPrompt"] = system_prompt
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.6-sol",
                effort="xhigh",
                systemPrompt=system_prompt,
                permissionMode="bypassPermissions",
            )
            session["messages"] = [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"historical-{index} " + ("payload " * 2_500),
                }
                for index in range(20)
            ]
            original_messages = copy.deepcopy(session["messages"])
            requests: list[dict[str, Any]] = []
            events: list[tuple[str, dict[str, Any]]] = []

            async def send(request):
                requests.append(copy.deepcopy(request))
                return {
                    "model": "gpt-5.6-sol",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 42_000, "output_tokens": 10},
                    "content": [{"type": "text", "text": "bounded"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {"emitEvent": lambda event, data: events.append((event, data))},
                    ),
                    prompt="current request",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["text"], "bounded")
            self.assertEqual(len(requests), 1)
            preflight = next(
                data
                for event, data in events
                if event == "provider.request_preflight"
            )
            relief = preflight["interactive_relief"]
            self.assertFalse(relief["applied"])
            self.assertIsNone(relief["originalInputTokens"])
            self.assertIsNone(relief["thresholdTokens"])
            self.assertIsNone(relief["targetTokens"])
            self.assertTrue(relief["targetMet"])
            self.assertGreater(preflight["categories"]["systemIdentity"], 25_000)
            self.assertEqual(session["messages"][:20], original_messages)
            request_text = json.dumps(requests[0]["messages"])
            self.assertIn("historical-0", request_text)
            self.assertIn("historical-19", request_text)
        finally:
            self.restore_home(root, previous)

    def test_parallel_batch_budget_artifactizes_individually_valid_results(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = ["first", "second"]
            config["allowedTools"] = ["first", "second"]
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.6-sol",
                effort="xhigh",
                systemPrompt="stable identity",
                permissionMode="bypassPermissions",
            )

            async def large_result(_input, _context):
                return {"content": "result " * 2_600}

            tools = [
                core.ToolDefinition(
                    name,
                    name,
                    {"type": "object"},
                    large_result,
                    concurrency="parallel",
                )
                for name in ("first", "second")
            ]
            requests: list[dict[str, Any]] = []
            events: list[tuple[str, dict[str, Any]]] = []

            async def send(request):
                requests.append(copy.deepcopy(request))
                if len(requests) == 1:
                    return {
                        "model": "gpt-5.6-sol",
                        "stopReason": "tool_use",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"{name}-id",
                                "name": name,
                                "input": {},
                            }
                            for name in ("first", "second")
                        ],
                    }
                return {
                    "model": "gpt-5.6-sol",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "content": [{"type": "text", "text": "done"}],
                }

            run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {
                            "tools": tools,
                            "emitEvent": lambda event, data: events.append(
                                (event, data)
                            ),
                        },
                    ),
                    prompt="run both",
                    providerOverride={"send": send},
                )
            )
            blocks = [
                block
                for message in requests[1]["messages"]
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            ]
            self.assertEqual(len(blocks), 2)
            self.assertTrue(
                all(serializedContentBytes(block["content"]) <= 12_000 for block in blocks)
            )
            self.assertTrue(all('"artifact"' in block["content"] for block in blocks))
            reader = next(
                tool
                for tool in core.createBuiltinTools()
                if tool.name == "ReadToolArtifact"
            )
            for block in blocks:
                envelope = json.loads(block["content"])
                artifact_path = Path(envelope["artifact"]["filePath"])
                self.assertTrue(artifact_path.is_file())
                self.assertGreater(len(artifact_path.read_text()), 18_000)
                page = run(
                    reader.handler(
                        {"artifact_id": envelope["artifact"]["artifactId"]},
                        {"session": session, "config": config, "cwd": str(workspace)},
                    )
                )
                self.assertIn("result result", json.loads(page["content"])["content"])
            budget = next(data for event, data in events if event == "tool.result_budget")
            self.assertLessEqual(budget["batch_used_bytes"], 24_000)
            self.assertEqual(budget["effective_result_budget_bytes"], 12_000)
        finally:
            self.restore_home(root, previous)

    def test_batch_budget_resets_every_iteration_and_next_turn(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = ["large"]
            config["allowedTools"] = ["large"]
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.6-sol",
                effort="xhigh",
                systemPrompt="stable identity",
                permissionMode="bypassPermissions",
            )

            async def large_result(_input, _context):
                return {"content": "durable output " * 3_000}

            tool = core.ToolDefinition(
                "large",
                "Return a large result.",
                {"type": "object"},
                large_result,
            )

            def exercise(prompt: str, rounds: int):
                events: list[tuple[str, dict[str, Any]]] = []
                calls = 0

                async def send(_request):
                    nonlocal calls
                    calls += 1
                    if calls <= rounds:
                        return {
                            "model": "gpt-5.6-sol",
                            "stopReason": "tool_use",
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"{prompt}-{calls}",
                                    "name": "large",
                                    "input": {},
                                }
                            ],
                        }
                    return {
                        "model": "gpt-5.6-sol",
                        "stopReason": "end_turn",
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                        "content": [{"type": "text", "text": "done"}],
                    }

                run(
                    core.runAgentPrompt(
                        session=session,
                        config=config,
                        runtime=bind_test_runtime(
                            session,
                            {
                                "tools": [tool],
                                "emitEvent": lambda event, data: events.append(
                                    (event, data)
                                ),
                            },
                        ),
                        prompt=prompt,
                        providerOverride={"send": send},
                    )
                )
                return [
                    data
                    for event, data in events
                    if event == "tool.result_budget"
                ]

            first = exercise("first", 3)
            self.assertEqual(
                [
                    event["effective_result_budget_bytes"]
                    for event in first
                ],
                [24_000, 24_000, 24_000],
            )
            self.assertTrue(
                all("turn_remaining_bytes" not in event for event in first)
            )
            self.assertGreater(
                sum(event["batch_used_bytes"] for event in first),
                64_000,
            )

            second = exercise("second", 1)
            self.assertEqual(
                second[0]["effective_result_budget_bytes"],
                24_000,
            )
            self.assertNotIn("turn_remaining_bytes", second[0])
        finally:
            self.restore_home(root, previous)

    def test_required_aggregate_artifact_failure_is_truthful_and_closed(self):
        async def fail_writer(_input):
            raise RuntimeError("storage unavailable")

        guarded = run(
            guardToolCallResult(
                config={"providerKind": "openai", "model": "gpt-5.6-sol"},
                sessionId="session",
                toolName="Read",
                outcome={
                    "kind": "returned",
                    "result": {"content": "important " * 4_000},
                },
                maxBytes=2_000,
                requireArtifact=True,
                artifactWriter=fail_writer,
            )
        )
        self.assertTrue(guarded["result"]["isError"])
        self.assertTrue(guarded["artifactUnavailable"])
        self.assertIn("artifact_persistence_failed", guarded["result"]["content"])
        self.assertNotIn("storage unavailable", guarded["result"]["content"])

        structured = run(
            guardToolCallResult(
                config={"providerKind": "openai", "model": "gpt-5.6-sol"},
                sessionId="session",
                toolName="List",
                outcome={
                    "kind": "returned",
                    "result": {
                        "content": json.dumps(
                            {"items": [{"id": index} for index in range(500)]}
                        )
                    },
                },
                maxBytes=20_000,
                requireArtifact=True,
                artifactWriter=fail_writer,
            )
        )
        self.assertTrue(structured["result"]["isError"])
        self.assertIn(
            "artifact_persistence_failed",
            structured["result"]["content"],
        )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            run(
                guardToolCallResult(
                    config={
                        "providerKind": "openai",
                        "model": "gpt-5.6-sol",
                    },
                    sessionId="session",
                    toolName="Read",
                    outcome={
                        "kind": "returned",
                        "result": {"content": "value"},
                    },
                    maxBytes=0,
                )
            )
