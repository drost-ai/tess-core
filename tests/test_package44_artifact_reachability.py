from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import agent_core as core
from agent_core.payloads.tools import _persist_artifact
from tests.support.agent_core_case import AgentCoreTestCase, bind_test_runtime, run
from tests.test_package38_interactive_context_relief import _codex_config


class Package44ArtifactReachabilityTests(AgentCoreTestCase):
    def _session(self, workspace: Path) -> dict[str, Any]:
        return core.createEmptySession(
            cwd=str(workspace),
            provider="openai",
            providerProfileName="codex",
            model="gpt-5.6-sol",
            effort="xhigh",
            systemPrompt="stable identity",
            permissionMode="bypassPermissions",
        )

    def _reader(self):
        return next(
            tool
            for tool in core.createBuiltinTools()
            if tool.name == "ReadToolArtifact"
        )

    def _context(
        self,
        session: dict[str, Any],
        config: dict[str, Any],
        workspace: Path,
    ) -> dict[str, Any]:
        return {
            "session": session,
            "config": config,
            "cwd": str(workspace),
            "allowedRoots": [str(workspace)],
            "outputJson": lambda *_: None,
        }

    def test_pages_reconstruct_unicode_exactly_without_new_artifacts(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            session = self._session(workspace)
            content = ("αβγ artifact evidence\n" * 4_000) + "terminal"
            artifact = run(
                _persist_artifact(
                    {
                        "sessionId": session["id"],
                        "toolName": "Bash",
                        "family": "bash",
                        "content": content,
                        "contentType": "text",
                    }
                )
            )
            artifact_dir = Path(artifact["filePath"]).parent
            original_files = sorted(path.name for path in artifact_dir.iterdir())
            config = _codex_config(home, workspace)
            chunks: list[str] = []
            offset = 0
            while True:
                result = run(
                    self._reader().handler(
                        {
                            "artifact_id": artifact["artifactId"],
                            "offset_bytes": offset,
                            "max_bytes": 16_384,
                        },
                        self._context(session, config, workspace),
                    )
                )
                payload = json.loads(result["content"])
                if offset == 0:
                    repeated = run(
                        self._reader().handler(
                            {
                                "artifact_id": artifact["artifactId"],
                                "offset_bytes": 0,
                                "max_bytes": 16_384,
                            },
                            self._context(session, config, workspace),
                        )
                    )
                    self.assertEqual(json.loads(repeated["content"]), payload)
                chunks.append(payload["content"])
                if payload["complete"]:
                    break
                self.assertGreater(payload["nextOffsetBytes"], offset)
                offset = payload["nextOffsetBytes"]
            self.assertEqual("".join(chunks), content)
            self.assertEqual(
                sorted(path.name for path in artifact_dir.iterdir()),
                original_files,
            )
        finally:
            self.restore_home(root, previous)

    def test_full_loop_page_bypasses_recursive_artifactization(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            config = _codex_config(home, workspace)
            config["tools"] = config["allowedTools"] = ["ReadToolArtifact"]
            session = self._session(workspace)
            artifact = run(
                _persist_artifact(
                    {
                        "sessionId": session["id"],
                        "toolName": "Bash",
                        "family": "bash",
                        "content": "x" * 80_000,
                        "contentType": "text",
                    }
                )
            )
            artifact_dir = Path(artifact["filePath"]).parent
            original_files = sorted(path.name for path in artifact_dir.iterdir())
            requests: list[dict[str, Any]] = []

            async def send(request: dict[str, Any]) -> dict[str, Any]:
                requests.append(copy.deepcopy(request))
                if len(requests) == 1:
                    return {
                        "model": "gpt-5.6-sol",
                        "stopReason": "tool_use",
                        "usage": {},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "page-1",
                                "name": "ReadToolArtifact",
                                "input": {"artifact_id": artifact["artifactId"]},
                            }
                        ],
                    }
                return {
                    "model": "gpt-5.6-sol",
                    "stopReason": "end_turn",
                    "usage": {},
                    "content": [{"type": "text", "text": "done"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {"tools": core.createBuiltinTools()},
                    ),
                    prompt="read artifact",
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
            page = json.loads(blocks[0]["content"])
            self.assertEqual(page["type"], "tool_artifact_page")
            self.assertNotIn("artifact", page)
            self.assertEqual(
                sorted(path.name for path in artifact_dir.iterdir()),
                original_files,
            )
        finally:
            self.restore_home(root, previous)

    def test_authority_traversal_and_symlink_edges_fail_closed(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            config = _codex_config(home, workspace)
            owner, other = self._session(workspace), self._session(workspace)
            artifact = run(
                _persist_artifact(
                    {
                        "sessionId": owner["id"],
                        "toolName": "Bash",
                        "family": "bash",
                        "content": '{"evidence":"α"}',
                        "contentType": "json",
                    }
                )
            )
            reader = self._reader()
            foreign = run(
                reader.handler(
                    {"artifact_id": artifact["artifactId"]},
                    self._context(other, config, workspace),
                )
            )
            traversal = run(
                reader.handler(
                    {"artifact_id": "../journal.sqlite3"},
                    self._context(owner, config, workspace),
                )
            )
            self.assertEqual(json.loads(foreign["content"])["code"], "artifact_not_found")
            self.assertEqual(
                json.loads(traversal["content"])["code"],
                "artifact_request_invalid",
            )
            data_path = Path(artifact["filePath"])
            replacement = data_path.with_name("unregistered.json")
            replacement.write_text('{"tampered":true}', encoding="utf-8")
            data_path.unlink()
            os.symlink(replacement, data_path)
            symlinked = run(
                reader.handler(
                    {"artifact_id": artifact["artifactId"]},
                    self._context(owner, config, workspace),
                )
            )
            self.assertTrue(symlinked["isError"])
            self.assertEqual(
                json.loads(symlinked["content"])["code"],
                "artifact_read_failed",
            )
            metadata_artifact = run(
                _persist_artifact(
                    {
                        "sessionId": owner["id"],
                        "toolName": "Bash",
                        "family": "bash",
                        "content": "metadata target",
                        "contentType": "text",
                    }
                )
            )
            metadata_path = (
                Path(metadata_artifact["filePath"])
                .with_name(f"{metadata_artifact['artifactId']}.meta.json")
            )
            metadata_copy = metadata_path.with_name("unregistered.meta.json")
            metadata_copy.write_bytes(metadata_path.read_bytes())
            metadata_path.unlink()
            os.symlink(metadata_copy, metadata_path)
            metadata_symlink = run(
                reader.handler(
                    {"artifact_id": metadata_artifact["artifactId"]},
                    self._context(owner, config, workspace),
                )
            )
            self.assertEqual(
                json.loads(metadata_symlink["content"])["code"],
                "artifact_metadata_invalid",
            )
        finally:
            self.restore_home(root, previous)

    def test_unavailable_artifact_returns_one_typed_error_without_replacement(self):
        root, home, workspace, previous = self.temp_context()
        try:
            core.configureRuntimeHome(str(home))
            run(core.ensureBaseLayout(str(workspace)))
            session = self._session(workspace)
            config = _codex_config(home, workspace)
            artifact_dir = Path(
                core.toolArtifactMetaPath(session["id"], "missing-artifact")
            ).parent
            before = list(artifact_dir.iterdir()) if artifact_dir.exists() else []
            result = run(
                self._reader().handler(
                    {"artifact_id": "missing-artifact"},
                    self._context(session, config, workspace),
                )
            )
            self.assertEqual(json.loads(result["content"])["code"], "artifact_not_found")
            after = list(artifact_dir.iterdir()) if artifact_dir.exists() else []
            self.assertEqual(after, before)
        finally:
            self.restore_home(root, previous)
