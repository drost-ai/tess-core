# Testing your agent

An application built on a model is hard to test if every test calls the model. Tess Core
lets you replace the provider with a script, so your tests are fast, free, deterministic
and able to run offline, while the rest of the runtime (the loop, tools, permissions,
storage, events) runs for real.

## A scripted provider

A provider override is an object with one async function, `send`. It receives the request
the runtime would have sent and returns the response the model would have given.

```python
class ScriptedProvider:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]


def text_reply(text):
    return {
        "model": "scripted",
        "stopReason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "text", "text": text}],
    }


def tool_reply(name, tool_input, tool_use_id="tu_1"):
    return {
        "model": "scripted",
        "stopReason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
    }
```

A response must include `model`.

## A test

```python
import pytest
from tess_core import SessionOptions, start_runtime


@pytest.mark.asyncio
async def test_agent_reads_the_file(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "notes.txt").write_text("the launch is on Friday")

    provider = ScriptedProvider(
        tool_reply("Read", {"file_path": str(tmp_path / "work" / "notes.txt")}),
        text_reply("The launch is on Friday."),
    )
    runtime = await start_runtime(
        tmp_path / "home",
        cwd=tmp_path / "work",
        daemon_enabled=False,
        session_options=lambda session_id, reason: SessionOptions(provider="anthropic"),
    )
    try:
        session = await runtime.sessions.open()
        result = await session.run_turn(
            "When is the launch?", provider_override={"send": provider.send}
        )
        assert result["text"] == "The launch is on Friday."
        assert result["toolCount"] == 1
        # the second request shows the model what the tool returned
        assert "the launch is on Friday" in str(provider.requests[1]["messages"])
    finally:
        await runtime.stop()
```

What this test exercised for real: the agent loop, the `Read` tool, the permission check,
session storage and the event stream. Only the model was replaced.

## Rules for reliable tests

- **A temporary runtime home per test.** Never point a test at a real one.
- **`daemon_enabled=False`** unless the test is about background work. It is faster and
  starts no extra process.
- **Always `await runtime.stop()`**, in a `finally` block or a fixture.
- **No real credentials.** With a provider override no key is read. Keep it that way by
  not setting any.
- **Short paths on macOS.** When the daemon is enabled it uses a Unix socket, and socket
  paths have a length limit. Keep the temporary home path short.

## What to assert

| You want to check | Look at |
|---|---|
| The final answer | `result["text"]`, `result["stopReason"]` |
| Which tools ran | `tool.started` events, or `result["toolCount"]` |
| What the model was shown | `provider.requests[n]["messages"]` and `["tools"]` |
| A permission decision | `permission.requested` and `permission.decided` events |
| Your own tool's effect | Your tool's side effect, as in any unit test |
| Saved state | Reopen the session by id and check `message_count` |

Collect events with a listener that records them:

```python
class Recorder:
    def __init__(self):
        self.events = []

    def on_event(self, session_id, envelope):
        self.events.append(envelope)

    def names(self):
        return [e["event"] for e in self.events]
```

## Testing the conversation, not the model

Scripted tests prove your application handles what a model might do: calls your tool with
these arguments, asks a question, fails, loops. They do not prove a real model will behave
well with your prompts. Keep a small, separate set of evaluations that call a real model,
and run them deliberately, not on every commit.

## Scripting the command line

The `run` command accepts scripted responses through the environment, for testing shell
scripts and CI jobs that call it:

```bash
TESS_CORE_HEADLESS_PROVIDER_RESPONSES='[{"model":"scripted","stopReason":"end_turn","usage":{"input_tokens":1,"output_tokens":1},"content":[{"type":"text","text":"ok"}]}]' \
    uv run python -m tess_core run -p "anything" --output-format json
```

`TESS_CORE_HEADLESS_PROVIDER_RESPONSES_FILE` reads the same list from a file.

## Running Tess Core's own tests

```bash
git clone https://github.com/drost-ai/tess-core.git
cd tess-core
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -n auto
```

The suite uses scripted providers throughout and needs no network or credentials.
