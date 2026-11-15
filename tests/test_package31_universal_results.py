from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core.process.bash_lifecycle import _process_group_live, run_inline_shell_command
from tess_core.payloads.bash_results import build_bash_result_payload
from tess_core.process.output import create_subprocess_output_collector
from tess_core.payloads.result_guard import (
    TOOL_RESULT_FINAL_BYTES,
    guard_tool_call_result,
    serialized_content_bytes,
)
from tess_core.payloads.utf8_bounds import has_lone_utf16_surrogate, utf8_byte_length


_SMALL_CONFIG = {
    "providerKind": "openai",
    "openaiBaseUrl": "https://example.invalid",
    "openaiApiMode": "chat_completions",
    "model": "tiny-model",
}


class Package31UniversalResultTests(TessCoreTestCase):
    def test_durable_memory_projects_oversized_manual_memory_inside_total_prompt_budget(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-opus-4-8",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            memory_path = Path(core.get_project_memory_path(str(workspace)))
            memory_path.parent.mkdir(parents=True)
            memory_path.write_text("needle " + ("漢🙂\\\"" * 180_000))

            selected = run(
                core.retrieve_relevant_durable_memory(
                    session=session,
                    prompt="find needle",
                    maxTokenBudget=1_600,
                    maxRecords=8,
                )
            )
            prompt = core.render_retrieved_memory_prompt(selected)
            self.assertIsNotNone(prompt)
            self.assertLessEqual(utf8_byte_length(prompt), 1_600)
            self.assertLessEqual(selected["tokenEstimate"], 1_600)
            self.assertLessEqual(selected["byteEstimate"], 1_600)
            self.assertEqual(selected["truncatedRecordIds"], ["manual-project-memory"])
            self.assertIn("project memory truncated from", prompt)
            self.assertFalse(has_lone_utf16_surrogate(prompt))

            too_small = run(
                core.retrieve_relevant_durable_memory(
                    session=session, prompt="find needle", maxTokenBudget=100
                )
            )
            self.assertEqual(too_small["records"], [])
            self.assertIsNone(core.render_retrieved_memory_prompt(too_small))
        finally:
            self.restore_home(root, previous)

    def test_dispatch_bounds_returned_and_thrown_results_before_provider_reuse(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = {
                "cwd": str(workspace),
                "runtimeHome": str(home),
                "allowedRoots": [str(workspace)],
                "agents": {},
                "provider": "anthropic",
                "providerKind": "anthropic",
                "providerProfileName": "default",
                "providerProfile": {"kind": "anthropic", "name": "default", "auth": {}},
                "providerProfiles": {"default": {"kind": "anthropic", "name": "default"}},
                "customHeaders": {},
                "model": "claude-opus-4-8",
                "effort": "medium",
                "assistantName": "Test",
                "assistantDescription": "",
                "systemPrompt": "test",
                "tools": ["huge_return", "huge_throw"],
                "allowedTools": ["huge_return", "huge_throw"],
                "disallowedTools": [],
                "permissionMode": "bypassPermissions",
                "sessionPersistence": False,
            }
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-opus-4-8",
                effort="medium",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )

            async def huge_return(_input, _context):
                return {"content": "🙂\\\"" * 50_000}

            async def huge_throw(_input, _context):
                raise RuntimeError(f"Bearer dispatch-secret {'failure' * 30_000}")

            tools = [
                core.ToolDefinition(
                    "huge_return", "huge return", {"type": "object"}, huge_return,
                    concurrency="parallel",
                ),
                core.ToolDefinition(
                    "huge_throw", "huge throw", {"type": "object"}, huge_throw,
                    concurrency="parallel",
                ),
            ]
            events = []
            requests = []

            async def send(request):
                requests.append(request)
                if len(requests) == 1:
                    return {
                        "model": "claude-opus-4-8",
                        "stopReason": "tool_use",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [
                            {"type": "tool_use", "id": "return-id", "name": "huge_return", "input": {}},
                            {"type": "tool_use", "id": "throw-id", "name": "huge_throw", "input": {}},
                        ],
                    }
                return {
                    "model": "claude-opus-4-8",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                    "content": [{"type": "text", "text": "done"}],
                }

            result = run(
                core.run_agent_prompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {
                            "interactive": False,
                            "askQuestion": lambda *_: "",
                            "emitEvent": lambda event, data: events.append((event, data)),
                            "tools": tools,
                        },
                    ),
                    prompt="exercise result guard",
                    providerOverride={"send": send},
                )
            )
            self.assertEqual(result["text"], "done")
            blocks = [
                block
                for message in requests[1]["messages"]
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            ]
            self.assertEqual(len(blocks), 2)
            self.assertTrue(
                all(
                    serialized_content_bytes(block["content"]) <= TOOL_RESULT_FINAL_BYTES["large"]
                    for block in blocks
                )
            )
            thrown = next(block for block in blocks if block["tool_use_id"] == "throw-id")
            self.assertTrue(thrown["is_error"])
            self.assertNotIn("dispatch-secret", thrown["content"])
            completed = [data for event, data in events if event == "tool.completed"]
            self.assertEqual(len(completed), 2)
            self.assertTrue(
                all(
                    serialized_content_bytes(item["output"]) <= TOOL_RESULT_FINAL_BYTES["large"]
                    for item in completed
                )
            )
        finally:
            self.restore_home(root, previous)

    def test_exit_zero_mutation_stays_successful_when_artifact_metadata_fails(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))

            async def exercise():
                async def fail_metadata(_path, _value):
                    raise RuntimeError("unbounded secret storage path must not escape")

                collector = await create_subprocess_output_collector(
                    sessionId="artifact-fault-session",
                    toolName="Bash",
                    family="bash",
                    maxSpoolBytes=512 * 1024,
                    maxRingBytes=2 * 1024,
                    metadataWriter=fail_metadata,
                )
                captured = await run_inline_shell_command(
                    shell="/bin/sh",
                    command="printf 'once\\n' >> marker.txt; head -c 131072 /dev/zero | tr '\\0' x",
                    cwd=str(workspace),
                    env=os.environ.copy(),
                    timeoutMs=5_000,
                    outputCollector=collector,
                )
                shaped = await build_bash_result_payload(
                    config=_SMALL_CONFIG,
                    sessionId="artifact-fault-session",
                    command="mutating command",
                    cwd=str(workspace),
                    run=captured,
                    spoolLimitBytes=512 * 1024,
                )
                return captured, shaped

            captured, shaped = run(exercise())
            self.assertEqual(captured["exitCode"], 0)
            self.assertFalse(captured["outputLimitExceeded"])
            self.assertTrue(captured["artifactUnavailable"])
            self.assertFalse(shaped["isError"])
            self.assertEqual(shaped["payload"]["status"], "completed")
            self.assertTrue(shaped["payload"]["artifactUnavailable"])
            self.assertRegex(shaped["payload"]["note"], r"completed; output view persistence failed")
            self.assertNotIn("secret storage path", json.dumps(shaped["payload"]))
            content = json.dumps(shaped["payload"], ensure_ascii=False, indent=2)
            self.assertLessEqual(serialized_content_bytes(content), TOOL_RESULT_FINAL_BYTES["small"])
            self.assertEqual((workspace / "marker.txt").read_text(encoding="utf-8"), "once\n")
        finally:
            self.restore_home(root, previous)

    @unittest.skipIf(os.name == "nt", "process-group assertions require POSIX")
    def test_mutating_output_overflow_is_bounded_non_retryable_and_drained(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            spool_limit = 32 * 1024
            ring_limit = 2 * 1024
            captured = run(
                run_inline_shell_command(
                    shell="/bin/sh",
                    command=(
                        "printf 'once\\n' >> marker.txt; "
                        "printf '%s' $$ > producer.pid; "
                        "while :; do printf '0123456789abcdef0123456789abcdef\\n'; done"
                    ),
                    cwd=str(workspace),
                    env=os.environ.copy(),
                    timeoutMs=5_000,
                    terminationGraceMs=100,
                    terminationFallbackMs=1_000,
                    sessionId="output-limit-session",
                    toolName="Bash",
                    maxSpoolBytes=spool_limit,
                    maxRingBytes=ring_limit,
                )
            )
            shaped = run(
                build_bash_result_payload(
                    config=_SMALL_CONFIG,
                    sessionId="output-limit-session",
                    command="mutating high-output command",
                    cwd=str(workspace),
                    run=captured,
                    spoolLimitBytes=spool_limit,
                )
            )
            self.assertTrue(captured["outputLimitExceeded"])
            self.assertLessEqual(captured["spoolBytes"], spool_limit)
            self.assertLessEqual(captured["ringBytes"], ring_limit)
            self.assertTrue(shaped["isError"])
            self.assertEqual(shaped["payload"]["status"], "terminated")
            self.assertEqual(shaped["payload"]["reason"], "output_limit")
            self.assertFalse(shaped["payload"]["retryable"])
            self.assertTrue(shaped["payload"]["sideEffectsMayHaveOccurred"])
            self.assertTrue(shaped["payload"]["outputTruncated"])
            self.assertEqual(shaped["payload"]["limitBytes"], spool_limit)
            content = json.dumps(shaped["payload"], ensure_ascii=False, indent=2)
            self.assertLessEqual(serialized_content_bytes(content), TOOL_RESULT_FINAL_BYTES["small"])
            self.assertEqual((workspace / "marker.txt").read_text(encoding="utf-8"), "once\n")
            pid = int((workspace / "producer.pid").read_text(encoding="utf-8"))
            self.assertFalse(_process_group_live(pid))
        finally:
            self.restore_home(root, previous)

    def test_all_payload_classes_bound_unicode_and_artifact_failure(self):
        cases = [
            ("small", _SMALL_CONFIG),
            (
                "medium",
                {"providerKind": "openai", "openaiApiMode": "responses", "model": "gpt-5-mini"},
            ),
            ("large", {"providerKind": "anthropic", "model": "claude-opus-4-8"}),
        ]
        raw = "\\\"漢🙂e\u0301" * 20_000 + "\ud800"

        async def exercise(payload_class, config):
            artifact_input = {}

            async def write_artifact(value):
                artifact_input.update(value)
                return {
                    "artifactId": f"artifact-{payload_class}",
                    "toolName": value["toolName"],
                    "sessionId": value["sessionId"],
                    "family": value["family"],
                    "filePath": f"/tmp/artifact-{payload_class}.txt",
                    "bytes": utf8_byte_length(value["content"]),
                    "lines": 1,
                    "contentType": value["contentType"],
                    "createdAt": "2026-07-17T00:00:00Z",
                }

            guarded = await guard_tool_call_result(
                config=config,
                sessionId=f"session-{payload_class}",
                toolName="custom_output",
                outcome={"kind": "returned", "result": {"content": raw}},
                artifactWriter=write_artifact,
            )
            return guarded, artifact_input

        for payload_class, config in cases:
            guarded, artifact_input = run(exercise(payload_class, config))
            self.assertLessEqual(
                serialized_content_bytes(guarded["result"]["content"]),
                TOOL_RESULT_FINAL_BYTES[payload_class],
            )
            self.assertFalse(has_lone_utf16_surrogate(guarded["result"]["content"]))
            self.assertFalse(guarded["result"].get("isError"))
            self.assertTrue(guarded["truncated"])
            self.assertEqual(artifact_input["content"], raw.replace("\ud800", "�"))

        async def artifact_failure():
            async def fail(_value):
                raise RuntimeError("unbounded /secret/path must not escape")

            return await guard_tool_call_result(
                config=_SMALL_CONFIG,
                sessionId="side-effect-session",
                toolName="mutating_custom_tool",
                outcome={"kind": "returned", "result": {"content": "x" * 100_000}},
                sideEffectsMayHaveOccurred=True,
                artifactWriter=fail,
            )

        guarded = run(artifact_failure())
        payload = json.loads(guarded["result"]["content"])
        self.assertFalse(guarded["result"].get("isError"))
        self.assertTrue(guarded["artifactUnavailable"])
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["sideEffectsMayHaveOccurred"])
        self.assertNotIn("secret/path", guarded["result"]["content"])
