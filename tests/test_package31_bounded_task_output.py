from __future__ import annotations

from tests.support.agent_core_case import *

import agent_core.payloads.bounded_io as bounded_io_module
from agent_core.payloads.bounded_io import read_file_prefix_bounded, read_file_tail_bounded
from agent_core.payloads.utf8_bounds import utf8_byte_length


class _ObservedFile:
    def __init__(self, handle, read_sizes: list[int]):
        self._handle = handle
        self._read_sizes = read_sizes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._handle.close()

    def fileno(self):
        return self._handle.fileno()

    def seek(self, *args):
        return self._handle.seek(*args)

    def read(self, size=-1):
        self._read_sizes.append(size)
        return self._handle.read(size)


class Package31BoundedTaskOutputTests(AgentCoreTestCase):
    def test_tail_reader_never_reads_or_allocates_the_full_multimegabyte_file(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-bounded-tail-") as directory:
            path = Path(directory) / "large.log"
            raw = (b"prefix-" * 600_000) + ("最后🙂terminal\n".encode("utf-8"))
            path.write_bytes(raw)
            limit = 12_000
            read_sizes: list[int] = []
            builtin_open = open

            def observed_open(*args, **kwargs):
                return _ObservedFile(builtin_open(*args, **kwargs), read_sizes)

            with mock.patch.object(bounded_io_module, "open", observed_open, create=True):
                result = run(read_file_tail_bounded(filePath=str(path), maxBytes=limit))

            self.assertTrue(result["truncated"])
            self.assertEqual(result["originalBytes"], len(raw))
            self.assertLessEqual(result["returnedBytes"], limit)
            self.assertEqual(result["returnedBytes"], utf8_byte_length(result["text"]))
            self.assertTrue(result["text"].endswith("最后🙂terminal\n"))
            self.assertTrue(read_sizes)
            self.assertNotIn(-1, read_sizes)
            self.assertLessEqual(sum(read_sizes), limit + 4)
            self.assertLessEqual(max(read_sizes), limit + 4)

            read_sizes.clear()
            with mock.patch.object(bounded_io_module, "open", observed_open, create=True):
                prefix = run(read_file_prefix_bounded(filePath=str(path), maxBytes=limit))
            self.assertTrue(prefix["truncated"])
            self.assertEqual(prefix["originalBytes"], len(raw))
            self.assertLessEqual(prefix["returnedBytes"], limit)
            self.assertTrue(prefix["text"].startswith("prefix-prefix-"))
            self.assertLessEqual(sum(read_sizes), limit + 4)
            self.assertLessEqual(max(read_sizes), limit + 4)

    def test_supervisor_and_task_tool_return_a_bounded_tail_with_full_log_recovery(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(
                core.resolve_config(
                    {
                        "provider": "openai",
                        "runtimeHome": str(home),
                        "permissionMode": "bypassPermissions",
                        "openaiBaseUrl": "http://compatible.test",
                        "openaiApiMode": "chat",
                        "model": "tiny-model",
                    },
                    str(workspace),
                )
            )
            session = core.create_empty_session(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="bypassPermissions",
            )
            task_id = "bounded-task-output"
            output_path = core.task_output_path(task_id)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            raw = (b"old-output\n" * 350_000) + "最后🙂terminal\n".encode("utf-8")
            Path(output_path).write_bytes(raw)
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "taskId": task_id,
                        "id": task_id,
                        "sessionId": session["id"],
                        "command": "large task",
                        "cwd": str(workspace),
                        "startedAt": "2026-01-01T00:00:00Z",
                        "status": "completed",
                        "outputFile": output_path,
                    },
                )
            )
            session["metadata"]["taskIds"] = [task_id]

            supervisor_result = run(core.read_task_output(task_id, tail_bytes=4096))
            self.assertTrue(supervisor_result["truncated"])
            self.assertLessEqual(utf8_byte_length(supervisor_result["output"]), 4096)
            self.assertTrue(supervisor_result["output"].endswith("最后🙂terminal\n"))

            context = {
                "session": session,
                "config": config,
                "interactive": False,
                "askQuestion": lambda *_: "",
                "outputJson": lambda *_: None,
                "cwd": str(workspace),
                "persist_session": lambda: None,
            }
            task_output = next(tool for tool in core.create_builtin_tools() if tool.name == "TaskOutput")
            result = run(task_output.handler({"task_id": task_id, "block": False}, context))
            payload = json.loads(result["content"])
            manifest = payload["output_manifest"]
            self.assertTrue(payload["output_truncated"])
            self.assertTrue(manifest["truncated"])
            self.assertTrue(manifest["sourceTruncated"])
            self.assertEqual(manifest["originalBytes"], len(raw))
            self.assertEqual(manifest["sourceFile"], output_path)
            self.assertEqual(
                manifest["recovery"],
                {"type": "task_output_file", "filePath": output_path, "taskId": task_id},
            )
            self.assertNotIn("artifact", manifest)
            self.assertLessEqual(utf8_byte_length(payload["output"]), 8000)
            self.assertTrue(payload["output"].endswith("最后🙂terminal"))
            self.assertEqual(Path(manifest["recovery"]["filePath"]).stat().st_size, len(raw))
        finally:
            self.restore_home(root, previous)
