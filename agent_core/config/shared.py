from __future__ import annotations

import os

from ..contracts.config import AgentDefinition, ProviderProfile


DEFAULT_ASSISTANT_NAME = "assistant"
DEFAULT_ASSISTANT_DESCRIPTION = "a helpful and reliable software engineering agent"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "xhigh"
DEFAULT_OPENAI_API_MODE = "responses"
DEFAULT_OPENAI_CODEX_AUTH_PATH = os.path.join(os.environ.get("CODEX_HOME", "~/.codex"), "auth.json")
DEFAULT_PROVIDER_PROFILE_NAME = "openai"
DEFAULT_TUI_SCREEN_MODE = "auto"

DEFAULT_IMAGE_INPUT_MIME_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"]
DEFAULT_MAX_IMAGE_INPUT_COUNT = 8
DEFAULT_MAX_IMAGE_INPUT_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES = 24 * 1024 * 1024
MAX_PROVIDER_IMAGE_INPUT_COUNT = 32
MAX_PROVIDER_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
MAX_PROVIDER_AGGREGATE_IMAGE_INPUT_BYTES = 40 * 1024 * 1024
MAX_PROVIDER_ENCODED_IMAGE_REQUEST_BYTES = 56 * 1024 * 1024

VALID_EFFORT_LEVELS = ["low", "medium", "high", "max", "xhigh"]

PROVIDER_MODELS = {
    "anthropic": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"],
}
PROVIDER_DEFAULT_MODEL = {"anthropic": DEFAULT_MODEL, "openai": "gpt-5.4"}
PROVIDER_DEFAULT_EFFORT = {"anthropic": "high", "openai": "xhigh"}

BUILTIN_PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "openai": {
        "kind": "openai",
        "apiMode": "responses",
        "defaultModel": "gpt-5.4",
        "auth": {"kind": "openai_api_key", "envVar": "OPENAI_API_KEY"},
        "description": "Official OpenAI Responses API",
        "capabilities": {
            "imageInput": True,
            "imageMimeTypes": DEFAULT_IMAGE_INPUT_MIME_TYPES,
            "maxImageCount": DEFAULT_MAX_IMAGE_INPUT_COUNT,
            "maxImageBytes": DEFAULT_MAX_IMAGE_INPUT_BYTES,
            "maxAggregateImageBytes": DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES,
            "maxEncodedImageRequestBytes": DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES,
        },
    },
    "codex": {
        "kind": "openai",
        "apiMode": "codex",
        "defaultModel": "gpt-5.4",
        "auth": {"kind": "openai_api_key_or_codex", "envVar": "OPENAI_API_KEY"},
        "description": "OpenAI Codex-compatible backend",
        "capabilities": {
            "openaiResponsesIncremental": True,
            "openaiResponsesWebSocket": True,
            "openaiTurnState": True,
            "imageInput": True,
            "imageMimeTypes": DEFAULT_IMAGE_INPUT_MIME_TYPES,
            "maxImageCount": DEFAULT_MAX_IMAGE_INPUT_COUNT,
            "maxImageBytes": DEFAULT_MAX_IMAGE_INPUT_BYTES,
            "maxAggregateImageBytes": DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES,
            "maxEncodedImageRequestBytes": DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES,
        },
    },
    "anthropic-console": {
        "kind": "anthropic",
        "defaultModel": "claude-sonnet-4-6",
        "auth": {"kind": "anthropic_api_key", "envVar": "ANTHROPIC_API_KEY"},
        "description": "Anthropic Console API key",
        "capabilities": {
            "imageInput": True,
            "imageMimeTypes": DEFAULT_IMAGE_INPUT_MIME_TYPES,
            "maxImageCount": DEFAULT_MAX_IMAGE_INPUT_COUNT,
            "maxImageBytes": DEFAULT_MAX_IMAGE_INPUT_BYTES,
            "maxAggregateImageBytes": DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES,
            "maxEncodedImageRequestBytes": DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES,
        },
    },
    "anthropic-claude-code": {
        "kind": "anthropic",
        "defaultModel": "claude-sonnet-4-6",
        "auth": {"kind": "anthropic_auth_token", "envVar": "ANTHROPIC_AUTH_TOKEN"},
        "description": "Claude Code / Claude.ai token",
        "capabilities": {
            "imageInput": True,
            "imageMimeTypes": DEFAULT_IMAGE_INPUT_MIME_TYPES,
            "maxImageCount": DEFAULT_MAX_IMAGE_INPUT_COUNT,
            "maxImageBytes": DEFAULT_MAX_IMAGE_INPUT_BYTES,
            "maxAggregateImageBytes": DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES,
            "maxEncodedImageRequestBytes": DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES,
        },
    },
}

BUILTIN_AGENTS: dict[str, AgentDefinition] = {
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
