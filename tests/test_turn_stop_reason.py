from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tess_core import SessionOptions, start_runtime


def _reply(stop_reason: str) -> dict:
    return {"model": "scripted", "stopReason": stop_reason, "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "done"}]}


class TurnStopReasonTests(unittest.TestCase):
    """A turn's result names its end the same way whichever provider ran it."""

    def test_provider_native_stop_reasons_are_reported_in_one_vocabulary(self) -> None:
        async def check(root: Path) -> None:
            (root / "work").mkdir()
            runtime = await start_runtime(
                root / "home", cwd=root / "work", daemon_enabled=False,
                session_options=lambda session_id, reason: SessionOptions(provider="anthropic"),
            )
            try:
                session = await runtime.sessions.open()
                completed: list[str] = []
                session.subscribe(lambda envelope: completed.append(envelope["data"].get("stop_reason")) if envelope["event"] == "turn.completed" else None)
                for native, reported in (("stop", "end_turn"), ("end_turn", "end_turn"), ("length", "max_tokens"), ("max_output_tokens", "max_tokens"), ("refusal", "refusal")):
                    async def send(request: dict, native: str = native) -> dict:
                        return _reply(native)

                    result = await session.run_turn("go", provider_override={"send": send})
                    self.assertEqual(result["stopReason"], reported, native)
                    self.assertEqual(completed[-1], reported, native)
            finally:
                await runtime.stop()

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(check(Path(folder).resolve()))


if __name__ == "__main__":
    unittest.main()
