from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.errors import (
    ProviderWireError,
    normalizeUnknownProviderFailure,
)


def _normalize(error, provider="openai"):
    return normalizeUnknownProviderFailure(
        error=error,
        provider=provider,
        providerLabel="Test Provider",
        phase="stream_read",
        attempt=1,
        hadAssistantOutput=False,
        hadToolUse=False,
    )


class ProviderWireEvidenceClassificationTests(unittest.TestCase):
    def test_status_429_rate_limit_classifies_retryable_with_evidence(self):
        failure = _normalize(
            ProviderWireError(
                "OpenAI request failed with status 429",
                status=429,
                body='{"error":{"message":"Rate limit reached","type":"rate_limit_error","code":"rate_limit_exceeded"}}',
                code="rate_limit_exceeded",
                errorType="rate_limit_error",
                requestId="req_wire_429",
                retryAfterMs=1500,
            )
        )
        self.assertEqual(failure.record["category"], "rate_limit")
        self.assertTrue(failure.record["retryable"])
        self.assertEqual(failure.record["status"], 429)
        self.assertEqual(failure.record["code"], "rate_limit_exceeded")
        self.assertEqual(failure.record["requestId"], "req_wire_429")
        self.assertEqual(failure.record["retryAfterMs"], 1500)

    def test_status_429_quota_body_classifies_non_retryable_quota(self):
        failure = _normalize(
            ProviderWireError(
                "OpenAI request failed with status 429",
                status=429,
                body='{"error":{"message":"You exceeded your current quota, please check your billing"}}',
            )
        )
        self.assertEqual(failure.record["category"], "quota")
        self.assertFalse(failure.record["retryable"])

    def test_status_401_classifies_auth_non_retryable(self):
        failure = _normalize(
            ProviderWireError(
                "OpenAI request failed with status 401",
                status=401,
                body='{"error":{"message":"Incorrect API key provided","type":"invalid_request_error"}}',
            )
        )
        self.assertEqual(failure.record["category"], "auth")
        self.assertFalse(failure.record["retryable"])

    def test_status_500_classifies_server_retryable(self):
        failure = _normalize(
            ProviderWireError(
                "OpenAI request failed with status 500",
                status=500,
                body='{"error":{"message":"The server had an error"}}',
            )
        )
        self.assertEqual(failure.record["category"], "server")
        self.assertTrue(failure.record["retryable"])

    def test_classification_is_not_gated_on_provider_name(self):
        failure = _normalize(
            ProviderWireError("upstream failed", status=429, body="slow down"),
            provider="anthropic",
        )
        self.assertEqual(failure.record["category"], "rate_limit")
        self.assertTrue(failure.record["retryable"])

    def test_statusless_evidence_classifies_from_code_and_type(self):
        failure = _normalize(
            ProviderWireError(
                "stream carried an in-band error",
                code="rate_limit_exceeded",
                errorType="rate_limit_error",
            )
        )
        self.assertEqual(failure.record["category"], "rate_limit")
        self.assertTrue(failure.record["retryable"])

    def test_foreign_exception_with_evidence_attribute_participates(self):
        class ThirdPartyTransportError(Exception):
            provider_error_evidence = {
                "status": 503,
                "body": "Service Unavailable",
                "requestId": "req_foreign",
            }

        failure = _normalize(ThirdPartyTransportError("upstream 503"))
        self.assertEqual(failure.record["category"], "server")
        self.assertTrue(failure.record["retryable"])
        self.assertEqual(failure.record["status"], 503)
        self.assertEqual(failure.record["requestId"], "req_foreign")

    def test_malformed_evidence_attribute_falls_back_to_text_heuristics(self):
        class BrokenEvidence(ConnectionResetError):
            provider_error_evidence = "not-a-mapping"

        failure = _normalize(BrokenEvidence("connection reset by peer"))
        self.assertEqual(failure.record["category"], "transport")
        self.assertTrue(failure.record["retryable"])

    def test_wire_error_body_lands_in_raw_body_diagnostics(self):
        failure = _normalize(
            ProviderWireError(
                "OpenAI request failed with status 500",
                status=500,
                body='{"error":{"message":"boom"}}',
            )
        )
        self.assertIn("boom", failure.record.get("rawBody") or "")

    def test_openai_sdk_module_exception_still_classifies(self):
        error_type = type("APIStatusError", (Exception,), {})
        error_type.__module__ = "openai"
        error = error_type("Error code: 429 - {'error': {'message': 'rate limited'}}")
        error.status_code = 429
        error.body = {"error": {"message": "rate limited"}}
        failure = _normalize(error)
        self.assertEqual(failure.record["category"], "rate_limit")
        self.assertTrue(failure.record["retryable"])


if __name__ == "__main__":
    unittest.main()
