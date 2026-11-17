from __future__ import annotations

from typing import Any

from ..config.shared import declared_image_input, model_implies_image_input


IMAGE_INPUT_UNSUPPORTED_MESSAGE = "This model does not support image inputs."


def request_has_image_inputs(request: dict[str, Any]) -> bool:
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if _content_has_image_input(content):
            return True
    return False


def model_supports_image_inputs(config: dict[str, Any], model: str) -> bool:
    """Whether `model` takes image inputs, by the rule tool-result attachments also use.

    The session's capabilities.imageInput wins (a loaded profile always has one, declared or
    inferred from its model); then the profile's own declaration; then the model's name.
    """
    profile = config.get("providerProfile") or {}
    capabilities = config.get("providerCapabilities")
    if isinstance(capabilities, dict) and isinstance(capabilities.get("imageInput"), bool):
        return capabilities["imageInput"]
    declared = declared_image_input(profile)
    if declared is not None:
        return declared
    kind = str(config.get("provider") or config.get("providerKind") or "").strip().lower()
    mode = config.get("openaiApiMode") or profile.get("apiMode")
    base_url = config.get("openaiBaseUrl") or profile.get("baseUrl")
    return model_implies_image_input(kind, model, base_url, mode)


def unsupported_image_input_response(model: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": IMAGE_INPUT_UNSUPPORTED_MESSAGE}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": model,
        "stopReason": "unsupported_image_input",
    }


def _content_has_image_input(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type in ("image", "input_image"):
            return True
    return False
