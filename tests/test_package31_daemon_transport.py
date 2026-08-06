from __future__ import annotations

import socket
import importlib

from tests.support.agent_core_case import *

from agent_core.daemon.protocol import (
    DAEMON_MAX_PENDING_GLOBAL,
    DAEMON_MAX_PENDING_PER_CLIENT,
    DAEMON_MAX_REQUEST_FRAME_BYTES,
    DAEMON_MAX_RESPONSE_FRAME_BYTES,
    DAEMON_PROTOCOL_VERSION,
    DaemonLineDecoder,
    DaemonProtocolError,
    encodeDaemonFrame,
)
from agent_core.daemon.transport import (
    DaemonServerPeer,
    createAuthenticatedDaemonServer,
    readOneDaemonFrame,
)

daemon_client_module = importlib.import_module("agent_core.daemon.client")


_TOKEN = "a" * 43
_EPOCH = "transport-epoch"


async def _start_peer(
    dispatch,
    *,
    auth_timeout_ms=2_000,
    rpc_timeout_ms=30_000,
    on_authenticated=None,
):
    server_socket, client_socket = socket.socketpair()
    server_socket.setblocking(False)
    client_socket.setblocking(False)
    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    owner = createAuthenticatedDaemonServer(
        token=_TOKEN,
        epoch=_EPOCH,
        dispatch=dispatch,
        authTimeoutMs=auth_timeout_ms,
        rpcTimeoutMs=rpc_timeout_ms,
        onAuthenticated=on_authenticated,
    )
    peer = DaemonServerPeer(owner, server_reader, server_writer)
    owner.peers.add(peer)
    peer_task = asyncio.create_task(peer.run())
    return owner, peer, peer_task, client_reader, client_writer


async def _authenticate(reader, writer, *, token=_TOKEN):
    decoder = DaemonLineDecoder(DAEMON_MAX_RESPONSE_FRAME_BYTES)
    frame = encodeDaemonFrame(
        {"type": "auth", "protocolVersion": DAEMON_PROTOCOL_VERSION, "token": token},
        DAEMON_MAX_REQUEST_FRAME_BYTES,
    )
    for offset in range(0, len(frame), 3):
        writer.write(frame[offset : offset + 3])
        await writer.drain()
    response = await readOneDaemonFrame(reader, decoder, 1)
    return decoder, response


async def _close_fixture(owner, peer_task, writer):
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    await owner.close()
    await asyncio.gather(peer_task, return_exceptions=True)


