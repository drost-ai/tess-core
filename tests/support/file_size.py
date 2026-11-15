from __future__ import annotations

from pathlib import Path


MAX_HAND_AUTHORED_LINES = 500

HAND_AUTHORED_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}

SKIPPED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    ".scenario-runs",
    "__pycache__",
    "tess_core_python.egg-info",
    "build",
    "dist",
}


def iter_hand_authored_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIPPED_DIR_NAMES for part in path.parts):
            continue
        if path.suffix not in HAND_AUTHORED_SUFFIXES:
            continue
        yield path


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def find_line_budget_violations(
    root: Path,
    allowlist: dict[str, int],
    *,
    max_lines: int = MAX_HAND_AUTHORED_LINES,
) -> list[str]:
    seen: dict[str, int] = {}
    failures: list[str] = []

    for path in iter_hand_authored_files(root):
        rel = path.relative_to(root).as_posix()
        lines = count_lines(path)
        seen[rel] = lines
        allowed = allowlist.get(rel)

        if lines > max_lines and allowed is None:
            failures.append(f"{rel}: {lines} lines exceeds {max_lines}")
        elif allowed is not None and lines > allowed:
            failures.append(f"{rel}: {lines} lines exceeds allowlist cap {allowed}")

    for rel, allowed in sorted(allowlist.items()):
        lines = seen.get(rel)
        if lines is None:
            failures.append(f"{rel}: allowlist entry points to a missing file")
        elif lines <= max_lines:
            failures.append(
                f"{rel}: now {lines} lines; remove allowlist cap {allowed}"
            )

    return failures
