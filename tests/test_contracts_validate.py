from __future__ import annotations

import unittest
from typing import Any, Literal, TypedDict

from tess_core.contracts import (
    ChildAgentRecord,
    MonitorRecord,
    RuntimeEventEnvelope,
    SessionRecord,
    TaskLaunchResult,
    TaskMeta,
    TaskRecord,
    assert_valid,
    validate,
)


class _NestedRequired(TypedDict):
    name: str


class _Nested(_NestedRequired, total=False):
    count: int


class _SampleRequired(TypedDict):
    taskId: str
    status: Literal["running", "completed"]


class _Sample(_SampleRequired, total=False):
    outputFile: str
    task_id: str  # legacy alias, declared so it is tolerated
    pid: int | None
    ratio: float
    nested: _Nested
    items: list[_Nested]
    tags: list[str]
    metadata: dict[str, Any]


class ContractsValidateTests(unittest.TestCase):
    def test_valid_record_returns_no_errors(self) -> None:
        record = {
            "taskId": "t-1",
            "status": "running",
            "outputFile": "/tmp/out.log",
            "pid": 12,
            "ratio": 2,
            "nested": {"name": "n", "count": 1},
            "items": [{"name": "a"}],
            "tags": ["x"],
            "metadata": {"anything": object()},
        }
        self.assertEqual(validate(record, _Sample), [])
        self.assertEqual(validate(record, _Sample, strict=True), [])

    def test_missing_required_key_is_reported(self) -> None:
        errors = validate({"status": "running"}, _Sample)
        self.assertEqual(errors, ["missing required key 'taskId'"])

    def test_unknown_key_reported_only_in_strict_mode(self) -> None:
        record = {"taskId": "t-1", "status": "running", "sesionId": "oops"}
        self.assertEqual(validate(record, _Sample), [])
        self.assertIn("unknown key 'sesionId'", validate(record, _Sample, strict=True))

    def test_legacy_alias_key_is_tolerated(self) -> None:
        record = {"taskId": "t-1", "task_id": "t-1", "status": "running"}
        self.assertEqual(validate(record, _Sample, strict=True), [])

    def test_literal_out_of_set_is_reported(self) -> None:
        errors = validate({"taskId": "t-1", "status": "exploded"}, _Sample)
        self.assertEqual(len(errors), 1)
        self.assertIn("status", errors[0])
        self.assertIn("exploded", errors[0])

    def test_scalar_type_mismatch_is_reported(self) -> None:
        errors = validate({"taskId": 7, "status": "running"}, _Sample)
        self.assertEqual(errors, ["taskId: expected str, got int"])

    def test_bool_is_not_accepted_as_int(self) -> None:
        errors = validate({"taskId": "t", "status": "running", "pid": True}, _Sample)
        self.assertEqual(len(errors), 1)
        self.assertIn("pid", errors[0])

    def test_none_allowed_only_when_union_declares_it(self) -> None:
        self.assertEqual(validate({"taskId": "t", "status": "running", "pid": None}, _Sample), [])
        errors = validate({"taskId": "t", "status": "running", "outputFile": None}, _Sample)
        self.assertEqual(len(errors), 1)
        self.assertIn("outputFile", errors[0])

    def test_int_accepted_for_float_annotation(self) -> None:
        self.assertEqual(validate({"taskId": "t", "status": "running", "ratio": 3}, _Sample), [])

    def test_nested_typeddict_and_lists_are_checked(self) -> None:
        record = {
            "taskId": "t",
            "status": "running",
            "nested": {"count": 1},
            "items": [{"name": "ok"}, {"count": 2}],
            "tags": ["a", 3],
        }
        errors = validate(record, _Sample)
        self.assertIn("nested.missing required key 'name'", errors)
        self.assertIn("items[1].missing required key 'name'", errors)
        self.assertTrue(any(error.startswith("tags[1]") for error in errors))

    def test_non_mapping_record_is_reported(self) -> None:
        self.assertEqual(validate("nope", _Sample), ["expected mapping for _Sample, got str"])

    def test_schema_must_be_typeddict(self) -> None:
        with self.assertRaises(TypeError):
            validate({}, dict)

    def test_assert_valid_raises_with_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            assert_valid({"status": "running"}, _Sample, label="sample")
        self.assertIn("sample", str(ctx.exception))
        self.assertIn("taskId", str(ctx.exception))

    def test_real_task_launch_result_validates(self) -> None:
        started = {"taskId": "abc", "task_id": "abc", "outputFile": "/tmp/o", "output_file": "/tmp/o"}
        self.assertEqual(validate(started, TaskLaunchResult, strict=True), [])

    def test_real_supervisor_task_meta_validates(self) -> None:
        meta = {
            "type": "background_command",
            "taskId": "t-1",
            "id": "t-1",
            "sessionId": "s-1",
            "command": "echo hi",
            "cwd": "/tmp",
            "startedAt": "2026-06-10T00:00:00Z",
            "updatedAt": "2026-06-10T00:00:00Z",
            "status": "running",
            "outputFile": "/tmp/out.log",
            "title": None,
            "description": None,
            "pid": 4242,
            "detailStatus": "launching",
            "owner": "daemon",
            "supervisorId": "sup-1",
        }
        self.assertEqual(validate(meta, TaskMeta, strict=True), [])

    def test_finalizer_task_meta_with_snake_only_id_validates(self) -> None:
        meta = {
            "type": "subagent",
            "task_id": "t-2",
            "agent_id": "agent-1",
            "session_id": "s-2",
            "cwd": "/tmp",
            "startedAt": "2026-06-10T00:00:00Z",
            "status": "completed",
            "completedAt": "2026-06-10T00:01:00Z",
            "outputFile": "/tmp/out.log",
            "stopReason": "end_turn",
            "summary": "done",
            "resultSummary": "done",
            "waitingReason": None,
            "completionReason": None,
            "attachableSessionId": "s-2",
            "retryable": True,
            "stoppable": False,
            "owner": "daemon",
        }
        self.assertEqual(validate(meta, TaskMeta, strict=True), [])


