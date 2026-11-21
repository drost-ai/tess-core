"""A tool result is returned whole up to one allowance, the same for every model. A larger one is
saved to a file, and the model gets the file's path and the start of the output, and reads on with Read."""
from __future__ import annotations

import json
from pathlib import Path

import tess_core._facade as core
from tests.support.tess_core_case import TessCoreTestCase, run

from tess_core.payloads.result_guard import guard_tool_call_result
from tess_core.payloads.tools import build_persisted_output


def _guard(content: str, **extra):
    return run(guard_tool_call_result(
        config={"providerKind": "openai", "model": "any-model"},
        sessionId="saved-output-session",
        toolName="custom_output",
        outcome={"kind": "returned", "result": {"content": content}},
        **extra,
    ))


class SavedToolOutputTests(TessCoreTestCase):
    def test_a_result_is_whole_at_the_allowance_and_saved_one_character_past_it(self):
        root, _home, _workspace, previous = self.temp_context()
        try:
            at_limit = _guard("a" * 30_000)
            self.assertFalse(at_limit["truncated"])
            self.assertEqual(at_limit["result"]["content"], "a" * 30_000)

            lines = "".join(f"line {index}\n" for index in range(4_000))
            over = _guard(lines)
            shown = over["result"]["content"]
            self.assertTrue(over["truncated"])
            self.assertFalse(over["result"].get("isError"))
            saved = Path(over["artifact"]["filePath"])
            self.assertEqual(saved.read_text(encoding="utf-8"), lines)
            self.assertEqual(
                shown.split("\n\n", 1)[0],
                f"<persisted-output>\nOutput too large ({len(lines) / 1024:.1f}KB). Full output saved to: {saved}",
            )
            preview = shown.split("Preview (first 2.0KB):\n", 1)[1]
            self.assertTrue(preview.startswith("line 0\nline 1\n"))
            self.assertTrue(preview.endswith("\n...\n</persisted-output>"))
            # Only the start is shown: nothing from the end of the output reaches the model.
            self.assertNotIn("line 3999", shown)
            self.assertLess(len(shown), 2_400)
        finally:
            self.restore_home(root, previous)

    def test_the_allowance_does_not_depend_on_the_model(self):
        root, _home, _workspace, previous = self.temp_context()
        try:
            content = "b" * 29_000
            for config in (
                {"providerKind": "anthropic", "model": "claude-opus"},
                {"providerKind": "openai", "openaiBaseUrl": "https://api.example.test", "model": "deepseek-flash"},
                {"providerKind": "openai", "openaiApiMode": "codex", "model": "gpt-sol", "providerProfile": {"apiMode": "codex"}},
            ):
                guarded = run(guard_tool_call_result(
                    config=config, sessionId="saved-output-session", toolName="custom_output",
                    outcome={"kind": "returned", "result": {"content": content}},
                ))
                self.assertEqual(guarded["result"]["content"], content)
        finally:
            self.restore_home(root, previous)

    def test_a_tool_may_declare_its_own_allowance(self):
        root, _home, _workspace, previous = self.temp_context()
        try:
            content = "c" * 90_000
            self.assertTrue(_guard(content)["truncated"])
            self.assertTrue(_guard(content, maxChars=50_000)["truncated"])
            self.assertEqual(_guard(content, maxChars=100_000)["result"]["content"], content)
            # 0 is for a tool that bounds its own results: only the absolute ceiling holds it.
            self.assertEqual(_guard(content, maxChars=0)["result"]["content"], content)
            self.assertTrue(_guard("d" * 600_000, maxChars=0)["truncated"])
            with self.assertRaisesRegex(ValueError, "non-negative integer"):
                _guard(content, maxChars=-1)
        finally:
            self.restore_home(root, previous)

    def test_a_host_tool_gets_the_default_and_the_agent_reads_the_rest_with_read(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            self.assertIsNone(core.ToolDefinition("t", "t", {"type": "object"}, None).maxResultChars)
            builtins = {tool.name: tool for tool in core.create_builtin_tools()}
            self.assertNotIn("ReadToolArtifact", builtins)
            self.assertEqual({tool.maxResultChars for tool in builtins.values()}, {0})

            numbered = "".join(f"row {index:05}\n" for index in range(5_000))  # 50,000 characters
            guarded = _guard(numbered)
            saved = guarded["artifact"]["filePath"]
            config = {"cwd": str(workspace), "runtimeHome": str(home), "allowedRoots": [str(workspace)]}
            session = core.create_empty_session(cwd=str(workspace), provider="openai", model="m", systemPrompt="s", permissionMode="bypassPermissions")
            context = {"session": session, "config": config, "cwd": str(workspace)}
            first = json.loads(run(builtins["Read"].handler({"file_path": saved}, context))["content"])["file"]
            self.assertEqual(first["numLines"], 2_000)  # a read's default is 2,000 lines
            self.assertTrue(first["content"].startswith("row 00000\n"))
            self.assertEqual(first["nextOffset"], 2_001)
            rest = json.loads(run(builtins["Read"].handler({"file_path": saved, "offset": 2_001, "limit": 3_000}, context))["content"])["file"]
            self.assertEqual(first["content"] + "\n" + rest["content"] + "\n", numbered)
        finally:
            self.restore_home(root, previous)

    def test_a_read_over_its_token_limit_is_refused_not_cut(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            wide = workspace / "wide.txt"
            wide.write_text("".join(f"{index:04} " + "w" * 195 + "\n" for index in range(1_500)), encoding="utf-8")  # 301,500 bytes
            config = {"cwd": str(workspace), "runtimeHome": str(home), "allowedRoots": [str(workspace)]}
            session = core.create_empty_session(cwd=str(workspace), provider="openai", model="m", systemPrompt="s", permissionMode="bypassPermissions")
            context = {"session": session, "config": config, "cwd": str(workspace)}
            read = next(tool for tool in core.create_builtin_tools() if tool.name == "Read")

            refused = run(read.handler({"file_path": str(wide)}, context))
            self.assertTrue(refused["isError"])
            self.assertTrue(refused["content"].startswith(
                "File content (75375 tokens) exceeds maximum allowed tokens (25000). Use offset and limit "
                "parameters to read specific portions of the file, or search for specific content instead "
                "of reading the whole file."
            ))
            self.assertNotIn("wwww", refused["content"])  # none of the file is returned with a refusal

            page = json.loads(run(read.handler({"file_path": str(wide), "offset": 1, "limit": 400}, context))["content"])["file"]
            self.assertEqual(page["numLines"], 400)
            self.assertFalse(run(read.handler({"file_path": str(wide), "offset": 1_101, "limit": 400}, context)).get("isError"))
        finally:
            self.restore_home(root, previous)

    def test_the_notice_says_so_when_the_output_could_not_be_saved(self):
        notice = build_persisted_output("e" * 40_000, 40_000, None)
        self.assertIn("The full output could not be saved; only this preview is available.", notice)
        self.assertNotIn("saved to:", notice)
