from __future__ import annotations

import unittest
from typing import Any, Literal, TypedDict

from agent_core.contracts import (
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
        from agent_core.contracts import launch_output_file, launch_task_id

        both = {"taskId": "camel", "task_id": "snake", "outputFile": "/camel", "output_file": "/snake"}
        self.assertEqual(launch_task_id(both), "camel")
        self.assertEqual(launch_output_file(both), "/camel")
        self.assertEqual(launch_task_id({"task_id": "snake"}), "snake")
        self.assertEqual(launch_output_file({"output_file": "/snake"}), "/snake")
        self.assertIsNone(launch_task_id({}))
        self.assertIsNone(launch_output_file({}))

    def test_task_meta_id_absorbs_finalizer_spelling(self) -> None:
        from agent_core.contracts import task_meta_id

        self.assertEqual(task_meta_id({"taskId": "a", "task_id": "b", "id": "c"}), "a")
        self.assertEqual(task_meta_id({"task_id": "b", "id": "c"}), "b")
        self.assertEqual(task_meta_id({"id": "c"}), "c")
        self.assertIsNone(task_meta_id({}))

    def test_event_accessors_mirror_reader_order(self) -> None:
        from agent_core.contracts import (
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
        from agent_core.contracts.events import RuntimeEventName
        from agent_core.runtime.event_parts.shared import KNOWN_RUNTIME_EVENTS

        self.assertEqual(self._literal_values(RuntimeEventName), set(KNOWN_RUNTIME_EVENTS))

    def test_context_relief_modes_match_runtime(self) -> None:
        from agent_core.contracts.events import ContextReliefMode
        from agent_core.runtime.event_parts.shared import _CONTEXT_RELIEF_MODES

        self.assertEqual(self._literal_values(ContextReliefMode), set(_CONTEXT_RELIEF_MODES))

    def test_task_statuses_match_runtime(self) -> None:
        from agent_core.contracts.records import TaskStatus
        from agent_core.runtime.event_parts.shared import _TASK_STATUSES

        self.assertEqual(self._literal_values(TaskStatus), set(_TASK_STATUSES))

    def test_task_kinds_match_runtime(self) -> None:
        from agent_core.contracts.records import TaskKind
        from agent_core.runtime.event_parts.shared import _TASK_KINDS

        self.assertEqual(self._literal_values(TaskKind), set(_TASK_KINDS))

    def test_permission_modes_match_runtime(self) -> None:
        from agent_core.contracts.config import PermissionMode
        from agent_core.tools.permissions import PERMISSION_MODES

        self.assertEqual(self._literal_values(PermissionMode), set(PERMISSION_MODES))

    def test_created_runtime_event_envelope_validates_strict(self) -> None:
        from agent_core.runtime.events import createRuntimeEvent

        envelope = createRuntimeEvent(
            {"event": "task.updated", "data": {"task_id": "t-1", "session_id": "s-1"}}
        )
        self.assertEqual(validate(envelope, RuntimeEventEnvelope, strict=True), [])

    def test_resolved_config_validates_strict(self) -> None:
        import asyncio
        import os
        import tempfile

        from agent_core.config import resolveConfig
        from agent_core.contracts import ResolvedConfig

        with tempfile.TemporaryDirectory(prefix="agent-core-contracts-") as root:
            home = os.path.join(root, "home")
            workspace = os.path.join(root, "workspace")
            os.makedirs(workspace)
            previous = os.environ.get("AGENT_CORE_HOME")
            os.environ["AGENT_CORE_HOME"] = home
            try:
                config = asyncio.run(resolveConfig({"runtimeHome": home}, workspace))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_CORE_HOME", None)
                else:
                    os.environ["AGENT_CORE_HOME"] = previous
        self.assertEqual(validate(config, ResolvedConfig, strict=True), [])

    def test_builtin_agents_and_profiles_validate(self) -> None:
        from agent_core.config_store.shared import BUILTIN_AGENTS, BUILTIN_PROVIDER_PROFILES
        from agent_core.contracts import AgentDefinition, ProviderProfile

        for name, definition in BUILTIN_AGENTS.items():
            self.assertEqual(validate(definition, AgentDefinition, strict=True), [], name)
        for name, profile in BUILTIN_PROVIDER_PROFILES.items():
            self.assertEqual(validate(profile, ProviderProfile, strict=True), [], name)

    def test_default_auto_mode_validates_strict(self) -> None:
        from agent_core.contracts import AutoModePolicy
        from agent_core.storage_store.settings import getDefaultAutoMode

        self.assertEqual(validate(getDefaultAutoMode(), AutoModePolicy, strict=True), [])

    def test_provider_failure_record_validates_strict(self) -> None:
        from agent_core.contracts import ProviderFailureRecord
        from agent_core.provider_errors import classifyProviderHttpFailure, createProviderFailureRecord

        record = createProviderFailureRecord(
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

        error = classifyProviderHttpFailure(
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
        from agent_core.storage import createEmptySession

        session = createEmptySession(
            cwd="/tmp",
            provider="anthropic",
            model="claude-sonnet-4-6",
            systemPrompt="prompt",
            permissionMode="default",
        )
        self.assertEqual(validate(session, SessionRecord, strict=True), [])


if __name__ == "__main__":
    unittest.main()
