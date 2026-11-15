from __future__ import annotations

import copy
import hashlib

from tests.support.tess_core_case import *


_AUTOMATIC_SENTINELS = (
    "FOREIGN_AUTOMATIC_SENTINEL",
    "CURRENT_ID_LEGACY_SENTINEL",
    "SESSIONLESS_LEGACY_SENTINEL",
    "FUTURE_SHAPE_LEGACY_SENTINEL",
    "CACHED_MANUAL_SENTINEL",
)


def _session(workspace: Path, session_id: str):
    return core.create_empty_session(
        cwd=str(workspace),
        provider="openai",
        model="gpt-5.4",
        effort="high",
        systemPrompt="s",
        permissionMode="default",
        sessionId=session_id,
    )


def _legacy_record(workspace: Path, session_id: str | None, text: str, *, source="compaction"):
    record = {
        "id": f"legacy-{text.lower()}",
        "workspaceRoot": str(workspace),
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "kind": "task_context",
        "source": source,
        "sourceDetail": "legacy automatic record",
        "confidence": "medium",
        "text": text,
    }
    if session_id is not None:
        record["sessionId"] = session_id
    return record


def _snapshot(path: Path):
    data = path.read_bytes()
    metadata = path.stat()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def _working_context(session, sentinel: str):
    return {
        "version": "v2",
        "sessionId": session["id"],
        "session": {
            "cwd": session["cwd"],
            "provider": session["provider"],
            "model": session["model"],
            "permissionMode": session["permissionMode"],
        },
        "project": {"description": sentinel, "keyFiles": []},
        "goals": {
            "active": [sentinel],
            "recentRequests": [],
            "constraints": [],
            "preferences": [],
        },
        "currentState": {"completed": [], "inProgress": [], "blocked": []},
        "decisions": [],
        "discoveries": [],
        "todos": {"open": [], "completed": []},
        "activity": {"commands": [], "searches": [], "web": [], "failures": []},
        "preservedTrace": [],
        "nextSteps": [],
        "criticalContext": [],
    }


def _compaction_state(working_context_path: Path, summary_path: Path):
    return {
        "latestCompactionId": "checkpoint-1",
        "latestCompactionGeneration": 1,
        "latestCompactionAt": "2026-01-01T00:00:00Z",
        "compactedThroughMessageIndex": 0,
        "compactedMessageCount": 0,
        "recentMessageCount": 0,
        "archiveMessageCount": 0,
        "rawTokenEstimate": 100,
        "compactedContextTokenEstimate": 50,
        "workingContextTokenEstimate": 50,
        "lastTrigger": "manual",
        "artifacts": {
            "workingContext": str(working_context_path),
            "summary": str(summary_path),
            "checkpoint": str(working_context_path.with_name("checkpoint.json")),
        },
    }


