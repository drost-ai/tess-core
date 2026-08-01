from __future__ import annotations

from agent_core.provider_turn_session import createProviderTurnSession
from agent_core.provider_watchdog import AbortSignal
from agent_core.providers.anthropic_http_transport import (
    activeAnthropicTransportThreadCount,
)
from agent_core_harness.fake_providers.server import FakeProviderHttpServer
from agent_core_harness.performance import (
    ANTHROPIC_TRANSPORT_BENCHMARK,
    runAnthropicTransportBenchmark,
)
from tests.support.agent_core_case import *


def _config(server, *, keep_alive=False):
    return {
        "provider": "anthropic",
        "anthropicBaseUrl": server.base_url,
        "apiKey": "package45-key",
        "customHeaders": {"x-package": "45"},
        "providerCapabilities": {"anthropicHttpKeepAlive": keep_alive},
    }


def _request():
    return {
        "model": "claude-sonnet-4-6",
        "system": "Package 45 test.",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
    }


def _turn(config, runtime, suffix="one"):
    return createProviderTurnSession(
        logical_turn_id=f"package45-{suffix}",
        session={"id": f"package45-{suffix}", "provider": "anthropic"},
        config=config,
        runtime=runtime,
        model="claude-sonnet-4-6",
    )


async def _send(config, runtime, turn, **kwargs):
    return await core.sendAnthropicMessagesRequest(
        config=config,
        request=_request(),
        runtime=runtime,
        maxAttempts=kwargs.pop("maxAttempts", 1),
        turnSession=turn,
        **kwargs,
    )


class Package45CancellationTests(unittest.TestCase):
    def test_cancel_closes_socket_settles_worker_and_preserves_verdict(self):
        async def exercise():
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic",
                script={"messages": [{"bodyDelayMs": 500}]},
                request_recorder=requests,
            ).start()
            signal = AbortSignal()
            runtime = {
                "abortSignal": signal,
                "streamProviderMessages": True,
                "emitEvent": lambda *_: None,
            }
            config = _config(server)
            turn = _turn(config, runtime, "cancel")

            async def abort_after_request():
                while not requests:
                    await asyncio.sleep(0)
                await asyncio.sleep(0.005)
                started = time.perf_counter()
                signal.abort()
                return started

            abort_task = asyncio.create_task(abort_after_request())
            with self.assertRaises(core.ProviderRequestError) as ctx:
                await _send(config, runtime, turn, streamIdleTimeoutMs=1_000)
            abort_at = await abort_task
            elapsed_ms = (time.perf_counter() - abort_at) * 1_000
            await turn.close()
            for _ in range(40):
                if requests[0].get("clientDisconnected"):
                    break
                await asyncio.sleep(0.005)
            telemetry = turn.transport_telemetry()
            server.close()
            return ctx.exception.record, elapsed_ms, requests, telemetry

        record, elapsed_ms, requests, telemetry = run(exercise())
        self.assertEqual(record["category"], "cancelled")
        self.assertEqual(record["phase"], "request_start")
        self.assertFalse(record["retryable"])
        self.assertFalse(record["hadAssistantOutput"])
        self.assertLess(elapsed_ms, 50)
        self.assertTrue(requests[0].get("clientDisconnected"))
        self.assertEqual(telemetry["threadsStarted"], telemetry["threadsSettled"])
        self.assertEqual(activeAnthropicTransportThreadCount(), 0)

    def test_event_signal_has_one_wait_and_no_poll_wakeups(self):
        class CountingSignal(AbortSignal):
            def __init__(self):
                super().__init__()
                self.wait_calls = 0

            async def wait(self):
                self.wait_calls += 1
                await super().wait()

        async def exercise():
            signal = CountingSignal()
            future = asyncio.get_running_loop().create_future()
            started = time.perf_counter()
            task = asyncio.create_task(
                anthropic_module._race_anthropic_activity(
                    future,
                    timeout_ms=5_000,
                    abortSignal=signal,
                )
            )
            await asyncio.sleep(0.01)
            signal.abort()
            with self.assertRaises(core.ProviderRequestError):
                await task
            return signal.wait_calls, (time.perf_counter() - started) * 1_000

        wait_calls, elapsed_ms = run(exercise())
        self.assertEqual(wait_calls, 1)
        self.assertLess(elapsed_ms, 50)

    def test_fifty_rapid_cancels_leave_no_transport_threads(self):
        async def exercise():
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic",
                script={"messages": [{"bodyDelayMs": 500}]},
                request_recorder=requests,
            ).start()

            async def one(index):
                signal = AbortSignal()
                runtime = {
                    "abortSignal": signal,
                    "streamProviderMessages": True,
                    "emitEvent": lambda *_: None,
                }
                config = _config(server)
                turn = _turn(config, runtime, str(index))
                task = asyncio.create_task(
                    _send(config, runtime, turn, streamIdleTimeoutMs=2_000)
                )
                while len(requests) <= index:
                    await asyncio.sleep(0)
                signal.abort()
                with contextlib.suppress(core.ProviderRequestError):
                    await task
                await turn.close()

            await asyncio.gather(*(one(index) for index in range(50)))
            for _ in range(100):
                if len(requests) == 50 and all(
                    item.get("clientDisconnected") for item in requests
                ):
                    break
                await asyncio.sleep(0.005)
            server.close()

        run(exercise())
        self.assertEqual(activeAnthropicTransportThreadCount(), 0)

    def test_pinned_worker_emits_leak_diagnostic(self):
        async def exercise():
            emitted = []
            runtime = {"emitEvent": lambda event, data: emitted.append((event, data))}
            config = {"provider": "anthropic", "providerCapabilities": {}}
            turn = _turn(config, runtime, "leak")
            release = threading.Event()
            worker = threading.Thread(target=release.wait, name="package45-pinned")
            turn.register_transport_worker(worker)
            worker.start()
            await turn._settle_workers(timeout=0.005)
            release.set()
            worker.join(timeout=1)
            turn.unregister_transport_worker(worker)
            return emitted

        emitted = run(exercise())
        self.assertEqual(emitted[0][0], "provider.transport_thread_leak")
        self.assertEqual(emitted[0][1]["thread_name"], "package45-pinned")


