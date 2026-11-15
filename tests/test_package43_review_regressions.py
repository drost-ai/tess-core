from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from tess_core.providers.partial_output import PartialOutputDelivery
from tess_core.providers.partial_output_state import (
    create_partial_output_state_store,
    finalize_partial_output_state,
)
from tests.support.tess_core_case import TessCoreTestCase, AsyncChunks, core, run, testing


class _OpenAiConnectionError(Exception):
    __module__ = "openai._exceptions"


class _OpenAiApiError(Exception):
    __module__ = "openai._exceptions"


class _Sink:
    schemaVersion = 2
    mode = "replaceable"

    def __init__(self, *, recover_ack: bool = True) -> None:
        self.recover_ack = recover_ack
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self.assistant_text = ""
        self.reasoning_text = ""
        self.committed = ""

    async def begin(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("begin", dict(operation)))
        return True

    async def append(self, operation: dict[str, Any]) -> dict[str, bool]:
        self.operations.append(("append", dict(operation)))
        if operation.get("contentKind") == "reasoning":
            self.reasoning_text = str(operation["text"])
        else:
            self.assistant_text = str(operation["text"])
        return {"acknowledged": True, "published": True}

    async def supersede(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("supersede", dict(operation)))
        self.assistant_text = ""
        self.reasoning_text = ""
        return True

    async def reconcile(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("reconcile", dict(operation)))
        self.assistant_text = str(operation["text"])
        return True

    async def commit(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("commit", dict(operation)))
        self.committed = str(operation["text"])
        return True

    async def interrupt(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("interrupt", dict(operation)))
        return True

    async def recover(self, operation: dict[str, Any]) -> bool:
        self.operations.append(("recover", dict(operation)))
        return self.recover_ack


class _DeltaThenHang:
    def __init__(self, delta: str) -> None:
        self.delta = delta

    async def __aiter__(self):
        yield {"type": "response.output_text.delta", "delta": self.delta}
        await asyncio.sleep(1)


def _request() -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "system": "stable",
        "messages": [{"role": "user", "content": "work"}],
        "tools": [],
    }


def _completed(text: str) -> AsyncChunks:
    return AsyncChunks(
        [
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-complete",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            }
        ]
    )