class Package31DaemonTransportTests(AgentCoreTestCase):
    def test_foreign_legacy_and_wrong_token_peers_dispatch_nothing(self):
        async def scenario():
            dispatches = 0

            async def dispatch(_peer, _request, _signal):
                nonlocal dispatches
                dispatches += 1
                return {"result": "unexpected"}

            owner, _peer, peer_task, reader, writer = await _start_peer(dispatch)
            decoder = DaemonLineDecoder(DAEMON_MAX_RESPONSE_FRAME_BYTES)
            writer.write(encodeDaemonFrame({"id": "legacy", "method": "daemon.status"}, 1_024))
            await writer.drain()
            response = await readOneDaemonFrame(reader, decoder, 1)
            self.assertEqual(
                response,
                {
                    "id": "legacy",
                    "ok": False,
                    "error": "Daemon protocol v2 requires authentication; upgrade the client.",
                },
            )
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
            await _close_fixture(owner, peer_task, writer)

            owner, _peer, peer_task, reader, writer = await _start_peer(dispatch)
            writer.write(
                encodeDaemonFrame(
                    {"type": "auth", "protocolVersion": 2, "token": "z" * 43},
                    1_024,
                )
            )
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
            self.assertEqual(dispatches, 0)
            await _close_fixture(owner, peer_task, writer)

        run(scenario())

    def test_fragmented_authenticated_peer_dispatches_and_duplicate_id_closes(self):
        async def scenario():
            dispatches = []

            async def dispatch(_peer, request, _signal):
                dispatches.append(request["id"])
                return {"result": {"method": request["method"]}}

            owner, _peer, peer_task, reader, writer = await _start_peer(dispatch)
            decoder, auth = await _authenticate(reader, writer)
            self.assertEqual(auth["type"], "auth_ok")
            self.assertEqual(auth["epoch"], _EPOCH)
            request = encodeDaemonFrame({"id": "valid", "method": "daemon.status"}, 1_024)
            writer.write(request[:5])
            writer.write(request[5:])
            await writer.drain()
            response = await readOneDaemonFrame(reader, decoder, 1)
            self.assertEqual(
                response,
                {"id": "valid", "ok": True, "result": {"method": "daemon.status"}},
            )
            duplicate = encodeDaemonFrame({"id": "reused", "method": "daemon.status"}, 1_024)
            writer.write(duplicate + duplicate)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
            self.assertEqual(dispatches, ["valid", "reused"])
            await _close_fixture(owner, peer_task, writer)

        run(scenario())

    def test_oversized_incomplete_frame_closes_without_dispatch(self):
        async def scenario():
            dispatches = 0

            async def dispatch(_peer, _request, _signal):
                nonlocal dispatches
                dispatches += 1
                return {"result": None}

            owner, _peer, peer_task, reader, writer = await _start_peer(dispatch)
            _decoder, _auth = await _authenticate(reader, writer)
            writer.write(b"a" * (DAEMON_MAX_REQUEST_FRAME_BYTES + 1))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
            self.assertEqual(dispatches, 0)
            await _close_fixture(owner, peer_task, writer)

        run(scenario())

    def test_auth_and_rpc_deadlines_abort_work_and_bound_responses(self):
        async def scenario():
            async def unused(_peer, _request, _signal):
                return {"result": None}

            owner, _peer, peer_task, reader, writer = await _start_peer(
                unused,
                auth_timeout_ms=40,
            )
            started = time.monotonic()
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
            self.assertLess(time.monotonic() - started, 0.5)
            await _close_fixture(owner, peer_task, writer)

            authenticated_callback_finished = False

            async def slow_authenticated(_peer):
                nonlocal authenticated_callback_finished
                await asyncio.sleep(0.15)
                authenticated_callback_finished = True

            owner, _peer, peer_task, reader, writer = await _start_peer(
                unused,
                auth_timeout_ms=40,
                on_authenticated=slow_authenticated,
            )
            started = time.monotonic()
            _decoder, auth = await _authenticate(reader, writer)
            self.assertEqual(auth["type"], "auth_ok")
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertFalse(authenticated_callback_finished)
            await _close_fixture(owner, peer_task, writer)
            self.assertTrue(authenticated_callback_finished)

            mode = "hang"
            aborts = 0

            async def dispatch(_peer, _request, signal):
                nonlocal aborts
                if mode == "oversize":
                    return {"result": "x" * DAEMON_MAX_RESPONSE_FRAME_BYTES}
                await signal.wait()
                aborts += 1
                raise signal.reason

            owner, _peer, peer_task, reader, writer = await _start_peer(
                dispatch,
                rpc_timeout_ms=40,
            )
            decoder, _auth = await _authenticate(reader, writer)
            writer.write(encodeDaemonFrame({"id": "slow", "method": "daemon.status"}, 1_024))
            await writer.drain()
            response = await readOneDaemonFrame(reader, decoder, 1)
            self.assertEqual(response["id"], "slow")
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "Daemon RPC timed out.")
            self.assertEqual(aborts, 1)
            mode = "oversize"
            writer.write(encodeDaemonFrame({"id": "large", "method": "daemon.status"}, 1_024))
            await writer.drain()
            response = await readOneDaemonFrame(reader, decoder, 1)
            self.assertEqual(response["id"], "large")
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "Daemon response exceeded the 2 MiB frame limit.")
            await _close_fixture(owner, peer_task, writer)

        run(scenario())

    def test_peer_pending_ceiling_applies_before_dispatch_and_close_aborts_once(self):
        async def scenario():
            dispatches = 0
            aborts = 0

            async def dispatch(_peer, _request, signal):
                nonlocal dispatches, aborts
                dispatches += 1
                await signal.wait()
                aborts += 1
                raise signal.reason

            owner, peer, peer_task, reader, writer = await _start_peer(
                dispatch,
                rpc_timeout_ms=10_000,
            )
            decoder, _auth = await _authenticate(reader, writer)
            frames = b"".join(
                encodeDaemonFrame(
                    {"id": f"request-{index}", "method": "daemon.status"},
                    1_024,
                )
                for index in range(DAEMON_MAX_PENDING_PER_CLIENT + 1)
            )
            writer.write(frames)
            await writer.drain()
            deadline = time.monotonic() + 1
            while dispatches < DAEMON_MAX_PENDING_PER_CLIENT and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertEqual(dispatches, DAEMON_MAX_PENDING_PER_CLIENT)
            overflow = await readOneDaemonFrame(reader, decoder, 1)
            self.assertEqual(
                overflow,
                {
                    "id": f"request-{DAEMON_MAX_PENDING_PER_CLIENT}",
                    "ok": False,
                    "error": "Daemon RPC pending-request limit reached.",
                },
            )
            await peer.close()
            await peer.close()
            deadline = time.monotonic() + 1
            while aborts < DAEMON_MAX_PENDING_PER_CLIENT and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertEqual(aborts, DAEMON_MAX_PENDING_PER_CLIENT)
            self.assertEqual(owner.active_global, 0)
            await _close_fixture(owner, peer_task, writer)

        run(scenario())

    def test_global_ceiling_rejects_before_dispatch(self):
        async def scenario():
            dispatches = 0

            async def dispatch(_peer, _request, _signal):
                nonlocal dispatches
                dispatches += 1
                return {"result": None}

            owner, _peer, peer_task, reader, writer = await _start_peer(dispatch)
            owner.active_global = DAEMON_MAX_PENDING_GLOBAL
            decoder, _auth = await _authenticate(reader, writer)
            writer.write(encodeDaemonFrame({"id": "overflow", "method": "daemon.status"}, 1_024))
            await writer.drain()
            response = await readOneDaemonFrame(reader, decoder, 1)
            self.assertEqual(response["error"], "Daemon RPC pending-request limit reached.")
            self.assertEqual(dispatches, 0)
            owner.active_global = 0
            await _close_fixture(owner, peer_task, writer)

        run(scenario())

    def test_logical_client_pending_ceiling_and_close_settle_all_calls(self):
        async def scenario():
            readers = []
            writers = []

            class FakeReader:
                def __init__(self):
                    self.closed = asyncio.Event()

            class FakeWriter:
                def __init__(self, reader):
                    self.reader = reader
                    self.closed = False
                    self.close_count = 0

                def write(self, _value):
                    return None

                async def drain(self):
                    return None

                def close(self):
                    if self.closed:
                        return
                    self.closed = True
                    self.close_count += 1
                    self.reader.closed.set()

                async def wait_closed(self):
                    await self.reader.closed.wait()

            async def fake_open(_socket_path, _token, _epoch):
                reader = FakeReader()
                writer = FakeWriter(reader)
                readers.append(reader)
                writers.append(writer)
                return reader, writer

            async def fake_read(reader, _decoder, _timeout):
                await reader.closed.wait()
                raise RuntimeError("Daemon connection closed")

            original_open = daemon_client_module.openAuthenticatedDaemonConnection
            original_read = daemon_client_module.readOneDaemonFrame
            daemon_client_module.openAuthenticatedDaemonConnection = fake_open
            daemon_client_module.readOneDaemonFrame = fake_read
            try:
                client = daemon_client_module._create_daemon_client(
                    "/private/unused.sock",
                    {"epoch": _EPOCH},
                    {"token": _TOKEN},
                )
                pending = [
                    asyncio.create_task(client["_request"]("daemon.status", {}))
                    for _index in range(DAEMON_MAX_PENDING_PER_CLIENT)
                ]
                deadline = time.monotonic() + 1
                while len(readers) < DAEMON_MAX_PENDING_PER_CLIENT and time.monotonic() < deadline:
                    await asyncio.sleep(0)
                self.assertEqual(len(readers), DAEMON_MAX_PENDING_PER_CLIENT)
                with self.assertRaises(DaemonProtocolError) as raised:
                    await client["_request"]("daemon.status", {})
                self.assertEqual(raised.exception.code, "daemon.pending_limit")
                await client["close"]()
                await client["close"]()
                outcomes = await asyncio.gather(*pending, return_exceptions=True)
                self.assertTrue(all(isinstance(outcome, RuntimeError) for outcome in outcomes))
                self.assertTrue(all(writer.close_count == 1 for writer in writers))
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    await client["_request"]("daemon.status", {})
            finally:
                daemon_client_module.openAuthenticatedDaemonConnection = original_open
                daemon_client_module.readOneDaemonFrame = original_read

        run(scenario())

    def test_logical_client_rpc_timeout_releases_slot_and_closes_writer(self):
        async def scenario():
            writers = []

            class FakeWriter:
                def __init__(self):
                    self.close_count = 0

                def write(self, _value):
                    return None

                async def drain(self):
                    return None

                def close(self):
                    self.close_count += 1

                async def wait_closed(self):
                    return None

            async def fake_open(_socket_path, _token, _epoch):
                writer = FakeWriter()
                writers.append(writer)
                return object(), writer

            calls = 0

            async def fake_read(_reader, _decoder, _timeout):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise asyncio.TimeoutError
                return {"id": request_ids[-1], "ok": True, "result": "recovered"}

            request_ids = []
            original_open = daemon_client_module.openAuthenticatedDaemonConnection
            original_read = daemon_client_module.readOneDaemonFrame
            original_uuid = daemon_client_module.uuid.uuid4
            daemon_client_module.openAuthenticatedDaemonConnection = fake_open
            daemon_client_module.readOneDaemonFrame = fake_read
            daemon_client_module.uuid.uuid4 = lambda: request_ids.append(f"id-{len(request_ids)}") or request_ids[-1]
            try:
                client = daemon_client_module._create_daemon_client(
                    "/private/unused.sock",
                    {"epoch": _EPOCH},
                    {"token": _TOKEN},
                )
                with self.assertRaises(DaemonProtocolError) as raised:
                    await client["_request"]("daemon.status", {})
                self.assertEqual(raised.exception.code, "daemon.request_timeout")
                self.assertEqual(await client["_request"]("daemon.status", {}), "recovered")
                self.assertEqual([writer.close_count for writer in writers], [1, 1])
                await client["close"]()
            finally:
                daemon_client_module.openAuthenticatedDaemonConnection = original_open
                daemon_client_module.readOneDaemonFrame = original_read
                daemon_client_module.uuid.uuid4 = original_uuid

        run(scenario())

    def test_subscription_requires_ack_before_registration(self):
        def stalled_worker(*args):
            stop_event = args[6]
            ready_event = args[7]
            stop_event.wait(timeout=1)
            ready_event.set()

        original_worker = daemon_client_module._subscription_worker
        original_timeout = daemon_client_module.resolveDaemonAuthTimeoutMs
        daemon_client_module._subscription_worker = stalled_worker
        daemon_client_module.resolveDaemonAuthTimeoutMs = lambda *_args: -1_000
        try:
            client = daemon_client_module._create_daemon_client(
                "/private/unused.sock",
                {"epoch": _EPOCH},
                {"token": _TOKEN},
            )
            with self.assertRaises(DaemonProtocolError) as raised:
                client["subscribe"](lambda _record: None)
            self.assertEqual(raised.exception.code, "daemon.request_timeout")
            run(client["close"]())
        finally:
            daemon_client_module._subscription_worker = original_worker
            daemon_client_module.resolveDaemonAuthTimeoutMs = original_timeout


if __name__ == "__main__":
    unittest.main()
