from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers import _core
from agent_core.providers._core import (
    create_provider_cancelled_error,
    create_provider_timeout_error,
    provider_retry_delay_ms,
    race_provider_activity,
    signal_aborted,
)
from agent_core.providers.errors import (
    ProviderRequestError,
    create_provider_failure_record,
    create_provider_request_error,
    fallback_eligible_provider_failure,
    anthropic_retry_delay_ms,
)
from agent_core.providers.watchdog import AbortSignal


def _race(awaitable, **overrides):
    options = {
        "provider": "openai",
        "provider_label": "OpenAI Responses",
        "message_label": "OpenAI",
        "timeout_ms": 5_000,
    }
    options.update(overrides)
    return race_provider_activity(awaitable, **options)


class _RecordingTurnSession:
    def __init__(self):
        self.bound = []
        self.cleared = []

    def bind_activity_task(self, task):
        self.bound.append(task)

    def clear_activity_task(self, task):
        self.cleared.append(task)


class CoreRaceTests(unittest.TestCase):
    def test_outer_cancellation_cancels_the_inner_task(self):
        async def exercise():
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def inner():
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            race = asyncio.ensure_future(_race(inner()))
            await started.wait()
            race.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await race
            await asyncio.wait_for(cancelled.wait(), 1)

        run(exercise())

    def test_timeout_after_output_blocks_retry_and_fallback_decisions(self):
        async def exercise():
            async def inner():
                await asyncio.sleep(30)

            with self.assertRaises(ProviderRequestError) as ctx:
                await _race(inner(), timeout_ms=1, had_output=True, phase="stream_read")
            return ctx.exception

        failure = run(exercise())
        record = failure.record
        self.assertEqual(record["category"], "timeout")
        # The record flags the fault class as retryable; the decisions carry
        # the partial-output policy.
        self.assertTrue(record["retryable"])
        self.assertTrue(record["hadAssistantOutput"])
        self.assertEqual(record["code"], "provider_stream_idle_timeout")
        self.assertFalse(core.can_retry_provider_failure(failure, 5))
        self.assertFalse(fallback_eligible_provider_failure(failure))

    def test_timeout_without_output_is_retryable(self):
        async def exercise():
            async def inner():
                await asyncio.sleep(30)

            with self.assertRaises(ProviderRequestError) as ctx:
                await _race(inner(), timeout_ms=1)
            return ctx.exception.record

        record = run(exercise())
        self.assertEqual(record["category"], "timeout")
        self.assertTrue(record["retryable"])
        self.assertEqual(record["code"], "provider_request_start_timeout")

    def test_abort_cancels_task_and_raises_cancelled(self):
        async def exercise():
            signal = AbortSignal()
            turn = _RecordingTurnSession()
            cancelled = asyncio.Event()

            async def inner():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def abort_soon():
                await asyncio.sleep(0.02)
                signal.abort()

            aborter = asyncio.create_task(abort_soon())
            with self.assertRaises(ProviderRequestError) as ctx:
                await _race(inner(), abort_signal=signal, turn_session=turn)
            await aborter
            await asyncio.wait_for(cancelled.wait(), 1)
            return ctx.exception.record, turn

        record, turn = run(exercise())
        self.assertEqual(record["category"], "cancelled")
        self.assertFalse(record["retryable"])
        self.assertEqual(len(turn.bound), 1)
        self.assertEqual(turn.cleared, turn.bound)

    def test_clear_activity_task_runs_on_timeout_exit(self):
        async def exercise():
            turn = _RecordingTurnSession()

            async def inner():
                await asyncio.sleep(30)

            with self.assertRaises(ProviderRequestError):
                await _race(inner(), timeout_ms=1, turn_session=turn)
            return turn

        turn = run(exercise())
        self.assertEqual(turn.cleared, turn.bound)

    def test_activity_state_extends_deadline_and_overrides_flags(self):
        async def exercise():
            state = {
                "last_activity": None,
                "saw_activity": True,
                "had_assistant_output": True,
                "had_tool_use": False,
            }

            async def inner():
                await asyncio.sleep(30)

            state["last_activity"] = asyncio.get_running_loop().time() - 100
            with self.assertRaises(ProviderRequestError) as ctx:
                await _race(
                    inner(),
                    timeout_ms=50,
                    phase="request_start",
                    activity_state=state,
                )
            return ctx.exception.record

        record = run(exercise())
        self.assertEqual(record["phase"], "stream_read")
        self.assertTrue(record["hadAssistantOutput"])