class CanonicalAccessorTests(unittest.TestCase):
    def test_launch_accessors_prefer_canonical_spelling(self) -> None:
        from tess_core.contracts import launch_output_file, launch_task_id

        both = {"taskId": "camel", "task_id": "snake", "outputFile": "/camel", "output_file": "/snake"}
        self.assertEqual(launch_task_id(both), "camel")
        self.assertEqual(launch_output_file(both), "/camel")
        self.assertEqual(launch_task_id({"task_id": "snake"}), "snake")
        self.assertEqual(launch_output_file({"output_file": "/snake"}), "/snake")
        self.assertIsNone(launch_task_id({}))
        self.assertIsNone(launch_output_file({}))

    def test_task_meta_id_absorbs_finalizer_spelling(self) -> None:
        from tess_core.contracts import task_meta_id

        self.assertEqual(task_meta_id({"taskId": "a", "task_id": "b", "id": "c"}), "a")
        self.assertEqual(task_meta_id({"task_id": "b", "id": "c"}), "b")
        self.assertEqual(task_meta_id({"id": "c"}), "c")
        self.assertIsNone(task_meta_id({}))

    def test_event_accessors_mirror_reader_order(self) -> None:
        from tess_core.contracts import (
            event_body,
            event_kind,
            event_name,
            event_payload,
            event_session_id,
            event_time,
        )

        envelope = {
            "event": "task.updated",
            "type": "task.updated",
            "kind": "task.updated",
            "data": {"session_id": "s-1", "raw": True},
            "payload": {"sessionId": "s-1", "typed": True},
            "createdAt": "2026-06-10T00:00:00Z",
            "timestamp": "2026-06-10T00:00:00Z",
            "sessionId": "s-1",
            "legacyEvent": "task.updated",
            "legacyData": {"raw": True},
        }
        self.assertEqual(event_kind(envelope), "task.updated")
        self.assertEqual(event_name(envelope), "task.updated")
        self.assertTrue(event_payload(envelope)["typed"])
        self.assertTrue(event_body(envelope)["raw"])
        self.assertEqual(event_time(envelope), "2026-06-10T00:00:00Z")
        self.assertEqual(event_session_id(envelope), "s-1")
        # typed view prefers kind/payload; raw view prefers event/data
        partial = {"kind": "k", "event": "e", "payload": {"p": 1}, "data": {"d": 1}}
        self.assertEqual(event_kind(partial), "k")
        self.assertEqual(event_name(partial), "e")
        self.assertEqual(event_payload(partial), {"p": 1})
        self.assertEqual(event_body(partial), {"d": 1})
        self.assertEqual(event_session_id({"data": {"session_id": "fallback"}}), "fallback")
        self.assertIsNone(event_session_id({}))


