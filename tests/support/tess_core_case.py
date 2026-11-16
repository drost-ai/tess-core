from __future__ import annotations

import asyncio
import base64
import contextlib
import http.server
import io
import json
import math
import os
import queue
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tess_core._facade as core
from tess_core import testing
import tess_core.agent as agent_module
import tess_core.mcp as mcp_module
import tess_core.tasks.monitor_narration as monitors_module
import tess_core.providers.anthropic as anthropic_module
import tess_core.providers.openai as openai_module
import tess_core.runtime.bootstrap as runtime_module
import tess_core.subagents as subagents_module
import tess_core.tools as tools_module


def run(coro):
    return asyncio.run(coro)


def bind_test_runtime(session, legacy=None):
    legacy = dict(legacy or {})
    daemon = legacy.pop("daemon", None)
    daemon_reason = legacy.pop(
        "daemonUnavailableReason",
        {
            "code": "test.daemon_unavailable",
            "message": "The task daemon is intentionally unavailable in this test fixture.",
        },
    )
    interactive = legacy.pop("interactive", False)
    ask_question = legacy.pop("askQuestion", lambda *_args, **_kwargs: "")
    emit_event = legacy.pop("emitEvent", lambda *_args, **_kwargs: None)
    tools = legacy.pop("tools", [])
    refresh = legacy.pop("refreshMcpToolRegistry", lambda: None)
    daemon_binding = (
        {"state": "connected", "client": daemon}
        if daemon is not None and core.is_task_supervisor_gateway(daemon)
        else {"state": "unavailable", "reason": daemon_reason}
    )
    return core.create_agent_runtime_bindings(
        {
            "tools": tools,
            "emitEvent": emit_event,
            "daemon": daemon_binding,
            "refreshMcpToolRegistry": refresh,
            "sessionBinding": core.create_runtime_session_binding(session["id"], 1, 1),
            "questionPolicy": {"interactive": interactive, "askQuestion": ask_question},
            **legacy,
        }
    )


class AsyncChunks:
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.ok = 200 <= status < 300

    async def text(self):
        return self.body


def load_builtin_tool_contract_fixture():
    fixture_dir = Path(__file__).parents[1] / "fixtures" / "tool_contracts"
    paths = sorted((fixture_dir / "builtins").glob("*.json"))
    if paths:
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    legacy_path = fixture_dir / "builtin_tools.json"
    return json.loads(legacy_path.read_text(encoding="utf-8"))


class TessCoreTestCase(unittest.TestCase):
    def temp_context(self):
        root = tempfile.TemporaryDirectory(prefix="tess-core-")
        home = Path(root.name) / "home"
        workspace = Path(root.name) / "workspace"
        home.mkdir()
        workspace.mkdir()
        previous = os.environ.get("TESS_CORE_HOME")
        os.environ["TESS_CORE_HOME"] = str(home)
        return root, home, workspace, previous

    def restore_home(self, root, previous):
        from tess_core.config._knobs import reset_configured_knobs
        from tess_core.providers.token_budget import reset_configured_model_limit_rules

        core.reset_configured_runtime_home()
        reset_configured_knobs()
        reset_configured_model_limit_rules()
        if previous is None:
            os.environ.pop("TESS_CORE_HOME", None)
        else:
            os.environ["TESS_CORE_HOME"] = previous
        # A process the test started may still be writing its last file into the
        # home as it exits; give it a moment before treating that as a failure.
        for _ in range(20):
            try:
                root.cleanup()
                return
            except OSError:
                time.sleep(0.05)
        root.cleanup()
