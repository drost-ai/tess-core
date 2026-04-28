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

import agent_core as core
from agent_core import testing
import agent_core.agent as agent_module
import agent_core.interactive_actions as interactive_actions_module
import agent_core.mcp as mcp_module
import agent_core.monitors as monitors_module
import agent_core.providers.anthropic as anthropic_module
import agent_core.providers.openai as openai_module
import agent_core.runtime as runtime_module
import agent_core.tasks.subagents as subagents_module
import agent_core.tools as tools_module


def run(coro):
    return asyncio.run(coro)


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


class AgentCoreTestCase(unittest.TestCase):
    def temp_context(self):
        root = tempfile.TemporaryDirectory(prefix="agent-core-python-")
        home = Path(root.name) / "home"
        workspace = Path(root.name) / "workspace"
        home.mkdir()
        workspace.mkdir()
        previous = os.environ.get("AGENT_CORE_HOME")
        os.environ["AGENT_CORE_HOME"] = str(home)
        return root, home, workspace, previous

    def restore_home(self, root, previous):
        if previous is None:
            os.environ.pop("AGENT_CORE_HOME", None)
        else:
            os.environ["AGENT_CORE_HOME"] = previous
        root.cleanup()
