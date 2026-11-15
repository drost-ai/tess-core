from __future__ import annotations

from tests.support.tess_core_case import *

import sys

from tess_core.payloads.cursor import (
    encode_payload_cursor,
    fingerprint_payload_source,
    resolve_payload_cursor,
)
from tess_core.mcp.payloads import shape_mcp_resource_list, shape_mcp_resource_read
from tess_core.acquisition.search import run_streaming_search
from tess_core.payloads.result_guard import TOOL_RESULT_FINAL_BYTES, serialized_content_bytes
from tess_core.payloads.utf8_bounds import slice_utf8_window, utf8_byte_length


class TextInlineBudgetCalibrationTests(TessCoreTestCase):
    def test_short_lined_text_within_byte_budget_returns_inline_full(self):
        # Field case: a stripped web page of ~7KB across ~300 short lines
        # (small class, 8KB budget) must NOT be excerpted — the old 140-line
        # cap cut it at ~3.2KB and cost artifact round trips for content
        # that fit inline.
        from tess_core.payloads.tools import TOOL_PAYLOAD_POLICIES, shape_text_tool_result

        root, _home, workspace, previous = self.temp_context()
        try:
            small_config = {"providerKind": "openai", "openaiBaseUrl": "https://api.example.test", "model": "deepseek-v4-flash"}
            text = "\n".join(f"nav item {index} · short line" for index in range(300))
            self.assertLess(len(text.encode("utf-8")), TOOL_PAYLOAD_POLICIES["small"]["textInlineBytes"])
            shaped = run(shape_text_tool_result(
                config=small_config, sessionId="calibration-session",
                toolName="WebFetch", family="web", text=text, strategy="head",
            ))
            self.assertEqual(shaped["view"]["strategy"], "inline_full")
            self.assertFalse(shaped["view"]["truncated"])
            self.assertEqual(shaped["text"], text)

            for policy in TOOL_PAYLOAD_POLICIES.values():
                # Byte budgets stay the binding constraint for short lines.
                self.assertGreaterEqual(policy["textInlineLines"] * 20, policy["textInlineBytes"])
                self.assertGreaterEqual((policy["textHeadLines"] + policy["textTailLines"]) * 20, policy["textInlineBytes"] // 2)
        finally:
            self.restore_home(root, previous)


class Package31PayloadPaginationTests(TessCoreTestCase):
    def test_payload_cursor_and_utf8_windows_are_bound_and_progressive(self):
        fingerprint = fingerprint_payload_source(["fixture", "漢🙂", "3"])
        encoded = encode_payload_cursor(
            family="read_text",
            sourceFingerprint=fingerprint,
            position={
                "recordOffset": 7,
                "byteOffset": 11,
                "sourceByteOffset": 101,
                "recordBytes": 900,
            },
        )
        self.assertTrue(encoded.startswith("acp1."))
        self.assertEqual(
            resolve_payload_cursor(
                family="read_text",
                sourceFingerprint=fingerprint,
                cursor=encoded,
                legacyBase=1,
            ),
            {
                "recordOffset": 7,
                "byteOffset": 11,
                "sourceByteOffset": 101,
                "recordBytes": 900,
                "source": "cursor",
            },
        )
        with self.assertRaisesRegex(ValueError, "does not belong to grep"):
            resolve_payload_cursor(
                family="grep",
                sourceFingerprint=fingerprint,
                cursor=encoded,
                legacyBase=0,
            )
        with self.assertRaisesRegex(ValueError, "source changed"):
            resolve_payload_cursor(
                family="read_text",
                sourceFingerprint=fingerprint_payload_source(["changed"]),
                cursor=encoded,
                legacyBase=1,
            )
        with self.assertRaisesRegex(ValueError, "cursor or offset, not both"):
            resolve_payload_cursor(
                family="read_text",
                sourceFingerprint=fingerprint,
                cursor=encoded,
                legacyOffset=8,
                legacyBase=1,
            )
        first = slice_utf8_window("漢🙂e\u0301", 0, 3)
        self.assertEqual(first["value"], "漢")
        second = slice_utf8_window("漢🙂e\u0301", first["nextByteOffset"], 4)
        self.assertEqual(second["value"], "🙂")
        forced = slice_utf8_window("🙂tail", 0, 1)
        self.assertEqual(forced["value"], "")
        self.assertEqual(forced["skippedBytes"], 4)
        self.assertEqual(forced["nextByteOffset"], 4)

    def test_zero_filled_cursor_and_offset_inputs_are_treated_as_absent(self):
        # Zero-filling providers (Codex-family models) populate every optional
        # schema field instead of omitting it: an unused cursor arrives as ""
        # or 0 and an unused offset as 0. Neither may trip the cursor/offset
        # mutual-exclusion guard.
        fingerprint = fingerprint_payload_source(["zero-fill", "fixture"])
        encoded = encode_payload_cursor(
            family="read_text",
            sourceFingerprint=fingerprint,
            position={"recordOffset": 7},
        )
        for empty_cursor in (None, "", 0, False):
            self.assertEqual(
                resolve_payload_cursor(
                    family="read_text",
                    sourceFingerprint=fingerprint,
                    cursor=empty_cursor,
                    legacyOffset=0,
                    legacyBase=1,
                ),
                {"recordOffset": 0, "source": "legacy"},
            )
        resolved = resolve_payload_cursor(
            family="read_text",
            sourceFingerprint=fingerprint,
            cursor=encoded,
            legacyOffset=0,
            legacyBase=1,
        )
        self.assertEqual(resolved["recordOffset"], 7)
        self.assertEqual(resolved["source"], "cursor")
        with self.assertRaisesRegex(ValueError, "cursor or offset, not both"):
            resolve_payload_cursor(
                family="read_text",
                sourceFingerprint=fingerprint,
                cursor=encoded,
                legacyOffset=1,
                legacyBase=1,
            )
        with self.assertRaisesRegex(ValueError, "Invalid read_text cursor"):
            resolve_payload_cursor(
                family="read_text",
                sourceFingerprint=fingerprint,
                cursor=5,
                legacyBase=1,
            )

    def test_read_tool_accepts_codex_zero_filled_pagination_input(self):
        root, home, workspace, previous = self.temp_context()
        try:
            target = workspace / "conversation.txt"
            target.write_text("first line\nsecond line\n", encoding="utf-8")
            context = self._context(home, workspace, "zero-fill-read")
            read = next(tool for tool in core.create_builtin_tools() if tool.name == "Read")
            # Exact input shapes observed from a Codex-provider session.
            for zero_filled in (
                {"file_path": str(target), "offset": 0, "cursor": 0, "limit": 500, "pages": "1"},
                {"file_path": str(target), "offset": 0, "cursor": "", "limit": 500, "pages": "1-1"},
                {"file_path": str(target), "offset": 0, "cursor": "", "limit": 500, "pages": ""},
            ):
                shaped = self._parse(run(read.handler(zero_filled, context)))
                self.assertEqual(shaped["file"]["startLine"], 1)
                self.assertIn("first line", shaped["file"]["content"])
                self.assertIn("second line", shaped["file"]["content"])
        finally:
            self.restore_home(root, previous)

    def test_read_cursor_reconstructs_oversized_unicode_line(self):
        root, home, workspace, previous = self.temp_context()
        try:
            line = "漢🙂e\u0301" * 8_000
            tail = "second🙂"
            target = workspace / "long-line.txt"
            target.write_text(f"{line}\n{tail}", encoding="utf-8")
            context = self._context(home, workspace, "read-pagination")
            read = next(tool for tool in core.create_builtin_tools() if tool.name == "Read")

            first = self._parse(
                run(read.handler({"file_path": str(target), "offset": 1, "limit": 1}, context))
            )
            self.assertEqual(first["file"]["oversizedLine"]["originalBytes"], utf8_byte_length(line))
            self.assertEqual(first["file"]["nextOffset"], 2)
            self.assertLessEqual(first["file"]["selectedBytes"], 18_000)
            reconstructed = first["file"]["content"]
            cursor = first["file"]["nextCursor"]
            tail_cursor = None
            pages = 1
            while cursor and pages < 20:
                page = self._parse(
                    run(read.handler({"file_path": str(target), "cursor": cursor, "limit": 1}, context))
                )
                pages += 1
                self.assertLessEqual(page["file"]["selectedBytes"], 18_000)
                self.assertEqual(page["file"]["startLine"], 1)
                reconstructed += page["file"]["content"]
                cursor = page["file"].get("nextCursor")
                if "oversizedLine" not in page["file"]:
                    tail_cursor = cursor
                    break
            self.assertGreater(pages, 2)
            self.assertLess(pages, 20)
            self.assertEqual(reconstructed, line)
            self.assertIsNotNone(tail_cursor)
            tail_page = self._parse(
                run(read.handler({"file_path": str(target), "cursor": tail_cursor, "limit": 1}, context))
            )
            self.assertEqual(tail_page["file"]["content"], tail)
            legacy = self._parse(
                run(read.handler({"file_path": str(target), "offset": first["file"]["nextOffset"], "limit": 1}, context))
            )
            self.assertEqual(legacy["file"]["content"], tail)
            with self.assertRaisesRegex(ValueError, "cursor or offset, not both"):
                run(
                    read.handler(
                        {
                            "file_path": str(target),
                            "offset": 1,
                            "cursor": first["file"]["nextCursor"],
                        },
                        context,
                    )
                )
            target.write_text(f"{line}\n{tail}\nchanged", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source changed"):
                run(read.handler({"file_path": str(target), "cursor": first["file"]["nextCursor"]}, context))
        finally:
            self.restore_home(root, previous)

    def test_glob_and_grep_cursors_are_snapshot_stable(self):
        root, home, workspace, previous = self.temp_context()
        try:
            directory = workspace / "paged"
            directory.mkdir()
            for index in range(7):
                (directory / f"漢🙂-{index}.txt").write_text(f"needle🙂-{index}\n", encoding="utf-8")
            context = self._context(home, workspace, "search-pagination")
            tools = {tool.name: tool for tool in core.create_builtin_tools()}
            glob_matches = self._collect_pages(
                tools["Glob"],
                {"pattern": "*.txt", "path": str(directory), "offset": 0, "limit": 2},
                context,
            )
            self.assertEqual(len(glob_matches), 7)
            self.assertEqual(len(set(glob_matches)), 7)
            stable = self._parse(
                run(tools["Glob"].handler({"pattern": "*.txt", "path": str(directory), "limit": 2}, context))
            )
            added = directory / "added-after-cursor.txt"
            added.write_text("new\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "results changed.*restart pagination"):
                run(
                    tools["Glob"].handler(
                        {"pattern": "*.txt", "path": str(directory), "cursor": stable["nextCursor"], "limit": 2},
                        context,
                    )
                )
            added.unlink()
            grep_matches = self._collect_pages(
                tools["Grep"],
                {"pattern": "needle", "path": str(directory), "output_mode": "content", "offset": 0, "head_limit": 2},
                context,
            )
            self.assertEqual(len(grep_matches), 7)
            self.assertEqual(len(set(grep_matches)), 7)
        finally:
            self.restore_home(root, previous)

    def test_notebook_cursor_returns_each_cell_once(self):
        root, home, workspace, previous = self.temp_context()
        try:
            target = workspace / "paged.ipynb"
            target.write_text(
                json.dumps(
                    {
                        "nbformat": 4,
                        "nbformat_minor": 5,
                        "metadata": {},
                        "cells": [
                            {"cell_type": "markdown", "source": [f"cell-{index} 漢🙂"], "metadata": {}}
                            for index in range(5)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            context = self._context(home, workspace, "notebook-pagination")
            read = next(tool for tool in core.create_builtin_tools() if tool.name == "Read")
            indexes = []
            current = {"file_path": str(target), "offset": 0, "limit": 1}
            for _ in range(10):
                page = self._parse(run(read.handler(current, context)))
                indexes.extend(cell["index"] for cell in page["file"]["cells"])
                if "nextCursor" not in page["file"]:
                    break
                current = {
                    "file_path": str(target),
                    "cursor": page["file"]["nextCursor"],
                    "limit": 1,
                }
            self.assertEqual(indexes, [0, 1, 2, 3, 4])
        finally:
            self.restore_home(root, previous)

    def test_mcp_resource_cursors_resume_bounded_unicode_pages(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolve_config({"provider": "anthropic", "runtimeHome": str(home)}, str(workspace)))
            context = {"config": {**config, "providerKind": "anthropic"}, "sessionId": "mcp-pagination"}
            resources = [
                {"server": "srv", "uri": f"fixture://{index}", "name": f"資源🙂-{index}"}
                for index in range(7)
            ]
            listed = []
            current = {"resources": resources, "context": context, "offset": 0, "requestedLimit": 2}
            for _ in range(10):
                page = run(shape_mcp_resource_list(**current))
                listed.extend(resource["uri"] for resource in page["resources"])
                if "nextCursor" not in page:
                    break
                current = {
                    "resources": resources,
                    "context": context,
                    "cursor": page["nextCursor"],
                    "requestedLimit": 2,
                }
            self.assertEqual(listed, [resource["uri"] for resource in resources])

            text = "漢🙂e\u0301" * 4_000
            value = {"contents": [{"uri": "fixture://huge", "mimeType": "text/plain", "text": text}]}
            page = run(
                shape_mcp_resource_read(
                    server="srv",
                    uri="fixture://huge",
                    value=value,
                    context=context,
                    offset=0,
                    requestedLimit=1,
                )
            )
            first_next_offset = page["nextOffset"]
            reconstructed = page["contents"][0]["text"]
            pages = 1
            while "nextCursor" in page and pages < 30:
                page = run(
                    shape_mcp_resource_read(
                        server="srv",
                        uri="fixture://huge",
                        value=value,
                        context=context,
                        cursor=page["nextCursor"],
                        requestedLimit=1,
                    )
                )
                pages += 1
                reconstructed += page["contents"][0].get("text", "") if page["contents"] else ""
                self.assertLessEqual(serialized_content_bytes(json.dumps(page, ensure_ascii=False)), TOOL_RESULT_FINAL_BYTES["large"])
            self.assertGreater(pages, 2)
            self.assertLess(pages, 30)
            self.assertEqual(reconstructed, text)
            self.assertEqual(first_next_offset, 1)
            legacy = run(
                shape_mcp_resource_read(
                    server="srv",
                    uri="fixture://huge",
                    value=value,
                    context=context,
                    offset=first_next_offset,
                    requestedLimit=1,
                )
            )
            self.assertEqual(legacy["returnedContents"], 0)
            self.assertNotIn("nextCursor", legacy)
        finally:
            self.restore_home(root, previous)

    def test_search_record_and_spool_limits_are_bounded(self):
        root, home, workspace, previous = self.temp_context()
        try:
            huge = workspace / "huge.txt"
            huge.write_text(f"needle🙂{'x' * 1_100_000}\n", encoding="utf-8")
            context = self._context(home, workspace, "huge-search")
            grep = next(tool for tool in core.create_builtin_tools() if tool.name == "Grep")
            payload = self._parse(
                run(grep.handler({"pattern": "needle", "path": str(workspace), "output_mode": "content", "head_limit": 1}, context))
            )
            self.assertEqual(payload["totalMatches"], 1)
            self.assertEqual(payload["advancedMatches"], 1)
            self.assertEqual(payload["returnedMatches"], 1)
            self.assertGreaterEqual(payload["fragmentedRecords"], 1)
            self.assertGreaterEqual(payload["oversizedRecords"], 1)
            self.assertNotIn("nextCursor", payload)
            self.assertTrue(Path(payload["artifact"]["filePath"]).exists())
            self.assertLessEqual(serialized_content_bytes(json.dumps(payload, ensure_ascii=False)), TOOL_RESULT_FINAL_BYTES["large"])

            result = run(
                run_streaming_search(
                    binary=sys.executable,
                    argv=["-c", "import sys; sys.stdout.write(('needle-' + 'z'*1000 + '\\n')*2000)"],
                    cwd=str(workspace),
                    sessionId="search-limit-session",
                    toolName="Grep",
                    family="grep",
                    contentType="text",
                    offset=0,
                    sampleItems=20,
                    sampleBytes=4_000,
                    maxSpoolBytes=64 * 1024,
                    maxRingBytes=2_048,
                )
            )
            self.assertTrue(result["outputLimitExceeded"])
            self.assertTrue(result["processGroupDrained"])
            self.assertLessEqual(result["spoolBytes"], 64 * 1024)
            self.assertLessEqual(result["ringBytes"], 2_048)
            self.assertLessEqual(len(result["matches"]), 20)
            self.assertTrue(result["stdoutArtifact"]["truncated"])
            self.assertLessEqual(Path(result["stdoutArtifact"]["filePath"]).stat().st_size, 64 * 1024)
        finally:
            self.restore_home(root, previous)

    def test_search_streams_beyond_legacy_ten_megabyte_buffer(self):
        root, home, workspace, previous = self.temp_context()
        try:
            result = run(
                run_streaming_search(
                    binary=sys.executable,
                    argv=["-c", "import sys; sys.stdout.write(('needle-' + 'y'*1000 + '\\n')*11000)"],
                    cwd=str(workspace),
                    sessionId="large-search-session",
                    toolName="Grep",
                    family="grep",
                    contentType="text",
                    offset=0,
                    sampleItems=160,
                    sampleBytes=18_000,
                )
            )
            self.assertFalse(result["outputLimitExceeded"])
            self.assertEqual(result["totalMatches"], 11_000)
            self.assertGreater(result["stdoutBytes"], 10 * 1024 * 1024)
            self.assertLessEqual(len(result["matches"]), 160)
            self.assertGreater(Path(result["stdoutArtifact"]["filePath"]).stat().st_size, 10 * 1024 * 1024)
        finally:
            self.restore_home(root, previous)

    def _context(self, home, workspace, session_id):
        config = run(core.resolve_config({"provider": "anthropic", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
        session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions", sessionId=session_id)
        return {"session": session, "config": {**config, "providerKind": "anthropic"}, "interactive": False, "cwd": str(workspace), "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "persist_session": lambda: None}

    def _collect_pages(self, tool, initial, context):
        matches = []
        current = initial
        for _ in range(20):
            page = self._parse(run(tool.handler(current, context)))
            matches.extend(page["matches"])
            if "nextCursor" not in page:
                return matches
            current = {key: value for key, value in current.items() if key not in ("offset", "cursor")}
            current["cursor"] = page["nextCursor"]
        self.fail("pagination did not terminate")

    def _parse(self, result):
        self.assertFalse(result.get("isError"), result.get("content"))
        return json.loads(result["content"])
