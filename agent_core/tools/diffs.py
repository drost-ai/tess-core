from __future__ import annotations

from typing import Any

from .validation import _without_none_values


def _file_change_payload(tool: str, file_path: str, previous: str | None, next_text: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "type": "file_change",
        "tool": tool,
        "action": "create" if previous is None else "update",
        "filePath": file_path,
        "bytes": len(next_text.encode("utf-8")),
        "note": (options or {}).get("note"),
        "diff": _build_unified_diff(file_path, previous or "", next_text),
    }
    return _without_none_values(payload)


def _build_unified_diff(_file_path: str, before_text: str, after_text: str) -> str:
    before_lines = before_text.replace("\r\n", "\n").split("\n")
    after_lines = after_text.replace("\r\n", "\n").split("\n")
    operations = _build_line_diff_operations(before_lines, after_lines)
    changed_indexes = [index for index, operation in enumerate(operations) if operation["type"] != "equal"]
    if not changed_indexes:
        return "(no changes)"

    keep_indexes = _build_diff_context_indexes(operations, 3)
    max_line_num = 0
    before_cursor = 0
    after_cursor = 0
    for operation in operations:
        if operation["type"] == "equal":
            before_cursor += 1
            after_cursor += 1
        elif operation["type"] == "remove":
            before_cursor += 1
        else:
            after_cursor += 1
        max_line_num = max(max_line_num, before_cursor, after_cursor)
    num_width = max(3, len(str(max_line_num)))

    rendered = []
    before_line = 0
    after_line = 0
    previous_kept_index = -1
    for index, operation in enumerate(operations):
        if operation["type"] == "equal":
            before_line += 1
            after_line += 1
        elif operation["type"] == "remove":
            before_line += 1
        else:
            after_line += 1

        if index not in keep_indexes:
            continue
        if previous_kept_index != -1 and index > previous_kept_index + 1:
            rendered.append("...")
        line_num = before_line if operation["type"] == "remove" else after_line
        marker = "+" if operation["type"] == "add" else "-" if operation["type"] == "remove" else " "
        rendered.append(f"{str(line_num).rjust(num_width)} {marker} {operation['line']}")
        previous_kept_index = index
    return "\n".join(rendered)


def _build_line_diff_operations(before_lines: list[str], after_lines: list[str]) -> list[dict[str, str]]:
    if len(before_lines) * len(after_lines) > 1_000_000:
        return [{"type": "remove", "line": line} for line in before_lines] + [{"type": "add", "line": line} for line in after_lines]

    rows = len(before_lines)
    columns = len(after_lines)
    dp = [[0 for _ in range(columns + 1)] for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for column in range(columns - 1, -1, -1):
            if before_lines[row] == after_lines[column]:
                dp[row][column] = dp[row + 1][column + 1] + 1
            else:
                dp[row][column] = max(dp[row + 1][column], dp[row][column + 1])

    operations: list[dict[str, str]] = []
    row = 0
    column = 0
    while row < rows and column < columns:
        if before_lines[row] == after_lines[column]:
            operations.append({"type": "equal", "line": before_lines[row]})
            row += 1
            column += 1
            continue
        if dp[row + 1][column] >= dp[row][column + 1]:
            operations.append({"type": "remove", "line": before_lines[row]})
            row += 1
        else:
            operations.append({"type": "add", "line": after_lines[column]})
            column += 1
    while row < rows:
        operations.append({"type": "remove", "line": before_lines[row]})
        row += 1
    while column < columns:
        operations.append({"type": "add", "line": after_lines[column]})
        column += 1
    return operations


def _build_diff_context_indexes(operations: list[dict[str, str]], context_size: int) -> set[int]:
    indexes: set[int] = set()
    for index, operation in enumerate(operations):
        if operation["type"] == "equal":
            continue
        for cursor in range(max(0, index - context_size), min(len(operations) - 1, index + context_size) + 1):
            indexes.add(cursor)
    return indexes
