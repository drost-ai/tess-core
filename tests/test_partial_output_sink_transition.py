from __future__ import annotations

from typing import Any

from agent_core.providers.partial_output import PartialOutputDelivery
from tests.support.agent_core_case import (
    AgentCoreTestCase,
    bind_test_runtime,
    core,
    run,
)


class _RecordingStore:
    schemaVersion = 1

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = dict(value)
        self.saves: list[dict[str, Any]] = []

    async def load(self) -> dict[str, Any]:
        return dict(self.value)

    async def save(self, value: dict[str, Any]) -> bool:
        self.value = dict(value)
        self.saves.append(dict(value))
        return True


def _published_state() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "sessionId": "session-sink-transition",
        "logicalCallId": "legacy-logical-call",
        "generation": 1,
        "deliveryMode": "replaceable",
        "previewPublished": True,
    }


class PartialOutputSinkTransitionTests(AgentCoreTestCase):
    def test_published_preview_remains_fail_closed_without_explicit_policy(self) -> None:
        store = _RecordingStore(_published_state())

        async def exercise() -> dict[str, Any]:
            return await PartialOutputDelivery(
                {"partialOutputStateStore": store},
                "new-logical-call",
                session_id="session-sink-transition",
            ).begin(1)

        result = run(exercise())

        self.assertFalse(result["acknowledged"])
        self.assertEqual(result["reason"], "recover_unavailable")
        self.assertEqual(store.saves, [])

    def test_explicit_sink_free_transition_retires_abandoned_preview(self) -> None:
        store = _RecordingStore(_published_state())

        async def exercise() -> dict[str, Any]:
            return await PartialOutputDelivery(
                {
                    "partialOutputStateStore": store,
                    "partialOutputRecoveryPolicy": "discard_if_sink_unavailable",
                },
                "new-logical-call",
                session_id="session-sink-transition",
            ).begin(1)

        result = run(exercise())

        self.assertTrue(result["acknowledged"])
        self.assertEqual(store.saves[0]["logicalCallId"], "legacy-logical-call")
        self.assertEqual(store.saves[0]["terminalState"], "interrupted")
        self.assertEqual(store.saves[1]["logicalCallId"], "new-logical-call")
        self.assertIsNone(store.saves[1]["terminalState"])

    def test_unknown_recovery_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "recovery policy"):
            PartialOutputDelivery(
                {"partialOutputRecoveryPolicy": "silently_ignore_everything"},
                "logical-call",
            )

    def test_runtime_bindings_preserve_validated_recovery_policy(self) -> None:
        bindings = bind_test_runtime(
            {"id": "session-sink-transition"},
            {"partialOutputRecoveryPolicy": "discard_if_sink_unavailable"},
        )

        runtime = core.materializeAgentExecutionRuntime(bindings)

        self.assertEqual(
            runtime["partialOutputRecoveryPolicy"],
            "discard_if_sink_unavailable",
        )
