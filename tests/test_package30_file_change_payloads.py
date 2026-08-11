from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, bind_test_runtime, core, run
from agent_core._utils import pretty_json


diff_module = importlib.import_module("agent_core.tools.file_change_diff")
payload_module = importlib.import_module("agent_core.tools.file_change_payloads")


class Package30FileChangePayloadTests(AgentCoreTestCase):
    def _context(self, home: Path, workspace: Path):
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
        context = {
            "session": session,
            "config": config,
            "interactive": False,
            "askQuestion": lambda *_: "",
            "outputJson": lambda *_: None,
            "cwd": str(workspace),
            "persist_session": lambda: None,
        }
        return config, session, context, {tool.name: tool for tool in core.create_builtin_tools()}

    @staticmethod
    def _diff_counts(diff: str) -> tuple[int, int, int]:
        lines = diff.splitlines()
        return (
            sum(bool(re.match(r"^\s*\d+ \+ ", line)) for line in lines),
            sum(bool(re.match(r"^\s*\d+ - ", line)) for line in lines),
            sum(bool(re.match(r"^\s*\d+   ", line)) for line in lines),
        )

    def test_old_lcs_threshold_and_hundred_thousand_line_edit_stay_local(self):
        before = "\n".join(
            "const value = 'before';" if index == 500 else f"line-{index}"
            for index in range(1_001)
        )
        after = before.replace("const value = 'before';", "const value = 'after';")
        self.assertEqual(
            self._diff_counts(diff_module.build_unified_diff("large.ts", before, after)),
            (1, 1, 6),
        )

        huge_before = "\n".join(
            "needle-before" if index == 50_000 else f"line-{index}"
            for index in range(100_000)
        )
        huge_after = huge_before.replace("needle-before", "needle-after")
        document = diff_module.build_edit_diff_document(
            {
                "beforeText": huge_before,
                "afterText": huge_after,
                "oldString": "needle-before",
                "newString": "needle-after",
                "replaceAll": False,
            }
        )
        self.assertEqual((document["addedLines"], document["removedLines"]), (1, 1))
        self.assertEqual(document["strategy"], "exact_edit_hunks")
        self.assertLess(len(document["diff"].encode()), 8_000)

    def test_field_shaped_edit_reports_exact_counts_and_final_bounds(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, _session, context, tools = self._context(home, workspace)
            path = workspace / "large.ts"
            lines = [f"line-{index}" for index in range(3_786)]
            old_lines = lines[1_800:1_812]
            new_lines = [f"replacement-{index}" for index in range(18)]
            path.write_text("\n".join(lines), encoding="utf-8")
            result = run(
                tools["Edit"].handler(
                    {
                        "file_path": str(path),
                        "old_string": "\n".join(old_lines),
                        "new_string": "\n".join(new_lines),
                    },
                    context,
                )
            )
            payload = json.loads(result["content"])
            self.assertLessEqual(len(result["content"].encode()), 12_000)
            self.assertEqual(payload["fileBytes"], len(path.read_bytes()))
            self.assertEqual((payload["addedLines"], payload["removedLines"]), (18, 12))
            self.assertEqual(payload["diffStrategy"], "exact_edit_hunks")
            self.assertFalse(payload["diffView"]["truncated"])
            self.assertLessEqual(payload["diffView"]["returnedLines"], 120)
        finally:
            self.restore_home(root, previous)

    def test_replace_all_reports_separated_exact_hunks(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, _session, context, tools = self._context(home, workspace)
            path = workspace / "repeated.ts"
            match_indexes = {100, 1_000, 1_900}
            before = "\n".join(
                "const marker = 'before';" if index in match_indexes else f"line-{index}"
                for index in range(2_100)
            )
            path.write_text(before, encoding="utf-8")
            payload = json.loads(
                run(
                    tools["Edit"].handler(
                        {
                            "file_path": str(path),
                            "old_string": "const marker = 'before';",
                            "new_string": "const marker = 'after';",
                            "replace_all": True,
                        },
                        context,
                    )
                )["content"]
            )
            self.assertEqual((payload["addedLines"], payload["removedLines"]), (3, 3))
            self.assertEqual(payload["diff"].splitlines().count("..."), 2)
            self.assertEqual(path.read_text().count("'after'"), 3)
        finally:
            self.restore_home(root, previous)

    def test_large_create_and_broad_write_spill_accurate_artifacts(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, _session, context, tools = self._context(home, workspace)
            path = workspace / "broad.ts"
            created = "\n".join(f"created-{index}" for index in range(2_000))
            create_result = run(tools["Write"].handler({"file_path": str(path), "content": created}, context))
            create = json.loads(create_result["content"])
            self.assertEqual((create["diffStrategy"], create["addedLines"], create["removedLines"]), ("create", 2_000, 0))
            self.assertTrue(create["diffView"]["truncated"])
            self.assertLessEqual(len(create_result["content"].encode()), 12_000)
            self.assertLessEqual(create["diffView"]["returnedBytes"], 8_000)
            self.assertLessEqual(create["diffView"]["returnedLines"], 120)
            create_artifact = Path(create["diffView"]["artifact"]["filePath"]).read_text()
            self.assertRegex(create_artifact, r"(?m)^\s*2000 \+ created-1999$")

            replaced = "\n".join(f"replaced-{index}" for index in range(2_000))
            update_result = run(tools["Write"].handler({"file_path": str(path), "content": replaced}, context))
            update = json.loads(update_result["content"])
            self.assertEqual((update["diffStrategy"], update["addedLines"], update["removedLines"]), ("changed_region_write_diff", 2_000, 2_000))
            artifact = Path(update["diffView"]["artifact"]["filePath"]).read_text()
            self.assertRegex(artifact, r"(?m)^\s*1 - created-0$")
            self.assertRegex(artifact, r"(?m)^\s*2000 \+ replaced-1999$")
        finally:
            self.restore_home(root, previous)

    def test_json_escaping_unicode_and_artifact_failure_remain_bounded_success(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, context, tools = self._context(home, workspace)
            path = workspace / "escaped.ts"
            content = "\n".join(
                f'const escaped{index} = "\\\\\\"😀{index}";'
                for index in range(500)
            )
            result = run(tools["Write"].handler({"file_path": str(path), "content": content}, context))
            payload = json.loads(result["content"])
            self.assertLessEqual(len(result["content"].encode()), 12_000)
            self.assertTrue(payload["diffView"]["truncated"])
            self.assertTrue(payload["diffView"]["artifact"]["filePath"])

            async def unavailable(_args):
                raise OSError("injected artifact failure")

            degraded = run(
                payload_module.build_write_file_change_payload(
                    config=config,
                    sessionId=session["id"],
                    filePath=str(workspace / "unavailable.ts"),
                    previousContent=None,
                    nextContent="\n".join(f"created-{index}" for index in range(2_000)),
                    artifactWriter=unavailable,
                )
            )
            serialized = pretty_json(degraded)
            self.assertLessEqual(len(serialized.encode()), 12_000)
            self.assertTrue(degraded["diffView"]["truncated"])
            self.assertTrue(degraded["diffView"]["artifactUnavailable"])
            self.assertNotIn("artifact", degraded["diffView"])
            self.assertIn("could not be persisted", degraded["warning"])

            original_writer = payload_module._persist_artifact
            mutation_path = workspace / "mutation-survives.ts"
            try:
                payload_module._persist_artifact = unavailable
                mutation_result = run(
                    tools["Write"].handler(
                        {
                            "file_path": str(mutation_path),
                            "content": "\n".join(
                                f"survives-{index}" for index in range(2_000)
                            ),
                        },
                        context,
                    )
                )
            finally:
                payload_module._persist_artifact = original_writer
            self.assertFalse(mutation_result.get("isError"))
            self.assertTrue(mutation_path.exists())
            self.assertTrue(
                json.loads(mutation_result["content"])["diffView"][
                    "artifactUnavailable"
                ]
            )
        finally:
            self.restore_home(root, previous)

    def test_agent_loop_next_request_and_archive_keep_only_bounded_result(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, _context, tools = self._context(home, workspace)
            path = workspace / "loop-large.ts"
            lines = [f"line-{index}" for index in range(3_786)]
            old_lines = lines[1_800:1_812]
            new_lines = [f"replacement-{index}" for index in range(18)]
            path.write_text("\n".join(lines), encoding="utf-8")
            calls: list[dict] = []

            async def send(request):
                calls.append(request)
                if len(calls) == 1:
                    return {
                        "model": config["model"],
                        "stopReason": "tool_use",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "edit-large",
                                "name": "Edit",
                                "input": {
                                    "file_path": str(path),
                                    "old_string": "\n".join(old_lines),
                                    "new_string": "\n".join(new_lines),
                                },
                            }
                        ],
                    }
                return {
                    "model": config["model"],
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "done"}],
                }

            run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, {
                        "interactive": False,
                        "askQuestion": lambda *_: "",
                        "emitEvent": lambda *_: None,
                        "tools": [tools["Edit"]],
                    }),
                    prompt="edit it",
                    providerOverride={"send": send},
                )
            )
            request_result = next(
                block["content"]
                for message in calls[1]["messages"]
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            )
            self.assertLessEqual(len(request_result.encode()), 12_000)
            request_payload = json.loads(request_result)
            self.assertEqual((request_payload["addedLines"], request_payload["removedLines"]), (18, 12))

            archived = [json.loads(line) for line in Path(core.session_transcript_log_path(session["id"])).read_text().splitlines()]
            archived_result = next(
                block["content"]
                for entry in archived
                if isinstance(entry.get("content"), list)
                for block in entry["content"]
                if block.get("type") == "tool_result"
            )
            self.assertLessEqual(len(archived_result.encode()), 12_000)
            self.assertEqual(archived_result, request_result)
        finally:
            self.restore_home(root, previous)
