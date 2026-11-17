from __future__ import annotations

from ..contracts.config import AgentDefinition, ProviderProfile


DEFAULT_ASSISTANT_NAME = "assistant"
DEFAULT_ASSISTANT_DESCRIPTION = "a helpful and reliable software engineering agent"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "xhigh"
DEFAULT_OPENAI_API_MODE = "responses"
DEFAULT_PROVIDER_PROFILE_NAME = "openai"
DEFAULT_TUI_SCREEN_MODE = "auto"

DEFAULT_IMAGE_INPUT_MIME_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"]
def default_max_image_input_count() -> int:
    from ._knobs import knob_value

    return int(knob_value("images.max_count"))


def default_max_image_input_bytes() -> int:
    from ._knobs import knob_value

    return int(knob_value("images.max_bytes"))


def default_max_aggregate_image_input_bytes() -> int:
    from ._knobs import knob_value

    return int(knob_value("images.max_aggregate_bytes"))


def default_max_encoded_image_request_bytes() -> int:
    from ._knobs import knob_value

    return int(knob_value("images.max_encoded_request_bytes"))
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

# Knob-derived image limits are deliberately absent: profile resolution
# fills them live, and baking them at import time (before configure_knobs)
# shadowed user overrides with stale defaults.
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


# Whether a model takes image inputs. One rule for every place that asks: loading a
# profile, attaching an image to a tool result, and sending a request with images.
# A declaration wins (capabilities.imageInput, then supportsVision or an older spelling);
# without one, the model's name decides.

def declared_image_input(profile: dict | None) -> bool | None:
    """The profile's own answer to "does this model take images?", or None if it is silent."""
    profile = profile if isinstance(profile, dict) else {}
    capabilities = profile.get("capabilities")
    if isinstance(capabilities, dict) and isinstance(capabilities.get("imageInput"), bool):
        return capabilities["imageInput"]
    for key in ("supportsVision", "supports_vision", "vision"):
        if isinstance(profile.get(key), bool):
            return profile[key]
    if isinstance(capabilities, dict):
        for key in ("vision", "image_inputs", "imageInputs", "multimodal"):
            if isinstance(capabilities.get(key), bool):
                return capabilities[key]
    if isinstance(capabilities, list):
        named = {str(item).strip().lower() for item in capabilities}
        if named & {"vision", "image_inputs", "image-inputs"}:
            return True
    return None


def model_implies_image_input(
    kind: str | None, model: str | None, base_url: str | None = None, api_mode: str | None = None
) -> bool:
    """What a model's name says about image input, for profiles that do not declare it."""
    name = str(model or "").strip().lower()
    if not name:
        return False
    if kind == "anthropic":
        return name.startswith("claude-")
    if kind != "openai":
        return False
    base = str(base_url or "").strip().lower()
    official = str(api_mode or "").strip().lower() == "codex" or not base or "api.openai.com" in base or "chatgpt.com" in base
    if official and name.startswith(("gpt-4", "gpt-5", "gpt-6", "o3", "o4")):
        return True
    return any(marker in name for marker in ("vision", "vl", "multimodal", "omni"))
