from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tess_core import SessionOptions, start_runtime


async def _send(request: dict) -> dict:
    return {"model": "scripted", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "done"}]}


class SessionSubscriptionTests(unittest.TestCase):
    """A subscription belongs to the session, not to one loading of it."""

    def test_a_subscriber_still_hears_a_session_that_was_unloaded_and_reopened(self) -> None:
        async def check(root: Path) -> None:
            (root / "work").mkdir()
            runtime = await start_runtime(
                root / "home", cwd=root / "work", daemon_enabled=False,
                session_options=lambda session_id, reason: SessionOptions(provider="anthropic"),
            )
            try:
                session = await runtime.sessions.open()
                heard: list[str] = []
                unsubscribe = session.subscribe(lambda envelope: heard.append(envelope["event"]))
                await session.run_turn("one", provider_override={"send": _send})
                self.assertIn("turn.completed", heard)

                # What the idle sweep does to a session nobody has used for a while.
                await runtime.sessions.close(session.id, reason="idle")
                self.assertFalse(session.is_open)
                heard.clear()
                await session.run_turn("two", provider_override={"send": _send})
                self.assertTrue(session.is_open)
                self.assertEqual([name for name in heard if name.startswith("turn.")], ["turn.started", "turn.completed"])

                unsubscribe()
                heard.clear()
                await session.run_turn("three", provider_override={"send": _send})
                self.assertEqual(heard, [])
            finally:
                await runtime.stop()

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(check(Path(folder).resolve()))


if __name__ == "__main__":
    unittest.main()