class Package43ReviewRegressionTests(TessCoreTestCase):
    def test_official_api_connection_error_is_retryable_transport(self) -> None:
        failure = core.normalize_unknown_provider_failure(
            error=_OpenAiConnectionError("Connection error."),
            provider="openai",
            providerLabel="OpenAI/Codex",
            phase="stream_read",
            attempt=1,
            hadAssistantOutput=False,
            hadToolUse=False,
        )

        self.assertEqual(failure.record["category"], "transport")
        self.assertTrue(failure.record["retryable"])

    def test_sdk_body_and_message_redact_credentials_and_request_content(self) -> None:
        error = _OpenAiApiError(
            'request failed with authorization="Bearer raw-secret" '
            'and input="private prompt"'
        )
        error.body = {
            "authorization": "Bearer body-secret",
            "input": "private prompt",
            "messages": [{"role": "user", "content": "private prompt"}],
            "error": {
                "type": "server_error",
                "code": "internal_error",
                "message": "processing failed",
            },
        }

        failure = core.normalize_unknown_provider_failure(
            error=error,
            provider="openai",
            providerLabel="OpenAI/Codex",
            phase="stream_read",
            attempt=1,
            hadAssistantOutput=False,
            hadToolUse=False,
        )
        serialized = json.dumps(failure.record, sort_keys=True)

        self.assertNotIn("raw-secret", serialized)
        self.assertNotIn("body-secret", serialized)
        self.assertNotIn("private prompt", serialized)
        self.assertIn("<redacted>", serialized)
        self.assertEqual(failure.record["errorType"], "server_error")
        self.assertEqual(failure.record["code"], "internal_error")

    def test_reasoning_only_failure_requires_acknowledged_supersession(self) -> None:
        sink = _Sink()
        calls = 0
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal calls
            calls += 1
            if calls == 1:
                return AsyncChunks(
                    [
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "delta": "obsolete reasoning",
                        },
                        {
                            "type": "response.failed",
                            "response": {"error": {"message": "server failed"}},
                        },
                    ]
                )
            return _completed("recovered")

        response = run(
            testing.send_openai_responses_request(
                token="test",
                mode="codex",
                request=_request(),
                runtime={
                    "includePartialMessages": True,
                    "partialOutputSink": sink,
                    "emitEvent": lambda event, data: events.append((event, data)),
                },
                maxAttempts=2,
                retryDelay=lambda _attempt: 0,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(response["content"], [{"type": "text", "text": "recovered"}])
        names = [name for name, _operation in sink.operations]
        self.assertLess(names.index("supersede"), names.index("begin", 1))
        reasoning_append = next(
            operation
            for name, operation in sink.operations
            if name == "append" and operation.get("contentKind") == "reasoning"
        )
        self.assertEqual(reasoning_append["text"], "obsolete reasoning")
        thinking = next(
            data
            for event, data in events
            if event == "message.assistant.thinking.partial"
        )
        self.assertEqual(thinking["generation"], 1)
        self.assertTrue(thinking["logicalCallId"])
        self.assertEqual(thinking["deliveryMode"], "replaceable")
        self.assertEqual(thinking["reasoning_kind"], "summary")
        self.assertTrue(thinking["user_visible"])
        self.assertEqual(thinking["provider_event_type"], "response.reasoning_summary_text.delta")

    def test_partial_idle_timeout_can_supersede_with_replaceable_sink(self) -> None:
        sink = _Sink()
        calls = 0

        async def create(_params: dict[str, Any]) -> Any:
            nonlocal calls
            calls += 1
            return _DeltaThenHang("tentative") if calls == 1 else _completed("recovered")

        response = run(
            testing.send_openai_responses_request(
                token="test",
                mode="codex",
                request=_request(),
                runtime={"partialOutputSink": sink},
                streamIdleTimeoutMs=5,
                maxAttempts=2,
                retryDelay=lambda _attempt: 0,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(calls, 2)
        self.assertEqual(response["content"], [{"type": "text", "text": "recovered"}])
        self.assertTrue(any(name == "supersede" for name, _ in sink.operations))

    def test_completed_response_reconciles_nonprefix_authoritative_text(self) -> None:
        sink = _Sink()

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            return AsyncChunks(
                [
                    {"type": "response.output_text.delta", "delta": "draft"},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-authoritative",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "authoritative",
                                        }
                                    ],
                                }
                            ],
                            "usage": {},
                        },
                    },
                ]
            )

        response = run(
            testing.send_openai_responses_request(
                token="test",
                mode="codex",
                request=_request(),
                runtime={"partialOutputSink": sink},
                maxAttempts=1,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(
            response["content"], [{"type": "text", "text": "authoritative"}]
        )
        self.assertEqual(sink.committed, "authoritative")

    def test_append_only_partial_is_explicitly_interrupted_without_sink(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            return AsyncChunks(
                [
                    {
                        "type": "response.output_text.delta",
                        "delta": "visible fragment",
                    },
                    {
                        "type": "response.failed",
                        "response": {"error": {"message": "server failed"}},
                    },
                ]
            )

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.send_openai_responses_request(
                    token="test",
                    mode="codex",
                    request=_request(),
                    runtime={
                        "includePartialMessages": True,
                        "emitEvent": lambda event, data: events.append((event, data)),
                    },
                    maxAttempts=2,
                    retryDelay=lambda _attempt: 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )
            )

        partials = [
            data
            for event, data in events
            if event == "message.assistant.partial"
        ]
        self.assertIn("interrupted", str(partials[-1].get("text_delta")).lower())
        receipt = raised.exception.record["interruptedPreview"]
        self.assertEqual(receipt["deliveryMode"], "append_only")
        self.assertTrue(receipt["deliveryAcknowledged"])

    def test_durable_state_requires_recovery_ack_after_abandoned_preview(self) -> None:
        root, _home, _workspace, previous = self.temp_context()
        try:
            store = create_partial_output_state_store("session-package43-review")
            first_sink = _Sink()

            async def abandon() -> None:
                delivery = PartialOutputDelivery(
                    {
                        "partialOutputSink": first_sink,
                        "partialOutputStateStore": store,
                    },
                    "logical-first",
                    session_id="session-package43-review",
                )
                self.assertTrue((await delivery.begin(1))["acknowledged"])
                self.assertTrue(
                    (
                        await delivery.append(
                            1,
                            delta="private fragment",
                            text="private fragment",
                            response_id="resp-first",
                            sequence_number=1,
                        )
                    )["acknowledged"]
                )

            run(abandon())
            abandoned = run(store.load())
            self.assertEqual(abandoned["logicalCallId"], "logical-first")
            self.assertTrue(abandoned["previewPublished"])
            self.assertNotIn("private fragment", json.dumps(abandoned))

            rejecting_sink = _Sink(recover_ack=False)

            async def reject() -> dict[str, Any]:
                delivery = PartialOutputDelivery(
                    {
                        "partialOutputSink": rejecting_sink,
                        "partialOutputStateStore": store,
                    },
                    "logical-next",
                    session_id="session-package43-review",
                )
                return await delivery.begin(1)

            rejected = run(reject())
            self.assertFalse(rejected["acknowledged"])
            self.assertEqual(rejected["reason"], "recover_rejected")

            recovering_sink = _Sink(recover_ack=True)

            async def recover() -> dict[str, Any]:
                delivery = PartialOutputDelivery(
                    {
                        "partialOutputSink": recovering_sink,
                        "partialOutputStateStore": store,
                    },
                    "logical-next",
                    session_id="session-package43-review",
                )
                return await delivery.begin(1)

            recovered = run(recover())
            self.assertTrue(recovered["acknowledged"])
            recovery = next(
                operation
                for name, operation in recovering_sink.operations
                if name == "recover"
            )
            self.assertEqual(recovery["abandoned"]["logicalCallId"], "logical-first")
            current = run(store.load())
            self.assertEqual(current["logicalCallId"], "logical-next")
            self.assertEqual(current["generation"], 1)
        finally:
            self.restore_home(root, previous)

    def test_recovery_closes_publish_before_state_acknowledgement_gap(self) -> None:
        root, _home, _workspace, previous = self.temp_context()
        try:
            session_id = "session-package43-ack-gap"
            store = create_partial_output_state_store(session_id)
            run(
                store.save(
                    {
                        "sessionId": session_id,
                        "logicalCallId": "logical-abandoned",
                        "generation": 1,
                        "deliveryMode": "replaceable",
                        "previewPublished": False,
                    }
                )
            )
            sink = _Sink()

            async def recover() -> dict[str, Any]:
                delivery = PartialOutputDelivery(
                    {
                        "partialOutputSink": sink,
                        "partialOutputStateStore": store,
                    },
                    "logical-next",
                    session_id=session_id,
                )
                return await delivery.begin(1)

            result = run(recover())
            self.assertTrue(result["acknowledged"])
            self.assertEqual(sink.operations[0][0], "recover")
            self.assertEqual(
                sink.operations[0][1]["abandoned"]["logicalCallId"],
                "logical-abandoned",
            )
        finally:
            self.restore_home(root, previous)

    def test_sidecar_commits_only_after_transcript_receipt_is_durable(self) -> None:
        root, _home, _workspace, previous = self.temp_context()
        try:
            session_id = "session-package43-transcript-fence"
            store = create_partial_output_state_store(session_id)
            sink = _Sink()

            async def stage() -> None:
                delivery = PartialOutputDelivery(
                    {
                        "partialOutputSink": sink,
                        "partialOutputStateStore": store,
                    },
                    "logical-commit",
                    session_id=session_id,
                )
                self.assertTrue((await delivery.begin(1))["acknowledged"])
                self.assertTrue(
                    (
                        await delivery.commit(
                            1,
                            "authoritative",
                            "resp-commit",
                            reasoning_text="reasoning",
                        )
                    )["acknowledged"]
                )

            run(stage())
            staged = run(store.load())
            self.assertTrue(staged["previewCommitAcknowledged"])
            self.assertIsNone(staged["terminalState"])
            receipt = {
                "logicalCallId": "logical-commit",
                "generation": 1,
                "assistantTextHash": staged["assistantTextHash"],
                "reasoningTextHash": staged["reasoningTextHash"],
            }
            self.assertTrue(run(finalize_partial_output_state(store, receipt)))
            committed = run(store.load())
            self.assertEqual(committed["terminalState"], "committed")
        finally:
            self.restore_home(root, previous)
