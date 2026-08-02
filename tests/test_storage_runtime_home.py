from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run


class RuntimeHomeScopeTests(AgentCoreTestCase):
    def test_git_root_walk_handles_repository_and_worktree_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            nested = repository / "a" / "b"
            nested.mkdir(parents=True)
            (repository / ".git").mkdir()
            self.assertEqual(core.findGitRoot(str(nested)), str(repository.resolve()))

            worktree = root / "worktree"
            child = worktree / "src"
            child.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: ../repository/.git/worktrees/test\n")
            self.assertEqual(core.findGitRoot(str(child)), str(worktree.resolve()))
            self.assertIsNone(core.findGitRoot(str(root / "outside")))

    def test_scoped_runtime_home_restores_global_home(self):
        root, home, workspace, previous = self.temp_context()
        try:
            scoped_home = home.parent / "profile-a"
            configured_home = home.parent / "configured"
            core.configureRuntimeHome(str(configured_home))

            self.assertEqual(str(configured_home), core.getStateRoot())
            self.assertIsNone(core.getContextRuntimeHome())

            with core.scopedRuntimeHome(str(scoped_home)) as active_home:
                self.assertEqual(str(scoped_home), active_home)
                self.assertEqual(str(scoped_home), core.getContextRuntimeHome())
                self.assertEqual(str(scoped_home), core.getStateRoot())
                self.assertEqual(
                    str(scoped_home / "sessions" / "profile-a.json"),
                    core.sessionPath("profile-a"),
                )

            self.assertIsNone(core.getContextRuntimeHome())
            self.assertEqual(str(configured_home), core.getStateRoot())
        finally:
            core.resetConfiguredRuntimeHome()
            self.restore_home(root, previous)

    def test_scoped_runtime_home_isolated_across_concurrent_tasks(self):
        root, home, workspace, previous = self.temp_context()
        try:
            profile_a_home = home.parent / "profile-a-core"
            profile_b_home = home.parent / "profile-b-core"

            async def persist_session(runtime_home, session_id):
                async with core.scopedRuntimeHome(str(runtime_home)):
                    await asyncio.sleep(0)
                    session = core.createEmptySession(
                        {
                            "sessionId": session_id,
                            "cwd": str(workspace),
                            "provider": "openai",
                            "model": "gpt-5.4",
                            "systemPrompt": "test",
                            "permissionMode": "default",
                        }
                    )
                    session["messages"].append({"role": "user", "content": f"hello {session_id}"})
                    await core.saveSession(session)
                    await asyncio.sleep(0)
                    await core.appendSessionTranscriptLogEntries(
                        session_id,
                        [
                            {
                                "type": "message",
                                "session_id": session_id,
                                "message_index": 0,
                                "persisted_at": "now",
                                "role": "user",
                                "content": f"hello {session_id}",
                            }
                        ],
                    )
                    return core.getStateRoot(), core.sessionPath(session_id), core.sessionTranscriptLogPath(session_id)

            async def main():
                return await asyncio.gather(
                    persist_session(profile_a_home, "profile-a"),
                    persist_session(profile_b_home, "profile-b"),
                )

            results = run(main())

            self.assertEqual(str(profile_a_home), results[0][0])
            self.assertEqual(str(profile_b_home), results[1][0])
            self.assertTrue((profile_a_home / "sessions" / "profile-a.json").exists())
            self.assertTrue((profile_a_home / "sessions" / "profile-a" / "full-transcript-with-tools.jsonl").exists())
            self.assertTrue((profile_b_home / "sessions" / "profile-b.json").exists())
            self.assertTrue((profile_b_home / "sessions" / "profile-b" / "full-transcript-with-tools.jsonl").exists())
            self.assertFalse((profile_a_home / "sessions" / "profile-b.json").exists())
            self.assertFalse((profile_b_home / "sessions" / "profile-a.json").exists())
            self.assertEqual(str(home), core.getStateRoot())
        finally:
            self.restore_home(root, previous)

    def test_resolve_config_uses_scoped_runtime_home_without_global_leak(self):
        root, home, workspace, previous = self.temp_context()
        try:
            scoped_home = home.parent / "profile-config-core"

            async def load_config():
                async with core.scopedRuntimeHome(str(scoped_home)):
                    config = await core.resolveConfig({"provider": "openai", "permissionMode": "default"}, str(workspace))
                    self.assertEqual(str(scoped_home), config["runtimeHome"])
                    self.assertEqual(str(scoped_home), core.getStateRoot())
                return config

            config = run(load_config())

            self.assertEqual(str(scoped_home), config["runtimeHome"])
            self.assertEqual(str(home), core.getStateRoot())
            self.assertTrue((scoped_home / "sessions").exists())
            self.assertFalse((home / "sessions").exists())
        finally:
            self.restore_home(root, previous)

    def test_image_payloads_are_redacted_from_session_persistence(self):
        root, home, workspace, previous = self.temp_context()
        try:
            image_data = "aW1hZ2UtYnl0ZXM="
            session = core.createEmptySession(
                {
                    "sessionId": "vision-session",
                    "cwd": str(workspace),
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "systemPrompt": "test",
                    "permissionMode": "default",
                }
            )
            session["messages"].append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {
                            "type": "image",
                            "mime_type": "image/png",
                            "path": "inbox/telegram/image.png",
                            "data": image_data,
                        },
                    ],
                }
            )

            async def persist():
                await core.saveSession(session)
                await core.appendSessionTranscriptLogEntries(
                    session["id"],
                    [
                        {
                            "type": "message",
                            "session_id": session["id"],
                            "message_index": 0,
                            "persisted_at": "now",
                            "role": "user",
                            "content": session["messages"][0]["content"],
                        }
                    ],
                )
                return await core.loadSession(session["id"])

            loaded = run(persist())

            session_text = (
                (home / "sessions" / "vision-session.json").read_text()
                + Path(core.sessionMessagesPath("vision-session")).read_text()
            )
            transcript_text = (
                home / "sessions" / "vision-session" / "full-transcript-with-tools.jsonl"
            ).read_text()
            self.assertNotIn(image_data, session_text)
            self.assertNotIn(image_data, transcript_text)
            self.assertNotIn("data:image", session_text)
            self.assertNotIn("data:image", transcript_text)
            self.assertIn("inbox/telegram/image.png", session_text)
            self.assertEqual(session["messages"][0]["content"][1]["data"], image_data)
            self.assertIn("current-turn-only", json.dumps(loaded["messages"]))
        finally:
            self.restore_home(root, previous)

    def test_resolve_config_rejects_conflicting_scoped_runtime_home(self):
        root, home, workspace, previous = self.temp_context()
        try:
            scoped_home = home.parent / "profile-config-core"
            other_home = home.parent / "other-core"

            async def load_config():
                async with core.scopedRuntimeHome(str(scoped_home)):
                    await core.resolveConfig(
                        {"provider": "openai", "permissionMode": "default", "runtimeHome": str(other_home)},
                        str(workspace),
                    )

            with self.assertRaisesRegex(ValueError, "runtimeHome conflicts"):
                run(load_config())
            self.assertEqual(str(home), core.getStateRoot())
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    import unittest

    unittest.main()
