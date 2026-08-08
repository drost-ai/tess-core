from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.mcp.state import _create_state, _ensure_connected


def _noop_event(*_args, **_kwargs) -> None:
    return None


def _dead_config(name: str = "dead") -> dict:
    return {
        "name": name,
        "transport": "stdio",
        "commandOrUrl": "/nonexistent/mcp-server-binary",
        "startupTimeoutMs": 500,
        "requestTimeoutMs": 500,
    }


def _write_slow_start_fixture(workspace: Path) -> Path:
    target = workspace / "mcp_slow_start.py"
    target.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import sys
            import time

            with open(os.environ["MCP_PID_LOG"], "a", encoding="utf-8") as handle:
                handle.write(str(os.getpid()) + "\n")
            time.sleep(0.2)

            def send(message):
                sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()

            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                request = json.loads(line.decode("utf-8"))
                request_id = request.get("id")
                method = request.get("method")
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"resources": {}},
                        "serverInfo": {"name": "slow", "version": "1"},
                    }})
                elif method == "resources/list":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": [
                        {"uri": "mem://slow/one", "name": "one"},
                    ]}})
                elif method == "tools/list":
                    send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}})
                elif request_id is not None:
                    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            '''
        ),
        encoding="utf-8",
    )
    return target


class McpReconnectBackoffTests(AgentCoreTestCase):
    def test_failed_connects_back_off_exponentially_without_reattempting(self):
        root, home, workspace, previous = self.temp_context()
        try:
            state = _create_state(_dead_config())

            async def scenario():
                with self.assertRaises(RuntimeError):
                    await _ensure_connected(state, str(workspace), _noop_event)
                self.assertEqual(state["reconnectFailures"], 1)
                self.assertGreater(state["reconnectBackoffUntil"], time.monotonic())

                # Inside the backoff window: fails fast, no new attempt.
                with self.assertRaisesRegex(RuntimeError, "reconnect backoff"):
                    await _ensure_connected(state, str(workspace), _noop_event)
                self.assertEqual(state["reconnectFailures"], 1)

                # After the window elapses the next attempt runs and doubles it.
                state["reconnectBackoffUntil"] = 0.0
                with self.assertRaises(RuntimeError):
                    await _ensure_connected(state, str(workspace), _noop_event)
                self.assertEqual(state["reconnectFailures"], 2)
                self.assertGreater(
                    state["reconnectBackoffUntil"], time.monotonic() + 1.5
                )

            run(scenario())
        finally:
            self.restore_home(root, previous)


class McpReconnectRaceTests(AgentCoreTestCase):
    def test_concurrent_ensure_connected_spawns_one_process(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fixture = _write_slow_start_fixture(workspace)
            pid_log = workspace / "pids.log"
            state = _create_state(
                {
                    "name": "slow",
                    "transport": "stdio",
                    "commandOrUrl": sys.executable,
                    "args": [str(fixture)],
                    "env": {"MCP_PID_LOG": str(pid_log)},
                    "startupTimeoutMs": 5000,
                    "requestTimeoutMs": 3000,
                }
            )

            async def scenario():
                first, second = await asyncio.gather(
                    _ensure_connected(state, str(workspace), _noop_event),
                    _ensure_connected(state, str(workspace), _noop_event),
                )
                self.assertIs(first, second)
                self.assertTrue(state["connected"])

            run(scenario())
            pids = pid_log.read_text(encoding="utf-8").split()
            self.assertEqual(len(pids), 1, f"expected one spawned process, saw pids {pids}")
            run(state["client"].close())
        finally:
            self.restore_home(root, previous)


class McpResourceListingIsolationTests(AgentCoreTestCase):
    def test_dead_server_does_not_fail_or_stall_the_sweep(self):
        root, home, workspace, previous = self.temp_context()
        runtime = None
        try:
            fixture = _write_slow_start_fixture(workspace)
            pid_log = workspace / "pids.log"
            runtime = run(
                core.createMcpRuntime(
                    {
                        "cwd": str(workspace),
                        "servers": [
                            {
                                "name": "alive",
                                "transport": "stdio",
                                "commandOrUrl": sys.executable,
                                "args": [str(fixture)],
                                "env": {"MCP_PID_LOG": str(pid_log)},
                                "startupTimeoutMs": 5000,
                                "requestTimeoutMs": 3000,
                            },
                            _dead_config(),
                        ],
                    }
                )
            )
            started = time.monotonic()
            resources = run(runtime["listResources"]())
            elapsed = time.monotonic() - started
            self.assertEqual([item["uri"] for item in resources], ["mem://slow/one"])
            self.assertLess(elapsed, 2.0)

            # Naming the dead server keeps its error.
            with self.assertRaises(RuntimeError):
                run(runtime["listResources"]("dead"))
        finally:
            if runtime:
                run(runtime["close"]())
            self.restore_home(root, previous)