class Package45KeepAliveTests(unittest.TestCase):
    def test_capability_reuses_same_turn_but_not_cross_turn(self):
        async def exercise():
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic", script={}, request_recorder=requests
            ).start()
            runtime = {"emitEvent": lambda *_: None}
            config = _config(server, keep_alive=True)
            first = _turn(config, runtime, "first")
            await _send(config, runtime, first)
            await _send(config, runtime, first)
            first_telemetry = first.transport_telemetry()
            await first.close()
            second = _turn(config, runtime, "second")
            await _send(config, runtime, second)
            await second.close()
            server.close()
            return requests, first_telemetry

        requests, telemetry = run(exercise())
        self.assertEqual([item["connectionId"] for item in requests], [1, 1, 2])
        self.assertEqual(telemetry["reusedRequests"], 1)

    def test_capability_defaults_off_and_preserves_wire_payload(self):
        async def exercise(enabled):
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic", script={}, request_recorder=requests
            ).start()
            runtime = {"emitEvent": lambda *_: None}
            config = _config(server, keep_alive=enabled)
            turn = _turn(config, runtime, f"wire-{enabled}")
            await _send(config, runtime, turn)
            await _send(config, runtime, turn)
            await turn.close()
            server.close()
            return requests

        fresh = run(exercise(False))
        reused = run(exercise(True))
        self.assertEqual(len({item["connectionId"] for item in fresh}), 2)
        self.assertEqual(len({item["connectionId"] for item in reused}), 1)
        self.assertEqual(fresh[0]["body"], reused[0]["body"])
        self.assertEqual(fresh[0]["headers"], reused[0]["headers"])
        self.assertEqual(fresh[0]["path"], reused[0]["path"])

    def test_stale_connection_latches_one_way_fallback(self):
        async def exercise():
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic",
                script={
                    "messages": [
                        {"closeAfterResponse": True},
                        {},
                        {},
                    ]
                },
                request_recorder=requests,
            ).start()
            runtime = {"emitEvent": lambda *_: None}
            config = _config(server, keep_alive=True)
            turn = _turn(config, runtime, "fallback")
            await _send(config, runtime, turn)
            await _send(config, runtime, turn, maxAttempts=2)
            await _send(config, runtime, turn)
            telemetry = turn.transport_telemetry()
            await turn.close()
            server.close()
            return requests, telemetry

        requests, telemetry = run(exercise())
        self.assertTrue(telemetry["fallbackLatched"])
        self.assertEqual(telemetry["fallbackCount"], 1)
        self.assertEqual(len({item["connectionId"] for item in requests}), 3)
        self.assertEqual(len(requests), 3)

    def test_protocol_failure_does_not_latch_transport_fallback(self):
        async def exercise():
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic",
                script={
                    "messages": [
                        {
                            "contentType": "text/event-stream",
                            "rawBody": "event: message_start\ndata: {bad}\n\n",
                        },
                        {},
                    ]
                },
                request_recorder=requests,
            ).start()
            runtime = {
                "streamProviderMessages": True,
                "emitEvent": lambda *_: None,
            }
            config = _config(server, keep_alive=True)
            turn = _turn(config, runtime, "protocol")
            with self.assertRaises(core.ProviderRequestError) as ctx:
                await _send(config, runtime, turn)
            await _send(config, runtime, turn)
            telemetry = turn.transport_telemetry()
            await turn.close()
            server.close()
            return ctx.exception.record, requests, telemetry

        failure, requests, telemetry = run(exercise())
        self.assertEqual(failure["category"], "protocol")
        self.assertFalse(telemetry["fallbackLatched"])
        self.assertEqual([item["connectionId"] for item in requests], [1, 1])

    def test_transport_benchmark_records_before_after_and_cancel(self):
        report = runAnthropicTransportBenchmark(
            samples=1,
            calls_per_turn=3,
            connection_setup_delay_ms=20,
        )
        self.assertEqual(report["benchmark"], ANTHROPIC_TRANSPORT_BENCHMARK)
        evidence = report["beforeAfter"]
        self.assertEqual(evidence["before"]["connectionCountMedian"], 3)
        self.assertEqual(evidence["after"]["connectionCountMedian"], 1)
        self.assertEqual(evidence["connectionsAvoidedPerTurn"], 2)
        self.assertEqual(evidence["modeledSetupLatencyAvoidedMs"], 40)
        self.assertEqual(report["cancellation"]["after"]["failureCategory"], "cancelled")
        self.assertTrue(report["cancellation"]["after"]["serverObservedDisconnect"])
        self.assertEqual(report["cancellation"]["after"]["liveThreadsAfterClose"], 0)
