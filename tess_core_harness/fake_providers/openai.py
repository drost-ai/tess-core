from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from .model import json_safe


class FakeOpenAIClientFactory:
    def __init__(
        self,
        *,
        api: str,
        script: dict[str, Any] | None,
        request_recorder: list[dict[str, Any]],
        abort_signal: Any,
    ) -> None:
        self.api = api
        self.script = _normalize_script(script, api)
        self.request_recorder = request_recorder
        self.abort_signal = abort_signal
        self.create_count = 0

    def client_factory(self, options: dict[str, Any]) -> Any:
        async def create(params: dict[str, Any]) -> ScriptedAsyncStream:
            self.create_count += 1
            attempt = self.create_count
            attempt_script = _select_attempt(self.script, attempt)
            self.request_recorder.append(
                {
                    "provider": "openai",
                    "api": self.api,
                    "attempt": attempt,
                    "options": json_safe(options),
                    "params": json_safe(params),
                }
            )
            delay_ms = int(attempt_script.get("createDelayMs") or 0)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
            if attempt_script.get("createError") is not None:
                raise RuntimeError(str(attempt_script["createError"]))
            stream = attempt_script.get("stream") or []
            return ScriptedAsyncStream(stream, self.abort_signal)

        if self.api == "chat_completions":
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return SimpleNamespace(responses=SimpleNamespace(create=create))


class ScriptedAsyncStream:
    def __init__(self, actions: list[Any], abort_signal: Any) -> None:
        self.actions = actions
        self.abort_signal = abort_signal

    async def __aiter__(self):
        for action in self.actions:
            if not isinstance(action, dict):
                yield action
                continue
            if action.get("abort"):
                abort = getattr(self.abort_signal, "abort", None)
                if callable(abort):
                    abort()
                else:
                    setattr(self.abort_signal, "aborted", True)
            delay_ms = int(action.get("delayMs") or 0)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
            if action.get("raise") is not None:
                raise RuntimeError(str(action["raise"]))
            if "event" in action:
                yield action["event"]
                continue
            if "chunk" in action:
                yield action["chunk"]
                continue
            if "payload" in action:
                yield action["payload"]
                continue
            if set(action) <= {"abort", "delayMs"}:
                continue
            yield action


def _normalize_script(script: dict[str, Any] | None, api: str) -> dict[str, Any]:
    if script and isinstance(script.get("attempts"), list) and script["attempts"]:
        return script
    return {"attempts": [_default_attempt(api)]}


def _select_attempt(script: dict[str, Any], attempt: int) -> dict[str, Any]:
    attempts = script.get("attempts") or []
    selected = attempts[min(attempt - 1, len(attempts) - 1)]
    if isinstance(selected, list):
        return {"stream": selected}
    if isinstance(selected, dict):
        return selected
    return {"stream": []}


def _default_attempt(api: str) -> dict[str, Any]:
    if api == "chat_completions":
        return {
            "stream": [
                {
                    "choices": [
                        {
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ]
        }
    return {
        "stream": [
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        ]
    }
