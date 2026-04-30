from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from .._utils import clone
from ..providers.openai import isCodexBaseUrl
from ..storage import (
    configureRuntimeHome,
    ensureBaseLayout,
    fileOrJson,
    getContextRuntimeHome,
    getPlansDir,
    loadAuth,
    loadCodexAuthToken,
    loadMcpServers,
    loadSettingsBySources,
    resolveMaybeRelative,
    resolveRuntimeHome,
)


DEFAULT_ASSISTANT_NAME = "assistant"
DEFAULT_ASSISTANT_DESCRIPTION = "a helpful and reliable software engineering agent"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "xhigh"
DEFAULT_OPENAI_API_MODE = "responses"
DEFAULT_OPENAI_CODEX_AUTH_PATH = os.path.join(os.environ.get("CODEX_HOME", "~/.codex"), "auth.json")
DEFAULT_PROVIDER_PROFILE_NAME = "openai"
DEFAULT_TUI_SCREEN_MODE = "auto"

VALID_EFFORT_LEVELS = ["low", "medium", "high", "max", "xhigh"]

PROVIDER_MODELS = {
    "anthropic": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"],
}
PROVIDER_DEFAULT_MODEL = {"anthropic": DEFAULT_MODEL, "openai": "gpt-5.4"}
PROVIDER_DEFAULT_EFFORT = {"anthropic": "high", "openai": "xhigh"}

BUILTIN_PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "openai": {
        "kind": "openai",
        "apiMode": "responses",
        "defaultModel": "gpt-5.4",
        "auth": {"kind": "openai_api_key", "envVar": "OPENAI_API_KEY"},
        "description": "Official OpenAI Responses API",
    },
    "codex": {
        "kind": "openai",
        "apiMode": "codex",
        "defaultModel": "gpt-5.4",
        "auth": {"kind": "openai_api_key_or_codex", "envVar": "OPENAI_API_KEY"},
        "description": "OpenAI Codex-compatible backend",
    },
    "anthropic-console": {
        "kind": "anthropic",
        "defaultModel": "claude-sonnet-4-6",
        "auth": {"kind": "anthropic_api_key", "envVar": "ANTHROPIC_API_KEY"},
        "description": "Anthropic Console API key",
    },
    "anthropic-claude-code": {
        "kind": "anthropic",
        "defaultModel": "claude-sonnet-4-6",
        "auth": {"kind": "anthropic_auth_token", "envVar": "ANTHROPIC_AUTH_TOKEN"},
        "description": "Claude Code / Claude.ai token",
    },
}

BUILTIN_AGENTS = {
    "default": {
        "description": "General-purpose coding agent",
        "prompt": "You are a pragmatic software engineer. Solve the user's task end to end, inspect the codebase before changing it, and verify your work.",
    },
    "reviewer": {
        "description": "Code reviewer",
        "prompt": "You are a strict code reviewer. Prioritize bugs, regressions, risky assumptions, and missing tests. Keep summaries brief.",
    },
    "planner": {
        "description": "Planning agent",
        "prompt": "You are a planning-focused engineering lead. Break work into concrete steps, note assumptions, and emphasize execution order.",
    },
    "debugger": {
        "description": "Debugging agent",
        "prompt": "You are a debugging specialist. Reproduce issues, isolate causes, propose the smallest defensible fix, and verify the result.",
    },
}
