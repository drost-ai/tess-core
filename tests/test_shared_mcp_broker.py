"""Disposable host broker tests: no live config/services or credentials touched."""
import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

from tess_core.mcp.clients import JsonRpcClient
from tess_core.mcp.stdio_transport import StdioMcpClient
from tess_core.mcp.http_transport import HttpMcpClient
from tess_core.mcp.shared_broker import BrokerGrant
from tess_core.mcp.shared_broker_http import SharedMcpBroker, delegate_shared_mcp_descriptor
from tess_core.runtime.secrets import resolve_mcp_server_config_secrets, authorize_runtime_secret_write, store_runtime_secret
from tess_core.storage.paths import RuntimeHomeScope


SERVER = r'''
import json, os, sys
with open(sys.argv[1], 'a') as log:
    log.write(str(os.getpid())+'\n')
for line in sys.stdin:
    msg=json.loads(line)
    if 'id' not in msg: continue
    method=msg['method']; params=msg.get('params') or {}
    if method=='initialize':
        result={'protocolVersion':'2025-06-18','capabilities':{'tools':{},'resources':{}},'serverInfo':{'name':'disposable','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':n,'inputSchema':{'type':'object'}} for n in ['echo','secret']]}
    elif method=='resources/list':
        result={'resources':[{'name':n,'uri':'test://'+n} for n in ['public','private']]}
    elif method=='tools/call' and params.get('arguments',{}).get('env'):
        result={'content':[{'type':'text','text':os.environ.get('BROKER_TEST_SECRET','missing')}]}
    elif method=='tools/call' and params.get('arguments',{}).get('wait'): continue
    elif method=='resources/read': result={'contents':[{'uri':params['uri'],'text':'value'}]}
    else: result={'content':[{'type':'text','text':str(params.get('arguments',{}).get('value','ok'))}]}
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
'''
CHILD = r'''
import asyncio, json, sys
from tess_core.mcp.http_transport import HttpMcpClient
from tess_core.runtime.secrets import resolve_mcp_server_config_secrets
from tess_core.storage.paths import RuntimeHomeScope
async def main():
    descriptor=json.loads(sys.stdin.readline())
    client=HttpMcpClient(await resolve_mcp_server_config_secrets(descriptor),'.')
    await client.initialize()
    print('ready',flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    result=await client.request('tools/call',{'name':'echo','arguments':{'value':sys.argv[2]}})
    assert result['content'][0]['text']==sys.argv[2]
    catalog=await client.request('tools/list',{})
    assert [t['name'] for t in catalog['tools']]==['echo']
    try: await client.request('tools/call',{'name':'secret'})
    except Exception: pass
    else: raise AssertionError('scope bypass')
    await client.close()
with RuntimeHomeScope(sys.argv[1]): asyncio.run(main())
'''


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scope = RuntimeHomeScope(str(self.root / 'state'))
        self.scope.__enter__()
        self.server = self.root / 'server.py'
        self.server.write_text(SERVER)
        self.descriptor = {'name': 'disposable', 'transport': 'stdio', 'commandOrUrl': sys.executable,
                           'args': [str(self.server), str(self.root / 'spawns')],
                           'sharing': {'mode': 'host'}, 'delegation': {'mode': 'allow', 'scope': 'shared'}}
        self.broker = SharedMcpBroker()
        await self.broker.start()

    async def asyncTearDown(self):
        await self.broker.close()
        self.scope.__exit__(None, None, None)
        self.tmp.cleanup()

    async def admit(self, grant, fingerprint='upstream-v1', descriptor=None):
        return await self.broker.acquire_descriptor(descriptor or self.descriptor, cwd=str(self.root),
                                                    source_fingerprint=fingerprint, grant=grant)

    async def client(self, descriptor):
        client = HttpMcpClient(await resolve_mcp_server_config_secrets(descriptor), str(self.root))
        await client.initialize()
        return client

    async def test_real_32_subprocess_clients_one_backend(self):
        parent = await self.admit(BrokerGrant('parent', None, None))
        clients = await asyncio.gather(*(delegate_shared_mcp_descriptor(parent, BrokerGrant(str(i), frozenset({'echo'}))) for i in range(32)))
        self.assertEqual(len({d['headers']['Authorization']['id'] for d in clients}), 32)
        self.assertNotIn('Bearer ', json.dumps(clients))
        processes = []
        try:
            for i, descriptor in enumerate(clients):
                proc = await asyncio.create_subprocess_exec(sys.executable, '-c', CHILD, str(self.root / 'state'), str(i),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                processes.append(proc)
                proc.stdin.write((json.dumps(descriptor)+'\n').encode())
                await proc.stdin.drain()
            ready = await asyncio.wait_for(asyncio.gather(*(p.stdout.readline() for p in processes)), 60)
            self.assertEqual(ready, [b'ready\n'] * 32)
            self.assertEqual(len((self.root / 'spawns').read_text().splitlines()), 1)
            for proc in processes:
                proc.stdin.write(b'go\n')
                await proc.stdin.drain()
            outputs = await asyncio.wait_for(asyncio.gather(*(p.communicate() for p in processes)), 60)
            for proc, (_, stderr) in zip(processes, outputs):
                self.assertEqual(proc.returncode, 0, stderr.decode())
            self.assertEqual(len((self.root / 'spawns').read_text().splitlines()), 1)
            # All child processes exiting must not terminate the host service.
            client = await self.client(parent)
            await client.request('tools/call', {'name': 'secret'})
            await client.close()
        finally:
            for proc in processes:
                if proc.returncode is None:
                    proc.kill()
            await asyncio.gather(*(p.wait() for p in processes))

    async def test_scopes_resources_forgery_delegation_and_revocation(self):
        parent = await self.admit(BrokerGrant('parent', None, None))
        child = await delegate_shared_mcp_descriptor(parent, BrokerGrant('child', None, frozenset({'test://public'})),
                                                      enabled_tools=frozenset({'mcp__disposable__echo'}))
        client = await self.client(child)
        self.assertEqual([t['name'] for t in (await client.request('tools/list'))['tools']], ['echo'])
        self.assertEqual([r['uri'] for r in (await client.request('resources/list'))['resources']], ['test://public'])
        await client.request('resources/read', {'uri': 'test://public'})
        for method, params in [('tools/call', {'name':'secret'}), ('resources/read', {'uri':'test://private'}),
                               ('resources/subscribe', {'uri':'test://public'}), ('prompts/get', {'name':'secret'})]:
            with self.assertRaises(Exception):
                await client.request(method, params)
        with self.assertRaises(Exception):
            await delegate_shared_mcp_descriptor(child, BrokerGrant('bad', None, None))
        grandchild = await delegate_shared_mcp_descriptor(child, BrokerGrant('grand', frozenset({'echo'})))
        forged = {**child, 'headers': {'Authorization': 'Bearer wrong'}}
        with self.assertRaises(Exception):
            await HttpMcpClient(forged, '.').request('tools/list')
        await self.broker.revoke_descriptor(child)
        with self.assertRaises(Exception):
            await client.request('tools/list')
        with self.assertRaises(Exception):
            await self.client(grandchild)
        survivor = await self.client(parent)
        await survivor.request('tools/call', {'name': 'echo'})
        await survivor.close()
        await client.close()

    async def test_source_isolation_and_personal_exclusion(self):
        grant = BrokerGrant('child', None, None)
        await self.admit(grant)
        await self.admit(grant, fingerprint='different-upstream')
        self.assertEqual(len((self.root / 'spawns').read_text().splitlines()), 2)
        for descriptor in ({**self.descriptor, 'sharing': None},
                           {**self.descriptor, 'delegation': {'mode':'allow','scope':'session'}},
                           {**self.descriptor, 'delegation': {'mode':'deny','scope':'shared'}}):
            with self.assertRaises(PermissionError):
                await self.admit(grant, descriptor=descriptor)
        self.assertEqual(len((self.root / 'spawns').read_text().splitlines()), 2)

    async def test_independent_cancellation_and_owner_shutdown(self):
        class BlockingClient(JsonRpcClient):
            closed = 0
            async def initialize(self):
                self.negotiated_protocol_version = '2025-06-18'
            async def request(self, method, params=None):
                if params.get('arguments', {}).get('wait'):
                    await asyncio.sleep(60)
                return {'content': []}
            async def close(self):
                self.closed += 1
        backend = BlockingClient(self.descriptor, '.')
        broker = SharedMcpBroker(lambda *_: backend)
        await broker.start()
        try:
            a = await broker.connect(self.descriptor, cwd='.', source_fingerprint='x', grant=BrokerGrant('a', None))
            b = await broker.connect(self.descriptor, cwd='.', source_fingerprint='x', grant=BrokerGrant('b', None))
            pending = asyncio.create_task(a.request('tools/call', {'name':'echo','arguments':{'wait':True}}))
            await asyncio.sleep(.01)
            await a.close()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await b.request('tools/call', {'name':'echo'})
            self.assertEqual(backend.closed, 0)
            await broker.close()
            await broker.close()
            self.assertEqual(backend.closed, 1)
        finally:
            await broker.close()

    async def test_real_stdio_repeated_cancel_does_not_starve_sibling(self):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))
        client = StdioMcpClient(self.descriptor, str(self.root))
        await client.initialize()
        try:
            for _ in range(12):
                call = asyncio.create_task(client.request('tools/call', {'name':'echo','arguments':{'wait':True}}))
                await asyncio.sleep(.02)
                call.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await call
            result = await asyncio.wait_for(client.request('tools/call', {'name':'echo'}), 2)
            self.assertEqual(result['content'][0]['text'], 'ok')
            self.assertEqual(client.pending, {})
        finally:
            await client.close()

    async def test_revoke_removes_whole_subtree_before_resistant_drain(self):
        parent = await self.admit(BrokerGrant('parent', None, None))
        child = await delegate_shared_mcp_descriptor(parent, BrokerGrant('child', None, None))
        sibling = await delegate_shared_mcp_descriptor(parent, BrokerGrant('sibling', None, None))
        async def token(descriptor):
            return (await resolve_mcp_server_config_secrets(descriptor))['headers']['Authorization'][7:]
        parent_token, child_token, sibling_token = await asyncio.gather(*(token(d) for d in (parent,child,sibling)))
        cancelled = asyncio.Event()
        finish = asyncio.Event()
        async def resistant():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                await finish.wait()
        task = asyncio.create_task(resistant())
        self.broker._leases[child_token].tasks.add(task)
        await asyncio.sleep(0)
        revoke = asyncio.create_task(self.broker.revoke(parent_token))
        await cancelled.wait()
        self.assertNotIn(sibling_token, self.broker._leases)
        with self.assertRaises(PermissionError):
            await self.broker.request(sibling_token, 'tools/call', {'name':'echo'})
        with self.assertRaises(PermissionError):
            await self.broker.request(sibling_token, 'broker/delegate', {'child_id':'bad','tools':None,'resources':None})
        revoke.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await revoke
        self.assertFalse(self.broker._leases)
        finish.set()
        await task

    async def test_remote_cancel_does_not_close_service_or_sibling(self):
        descriptor = await self.admit(BrokerGrant('child', None, None))
        client = await self.client(descriptor)
        try:
            pending = asyncio.create_task(client.request('tools/call', {'name':'echo','arguments':{'wait':True}}))
            await asyncio.sleep(.05)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            sibling = await self.client(descriptor)
            await asyncio.wait_for(sibling.request('tools/call', {'name':'echo'}), 2)
            await sibling.close()
            await asyncio.sleep(.05)
            backend = next(iter(self.broker._clients.values()))
            self.assertFalse(backend.pending)
        finally:
            await client.close()

    async def test_upstream_env_secret_reference_resolved_only_at_connection(self):
        secret = 'disposable-upstream-secret'
        reference = await store_runtime_secret(purpose='mcp_env', secret=secret.encode(), idempotencyKey='test-env',
            authorization=authorize_runtime_secret_write(purpose='mcp_env', idempotencyKey='test-env'))
        config = {**self.descriptor, 'env': {'BROKER_TEST_SECRET': reference}}
        descriptor = await self.admit(BrokerGrant('child', frozenset({'echo'})), descriptor=config)
        self.assertNotIn(secret, json.dumps(config))
        self.assertNotIn(secret, json.dumps(descriptor))
        self.assertEqual(config['env']['BROKER_TEST_SECRET'], reference)
        client = await self.client(descriptor)
        try:
            result = await client.request('tools/call', {'name':'echo','arguments':{'env':True}})
            self.assertEqual(result['content'][0]['text'], secret)
        finally:
            await client.close()

    async def test_host_terminal_revoke_child_all_matching_leases(self):
        parent = await self.admit(BrokerGrant('parent', None, None))
        child_a = await delegate_shared_mcp_descriptor(parent, BrokerGrant('terminated', None, None))
        child_b = await delegate_shared_mcp_descriptor(parent, BrokerGrant('terminated', None, None))
        descendant = await delegate_shared_mcp_descriptor(child_a, BrokerGrant('descendant', None, None))
        sibling = await delegate_shared_mcp_descriptor(parent, BrokerGrant('survivor', None, None))
        disconnected = await self.client(child_a)
        await disconnected.close()
        await self.broker.revoke_child('terminated')
        for descriptor in (child_a, child_b, descendant):
            with self.assertRaises(Exception):
                await self.client(descriptor)
        surviving = await self.client(sibling)
        await surviving.request('tools/call', {'name':'echo'})
        with self.assertRaises(Exception):
            await surviving.request('broker/revoke_child', {'child_id':'parent'})
        await surviving.close()
        await self.broker.revoke_child('terminated')  # idempotent terminal event
