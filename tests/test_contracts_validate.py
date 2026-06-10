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


class ContractSyncTests(unittest.TestCase):
    """Literal annotations must stay identical to the runtime constant sets."""

    @staticmethod
    def _literal_values(alias: Any) -> set[str]:
        import typing

        return set(typing.get_args(alias))

    def test_runtime_event_names_match_known_events(self) -> None:
        from agent_core.contracts.events import RuntimeEventName
        from agent_core.runtime_event_parts.shared import KNOWN_RUNTIME_EVENTS

        self.assertEqual(self._literal_values(RuntimeEventName), set(KNOWN_RUNTIME_EVENTS))

    def test_context_relief_modes_match_runtime(self) -> None:
        from agent_core.contracts.events import ContextReliefMode
        from agent_core.runtime_event_parts.shared import _CONTEXT_RELIEF_MODES

        self.assertEqual(self._literal_values(ContextReliefMode), set(_CONTEXT_RELIEF_MODES))

    def test_task_statuses_match_runtime(self) -> None:
        from agent_core.contracts.records import TaskStatus
        from agent_core.runtime_event_parts.shared import _TASK_STATUSES

        self.assertEqual(self._literal_values(TaskStatus), set(_TASK_STATUSES))

    def test_task_kinds_match_runtime(self) -> None:
        from agent_core.contracts.records import TaskKind
        from agent_core.runtime_event_parts.shared import _TASK_KINDS

        self.assertEqual(self._literal_values(TaskKind), set(_TASK_KINDS))

    def test_permission_modes_match_runtime(self) -> None:
        from agent_core.contracts.config import PermissionMode
        from agent_core.tools.permissions import PERMISSION_MODES

        self.assertEqual(self._literal_values(PermissionMode), set(PERMISSION_MODES))

    def test_created_runtime_event_envelope_validates_strict(self) -> None:
        from agent_core.runtime_events import createRuntimeEvent

        envelope = createRuntimeEvent(
            {"event": "task.updated", "data": {"task_id": "t-1", "session_id": "s-1"}}
        )
        self.assertEqual(validate(envelope, RuntimeEventEnvelope, strict=True), [])

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