class DurableMemoryIsolationTests(TessCoreTestCase):
    def restore_home(self, root, previous):
        # Use absolute paths for this fixture's disposable tree. The guarded
        # runner cannot resolve shutil's relative open(..., dir_fd=...) reads.
        def remove_contents(directory):
            for child in directory.iterdir():
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    remove_contents(child)
                    child.rmdir()

        try:
            remove_contents(Path(root.name).resolve())
        finally:
            super().restore_home(root, previous)

    def test_legacy_automatic_records_are_inert_across_sessions(self):
        root, home, workspace, previous = self.temp_context()
        try:
            current = _session(workspace, "current-session")
            child = _session(workspace, "child-session")
            records = [
                _legacy_record(workspace, "old-session", _AUTOMATIC_SENTINELS[0]),
                _legacy_record(workspace, current["id"], _AUTOMATIC_SENTINELS[1]),
                _legacy_record(workspace, None, _AUTOMATIC_SENTINELS[2]),
                {
                    **_legacy_record(workspace, "future-session", _AUTOMATIC_SENTINELS[3]),
                    "futureAdmissionHint": "force",
                },
                _legacy_record(
                    workspace,
                    "cached-manual-session",
                    _AUTOMATIC_SENTINELS[4],
                    source="manual",
                ),
            ]
            run(core.save_project_memory_records(str(workspace), records))

            for session in (current, child):
                selected = run(
                    core.retrieve_relevant_durable_memory(
                        {"session": session, "prompt": _AUTOMATIC_SENTINELS[0]}
                    )
                )
                self.assertEqual(selected["records"], [])
                self.assertIsNone(core.render_retrieved_memory_prompt(selected))

            config = run(
                core.resolve_config(
                    {"provider": "openai", "runtimeHome": str(home)},
                    str(workspace),
                )
            )
            continuation = run(
                core.build_continuation_context(
                    {"session": current, "config": config, "prompt": "continue"}
                )
            )
            context = continuation["systemContext"] or ""
            for sentinel in _AUTOMATIC_SENTINELS:
                self.assertNotIn(sentinel, context)
        finally:
            self.restore_home(root, previous)

    def test_compaction_promotion_compatibility_api_is_read_only(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = _session(workspace, "promotion-session")
            legacy_path = Path(core.get_project_memory_records_path(str(workspace)))
            run(
                core.save_project_memory_records(
                    str(workspace),
                    [_legacy_record(workspace, "old-session", "PRESERVED_LEGACY_SENTINEL")],
                )
            )
            before = _snapshot(legacy_path)

            returned = run(
                core.update_durable_memory_from_working_context(
                    session,
                    _working_context(session, "NEW_COMPACTION_SENTINEL"),
                    {"enabled": True},
                )
            )

            self.assertEqual(returned, [])
            self.assertEqual(_snapshot(legacy_path), before)
            records = run(core.load_project_memory_records(str(workspace)))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["text"], "PRESERVED_LEGACY_SENTINEL")
        finally:
            self.restore_home(root, previous)

    def test_explicit_memory_is_the_only_cross_session_source_and_reads_do_not_write(self):
        root, home, workspace, previous = self.temp_context()
        try:
            first_session = _session(workspace, "manual-session")
            second_session = _session(workspace, "second-manual-session")
            memory_path = Path(core.get_project_memory_path(str(workspace)))
            memory_path.parent.mkdir(parents=True)
            memory_path.write_text("EXPLICIT_PROJECT_SENTINEL\n")
            legacy_path = Path(core.get_project_memory_records_path(str(workspace)))
            run(
                core.save_project_memory_records(
                    str(workspace),
                    [
                        _legacy_record(workspace, "old-session", "INERT_LEGACY_SENTINEL"),
                        _legacy_record(
                            workspace,
                            "cached-manual",
                            "STALE_CACHED_MANUAL_SENTINEL",
                            source="manual",
                        ),
                    ],
                )
            )
            before = (_snapshot(memory_path), _snapshot(legacy_path))

            first = run(
                core.retrieve_relevant_durable_memory(
                    {"session": first_session, "prompt": "unrelated prompt"}
                )
            )
            second = run(
                core.retrieve_relevant_durable_memory(
                    {"session": second_session, "prompt": "another unrelated prompt"}
                )
            )

            self.assertEqual(len(first["records"]), 1)
            self.assertEqual(first["records"][0]["source"], "manual")
            self.assertNotIn("sessionId", first["records"][0])
            self.assertEqual(second, first)
            self.assertEqual((_snapshot(memory_path), _snapshot(legacy_path)), before)

            prompt = core.render_retrieved_memory_prompt(first)
            self.assertIn('<project_memory source="manual" scope="workspace">', prompt)
            self.assertIn("EXPLICIT_PROJECT_SENTINEL", prompt)
            self.assertNotIn("INERT_LEGACY_SENTINEL", prompt)
            self.assertNotIn("STALE_CACHED_MANUAL_SENTINEL", prompt)

            disabled = run(
                core.retrieve_relevant_durable_memory(
                    {
                        "session": first_session,
                        "prompt": "EXPLICIT_PROJECT_SENTINEL",
                        "enabled": False,
                    }
                )
            )
            self.assertEqual(disabled["records"], [])
            self.assertIsNone(core.render_retrieved_memory_prompt(disabled))
            self.assertEqual((_snapshot(memory_path), _snapshot(legacy_path)), before)
        finally:
            self.restore_home(root, previous)

    def test_renderer_rejects_fabricated_automatic_records(self):
        fabricated = _legacy_record(
            Path("/tmp/project"),
            "foreign-session",
            "FABRICATED_SENTINEL",
        )
        self.assertIsNone(
            core.render_retrieved_memory_prompt(
                {
                    "records": [fabricated],
                    "tokenEstimate": 100,
                    "byteEstimate": 100,
                    "maxPromptBytes": 1_600,
                    "truncatedRecordIds": [],
                }
            )
        )

    def test_compaction_context_accepts_only_active_session_artifact(self):
        self._assert_active_session_artifact()

    def test_compaction_context_accepts_active_artifact_after_stale_runtime_home(self):
        self._assert_active_session_artifact(stale_runtime_home=True)

    def test_compaction_context_rejects_untrusted_artifacts(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolve_config(
                {"provider": "openai", "runtimeHome": str(home)}, str(workspace)
            ))
            config["durableMemory"]["enabled"] = False
            session = _session(workspace, "security-checkpoint-session")
            directory = Path(core.get_session_compactions_dir(session["id"]))
            directory.mkdir(parents=True)
            active = directory / "working-context.json"
            valid = _working_context(session, "UNTRUSTED_CHECKPOINT_SENTINEL")
            outside = workspace / "outside.json"
            outside.write_text(json.dumps(valid))
            foreign = Path(core.get_session_compactions_dir("foreign-session"))
            foreign.mkdir(parents=True)
            foreign_path = foreign / "working-context.json"
            foreign_path.write_text(json.dumps(valid))
            sibling = directory.with_name(directory.name + "-sibling")
            sibling.mkdir()
            sibling_path = sibling / "working-context.json"
            sibling_path.write_text(json.dumps(valid))
            escaped = directory / "escaped.json"
            escaped.symlink_to(outside)
            traversed = directory / ".." / sibling.name / sibling_path.name
            cases = [
                ("outside", outside, valid),
                ("foreign-directory", foreign_path, valid),
                ("sibling-prefix", sibling_path, valid),
                ("traversal", traversed, valid),
                ("symlink-escape", escaped, valid),
                ("foreign-session-id", active, {**valid, "sessionId": "foreign-session"}),
                ("missing-session-id", active, {k: v for k, v in valid.items() if k != "sessionId"}),
                ("malformed", active, None),
            ]
            for label, path, payload in cases:
                with self.subTest(case=label):
                    if path == active:
                        active.write_text(json.dumps(payload) if payload else "{invalid")
                    session["metadata"]["compactionState"] = _compaction_state(
                        path, directory / "summary.md"
                    )
                    self.assertIsNone(run(core.build_compaction_system_prompt_context(session)))
                    continuation = run(core.build_continuation_context({
                        "session": session, "config": config, "prompt": "continue",
                    }))
                    self.assertNotIn("UNTRUSTED_CHECKPOINT_SENTINEL", continuation["systemContext"] or "")
                    self.assertEqual(continuation["preflightSegments"]["checkpointTokens"], 0)
        finally:
            self.restore_home(root, previous)

    def _assert_active_session_artifact(self, *, stale_runtime_home=False):
        root, home, workspace, previous = self.temp_context()
        try:
            if stale_runtime_home:
                # Reproduce a preceding config consumer leaving a configured
                # home which takes precedence over TESS_CORE_HOME.
                core.configure_runtime_home(str(Path(root.name) / "stale-home"))
            # Bind the fixture home before deriving security-sensitive paths.
            config = run(
                core.resolve_config(
                    {"provider": "openai", "runtimeHome": str(home)},
                    str(workspace),
                )
            )
            session = _session(workspace, "active-checkpoint-session")
            compactions_dir = Path(core.get_session_compactions_dir(session["id"]))
            self.assertTrue(compactions_dir.resolve().is_relative_to(home.resolve()))
            compactions_dir.mkdir(parents=True)
            working_context_path = compactions_dir / "working-context.json"
            summary_path = compactions_dir / "summary.md"
            summary_path.write_text("SUMMARY_SENTINEL\n")
            session["metadata"]["compactionState"] = _compaction_state(
                working_context_path, summary_path
            )

            foreign = _session(workspace, "foreign-checkpoint-session")
            working_context_path.write_text(
                json.dumps(_working_context(foreign, "FOREIGN_CHECKPOINT_SENTINEL"))
            )
            self.assertIsNone(run(core.build_compaction_system_prompt_context(session)))

            outside_path = workspace / "outside-working-context.json"
            outside_path.write_text(
                json.dumps(_working_context(session, "OUTSIDE_PATH_SENTINEL"))
            )
            session["metadata"]["compactionState"]["artifacts"]["workingContext"] = str(
                outside_path
            )
            self.assertIsNone(run(core.build_compaction_system_prompt_context(session)))

            session["metadata"]["compactionState"]["artifacts"]["workingContext"] = str(
                working_context_path
            )
            working_context_path.write_text(
                json.dumps(_working_context(session, "ACTIVE_CHECKPOINT_SENTINEL"))
            )
            accepted = run(core.build_compaction_system_prompt_context(session))
            self.assertIn("ACTIVE_CHECKPOINT_SENTINEL", accepted)
            self.assertNotIn("FOREIGN_CHECKPOINT_SENTINEL", accepted)
            self.assertNotIn("OUTSIDE_PATH_SENTINEL", accepted)

            memory_path = Path(core.get_project_memory_path(str(workspace)))
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text("DISABLED_MANUAL_SENTINEL\n")
            config["durableMemory"]["enabled"] = False
            continuation = run(
                core.build_continuation_context(
                    {
                        "session": session,
                        "config": config,
                        "prompt": "continue from the checkpoint",
                    }
                )
            )
            context = continuation["systemContext"] or ""
            self.assertIn("ACTIVE_CHECKPOINT_SENTINEL", context)
            self.assertNotIn("DISABLED_MANUAL_SENTINEL", context)
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