class ContractSyncTests(unittest.TestCase):
    """Literal annotations must stay identical to the runtime constant sets."""

    @staticmethod
    def _literal_values(alias: Any) -> set[str]:
        import typing

        return set(typing.get_args(alias))

    def test_runtime_event_names_match_known_events(self) -> None:
        from tess_core.contracts.events import RuntimeEventName
        from tess_core.runtime.events.shared import KNOWN_RUNTIME_EVENTS

        self.assertEqual(self._literal_values(RuntimeEventName), set(KNOWN_RUNTIME_EVENTS))

    def test_context_relief_modes_match_runtime(self) -> None:
        from tess_core.contracts.events import ContextReliefMode
        from tess_core.runtime.events.shared import _CONTEXT_RELIEF_MODES

        self.assertEqual(self._literal_values(ContextReliefMode), set(_CONTEXT_RELIEF_MODES))

    def test_task_statuses_match_runtime(self) -> None:
        from tess_core.contracts.records import TaskStatus
        from tess_core.runtime.events.shared import _TASK_STATUSES

        self.assertEqual(self._literal_values(TaskStatus), set(_TASK_STATUSES))

    def test_task_kinds_match_runtime(self) -> None:
        from tess_core.contracts.records import TaskKind
        from tess_core.runtime.events.shared import _TASK_KINDS

        self.assertEqual(self._literal_values(TaskKind), set(_TASK_KINDS))

    def test_permission_modes_match_runtime(self) -> None:
        from tess_core.contracts.config import PermissionMode
        from tess_core.tools.permissions import PERMISSION_MODES
        from tess_core.shell_surface.shared import PERMISSION_MODE_ORDER
        self.assertEqual(set(PERMISSION_MODE_ORDER), set(PERMISSION_MODES))

        self.assertEqual(self._literal_values(PermissionMode), set(PERMISSION_MODES))

    def test_created_runtime_event_envelope_validates_strict(self) -> None:
        from tess_core.runtime.events import create_runtime_event

        envelope = create_runtime_event(
            {"event": "task.updated", "data": {"task_id": "t-1", "session_id": "s-1"}}
        )
        self.assertEqual(validate(envelope, RuntimeEventEnvelope, strict=True), [])

    def test_resolved_config_validates_strict(self) -> None:
        import asyncio
        from contextlib import contextmanager
        from pathlib import Path
        import tempfile

        from tess_core.config import resolve_config
        from tess_core.contracts import ResolvedConfig
        from tess_core.runtime import paths as runtime_paths
        from tess_core.runtime.paths import get_active_runtime_paths
        from tess_core.storage.paths import scoped_runtime_home

        @contextmanager
        def fixture_directory():
            # Absolute paths keep cleanup compatible with the unchanged audit
            # guard, which cannot resolve shutil's relative dir_fd reads.
            def remove_contents(directory):
                for child in directory.iterdir():
                    if child.is_symlink() or not child.is_dir():
                        child.unlink()
                    else:
                        remove_contents(child)
                        child.rmdir()

            with tempfile.TemporaryDirectory(prefix="tess-core-contracts-") as root:
                directory = Path(root).resolve()
                try:
                    yield directory
                finally:
                    remove_contents(directory)

        previous_paths = get_active_runtime_paths()
        previous_configured = runtime_paths._configured_runtime_paths
        previous_scoped = runtime_paths.get_scoped_runtime_paths()
        with fixture_directory() as root:
            home = str(root / "home")
            workspace = root / "workspace"
            workspace.mkdir()
            # Do not mutate the configured-global fallback or the environment.
            # The scope token restores even an existing caller's runtime scope.
            with scoped_runtime_home(home):
                config = asyncio.run(resolve_config({"runtimeHome": home}, str(workspace)))
                self.assertEqual(get_active_runtime_paths().home, home)
                self.assertEqual(config["runtimeHome"], home)
            self.assertEqual(get_active_runtime_paths(), previous_paths)
            self.assertIs(runtime_paths._configured_runtime_paths, previous_configured)
            self.assertIs(runtime_paths.get_scoped_runtime_paths(), previous_scoped)
        self.assertEqual(validate(config, ResolvedConfig, strict=True), [])

    def test_builtin_agents_and_profiles_validate(self) -> None:
        from tess_core.config.shared import BUILTIN_AGENTS, BUILTIN_PROVIDER_PROFILES
        from tess_core.contracts import AgentDefinition, ProviderProfile

        for name, definition in BUILTIN_AGENTS.items():
            self.assertEqual(validate(definition, AgentDefinition, strict=True), [], name)
        for name, profile in BUILTIN_PROVIDER_PROFILES.items():
            self.assertEqual(validate(profile, ProviderProfile, strict=True), [], name)

    def test_default_auto_mode_validates_strict(self) -> None:
        from tess_core.contracts import AutoModePolicy
        from tess_core.storage.settings import get_default_auto_mode

        self.assertEqual(validate(get_default_auto_mode(), AutoModePolicy, strict=True), [])

    def test_provider_failure_record_validates_strict(self) -> None:
        from tess_core.contracts import ProviderFailureRecord
        from tess_core.providers.errors import classify_provider_http_failure, create_provider_failure_record

        record = create_provider_failure_record(
            provider="anthropic",
            providerLabel="Anthropic",
            phase="request_start",
            category="server",
            retryable=True,
            attempt=1,
            hadAssistantOutput=False,
            hadToolUse=False,
            rawMessage="boom",
        )
        self.assertEqual(validate(record, ProviderFailureRecord, strict=True), [])

        error = classify_provider_http_failure(
            provider="openai",
            providerLabel="OpenAI",
            phase="request_start",
            status=429,
            body="rate limited",
            attempt=2,
            hadAssistantOutput=False,
            hadToolUse=False,
        )
        self.assertEqual(validate(error.record, ProviderFailureRecord, strict=True), [])

    def test_empty_session_record_validates_strict(self) -> None:
        from tess_core.storage import create_empty_session

        session = create_empty_session(
            cwd="/tmp",
            provider="anthropic",
            model="claude-sonnet-4-6",
            systemPrompt="prompt",
            permissionMode="default",
        )
        self.assertEqual(validate(session, SessionRecord, strict=True), [])


