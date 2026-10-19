"""Scripted disposable children only; run through scripts/test-packages.py."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from agent_core.mcp import stdio_transport
from agent_core.mcp.stdio_transport import StdioMcpClient
from agent_core.mcp.transport_policy import McpTransportError


RESPONDER = r'''
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'serverInfo': {'name': 'scripted', 'version': '1'}}}), flush=True)
    elif request.get('method') == 'notifications/initialized':
        MODE
'''


def client_for(tmp_path, monkeypatch, script, *, startup=1000):
    values = {'mcp.close_grace_ms': 100, 'mcp.close_stdin_grace_ms': 100,
              'mcp.close_kill_wait_ms': 2000}
    monkeypatch.setattr(stdio_transport, '_knob', values.__getitem__)
    return StdioMcpClient({'name': 'disposable', 'transport': 'stdio',
        'commandOrUrl': sys.executable, 'args': ['-I', '-u', '-c', script],
        'inheritEnv': False, 'env': {}, 'startupTimeoutMs': startup,
        'requestTimeoutMs': 5000}, str(tmp_path))


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def assert_closed(client):
    assert client.state == 'closed' and client.close_done.is_set()
    assert client.close_error is None
    assert not client.pending
    assert client.owner_write_fd is None and client.status_read_fd is None
    assert client.proc is None or client.proc.poll() is not None
    for thread in (client.reader_thread, client.stderr_thread, client.wait_thread):
        assert thread is None or not thread.is_alive()


def test_direct_handshake_deadline_cleans_silent_child(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch, 'import time; time.sleep(60)', startup=150)
    async def run():
        started = time.monotonic()
        with pytest.raises(McpTransportError) as error:
            await client.initialize()
        assert error.value.code == 'startup_timeout'
        assert time.monotonic() - started < 4
        assert_closed(client)
        await client.close()
    asyncio.run(run())


def test_handshake_deadline_includes_initialized_notification(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch, RESPONDER.replace('MODE', 'pass'), startup=200)
    reached = False
    async def stalled_notification(method, params=None):
        nonlocal reached
        assert method == 'notifications/initialized'
        reached = True
        await asyncio.Event().wait()
    monkeypatch.setattr(client, 'notify', stalled_notification)
    async def run():
        with pytest.raises(McpTransportError) as error:
            await client.initialize()
        assert error.value.code == 'startup_timeout' and reached
        assert_closed(client)
    asyncio.run(run())


def test_invalid_handshake_reaps_child(tmp_path, monkeypatch):
    script = "import sys,time; sys.stdin.readline(); print('not-json', flush=True); time.sleep(60)"
    client = client_for(tmp_path, monkeypatch, script)
    async def run():
        with pytest.raises(Exception):
            await client.initialize()
        assert_closed(client)
    asyncio.run(run())


def test_cancelled_startup_waits_for_cleanup_despite_repeated_cancel(tmp_path, monkeypatch):
    ready = tmp_path / 'ready'
    script = f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    client = client_for(tmp_path, monkeypatch, script, startup=5000)
    async def run():
        task = asyncio.create_task(client.initialize())
        await until(ready.exists)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_closed(client)
    asyncio.run(run())


@pytest.mark.parametrize('target', ['_read_loop', '_stderr_loop', '_wait_loop'])
def test_partial_thread_start_releases_process_pipes(tmp_path, monkeypatch, target):
    client = client_for(tmp_path, monkeypatch, 'import time; time.sleep(60)')
    start = threading.Thread.start
    def fail_thread(thread):
        if getattr(thread, '_target', None) == getattr(client, target):
            raise RuntimeError('synthetic thread-start failure')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_thread)
    async def run():
        with pytest.raises(RuntimeError, match='synthetic thread-start failure'):
            await client.initialize()
        assert_closed(client)
        await client.close()
    asyncio.run(run())


def test_spawn_failure_releases_all_descriptors(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch, '')
    client.config['cwd'] = str(tmp_path / 'missing')
    async def run():
        with pytest.raises(OSError):
            await client.initialize()
        assert_closed(client)
        await client.close()
    asyncio.run(run())


def test_concurrent_cancelled_close_settles_pending_once(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch, RESPONDER.replace('MODE', 'pass'))
    async def run():
        await client.initialize()
        pending = asyncio.create_task(client.request('never-reply'))
        await until(lambda: bool(client.pending))
        first = asyncio.create_task(client.close())
        await asyncio.sleep(0.02)
        second = asyncio.create_task(client.close())
        first.cancel()
        await asyncio.sleep(0.02)
        first.cancel()
        results = await asyncio.gather(first, second, pending, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert isinstance(results[2], McpTransportError)
        assert_closed(client)
        await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('observe_status_first', [False, True])
def test_unexpected_exit_cannot_remain_cached_healthy(tmp_path, monkeypatch, observe_status_first):
    from agent_core.mcp.state import _create_state, _ensure_connected, _server_status
    client = client_for(tmp_path, monkeypatch,
        RESPONDER.replace('MODE', 'time.sleep(0.1); sys.exit(7)'))
    async def run():
        await client.initialize()
        state = _create_state(client.config)
        state.update(client=client, connected=True, state='connected')
        await until(client.close_done.is_set)
        if observe_status_first:
            status = _server_status(state)
            assert status['connected'] is False and status['state'] == 'disconnected'
        with pytest.raises(RuntimeError, match='reconnect backoff'):
            await _ensure_connected(state, str(tmp_path), lambda *args: None)
        assert state['connected'] is False
        assert state['reconnectFailures'] == 1  # Repeated observation is inert.
        assert _server_status(state)['connected'] is False
        await client.close()
        assert_closed(client)
    asyncio.run(run())


def test_unexpected_exit_rejects_pending_and_closes(tmp_path, monkeypatch):
    client = client_for(tmp_path, monkeypatch,
        RESPONDER.replace('MODE', 'time.sleep(0.1); sys.exit(7)'))
    async def run():
        await client.initialize()
        with pytest.raises(Exception, match='exited'):
            await client.request('never-reply')
        await client.close()
        assert client.server_returncode == 7
        assert_closed(client)
    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='POSIX guardian process groups')
def test_abrupt_owner_death_cleans_stubborn_server_and_descendant(tmp_path, monkeypatch):
    marker, ready = tmp_path / 'pids', tmp_path / 'owner-ready'
    descendant = ('import signal,time; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)')
    script = ('import os,pathlib,signal,subprocess\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'child = subprocess.Popen([sys.executable, "-I", "-c", {descendant!r}])\n')
    # The script imports sys before spawning; all children remain in the
    # guardian group and deliberately retain their inherited stdio handles.
    script = 'import sys\n' + script + (
        f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid()) + " " + str(child.pid))\n'
        + RESPONDER.replace('MODE', 'pass') + '\ntime.sleep(60)\n')
    client = client_for(tmp_path, monkeypatch, script)
    worker = ('import asyncio,json,pathlib,sys,time\n'
        'from agent_core.mcp import stdio_transport\n'
        'stdio_transport._knob = lambda name: 100 if name != "mcp.close_kill_wait_ms" else 2000\n'
        'client = stdio_transport.StdioMcpClient(json.loads(sys.argv[1]), sys.argv[2])\n'
        'asyncio.run(client.initialize())\n'
        'pathlib.Path(sys.argv[3]).write_text(str(client.proc.pid))\n'
        'time.sleep(60)\n')
    owner = subprocess.Popen([sys.executable, '-B', '-c', worker,
        json.dumps(client.config), str(tmp_path), str(ready)], cwd=tmp_path,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    async def run():
        try:
            await until(ready.exists)
            pids = [int(ready.read_text()), *map(int, marker.read_text().split())]
            owner.kill()
            owner.wait(timeout=3)
            def all_gone():
                for pid in pids:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    return False
                return True
            await until(all_gone, timeout=5)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=3)
            owner.stderr.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='POSIX guardian process groups')
def test_guardian_kills_term_resistant_descendant_after_server_exits(tmp_path, monkeypatch):
    marker = tmp_path / 'descendant'
    descendant = ("import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)")
    script = ('import subprocess,sys,time,pathlib\n'
        f'subprocess.Popen([sys.executable, "-I", "-c", {descendant!r}])\n'
        f'while not pathlib.Path({str(marker)!r}).exists(): time.sleep(0.01)\n'
        + RESPONDER.replace('MODE', 'sys.exit(0)'))
    client = client_for(tmp_path, monkeypatch, script)
    async def run():
        try:
            await client.initialize()
            await until(marker.exists)
            pid = int(marker.read_text())
            await until(client.close_done.is_set)
            await client.close()
            assert_closed(client)
            def gone():
                try:
                    os.kill(pid, 0)  # Evidence for this synthetic PID only; no ps.
                except ProcessLookupError:
                    return True
                return False
            await until(gone)
        finally:
            await client.close()
    asyncio.run(run())
