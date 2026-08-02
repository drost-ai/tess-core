from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable


_REQUIRED_DIST_FILES = (
    "dist/index.js",
    "dist/testing.js",
    "dist/lib/tool-dispatch.js",
)


def inspect_typescript_peer(repo: str) -> dict[str, Any]:
    root = Path(repo).resolve()
    head = _git_output(root, "rev-parse", "HEAD")
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    dirty_entries = status.splitlines() if status else []
    source_files = _source_inputs(root)
    dist_files = sorted((root / "dist").rglob("*.js")) if (root / "dist").is_dir() else []
    required_dist_present = all((root / rel_path).is_file() for rel_path in _REQUIRED_DIST_FILES)
    expected_commit = os.environ.get("AGENT_CORE_TYPESCRIPT_EXPECTED_REF") or None
    source_latest = _latest_mtime_ns(source_files)
    dist_latest = _latest_mtime_ns(dist_files)
    build_fresh = bool(
        required_dist_present
        and source_latest is not None
        and dist_latest is not None
        and dist_latest >= source_latest
    )
    clean = not dirty_entries
    commit_matches = expected_commit is None or head == expected_commit
    provenance = {
        "repo": str(root),
        "sourceCommit": head,
        "expectedCommit": expected_commit,
        "commitMatchesExpected": commit_matches,
        "clean": clean,
        "dirtyEntryCount": len(dirty_entries),
        "sourceIdentity": _tree_identity(root, source_files),
        "distIdentity": _tree_identity(root, dist_files),
        "requiredDistFilesPresent": required_dist_present,
        "buildFresh": build_fresh,
    }
    provenance["trustedForGoldenUpdate"] = bool(
        head
        and clean
        and commit_matches
        and required_dist_present
        and build_fresh
    )
    return provenance


def provenance_error(
    provenance: dict[str, Any],
    *,
    require_trusted_reference: bool = False,
) -> str | None:
    if not provenance.get("requiredDistFilesPresent"):
        return "TypeScript parity peer is missing one or more required dist modules."
    if provenance.get("expectedCommit") and not provenance.get("commitMatchesExpected"):
        return (
            "TypeScript parity peer does not match AGENT_CORE_TYPESCRIPT_EXPECTED_REF "
            f"({provenance.get('sourceCommit')} != {provenance.get('expectedCommit')})."
        )
    if _truthy_env("AGENT_CORE_TYPESCRIPT_REQUIRE_CLEAN") and not provenance.get("clean"):
        return "TypeScript parity peer is dirty while AGENT_CORE_TYPESCRIPT_REQUIRE_CLEAN is enabled."
    if _truthy_env("AGENT_CORE_TYPESCRIPT_REQUIRE_FRESH_BUILD") and not provenance.get("buildFresh"):
        return "TypeScript parity peer dist output is older than its source inputs."
    if require_trusted_reference and not provenance.get("trustedForGoldenUpdate"):
        return "Golden updates require a clean TypeScript peer with a fresh, complete dist build."
    return None


def _source_inputs(root: Path) -> list[Path]:
    candidates = [
        root / "package.json",
        root / "package-lock.json",
        root / "tsconfig.json",
    ]
    src = root / "src"
    if src.is_dir():
        candidates.extend(src.rglob("*.ts"))
    return sorted(path for path in candidates if path.is_file())


def _tree_identity(root: Path, files: Iterable[Path]) -> str | None:
    digest = hashlib.sha256()
    count = 0
    for file_path in files:
        try:
            relative = file_path.relative_to(root).as_posix()
            content = file_path.read_bytes()
        except (OSError, ValueError):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        count += 1
    return digest.hexdigest() if count else None


def _latest_mtime_ns(files: Iterable[Path]) -> int | None:
    latest: int | None = None
    for file_path in files:
        try:
            modified = file_path.stat().st_mtime_ns
        except OSError:
            continue
        latest = modified if latest is None else max(latest, modified)
    return latest


def _git_output(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}