class CoreRetryPolicyTests(unittest.TestCase):
    LADDER = (1_000, 3_000, 5_000)

    def test_ladder_plus_bounded_jitter(self):
        for attempt, base in ((1, 1_000), (2, 3_000), (3, 5_000), (9, 5_000)):
            delay = provider_retry_delay_ms(attempt, ladder=self.LADDER)
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 119)

    def test_retry_after_extends_the_delay(self):
        failure = create_provider_request_error(
            create_provider_failure_record(
                provider="openai",
                providerLabel="OpenAI Responses",
                phase="request_start",
                category="rate_limit",
                retryable=True,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                retryAfterMs=15_000,
            )
        )
        with mock.patch.object(_core.random, "randint", return_value=0):
            delay = provider_retry_delay_ms(1, ladder=self.LADDER, failure=failure)
        self.assertEqual(delay, 15_000)

    def test_retry_after_is_capped(self):
        failure = create_provider_request_error(
            create_provider_failure_record(
                provider="openai",
                providerLabel="OpenAI Responses",
                phase="request_start",
                category="rate_limit",
                retryable=True,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                retryAfterMs=600_000,
            )
        )
        with mock.patch.object(_core.random, "randint", return_value=0):
            delay = provider_retry_delay_ms(1, ladder=self.LADDER, failure=failure)
        self.assertEqual(delay, 60_000)

    def test_short_retry_after_never_shrinks_the_ladder(self):
        failure = create_provider_request_error(
            create_provider_failure_record(
                provider="openai",
                providerLabel="OpenAI Responses",
                phase="request_start",
                category="rate_limit",
                retryable=True,
                attempt=3,
                hadAssistantOutput=False,
                hadToolUse=False,
                retryAfterMs=100,
            )
        )
        with mock.patch.object(_core.random, "randint", return_value=0):
            delay = provider_retry_delay_ms(3, ladder=self.LADDER, failure=failure)
        self.assertEqual(delay, 5_000)

    def test_openai_module_delay_now_jittered(self):
        deltas = set()
        ladder_one = 1_000
        for _ in range(40):
            deltas.add(openai_module.shared.openai_retry_delay_ms(1) - ladder_one)
        self.assertTrue(all(0 <= delta <= 119 for delta in deltas))

    def test_anthropic_delay_honors_retry_after_failure(self):
        failure = create_provider_request_error(
            create_provider_failure_record(
                provider="anthropic",
                providerLabel="Anthropic",
                phase="request_start",
                category="rate_limit",
                retryable=True,
                attempt=1,
                hadAssistantOutput=False,
                hadToolUse=False,
                retryAfterMs=20_000,
            )
        )
        with mock.patch.object(_core.random, "randint", return_value=0):
            delay = anthropic_retry_delay_ms(1, failure=failure)
        self.assertEqual(delay, 20_000)


class FallbackEligibilityTests(unittest.TestCase):
    def _failure(self, category, had_output=False, had_tool_use=False):
        return create_provider_request_error(
            create_provider_failure_record(
                provider="openai",
                providerLabel="OpenAI Responses",
                phase="stream_read",
                category=category,
                retryable=True,
                attempt=1,
                hadAssistantOutput=had_output,
                hadToolUse=had_tool_use,
            )
        )

    def test_clean_timeout_is_fallback_eligible(self):
        self.assertTrue(fallback_eligible_provider_failure(self._failure("timeout")))

    def test_timeout_after_partial_output_is_not_fallback_eligible(self):
        self.assertFalse(
            fallback_eligible_provider_failure(self._failure("timeout", had_output=True))
        )

    def test_transport_after_tool_use_is_not_fallback_eligible(self):
        self.assertFalse(
            fallback_eligible_provider_failure(
                self._failure("transport", had_tool_use=True)
            )
        )

    def test_quota_stays_ineligible(self):
        self.assertFalse(fallback_eligible_provider_failure(self._failure("quota")))


class SignalSetTests(unittest.TestCase):
    def test_signal_aborted_accepts_dicts_objects_and_none(self):
        self.assertFalse(signal_aborted(None))
        self.assertFalse(signal_aborted({"aborted": False}))
        self.assertTrue(signal_aborted({"aborted": True}))
        signal = AbortSignal()
        self.assertFalse(signal_aborted(signal))
        signal.abort()
        self.assertTrue(signal_aborted(signal))

    def test_cancelled_and_timeout_builders_share_the_policy(self):
        cancelled = create_provider_cancelled_error(
            provider="anthropic",
            provider_label="Anthropic",
            message_label="Anthropic",
            phase="stream_read",
            attempt=2,
            had_output=True,
            had_tool_use=False,
        )
        self.assertEqual(cancelled.record["category"], "cancelled")
        self.assertFalse(cancelled.record["retryable"])
        self.assertEqual(
            cancelled.record["rawMessage"], "Anthropic request was cancelled."
        )
        timeout = create_provider_timeout_error(
            provider="openai",
            provider_label="OpenAI/Codex",
            message_label="OpenAI",
            phase="stream_read",
            attempt=1,
            had_output=False,
            had_tool_use=True,
            timeout_ms=1234,
        )
        self.assertEqual(timeout.record["category"], "timeout")
        self.assertTrue(timeout.record["retryable"])
        self.assertIn("1234ms", timeout.record["rawMessage"])


if __name__ == "__main__":
    unittest.main()
