"""Disposable OS-process tests; never discover or signal live runtime servers."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tess_core.mcp.stdio_transport import StdioMcpClient


SERVER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
for line in sys.stdin:
    request = json.loads(line)
    if 'id' in request:
        result = {'protocolVersion': '2025-06-18', 'capabilities': {},
                  'serverInfo': {'name': 'disposable', 'version': '1'}}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
time.sleep(60)  # Deliberately ignore stdin EOF as well as TERM.
'''
WORKER = r'''
import asyncio, json, sys, time
from pathlib import Path
from tess_core.mcp.stdio_transport import StdioMcpClient
client = StdioMcpClient(json.loads(sys.argv[1]), sys.argv[2])
asyncio.run(client.initialize())
Path(sys.argv[3]).write_text(str(client.proc.pid))
time.sleep(60)
'''


def alive(pid):
    # Zombies cannot execute; on Linux an orphan may await init's reaper.
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                            capture_output=True, text=True, timeout=2)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@unittest.skipIf(os.name == 'nt', 'POSIX process-group containment')
class StdioContainmentTests(unittest.TestCase):
    def test_worker_stop_contains_mcp_but_not_parent_server(self):
        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=stop_signal), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                server = root / 'server.py'
                server.write_text(SERVER)

                def config(name):
                    return {'name': name, 'commandOrUrl': sys.executable,
                            'args': [str(server), str(root / (name + '.json'))],
                            'startupTimeoutMs': 3000, 'requestTimeoutMs': 3000}

                parent = StdioMcpClient(config('parent'), temp)
                worker = None
                guardian = None
                try:
                    asyncio.run(parent.initialize())
                    worker = subprocess.Popen(
                        [sys.executable, '-c', WORKER, json.dumps(config('child')),
                         temp, str(root / 'ready')],
                        cwd=str(Path(__file__).resolve().parents[1]),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                    self.assertTrue(wait_until(lambda: (root / 'ready').exists()))
                    guardian = int((root / 'ready').read_text())
                    child_pids = json.loads((root / 'child.json').read_text())
                    parent_pids = json.loads((root / 'parent.json').read_text())
                    self.assertEqual(os.getpgid(child_pids[0]), guardian)
                    self.assertNotEqual(guardian, worker.pid)
                    started = time.monotonic()
                    os.killpg(worker.pid, stop_signal)
                    worker.wait(timeout=3)
                    self.assertTrue(wait_until(lambda: not any(alive(p) for p in [guardian, *child_pids])))
                    self.assertLess(time.monotonic() - started, 8)
                    self.assertTrue(all(alive(p) for p in [parent.proc.pid, *parent_pids]))
                    # Not merely alive: unrelated parent's MCP remains usable.
                    self.assertIn('serverInfo', asyncio.run(parent.request('ping', {})))
                finally:
                    if worker is not None:
                        if worker.poll() is None:
                            os.killpg(worker.pid, signal.SIGKILL)
                        worker.wait(timeout=3)
                    if guardian is None and (root / 'ready').exists():
                        guardian = int((root / 'ready').read_text())
                    if guardian is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(guardian, signal.SIGKILL)
                    asyncio.run(parent.close())
                self.assertTrue(wait_until(lambda: not any(alive(p) for p in parent_pids)))

    def test_failed_spawn_releases_owner_and_status_descriptors(self):
        from unittest.mock import patch
        client = StdioMcpClient({'name': 'failed', 'commandOrUrl': 'unused'}, '.')
        with patch('tess_core.mcp.stdio_transport.subprocess.Popen', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                client._start_process()
        self.assertIsNone(client.owner_write_fd)
        self.assertIsNone(client.status_read_fd)
        asyncio.run(client.close())

    def test_reaped_guardian_never_signals_potentially_reused_group(self):
        from unittest.mock import Mock, patch
        client = StdioMcpClient({'name': 'reaped', 'commandOrUrl': 'unused'}, '.')
        client.proc = Mock(pid=123456, stdin=None, stdout=None, stderr=None)
        client.proc.poll.return_value = -9  # wait thread has reaped the leader
        with patch('os.killpg') as killpg:
            asyncio.run(client.close())
        killpg.assert_not_called()
        client.proc.send_signal.assert_not_called()
        client.proc.terminate.assert_not_called()
        client.proc.kill.assert_not_called()

    def test_guardian_timeout_reports_failure_without_unsafe_group_fallback(self):
        from unittest.mock import Mock, patch
        client = StdioMcpClient({'name': 'timeout', 'commandOrUrl': 'unused'}, '.')
        client.proc = Mock(pid=123456, stdin=None, stdout=None, stderr=None)
        with patch.object(client, '_wait_parent', return_value=False), patch('os.killpg') as killpg:
            with self.assertRaisesRegex(RuntimeError, 'guardian did not close'):
                asyncio.run(client.close())
        killpg.assert_not_called()
        client.proc.send_signal.assert_not_called()
        client.proc.kill.assert_not_called()
