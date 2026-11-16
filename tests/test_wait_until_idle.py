from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tess_core import SessionOptions, start_runtime
from tess_core.runtime import idle_wait


def _reply(text: str) -> dict:
    return {"model": "scripted", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}


class WaitUntilIdleTests(unittest.TestCase):
    """runtime.wait_until_idle() returns when turns, sub-agents and their unread messages are done."""

    def _run(self, scenario) -> None:
        async def body(root: Path) -> None:
            (root / "work").mkdir()
            runtime = await start_runtime(
                root / "home", cwd=root / "work", daemon_enabled=False,
                session_options=lambda session_id, reason: SessionOptions(provider="anthropic"),
            )
            try:
                await scenario(runtime)
            finally:
                await runtime.stop()

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(body(Path(folder).resolve()))

    def test_returns_at_once_when_nothing_is_going_on(self) -> None:
        async def scenario(runtime) -> None:
            await runtime.sessions.open()
            await asyncio.wait_for(runtime.wait_until_idle(), timeout=5)

        self._run(scenario)

    def test_waits_for_a_running_turn_and_names_it_on_timeout(self) -> None:
        async def scenario(runtime) -> None:
            session = await runtime.sessions.open()
            release = asyncio.Event()

            async def send(request: dict) -> dict:
                await release.wait()
                return _reply("done")

            turn = asyncio.create_task(session.run_turn("go", provider_override={"send": send}))
            await asyncio.sleep(0.2)
            with self.assertRaises(TimeoutError) as raised:
                await runtime.wait_until_idle(timeout=0.6)
            self.assertIn(f"session {session.id}: a turn is running", str(raised.exception))
            self.assertFalse(turn.done())

            release.set()
            await asyncio.wait_for(runtime.wait_until_idle(), timeout=10)
            self.assertTrue(turn.done())

        self._run(scenario)

    def test_waits_for_a_working_sub_agent_and_for_its_message_to_be_read(self) -> None:
        async def scenario(runtime) -> None:
            session = await runtime.sessions.open()
            child = {"agentId": "agent-1", "name": "counter", "state": "working"}
            # What the runtime sees over time: the child works, finishes with its
            # result still unread by the parent, and then the parent has read it.
            phases = [("working", False)] * 3 + [("stopped", True)] * 3 + [("stopped", False)] * 50
            seen: list[tuple[str, bool]] = []

            async def children():
                phase = phases[min(len(seen), len(phases) - 1)]
                seen.append(phase)
                return {session.id: [{**child, "state": phase[0]}]}

            async def unread(record):
                return seen[-1][1]

            with mock.patch.object(idle_wait, "inspect_children", children), mock.patch.object(idle_wait, "pending_child_work", unread):
                await asyncio.wait_for(runtime.wait_until_idle(), timeout=10)
            # It did not return while the child worked or while its message was unread,
            # and it saw quiet twice before believing it.
            self.assertEqual(seen[:6], [("working", False)] * 3 + [("stopped", True)] * 3)
            self.assertEqual(len(seen), 8)

        self._run(scenario)

    def test_returns_when_the_runtime_is_stopped(self) -> None:
        async def scenario(runtime) -> None:
            await runtime.stop()
            await asyncio.wait_for(runtime.wait_until_idle(), timeout=5)

        self._run(scenario)


if __name__ == "__main__":
    unittest.main()
