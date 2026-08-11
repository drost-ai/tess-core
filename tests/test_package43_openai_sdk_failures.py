from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.support.agent_core_case import AgentCoreTestCase, AsyncChunks, core, run, testing


_PROCESSING_MESSAGE = (
    "An error occurred while processing your request. You can retry your request, "
    "or contact us through our help center if the error persists."
)
_INCIDENTS = (
    ("2026-07-24T23:37:14Z", "heartbeat", "req-jul24-a"),
    ("2026-07-25T04:38:40Z", "heartbeat", "req-jul24-b"),
    ("2026-07-26T18:20:03Z", "11659", "req-jul26"),
    ("2026-07-27T20:55:40Z", "11742", "req-jul27"),
    ("2026-07-28T17:15:51Z", "11834", "52d3a48f-e0b2-4394-a637-1805e8bb58c5"),
    ("2026-07-28T17:23:07Z", "11837", "a9922345-e584-46a5-a901-2decb110db8b"),
)


class _OpenAiSdkError(Exception):
    __module__ = "openai._exceptions"

    def __init__(
        self,
        message: str = _PROCESSING_MESSAGE,
        *,
        status_code: Any = None,
        body: Any = None,
        error_type: Any = None,
        code: Any = None,
        request_id: Any = None,
        response_status: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.type = error_type
        self.code = code
        self.request_id = request_id
        self.response = SimpleNamespace(status_code=response_status)


def _base(**overrides: Any) -> dict[str, Any]:
    return {
        "provider": "openai",
        "providerLabel": "OpenAI/Codex",
        "phase": "stream_read",
        "attempt": 1,
        "hadAssistantOutput": False,
        "hadToolUse": False,
        **overrides,
    }


def _completed(text: str = "recovered") -> AsyncChunks:
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


class Package43OpenAiSdkFailureTests(AgentCoreTestCase):
    def test_six_incident_corpus_preserves_sdk_evidence_and_classifies_server(self) -> None:
        for occurred_at, turn, request_id in _INCIDENTS:
            with self.subTest(occurred_at=occurred_at, turn=turn):
                error = _OpenAiSdkError(
                    body={
                        "error": {
                            "message": _PROCESSING_MESSAGE,
                            "type": "server_error",
                            "code": "internal_error",
                        }
                    },
                    request_id=request_id,
                )
                failure = core.normalizeUnknownProviderFailure(_base(error=error))

                self.assertEqual(failure.record["category"], "server")
                self.assertTrue(failure.record["retryable"])
                self.assertEqual(failure.record["phase"], "stream_read")
                self.assertEqual(failure.record["requestId"], request_id)
                self.assertEqual(failure.record["errorType"], "server_error")
                self.assertEqual(failure.record["code"], "internal_error")
                self.assertFalse(failure.record["hadAssistantOutput"])
                self.assertFalse(failure.record["hadToolUse"])
                self.assertIn("processing your request", failure.record["rawMessage"])
                self.assertIn('"type":"server_error"', failure.record["rawBody"])

    def test_structured_sdk_classification_matrix_uses_strongest_evidence(self) -> None:
        cases = (
            (401, {}, None, None, "auth", False),
            (403, {}, None, None, "auth", False),
            (408, {}, None, None, "timeout", True),
            (429, {"error": {"message": "rate limit"}}, None, None, "rate_limit", True),
            (429, {"error": {"message": "insufficient quota and billing"}}, None, None, "quota", False),
            (400, {"error": {"message": "bad input"}}, None, None, "client_request", False),
            (500, {}, None, None, "server", True),
            (502, {}, None, None, "server", True),
            (503, {}, None, None, "server", True),
            (None, {"error": {"type": "server_error"}}, None, None, "server", True),
            (None, None, "authentication_error", None, "auth", False),
            (None, None, "invalid_request_error", None, "client_request", False),
            (None, None, "insufficient_quota", "insufficient_quota", "quota", False),
        )
        for status, body, error_type, code, category, retryable in cases:
            with self.subTest(status=status, error_type=error_type, category=category):
                failure = core.normalizeUnknownProviderFailure(
                    _base(
                        error=_OpenAiSdkError(
                            status_code=status,
                            body=body,
                            error_type=error_type,
                            code=code,
                            request_id="req-matrix",
                        )
                    )
                )
                self.assertEqual(failure.record["category"], category)
                self.assertEqual(failure.record["retryable"], retryable)

    def test_sdk_evidence_is_bounded_deterministic_and_excludes_unapproved_fields(self) -> None:
        class CredentialBearingSdkError(_OpenAiSdkError):
            __module__ = "openai"

        error = CredentialBearingSdkError(
            "x" * 10_000,
            body={
                "z": ["y" * 10_000],
                "a": {"message": "bounded"},
            },
            request_id="r" * 1_000,
        )
        error.headers = {"authorization": "Bearer secret"}
        error.request = {"input": "private prompt"}
        failure = core.normalizeUnknownProviderFailure(_base(error=error))
        serialized = json.dumps(failure.record, sort_keys=True)

        self.assertLessEqual(len(failure.record["rawMessage"]), 2_048)
        self.assertLessEqual(len(failure.record["rawBody"]), 8_192)
        self.assertLessEqual(len(failure.record["requestId"]), 256)
        self.assertTrue(failure.record["rawBody"].startswith('{"a":'))
        self.assertNotIn("Bearer secret", serialized)
        self.assertNotIn("private prompt", serialized)

    def test_similar_prose_from_unrelated_provider_or_exception_is_not_retryable(self) -> None:
        unrelated = core.normalizeUnknownProviderFailure(
            _base(error=RuntimeError(_PROCESSING_MESSAGE))
        )
        anthropic = core.normalizeUnknownProviderFailure(
            _base(
                provider="anthropic",
                providerLabel="Anthropic",
                error=_OpenAiSdkError(request_id="req-wrong-boundary"),
            )
        )

        self.assertEqual(unrelated.record["category"], "unknown")
        self.assertFalse(unrelated.record["retryable"])
        self.assertEqual(anthropic.record["category"], "unknown")
        self.assertFalse(anthropic.record["retryable"])
        self.assertIsNone(anthropic.record.get("requestId"))

    def test_sdk_stream_exception_retries_output_free_and_preserves_lineage(self) -> None:
        attempts = 0
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _OpenAiSdkError(
                    body={"error": {"type": "server_error", "code": "internal_error"}},
                    request_id="req-first",
                )
            return _completed()

        response = run(
            testing.sendOpenAiResponsesRequest(
                token="test",
                mode="codex",
                request={
                    "model": "gpt-5.6-sol",
                    "system": "stable",
                    "messages": [{"role": "user", "content": "work"}],
                    "tools": [],
                },
                runtime={"emitEvent": lambda event, data: events.append((event, data))},
                maxAttempts=2,
                retryDelay=lambda _attempt: 0,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(attempts, 2)
        self.assertEqual(response["content"], [{"type": "text", "text": "recovered"}])
        retry = next(data for event, data in events if event == "provider.retrying")
        recovered = next(data for event, data in events if event == "provider.recovered")
        self.assertEqual(retry["category"], "server")
        self.assertEqual(retry["requestId"], "req-first")
        self.assertEqual(retry["generation"], 1)
        self.assertEqual(recovered["generation"], 2)
        self.assertEqual(recovered["logicalCallId"], retry["logicalCallId"])

    def test_sdk_stream_exception_exhaustion_is_bounded(self) -> None:
        attempts = 0

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            nonlocal attempts
            attempts += 1
            raise _OpenAiSdkError(
                body={"error": {"type": "server_error"}},
                request_id=f"req-{attempts}",
            )

        with self.assertRaises(core.ProviderRequestError) as raised:
            run(
                testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request={
                        "model": "gpt-5.6-sol",
                        "system": "stable",
                        "messages": [{"role": "user", "content": "work"}],
                        "tools": [],
                    },
                    runtime={},
                    maxAttempts=2,
                    retryDelay=lambda _attempt: 0,
                    clientFactory=lambda _options: SimpleNamespace(
                        responses=SimpleNamespace(create=create)
                    ),
                )
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(raised.exception.record["attempt"], 2)
        self.assertEqual(raised.exception.record["generation"], 2)
        self.assertEqual(raised.exception.record["requestId"], "req-2")

    def test_interrupted_preview_receipt_is_typed_runtime_state_not_a_user_message(self) -> None:
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.6-sol",
                effort="high",
                systemPrompt="stable",
                permissionMode="bypassPermissions",
            )
            session["messages"].append({"role": "user", "content": "original request"})
            receipt = {
                "type": "assistant_interrupted_preview",
                "logicalCallId": "logical-receipt",
                "generation": 2,
                "deliveryMode": "replaceable",
                "textHash": "a" * 64,
                "textLength": 42,
                "deliveryAcknowledged": True,
                "reason": "generation_attempts_exhausted",
            }
            failure = core.createProviderFailureRecord(
                provider="openai",
                providerLabel="OpenAI/Codex",
                phase="stream_read",
                category="server",
                retryable=True,
                attempt=2,
                hadAssistantOutput=True,
                hadToolUse=False,
                logicalCallId="logical-receipt",
                generation=2,
                partialOutputMode="replaceable",
                interruptedPreview=receipt,
            )

            run(
                core.persist_provider_failure(
                    session,
                    {"sessionPersistence": True},
                    failure,
                )
            )

            self.assertEqual(
                session["messages"], [{"role": "user", "content": "original request"}]
            )
            self.assertEqual(
                session["metadata"]["lastAssistantInterruptedPreview"]["type"],
                "assistant_interrupted_preview",
            )
            log_path = Path(core.session_transcript_log_path(session["id"]))
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            interrupted = next(
                item
                for item in records
                if item["type"] == "assistant_interrupted_preview"
            )
            self.assertEqual(interrupted["receipt"]["logicalCallId"], "logical-receipt")
            self.assertNotIn("role", interrupted)
        finally:
            self.restore_home(root, previous)
