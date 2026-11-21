from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import tess_core._facade as core
from tests.support.tess_core_case import TessCoreTestCase, bind_test_runtime, run

from tess_core.providers.token_budget import resolve_interactive_context_policy
from tess_core.agent.tool_result_budget import ToolResultBatchBudget
from tess_core.payloads.result_guard import guard_tool_call_result, serialized_content_bytes


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


class Package38InteractiveContextReliefTests(TessCoreTestCase):
    def test_calling_several_tools_at_once_does_not_shrink_any_result(self):
        budget = ToolResultBatchBudget(
            {"profileName": "codex", "kind": "openai", "apiMode": "codex"}
        )
        self.assertEqual({budget.perResultLimit(count) for count in (1, 2, 8, 64)}, {None})
        self.assertIsNone(budget.record([], []))

    def test_removed_settings_are_inert_and_nothing_is_shared_out_of_a_batch(self):
        policy = resolve_interactive_context_policy(
            {"profileName": "codex", "kind": "openai", "apiMode": "codex"}
        )
        self.assertIsNone(policy["contextReliefThresholdTokens"])
        self.assertIsNone(policy["contextReliefTargetTokens"])
        self.assertIsNone(policy["inlineToolResultBatchBytes"])
        self.assertIsNone(policy["inlineToolResultTurnBytes"])
        self.assertEqual(policy["reliefSource"], "disabled")
        self.assertEqual(policy["toolBudgetSource"], "disabled")

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
        self.assertIsNone(historical["inlineToolResultBatchBytes"])
        self.assertEqual(historical["toolBudgetSource"], "deprecated_ignored")
        self.assertEqual(historical["reliefSource"], "deprecated_ignored")
        self.assertTrue(historical["deprecatedTurnBudgetIgnored"])

    def test_removed_interactive_target_does_not_block_provider_dispatch(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            system_prompt = "irreducible identity " * 12_000
            config["systemPrompt"] = system_prompt
            session = core.create_empty_session(
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
                core.run_agent_prompt(
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
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            system_prompt = "identity guidance " * 7_500
            config["systemPrompt"] = system_prompt
            session = core.create_empty_session(
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
                core.run_agent_prompt(
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
            self.assertNotIn("interactive_relief", preflight)
            self.assertGreater(preflight["categories"]["systemIdentity"], 25_000)
            self.assertEqual(session["messages"][:20], original_messages)
            request_text = json.dumps(requests[0]["messages"])
            self.assertIn("historical-0", request_text)
            self.assertIn("historical-19", request_text)
        finally:
            self.restore_home(root, previous)

    def test_parallel_results_are_each_held_to_their_own_allowance(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = ["first", "second"]
            config["allowedTools"] = ["first", "second"]
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.6-sol",
                effort="xhigh",
                systemPrompt="stable identity",
                permissionMode="bypassPermissions",
            )

            def returning(repeats):
                async def handler(_input, _context):
                    return {"content": "result " * repeats}
                return handler

            # 18,200 characters is within the allowance; 42,000 is over it.
            tools = [
                core.ToolDefinition(
                    name,
                    name,
                    {"type": "object"},
                    returning(repeats),
                    concurrency="parallel",
                )
                for name, repeats in (("first", 2_600), ("second", 6_000))
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
                core.run_agent_prompt(
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
            whole, saved = blocks
            self.assertEqual(whole["content"], "result " * 2_600)
            self.assertTrue(saved["content"].startswith("<persisted-output>\nOutput too large (41.0KB). Full output saved to: "))
            self.assertIn("\n\nPreview (first 2.0KB):\nresult result", saved["content"])
            self.assertTrue(saved["content"].endswith("\n...\n</persisted-output>"))
            self.assertLess(len(saved["content"]), 2_600)
            saved_path = Path(saved["content"].split("Full output saved to: ", 1)[1].split("\n", 1)[0])
            self.assertEqual(saved_path.read_text(), "result " * 6_000)
            reader = next(tool for tool in core.create_builtin_tools() if tool.name == "Read")
            page = run(
                reader.handler(
                    {"file_path": str(saved_path)},
                    {"session": session, "config": config, "cwd": str(workspace)},
                )
            )
            self.assertIn("result result", json.loads(page["content"])["file"]["content"])
            self.assertFalse([event for event, _ in events if event == "tool.result_budget"])
        finally:
            self.restore_home(root, previous)

    def test_required_aggregate_artifact_failure_is_truthful_and_closed(self):
        async def fail_writer(_input):
            raise RuntimeError("storage unavailable")

        guarded = run(
            guard_tool_call_result(
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

        with self.assertRaisesRegex(ValueError, "positive integer"):
            run(
                guard_tool_call_result(
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
