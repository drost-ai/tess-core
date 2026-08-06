from __future__ import annotations

from pathlib import Path

from tests.support.agent_core_case import *

import agent_core.agent_loop.context as context_module
from agent_core.providers.prompt_cache_state import promptCacheStatePath, readPromptCacheState
from agent_core.providers.anthropic_parts.payloads import buildAnthropicMessagesEnvelope
from agent_core.providers.anthropic_parts.sse import normalizeAnthropicSseResponse
from agent_core.providers.prompt_cache_finalization import (
    isPromptCacheEvictionTerminalSuccess,
    recordPromptCacheEvictionIntentAtFinalization,
)
from agent_core.runtime.paths import resolveRuntimePaths


class PromptCacheLifecycleTests(AgentCoreTestCase):
    def setUp(self):
        self.root, self.home, self.workspace, self.previous = self.temp_context()

    def tearDown(self):
        self.restore_home(self.root, self.previous)

    def _config(self, *, evict=False):
        return {
            "cwd": str(self.workspace),
            "runtimeHome": str(self.home),
            "allowedRoots": [str(self.workspace)],
            "agents": {},
            "provider": "anthropic",
            "providerKind": "anthropic",
            "providerProfileName": "official",
            "providerProfile": {"kind": "anthropic", "name": "official", "auth": {}},
            "providerProfiles": {"official": {"kind": "anthropic", "name": "official"}},
            "apiKey": "test-key",
            "customHeaders": {},
            "model": "claude-opus-4-8",
            "effort": "medium",
            "assistantName": "Test",
            "assistantDescription": "",
            "systemPrompt": "stable system",
            "tools": [],
            "allowedTools": [],
            "disallowedTools": [],
            "permissionMode": "bypassPermissions",
            "sessionPersistence": False,
            "promptCache": {
                "enabled": True,
                "mode": "explicit",
                "ttl": "1h",
                "providerDiagnostics": True,
                "evictOnComplete": evict,
            },
        }

    def _session(self):
        return core.createEmptySession(
            cwd=str(self.workspace),
            provider="anthropic",
            model="claude-opus-4-8",
            effort="medium",
            systemPrompt="stable system",
            permissionMode="bypassPermissions",
        )

    def _run_turn(self, session, config, send, events, runtime_overrides=None):
        async def fake_measure(_args):
            return {"inputTokens": 1200, "source": "deterministic-test"}

        runtime = {"emitEvent": lambda event, data: events.append((event, data)), "tools": []}
        runtime.update(runtime_overrides or {})
        with mock.patch.object(context_module, "measureRequestInputTokens", fake_measure):
            return run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, runtime),
                    prompt="continue",
                    providerOverride={"send": send},
                )
            )

    def test_two_successful_turns_commit_state_and_reuse_diagnostic_id(self):
        session, config, events, sent_ids = self._session(), self._config(), [], []

        async def send(request):
            envelope = buildAnthropicMessagesEnvelope(request, config)
            sent_ids.append(envelope["payload"]["diagnostics"]["previous_message_id"])
            observation = {
                **envelope["promptCache"],
                "responseId": f"msg_{len(sent_ids)}",
                "providerDiagnosis": "warmup_no_comparison" if len(sent_ids) == 1 else "no_divergence",
            }
            return {
                "model": request["model"],
                "stopReason": "end_turn",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_creation_5m_input_tokens": 0,
                    "cache_creation_1h_input_tokens": 0,
                    "cache_read_input_tokens": 50,
                    "cache_creation_ttl_detail_complete": True,
                },
                "content": [{"type": "text", "text": "done"}],
                "promptCache": observation,
            }

        self._run_turn(session, config, send, events)
        self._run_turn(session, config, send, events)
        self.assertEqual(sent_ids, [None, "msg_1"])
        paths = resolveRuntimePaths(candidate=str(self.home))
        state = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["response"]["id"], "msg_2")
        observed = [data for event, data in events if event == "provider.prompt_cache" and data.get("phase") == "observed"]
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[-1]["state_status"], "committed")

    def test_terminal_success_records_eviction_but_failure_does_not_create_state(self):
        session, config, events = self._session(), self._config(evict=True), []

        async def send(request):
            envelope = buildAnthropicMessagesEnvelope(request, config)
            return {
                "model": request["model"],
                "stopReason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [{"type": "text", "text": "complete"}],
                "promptCache": {**envelope["promptCache"], "providerDiagnosis": "warmup_no_comparison"},
            }

        self._run_turn(session, config, send, events)
        paths = resolveRuntimePaths(candidate=str(self.home))
        state = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertTrue(state["eviction"]["pending"])
        self.assertEqual(
            [data["phase"] for event, data in events if event == "provider.prompt_cache"],
            ["observed", "eviction_intent"],
        )

        failed_session, failed_events = self._session(), []

        async def fail(_request):
            raise RuntimeError("provider failed before terminal response")

        with self.assertRaises(RuntimeError):
            self._run_turn(failed_session, config, fail, failed_events)
        missing = run(readPromptCacheState({"sessionId": failed_session["id"], "paths": paths}))
        self.assertEqual(missing["status"], "missing")

    def test_context_failure_commits_only_the_reactive_relief_success(self):
        session, config, events, attempts = self._session(), self._config(), [], []

        async def send(request):
            attempts.append(request["promptCache"]["dispatchContext"]["dispatchOrder"]["reactiveAttempt"])
            if len(attempts) == 1:
                raise core.createProviderRequestError(
                    core.createProviderFailureRecord(
                        provider="anthropic",
                        providerLabel="Anthropic",
                        phase="request_start",
                        category="client_request",
                        retryable=False,
                        attempt=1,
                        hadAssistantOutput=False,
                        hadToolUse=False,
                        rawMessage="maximum context length exceeded",
                    )
                )
            envelope = buildAnthropicMessagesEnvelope(request, config)
            return {
                "model": request["model"],
                "stopReason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [{"type": "text", "text": "relieved"}],
                "promptCache": {**envelope["promptCache"], "providerDiagnosis": "warmup_no_comparison"},
            }

        self._run_turn(session, config, send, events)
        self.assertEqual(attempts, [0, 1])
        paths = resolveRuntimePaths(candidate=str(self.home))
        state = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["request"]["preparation"], "reactive_relief")
        observed = [data for event, data in events if event == "provider.prompt_cache" and data.get("phase") == "observed"]
        self.assertEqual(len(observed), 1)

    def test_context_attempts_exhausted_and_partial_stream_write_nothing(self):
        for kind in ("context", "partial_stream"):
            session, config, events = self._session(), self._config(), []

            async def fail(_request, failure=kind):
                if failure == "partial_stream":
                    return normalizeAnthropicSseResponse(
                        'event: message_start\ndata: {"type":"message_start","message":{"id":"partial","usage":{}}}\n\n',
                        {"model": "claude-opus-4-8", "messages": [], "tools": []},
                    )
                raise core.createProviderRequestError(
                    core.createProviderFailureRecord(
                        provider="anthropic", providerLabel="Anthropic", phase="request_start",
                        category="client_request", retryable=False, attempt=1,
                        hadAssistantOutput=False, hadToolUse=False,
                        rawMessage="maximum context length exceeded",
                    )
                )

            with self.subTest(kind=kind), self.assertRaises(Exception):
                self._run_turn(session, config, fail, events)
            paths = resolveRuntimePaths(candidate=str(self.home))
            self.assertEqual(
                run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["status"],
                "missing",
            )
            self.assertFalse(any(event == "provider.prompt_cache" for event, _ in events))

    def test_valid_empty_then_valid_retry_commits_both_observations(self):
        session, config, events = self._session(), self._config(), []
        calls = 0

        async def send(request):
            nonlocal calls
            calls += 1
            envelope = buildAnthropicMessagesEnvelope(request, config)
            return {
                "model": request["model"], "stopReason": "end_turn",
                "content": [] if calls == 1 else [{"type": "text", "text": "retried"}],
                "usage": {"cache_creation_input_tokens": calls},
                "promptCache": {**envelope["promptCache"], "responseId": f"empty-{calls}"},
            }

        result = self._run_turn(session, config, send, events)
        self.assertEqual((result["stopReason"], result["text"]), ("end_turn", "retried"))
        self.assertEqual(result["usage"]["cache_creation_input_tokens"], 3)
        paths = resolveRuntimePaths(candidate=str(self.home))
        state = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertEqual((state["revision"], state["response"]["id"]), (2, "empty-2"))
        observed = [data for event, data in events if event == "provider.prompt_cache" and data.get("phase") == "observed"]
        self.assertEqual(len(observed), 2)
        retries = [data for event, data in events if event == "provider.empty_response"]
        self.assertEqual([(item["attempt"], item["retrying"]) for item in retries], [(1, True)])

    def test_repeated_empty_response_stops_after_one_retry_without_assistant_message(self):
        session, config, events = self._session(), self._config(evict=True), []
        calls = 0

        async def send(request):
            nonlocal calls
            calls += 1
            envelope = buildAnthropicMessagesEnvelope(request, config)
            if calls == 2:
                self.assertIn("[Continuation required]", json.dumps(request["messages"]))
            return {
                "model": request["model"], "stopReason": "end_turn", "content": [],
                "usage": {"input_tokens": 5, "output_tokens": 0},
                "promptCache": {**envelope["promptCache"], "responseId": f"empty-{calls}"},
            }

        result = self._run_turn(session, config, send, events)
        self.assertEqual((calls, result["stopReason"]), (2, "empty_response"))
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertFalse(any(message.get("role") == "assistant" for message in session["messages"]))
        empty_events = [data for event, data in events if event == "provider.empty_response"]
        self.assertEqual(
            [(item["attempt"], item["retrying"]) for item in empty_events],
            [(1, True), (2, False)],
        )
        paths = resolveRuntimePaths(candidate=str(self.home))
        state = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertEqual(state["revision"], 2)
        self.assertNotIn("eviction", state)

    def test_terminal_observation_commits_before_cancellation_is_honored(self):
        session, config, events = self._session(), self._config(), []
        signal = SimpleNamespace(aborted=False, reason=None)

        async def send(request):
            envelope = buildAnthropicMessagesEnvelope(request, config)
            signal.aborted = True
            signal.reason = RuntimeError("cancelled after terminal response")
            return {
                "model": request["model"], "stopReason": "end_turn",
                "content": [{"type": "text", "text": "terminal"}],
                "usage": {"cache_creation_input_tokens": 1},
                "promptCache": {**envelope["promptCache"], "responseId": "terminal-id"},
            }

        result = self._run_turn(session, config, send, events, {"abortSignal": signal})
        self.assertEqual(result["stopReason"], "cancelled")
        paths = resolveRuntimePaths(candidate=str(self.home))
        state = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertEqual((state["revision"], state["response"]["id"]), (1, "terminal-id"))
        observed = [data for event, data in events if event == "provider.prompt_cache" and data.get("phase") == "observed"]
        self.assertEqual(len(observed), 1)

    def test_cancellation_before_terminal_response_never_commits(self):
        session, config, events = self._session(), self._config(), []
        signal = SimpleNamespace(aborted=False, reason=None)

        async def send(_request):
            signal.aborted = True
            error = RuntimeError("cancelled before terminal response")
            error.name = "AbortError"
            signal.reason = error
            raise error

        result = self._run_turn(session, config, send, events, {"abortSignal": signal})
        self.assertEqual(result["stopReason"], "cancelled")
        paths = resolveRuntimePaths(candidate=str(self.home))
        self.assertEqual(
            run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["status"],
            "missing",
        )

    def test_state_and_event_callback_failures_cannot_change_provider_success(self):
        session, config, events = self._session(), self._config(), []
        paths = resolveRuntimePaths(candidate=str(self.home))
        Path(promptCacheStatePath(session["id"], paths)).mkdir(parents=True)

        async def send(request):
            envelope = buildAnthropicMessagesEnvelope(request, config)
            return {
                "model": request["model"], "stopReason": "end_turn",
                "content": [{"type": "text", "text": "provider success"}],
                "usage": {"cache_creation_input_tokens": 10},
                "promptCache": envelope["promptCache"],
            }

        result = self._run_turn(session, config, send, events)
        self.assertEqual(result["text"], "provider success")
        self.assertTrue(any(event == "provider.prompt_cache" and data.get("phase") == "state_error" for event, data in events))

        other_session = self._session()

        class ExplodingEvents:
            def append(self, _event):
                raise RuntimeError("observer failure")

        result = self._run_turn(other_session, config, send, ExplodingEvents())
        self.assertEqual(result["text"], "provider success")
        self.assertEqual(
            run(readPromptCacheState({"sessionId": other_session["id"], "paths": paths}))["state"]["revision"],
            1,
        )

    def test_eviction_terminal_matrix_is_success_only(self):
        cases = tuple(({"stopReason": reason}, True) for reason in ("end_turn", "stop", "completed")) + tuple(
            ({"stopReason": reason}, False)
            for reason in (
                "tool_use", "empty_response", "cancelled", "checkpoint_requested",
                "approval_required", "plan_approval_required", "plan_rejected",
                "plan_chat_requested", "max_output_tokens", "max_budget_exceeded",
                "subagent_max_turns_exceeded", "subagent_max_tool_calls_exceeded", "unknown",
            )
        ) + (
            ({"stopReason": "end_turn", "signal": "SIGINT"}, False),
            ({"stopReason": "end_turn", "error": "late failure"}, False),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(isPromptCacheEvictionTerminalSuccess(args), expected)

    def test_finalization_intent_is_idempotent_and_observer_safe(self):
        session, config, events = self._session(), self._config(evict=True), []

        async def send(request):
            envelope = buildAnthropicMessagesEnvelope(request, config)
            return {
                "model": request["model"], "stopReason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": "done"}],
                "promptCache": envelope["promptCache"],
            }

        self._run_turn(session, config, send, events)
        paths = resolveRuntimePaths(candidate=str(self.home))
        before = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        run(
            recordPromptCacheEvictionIntentAtFinalization(
                {
                    "session": session, "config": config,
                    "runtime": {"emitEvent": lambda *_: (_ for _ in ()).throw(RuntimeError("observer"))},
                    "stopReason": "end_turn",
                }
            )
        )
        after = run(readPromptCacheState({"sessionId": session["id"], "paths": paths}))["state"]
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["eviction"]["finalizationKey"], before["eviction"]["finalizationKey"])

if __name__ == "__main__":
    unittest.main()
