"""Deterministic native daemon inspection; run via scripts/test-packages.py."""
import asyncio
import hashlib

import pytest

from agent_core.daemon.lifecycle import _is_daemon_run_process
from agent_core.process import identity, identity_darwin


@pytest.mark.parametrize('argv,expected', [
    (['python', '-m', 'agent_core.process.daemon_worker', '--instance-id', 'owned'], True),
    (['python', '-m', 'agent_core.process.daemon_worker', '--instance-id', 'other'], False),
    (['python', '-m', 'unrelated', '--instance-id', 'owned'], False),
    (['python', '-m', 'agent_core.process.daemon_worker.extra', '--instance-id', 'owned'], False),
    (['python', '-m', 'agent_core.process.daemon_worker', '--instance-id', 'owned-extra'], False),
    (['python', 'agent_core.process.daemon_worker', 'owned'], False),
    ([], False),
])
def test_daemon_exact_tokens(monkeypatch, argv, expected):
    async def read(pid):
        assert pid == 123
        return argv
    monkeypatch.setattr(identity, 'read_native_process_argv', read)
    assert asyncio.run(_is_daemon_run_process(123, 'owned')) is expected


def test_daemon_inspection_error_is_false(monkeypatch):
    async def read(pid):
        raise identity.ProcessIdentityError('process.identity_unavailable', 'synthetic')
    monkeypatch.setattr(identity, 'read_native_process_argv', read)
    assert asyncio.run(_is_daemon_run_process(123, 'owned')) is False


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
@pytest.mark.parametrize('failure', [None, 'birth', 'command', 'empty', 'unterminated', 'oversized'])
def test_native_argv_bounded_and_birth_checked(monkeypatch, platform, failure):
    command = b'python\0-m\0agent_core.process.daemon_worker\0--instance-id\0owned\0'
    if failure == 'empty':
        command = b''
    elif failure == 'unterminated':
        command = b'python'
    elif failure == 'oversized':
        command = b'x' * identity.MAX_PROCESS_COMMAND_BYTES + b'\0'
    calls = 0
    async def inspect(pid):
        nonlocal calls
        calls += 1
        return {'pid': pid, 'startToken': 'changed' if failure == 'birth' and calls == 2 else 'birth',
                'observedCommandDigest': 'wrong' if failure == 'command' else hashlib.sha256(command).hexdigest()}
    def read_linux(path, bound):
        assert path == '/proc/123/cmdline'
        assert bound == identity.MAX_PROCESS_COMMAND_BYTES
        return command
    def read_darwin(pid, *, max_command_bytes):
        assert pid == 123 and max_command_bytes == identity.MAX_PROCESS_COMMAND_BYTES
        return command
    monkeypatch.setattr(identity.sys, 'platform', platform)
    monkeypatch.setattr(identity, '_inspect_process_identity', inspect)
    monkeypatch.setattr(identity, '_read_bounded_file', read_linux)
    monkeypatch.setattr(identity_darwin, 'read_darwin_process_command', read_darwin)
    if failure:
        with pytest.raises(identity.ProcessIdentityError):
            asyncio.run(identity.read_native_process_argv(123))
    else:
        assert asyncio.run(identity.read_native_process_argv(123)) == ['python', '-m', 'agent_core.process.daemon_worker', '--instance-id', 'owned']


@pytest.mark.parametrize('pid', [0, -1, True, '123'])
def test_native_argv_rejects_invalid_pid(pid):
    with pytest.raises(identity.ProcessIdentityError):
        asyncio.run(identity.read_native_process_argv(pid))
