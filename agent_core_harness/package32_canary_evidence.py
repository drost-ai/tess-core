from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path
from typing import Any


MAX_CANARY_EVIDENCE_BYTES = 128 * 1024


class Package32CanaryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def requirePaidCanaryPreflight(options: dict[str, Any]) -> dict[str, Any]:
    if options.get("authorized") is not True:
        raise Package32CanaryError(
            "authorization_required",
            "Explicit paid-provider authorization is required.",
        )
    model = options.get("model")
    if model != "claude-opus-4-8":
        raise Package32CanaryError(
            "model_not_reviewed",
            "Package 32 canaries require the reviewed claude-opus-4-8 model.",
        )
    ceiling = options.get("maxCostUsd")
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, (int, float))
        or not math.isfinite(ceiling)
        or ceiling <= 0
    ):
        raise Package32CanaryError(
            "cost_ceiling_required",
            "A finite positive maximum USD cost ceiling is required.",
        )
    runtime_home = _absolute_path(options.get("runtimeHome"), "runtime_home_required")
    evidence_path = _absolute_path(options.get("evidenceFile"), "evidence_file_required")
    if _same_or_nested(runtime_home, evidence_path):
        raise Package32CanaryError(
            "evidence_path_unsafe",
            "Evidence must not be stored inside the disposable runtime home.",
        )
    api_key = options.get("apiKey")
    auth_token = options.get("authToken")
    normalized_api_key = api_key.strip() if isinstance(api_key, str) else ""
    normalized_auth_token = auth_token.strip() if isinstance(auth_token, str) else ""
    if not normalized_api_key and not normalized_auth_token:
        raise Package32CanaryError(
            "credential_required",
            "An Anthropic API key or auth token is required after authorization.",
        )
    return {
        "model": model,
        "maxCostUsd": float(ceiling),
        "runtimeHome": runtime_home,
        "evidenceFile": evidence_path,
        "apiKey": normalized_api_key or None,
        "authToken": normalized_auth_token or None,
    }


def preparePrivateCanaryRuntime(runtime_home: str) -> None:
    target = Path(runtime_home)
    if target.exists() or target.is_symlink():
        raise Package32CanaryError(
            "runtime_home_exists",
            "The paid canary runtime home must be a new isolated path.",
        )
    _require_private_directory(target.parent)
    target.mkdir(parents=False, mode=0o700)
    os.chmod(target, 0o700)
    _require_private_directory(target)


def reservePrivateEvidenceFile(evidence_path: str) -> int:
    target = Path(evidence_path)
    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    _require_private_directory(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise Package32CanaryError(
            "evidence_exists",
            "Refusing to replace existing canary evidence.",
        ) from exc
    os.fchmod(descriptor, 0o600)
    return descriptor


def writePrivateCanaryEvidence(descriptor: int, evidence: dict[str, Any]) -> None:
    payload = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_CANARY_EVIDENCE_BYTES:
        raise Package32CanaryError(
            "evidence_oversized",
            "Canary evidence exceeds the 128 KiB contract.",
        )
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("Canary evidence write made no progress.")
        offset += written
    os.fsync(descriptor)


def safeFailureEvidence(base: dict[str, Any], error: BaseException) -> dict[str, Any]:
    if isinstance(error, Package32CanaryError):
        code = error.code
    else:
        record = getattr(error, "record", None)
        if isinstance(record, dict):
            phase = _closed_failure_component(record.get("phase"), "provider")
            category = _closed_failure_component(record.get("category"), "failure")
            code = f"provider_{phase}_{category}"
        else:
            code = "internal_failure"
    return {**base, "status": "failed", "failureReason": code}


def _closed_failure_component(value: Any, fallback: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in str(value or "").strip().lower()
    ).strip("_")
    return normalized[:48] or fallback


def _absolute_path(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Package32CanaryError(code, "An explicit absolute path is required.")
    normalized = os.path.normpath(value)
    if not os.path.isabs(normalized):
        raise Package32CanaryError(code, "An explicit absolute path is required.")
    return normalized


def _same_or_nested(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False


def _require_private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise Package32CanaryError("private_path_unsafe", "Canary path is not a directory.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise Package32CanaryError("private_path_unsafe", "Canary path is not caller-owned.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise Package32CanaryError("private_path_unsafe", "Canary path is not owner-only.")
