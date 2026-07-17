from __future__ import annotations

import stat

from tests.support.agent_core_case import *

from agent_core.daemon_runtime.protocol import (
    DAEMON_MAX_REQUEST_FRAME_BYTES,
    DAEMON_PROTOCOL_VERSION,
    DaemonLineDecoder,
    DaemonProtocolError,
    encodeDaemonFrame,
    parseDaemonFrame,
    parseDaemonRequest,
)
from agent_core.daemon_runtime.security import (
    DaemonSecurityError,
    createDaemonAuthMaterial,
    daemonAuthTokenPath,
    prepareDaemonRuntimeSecurity,
    readDaemonAuthMaterial,
    verifyPrivateDaemonRuntime,
)
import agent_core.daemon_runtime.server as daemon_server_module


class Package31DaemonProtocolSecurityTests(AgentCoreTestCase):
    def test_bounded_newline_decoder_handles_fragmentation_and_limit_edges(self):
        decoder = DaemonLineDecoder(32)
        self.assertEqual(decoder.push(b'{"id":'), [])
        self.assertEqual(
            decoder.push(b'"one"}\n{"id"'),
            [b'{"id":"one"}'],
        )
        self.assertEqual(
            [parseDaemonFrame(frame) for frame in decoder.push(b':"two"}\n')],
            [{"id": "two"}],
        )
        with self.assertRaises(DaemonProtocolError) as raised:
            decoder.push(b"a" * 33)
        self.assertEqual(raised.exception.code, "daemon.frame_oversized")
        self.assertEqual(decoder.bufferedBytes, 0)
        exact = encodeDaemonFrame({"body": "x" * 18}, 32)
        self.assertLessEqual(len(exact) - 1, 32)
        with self.assertRaises(DaemonProtocolError) as raised:
            encodeDaemonFrame({"body": "x" * 128}, 32)
        self.assertEqual(raised.exception.code, "daemon.frame_oversized")
        with self.assertRaises(DaemonProtocolError) as raised:
            parseDaemonFrame(b"\xff")
        self.assertEqual(raised.exception.code, "daemon.frame_malformed")

    def test_request_encoder_rejects_one_mib_plus_one_before_write(self):
        payload = {"body": "x" * DAEMON_MAX_REQUEST_FRAME_BYTES}
        with self.assertRaises(DaemonProtocolError) as raised:
            encodeDaemonFrame(payload, DAEMON_MAX_REQUEST_FRAME_BYTES)
        self.assertEqual(raised.exception.code, "daemon.frame_oversized")

    def test_request_schema_rejects_malformed_ids_methods_and_params(self):
        invalid = (
            None,
            {},
            {"id": "", "method": "daemon.status"},
            {"id": "x" * 129, "method": "daemon.status"},
            {"id": "valid", "method": ""},
            {"id": "valid", "method": "daemon.status", "params": []},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(DaemonProtocolError) as raised:
                    parseDaemonRequest(value)
                self.assertEqual(raised.exception.code, "daemon.frame_malformed")
        self.assertEqual(
            parseDaemonRequest(
                {"id": "valid", "method": "daemon.status", "params": {"x": 1}}
            ),
            {"id": "valid", "method": "daemon.status", "params": {"x": 1}},
        )

    def test_private_auth_material_is_bound_to_state_without_token_leakage(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            auth = run(createDaemonAuthMaterial())
            state = {
                "protocolVersion": DAEMON_PROTOCOL_VERSION,
                "authTokenId": auth["tokenId"],
            }
            self.assertEqual(run(readDaemonAuthMaterial(state)), auth)
            token_path = Path(daemonAuthTokenPath())
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            self.assertNotIn(auth["token"], json.dumps(state))
            with self.assertRaises(DaemonSecurityError) as raised:
                run(readDaemonAuthMaterial({**state, "authTokenId": "0" * 64}))
            self.assertEqual(raised.exception.code, "daemon.auth_material_invalid")
        finally:
            self.restore_home(root, previous)

    def test_owner_valid_legacy_tree_tightens_without_changing_bytes(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            daemon_dir = Path(home, "daemon")
            logs_dir = daemon_dir / "logs"
            logs_dir.mkdir(parents=True, mode=0o755)
            daemon_dir.chmod(0o755)
            logs_dir.chmod(0o755)
            state_path = daemon_dir / "state.json"
            event_path = daemon_dir / "events.jsonl"
            state_bytes = b'{"pid":99999999,"legacy":true}\n'
            state_path.write_bytes(state_bytes)
            event_path.write_bytes(b"legacy-event\n")
            state_path.chmod(0o644)
            event_path.chmod(0o644)
            run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            self.assertEqual(state_path.read_bytes(), state_bytes)
            self.assertEqual(stat.S_IMODE(daemon_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(logs_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(event_path.stat().st_mode), 0o600)
            before = state_path.stat().st_ctime_ns
            run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            self.assertEqual(state_path.stat().st_ctime_ns, before)
            run(verifyPrivateDaemonRuntime())
        finally:
            self.restore_home(root, previous)

    def test_unsafe_file_and_symlink_fail_closed_without_modification(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            daemon_dir = Path(home, "daemon")
            (daemon_dir / "logs").mkdir(parents=True, mode=0o700)
            unsafe = daemon_dir / "state.json"
            unsafe.write_bytes(b"unsafe-bytes\n")
            unsafe.chmod(0o666)
            with self.assertRaises(DaemonSecurityError) as raised:
                run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            self.assertEqual(raised.exception.code, "daemon.security_unsafe")
            self.assertEqual(unsafe.read_bytes(), b"unsafe-bytes\n")
            self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o666)
        finally:
            self.restore_home(root, previous)

        root, home, _workspace, previous = self.temp_context()
        try:
            daemon_dir = Path(home, "daemon")
            foreign = Path(home, "foreign")
            foreign.mkdir(mode=0o700)
            (foreign / "marker").write_text("preserve")
            daemon_dir.symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(DaemonSecurityError) as raised:
                run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            self.assertEqual(raised.exception.code, "daemon.security_unsafe")
            self.assertTrue(daemon_dir.is_symlink())
            self.assertEqual((foreign / "marker").read_text(), "preserve")
        finally:
            self.restore_home(root, previous)

    def test_permission_migration_rolls_back_prior_changes(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            daemon_dir = Path(home, "daemon")
            logs_dir = daemon_dir / "logs"
            logs_dir.mkdir(parents=True, mode=0o755)
            state_path = daemon_dir / "state.json"
            state_path.write_text('{"pid":99999999}\n')
            for target, mode in (
                (daemon_dir, 0o755),
                (logs_dir, 0o755),
                (state_path, 0o644),
            ):
                target.chmod(mode)
            paths = (daemon_dir, logs_dir, state_path)
            before = {path: stat.S_IMODE(path.stat().st_mode) for path in paths}
            calls = 0

            async def injected_chmod(target: str, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected chmod failure")
                os.chmod(target, mode)

            with self.assertRaises(DaemonSecurityError) as raised:
                run(
                    prepareDaemonRuntimeSecurity(
                        {
                            "requireStoppedForMigration": True,
                            "chmod": injected_chmod,
                        }
                    )
                )
            self.assertEqual(raised.exception.code, "daemon.security_migration_failed")
            self.assertEqual(
                {path: stat.S_IMODE(path.stat().st_mode) for path in paths},
                before,
            )
        finally:
            self.restore_home(root, previous)

    def test_live_legacy_daemon_blocks_permission_migration_byte_stably(self):
        root, home, _workspace, previous = self.temp_context()
        try:
            daemon_dir = Path(home, "daemon")
            logs_dir = daemon_dir / "logs"
            logs_dir.mkdir(parents=True, mode=0o755)
            daemon_dir.chmod(0o755)
            logs_dir.chmod(0o755)
            state_path = daemon_dir / "state.json"
            state_bytes = json.dumps({"pid": os.getpid(), "phase": "running"}).encode()
            state_path.write_bytes(state_bytes)
            state_path.chmod(0o644)
            with self.assertRaises(DaemonSecurityError) as raised:
                run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            self.assertEqual(raised.exception.code, "daemon.security_live_migration")
            self.assertEqual(state_path.read_bytes(), state_bytes)
            self.assertEqual(stat.S_IMODE(daemon_dir.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(logs_dir.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o644)
        finally:
            self.restore_home(root, previous)

    def test_private_verification_tolerates_atomic_state_publication(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            now = core.now_iso()
            state = {
                "instanceId": "security-churn",
                "protocolVersion": DAEMON_PROTOCOL_VERSION,
                "authTokenId": "fixture-token-id",
                "phase": "starting",
                "epoch": "security-churn-epoch",
                "pid": 99_999_999,
                "socketPath": core.daemonSocketPath(),
                "startedAt": now,
                "updatedAt": now,
            }

            async def scenario():
                async def write(writer):
                    for index in range(40):
                        await core.saveDaemonState(
                            {**state, "updatedAt": f"{now}:{writer}:{index}"}
                        )

                async def verify():
                    for _index in range(80):
                        await verifyPrivateDaemonRuntime()

                await asyncio.gather(
                    *(write(writer) for writer in range(3)),
                    *(verify() for _reader in range(3)),
                )

            run(scenario())
            run(verifyPrivateDaemonRuntime())
        finally:
            self.restore_home(root, previous)

    def test_clients_cannot_attach_before_running_state(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            auth = run(createDaemonAuthMaterial())
            now = core.now_iso()
            run(
                core.saveDaemonState(
                    {
                        "pid": os.getpid(),
                        "phase": "recovering",
                        "protocolVersion": DAEMON_PROTOCOL_VERSION,
                        "authTokenId": auth["tokenId"],
                        "epoch": "pre-ready",
                        "socketPath": core.daemonSocketPath(),
                        "startedAt": now,
                        "updatedAt": now,
                    }
                )
            )
            self.assertIsNone(run(core.connectTaskSupervisorDaemonClient()))
        finally:
            self.restore_home(root, previous)

    def test_owned_cleanup_preserves_purpose_confused_socket_and_instance(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            run(core.ensureBaseLayout(str(workspace)))
            run(prepareDaemonRuntimeSecurity({"requireStoppedForMigration": True}))
            auth = run(createDaemonAuthMaterial())
            now = core.now_iso()
            epoch = "cleanup-purpose-confusion"
            socket_path = core.daemonSocketPath()
            Path(socket_path).write_bytes(b"operator-owned-evidence")
            Path(socket_path).chmod(0o600)
            run(
                core.saveDaemonState(
                    {
                        "instanceId": "local-daemon",
                        "protocolVersion": DAEMON_PROTOCOL_VERSION,
                        "authTokenId": auth["tokenId"],
                        "phase": "running",
                        "epoch": epoch,
                        "pid": os.getpid(),
                        "cwd": str(workspace),
                        "socketPath": socket_path,
                        "startedAt": now,
                        "updatedAt": now,
                        "mode": "detached_python",
                        "status": "running",
                    }
                )
            )
            with self.assertRaises(DaemonSecurityError) as raised:
                run(
                    daemon_server_module._clear_owned_daemon_instance(
                        epoch,
                        os.getpid(),
                        auth["tokenId"],
                        socket_path,
                    )
                )
            self.assertEqual(raised.exception.code, "daemon.security_unsafe")
            self.assertEqual(Path(socket_path).read_bytes(), b"operator-owned-evidence")
            self.assertTrue(Path(core.daemonStatePath()).exists())
            self.assertTrue(Path(daemonAuthTokenPath()).exists())
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
