from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.providers.prompt_cache_state import commitPromptCacheObservation
from agent_core.runtime.paths import resolve_runtime_paths


async def main() -> None:
    candidate_path, runtime_home, ready_path, go_path, result_path = map(Path, sys.argv[1:])
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["paths"] = resolve_runtime_paths(candidate=str(runtime_home))
    ready_path.write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not go_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("prompt-cache state worker barrier timed out")
        await asyncio.sleep(0.01)
    result = await commitPromptCacheObservation(candidate)
    result_path.write_text(json.dumps({"status": result["status"]}) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