class OptionalKeyTests(unittest.TestCase):
    """Contract modules postpone annotation evaluation, which hides a
    ``NotRequired`` marker from ``TypedDict``: the key is then counted as
    required. Optional keys are declared on a ``total=False`` class instead."""

    def test_no_contract_marks_optional_keys_per_key(self) -> None:
        import importlib
        import pkgutil
        import typing

        import tess_core.contracts as contracts

        marked: list[str] = []
        for info in pkgutil.iter_modules(contracts.__path__):
            module = importlib.import_module(f"tess_core.contracts.{info.name}")
            for name, schema in vars(module).items():
                if not typing.is_typeddict(schema) or schema.__module__ != module.__name__:
                    continue
                hints = typing.get_type_hints(schema, include_extras=True)
                marked += [
                    f"{info.name}.{name}.{key}"
                    for key, hint in hints.items()
                    if typing.get_origin(hint) in (typing.NotRequired, typing.Required)
                ]
        self.assertEqual(marked, [])

    def test_secret_reference_validates_without_its_optional_keys(self) -> None:
        from tess_core.contracts.config import ResolvedConfig, RuntimeSecretRef

        reference = {"kind": "runtime_secret", "id": "secret-1", "purpose": "provider_oauth_token", "version": 1}
        self.assertEqual(validate(reference, RuntimeSecretRef, strict=True), [])
        self.assertFalse([e for e in validate({"openaiToken": reference}, ResolvedConfig) if "openaiToken" in e])
        self.assertEqual(validate({**reference, "backend": "encrypted_file"}, RuntimeSecretRef, strict=True), [])
        self.assertEqual(validate({"kind": "runtime_secret"}, RuntimeSecretRef)[0], "missing required key 'id'")


if __name__ == "__main__":
    unittest.main()
