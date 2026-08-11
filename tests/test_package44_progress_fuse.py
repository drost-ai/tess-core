from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import agent_core._facade as core
from agent_core.agent.progress import LogicalTurnProgressLedger, append_tool_result_batch
from tests.support.agent_core_case import AgentCoreTestCase, bind_test_runtime, run
from tests.test_package38_interactive_context_relief import _codex_config


class Package44ProgressFuseTests(AgentCoreTestCase):
    def _session(self, workspace: Path) -> dict[str, Any]:
        return core.create_empty_session(
            cwd=str(workspace),
            provider="openai",
            providerProfileName="codex",
            model="gpt-5.6-sol",
            effort="xhigh",
            systemPrompt="stable identity",
            permissionMode="bypassPermissions",
        )

    def _run_repeating_tool(
        self,
        *,
        session: dict[str, Any],
        config: dict[str, Any],
        tool: core.ToolDefinition,
        tool_input: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        calls = 0

        async def send(_request: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {
                "model": "gpt-5.6-sol",
                "stopReason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 10},
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"repeat-{calls}",
                        "name": tool.name,
                        "input": tool_input,
                    }
                ],
            }

        result = run(
            core.run_agent_prompt(
                session=session,
                config=config,
                runtime=bind_test_runtime(session, {"tools": [tool]}),
                prompt="continue until local safety stops this",
                providerOverride={"send": send},
            )
        )
        return result, calls

    def test_same_large_result_with_fresh_artifact_ids_stops_on_third_cycle(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = config["allowedTools"] = ["large-repeat"]

            async def repeat(_input: dict[str, Any], _context: Any):
                return {"content": "unchanged durable evidence\n" * 4_000}

            result, calls = self._run_repeating_tool(
                session=self._session(workspace),
                config=config,
                tool=core.ToolDefinition(
                    "large-repeat",
                    "Return the same large result.",
                    {"type": "object"},
                    repeat,
                ),
                tool_input={"target": "same"},
            )
            self.assertEqual(result["stopReason"], "no_progress")
            self.assertEqual(result["noProgress"]["reason"], "repeated_tool_cycle")
            self.assertEqual(calls, 3)
        finally:
            self.restore_home(root, previous)

    def test_failed_artifact_pages_use_specific_terminal_subcode(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = config["allowedTools"] = ["ReadToolArtifact"]
            reader = next(
                tool
                for tool in core.create_builtin_tools()
                if tool.name == "ReadToolArtifact"
            )
            result, calls = self._run_repeating_tool(
                session=self._session(workspace),
                config=config,
                tool=reader,
                tool_input={
                    "artifact_id": "missing-artifact",
                    "offset_bytes": 0,
                },
            )
            self.assertEqual(result["stopReason"], "no_progress")
            self.assertEqual(
                result["noProgress"]["reason"],
                "artifact_output_unreachable",
            )
            self.assertEqual(result["noProgress"]["artifactId"], "missing-artifact")
            self.assertEqual(calls, 3)
        finally:
            self.restore_home(root, previous)

    def test_incident_scale_productive_work_has_no_time_or_call_fuse(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = config["allowedTools"] = ["progress"]
            session = self._session(workspace)

            async def progress(input: dict[str, Any], _context: Any):
                return {"content": f"novel evidence {input['index']}"}

            tool = core.ToolDefinition(
                "progress",
                "Return distinct evidence.",
                {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                },
                progress,
            )
            calls = 0

            async def send(_request: dict[str, Any]) -> dict[str, Any]:
                nonlocal calls
                calls += 1
                if calls > 385:
                    return {
                        "model": "gpt-5.6-sol",
                        "stopReason": "end_turn",
                        "usage": {},
                        "content": [{"type": "text", "text": "complete"}],
                    }
                return {
                    "model": "gpt-5.6-sol",
                    "stopReason": "tool_use",
                    "usage": {},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"progress-{calls}-{index}",
                            "name": "progress",
                            "input": {"index": calls * 10 + index},
                        }
                        for index in range(3)
                    ],
                }

            clock = iter(index * 3_600 for index in range(20_000))
            with patch(
                "agent_core.agent._turn_state.time",
                SimpleNamespace(monotonic=lambda: next(clock)),
            ):
                result = run(
                    core.run_agent_prompt(
                        session=session,
                        config=config,
                        runtime=bind_test_runtime(session, {"tools": [tool]}),
                        prompt="perform productive work",
                        providerOverride={"send": send},
                    )
                )
            self.assertEqual(result["text"], "complete")
            self.assertEqual(calls, 386)
            self.assertEqual(result["toolCount"], 1_155)
        finally:
            self.restore_home(root, previous)

    def test_semantic_result_ids_are_not_discarded_as_transport_noise(self):
        session: dict[str, Any] = {"metadata": {}}
        ledger = LogicalTurnProgressLedger(session, "turn-semantic-ids")
        for index, record_id in enumerate(("record-a", "record-b"), start=1):
            observation = ledger.observe(
                [{"name": "query", "input": {"table": "items"}}],
                [
                    {
                        "content": f'{{"id":"{record_id}","sequence":{index}}}',
                    }
                ],
                provider_call_index=index,
            )
            self.assertIsNone(observation)
            self.assertEqual(ledger.receipt["consecutiveNoProgress"], 0)

    def test_nonconsecutive_revisit_does_not_trigger_repeated_cycle(self):
        session: dict[str, Any] = {"metadata": {}}
        ledger = LogicalTurnProgressLedger(session, "turn-revisit")

        def observe(value: str):
            return ledger.observe(
                [{"name": "Read", "input": {"file_path": "notes.md"}}],
                [{"content": f'{{"value":"{value}"}}'}],
                provider_call_index=1,
            )

        self.assertIsNone(observe("alpha"))
        self.assertIsNone(observe("beta"))
        self.assertIsNone(observe("alpha"))
        self.assertIsNone(observe("gamma"))
        self.assertIsNone(observe("alpha"))
        self.assertEqual(ledger.receipt["lastBatchOccurrenceCount"], 1)
        self.assertEqual(ledger.receipt["lastBatchTotalOccurrences"], 3)

    def test_short_multi_batch_cycle_stops_at_three_completed_patterns(self):
        session: dict[str, Any] = {"metadata": {}}
        ledger = LogicalTurnProgressLedger(session, "turn-alternating")

        def observe(value: str, call_index: int):
            return ledger.observe(
                [{"name": "Read", "input": {"file_path": "notes.md"}}],
                [{"content": f'{{"value":"{value}"}}'}],
                provider_call_index=call_index,
            )

        terminal = None
        for call_index in range(1, 7):
            terminal = observe(
                "alpha" if call_index % 2 else "beta",
                call_index,
            )
            if call_index < 6:
                self.assertIsNone(terminal)
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal["reason"], "repeated_tool_cycle")
        self.assertEqual(terminal["providerCallIndex"], 6)
        self.assertEqual(ledger.receipt["lastCycleLength"], 2)
        self.assertEqual(ledger.receipt["repeatedCycleCount"], 3)

    def test_advancing_artifact_cursor_resets_repeated_page_counter(self):
        session: dict[str, Any] = {"metadata": {}}
        ledger = LogicalTurnProgressLedger(session, "turn-artifact-pages")

        def page(offset: int, next_offset: int) -> tuple[dict[str, Any], dict[str, Any]]:
            return (
                {
                    "name": "ReadToolArtifact",
                    "input": {
                        "artifact_id": "artifact-1",
                        "offset_bytes": offset,
                    },
                },
                {
                    "content": (
                        '{"type":"tool_artifact_page",'
                        '"artifactId":"artifact-1",'
                        f'"nextOffsetBytes":{next_offset},'
                        '"complete":false}'
                    )
                },
            )

        first_use, first_result = page(0, 100)
        self.assertIsNone(
            ledger.observe([first_use], [first_result], provider_call_index=1)
        )
        self.assertIsNone(
            ledger.observe([first_use], [first_result], provider_call_index=2)
        )
        self.assertEqual(ledger.receipt["consecutiveNoProgress"], 1)
        next_use, next_result = page(100, 200)
        self.assertIsNone(
            ledger.observe([next_use], [next_result], provider_call_index=3)
        )
        self.assertEqual(ledger.receipt["consecutiveNoProgress"], 0)

    def test_interleaved_image_blocks_do_not_shift_ledger_alignment(self):
        session: dict[str, Any] = {"messages": [], "metadata": {}}
        ledger = LogicalTurnProgressLedger(session, "turn-image-alignment")
        tool_uses = [
            {"id": "shot-1", "name": "Screenshot", "input": {}},
            {
                "id": "art-1",
                "name": "ReadToolArtifact",
                "input": {"artifact_id": "artifact-1", "offset_bytes": 0},
            },
        ]
        screenshot_result = {"type": "tool_result", "tool_use_id": "shot-1", "content": "captured"}
        image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
        artifact_result = {
            "type": "tool_result",
            "tool_use_id": "art-1",
            "content": '{"type":"tool_artifact_page","artifactId":"artifact-1","nextOffsetBytes":100,"complete":false}',
        }
        outcome = append_tool_result_batch(
            session=session,
            ledger=ledger,
            logical_turn_id="turn-image-alignment",
            tool_uses=tool_uses,
            tool_results=[screenshot_result, artifact_result],
            transcript_blocks=[screenshot_result, image_block, artifact_result],
            provider_call_index=1,
        )
        self.assertIsNone(outcome)
        # The artifact page advanced the cursor; the interleaved image block
        # must not be misread as that tool's (failed) result.
        self.assertEqual(ledger.receipt["artifactOffsets"], {"artifact-1": 100})
        self.assertEqual(ledger.receipt["artifactFailures"], {"artifact-1": 0})
        # The conversation still carries all three blocks, image included.
        self.assertEqual(
            [block.get("type") for block in session["messages"][-1]["content"]],
            ["tool_result", "image", "tool_result"],
        )

    def test_general_fuse_is_bounded_and_durable(self):
        session: dict[str, Any] = {"metadata": {}}
        ledger = LogicalTurnProgressLedger(session, "turn-general-fuse")
        for occurrence in range(2):
            for index in range(12):
                observation = ledger.observe(
                    [
                        {
                            "type": "tool_use",
                            "id": f"{occurrence}-{index}",
                            "name": "probe",
                            "input": {"index": index},
                        }
                    ],
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"{occurrence}-{index}",
                            "content": f"unchanged probe {index}",
                        }
                    ],
                    provider_call_index=occurrence * 12 + index + 1,
                )
                if occurrence == 0 or index < 11:
                    self.assertIsNone(observation)
        self.assertEqual(observation["reason"], "no_semantic_progress")
        self.assertEqual(
            session["metadata"]["turnProgress"]["terminalReason"],
            "no_semantic_progress",
        )
        recovered = LogicalTurnProgressLedger(session, "turn-general-fuse")
        self.assertEqual(
            recovered.terminalResult()["reason"],
            "no_semantic_progress",
        )

    def test_host_logical_turn_id_recovers_durable_terminal_without_dispatch(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            session = self._session(workspace)
            session["messages"].append(
                {"role": "user", "content": "durable owner request"}
            )
            ledger = LogicalTurnProgressLedger(session, "owner-turn-durable")
            terminal = None
            for provider_call_index in range(1, 4):
                terminal = ledger.observe(
                    [{"name": "Read", "input": {"file_path": "same.md"}}],
                    [{"content": "unchanged evidence"}],
                    provider_call_index=provider_call_index,
                )
            self.assertEqual(terminal["reason"], "repeated_tool_cycle")
            run(core.save_session(session))
            restored = run(core.load_session(session["id"]))
            self.assertIsNotNone(restored)

            dispatched = False
            events: list[tuple[str, dict[str, Any]]] = []

            async def send(_request: dict[str, Any]) -> dict[str, Any]:
                nonlocal dispatched
                dispatched = True
                raise AssertionError("recovered terminal must not dispatch")

            result = run(
                core.run_agent_prompt(
                    session=restored,
                    config=config,
                    runtime=bind_test_runtime(
                        restored,
                        {
                            "emitEvent": lambda event, data: events.append(
                                (event, data)
                            )
                        },
                    ),
                    prompt="synthetic recovery must remain stopped",
                    logicalTurnId="owner-turn-durable",
                    providerOverride={"send": send},
                )
            )

            self.assertFalse(dispatched)
            self.assertEqual(result["stopReason"], "no_progress")
            recovered_event = next(
                data for event, data in events if event == "agent.no_progress"
            )
            self.assertTrue(recovered_event["recovered_terminal"])
            self.assertEqual(
                recovered_event["logical_turn_id"],
                "owner-turn-durable",
            )
        finally:
            self.restore_home(root, previous)
