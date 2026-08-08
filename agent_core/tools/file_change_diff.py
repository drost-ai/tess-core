from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knobValue

    return knobValue(name)


from typing import Any


LINE_DIFF_PRODUCT_LIMIT = 1_000_000


def normalize_diff_lines(value: str) -> list[str]:
    return value.replace("\r\n", "\n").split("\n")


def build_line_diff_operations(
    before_lines: list[str], after_lines: list[str]
) -> list[dict[str, str]]:
    if len(before_lines) * len(after_lines) <= LINE_DIFF_PRODUCT_LIMIT:
        return _build_lcs_operations(before_lines, after_lines)

    prefix_length = 0
    while (
        prefix_length < len(before_lines)
        and prefix_length < len(after_lines)
        and before_lines[prefix_length] == after_lines[prefix_length]
    ):
        prefix_length += 1

    suffix_length = 0
    while (
        suffix_length < len(before_lines) - prefix_length
        and suffix_length < len(after_lines) - prefix_length
        and before_lines[len(before_lines) - 1 - suffix_length]
        == after_lines[len(after_lines) - 1 - suffix_length]
    ):
        suffix_length += 1

    before_end = len(before_lines) - suffix_length
    after_end = len(after_lines) - suffix_length
    before_middle = before_lines[prefix_length:before_end]
    after_middle = after_lines[prefix_length:after_end]
    if len(before_middle) * len(after_middle) <= LINE_DIFF_PRODUCT_LIMIT:
        middle = _build_lcs_operations(before_middle, after_middle)
    else:
        middle = [
            *({"type": "remove", "line": line} for line in before_middle),
            *({"type": "add", "line": line} for line in after_middle),
        ]
    return [
        *({"type": "equal", "line": line} for line in before_lines[:prefix_length]),
        *middle,
        *({"type": "equal", "line": line} for line in before_lines[before_end:]),
    ]


def build_diff_context_indexes(
    operations: list[dict[str, str]], context_size: int
) -> set[int]:
    indexes: set[int] = set()
    for index, operation in enumerate(operations):
        if operation["type"] == "equal":
            continue
        for cursor in range(
            max(0, index - context_size),
            min(len(operations) - 1, index + context_size) + 1,
        ):
            indexes.add(cursor)
    return indexes


def build_edit_diff_document(args: dict[str, Any]) -> dict[str, Any]:
    before_text = args["beforeText"]
    after_text = args["afterText"]
    if before_text == after_text:
        return _no_changes_document()

    before_lines = normalize_diff_lines(before_text)
    after_lines = normalize_diff_lines(after_text)
    before_starts = _build_line_starts(before_text)
    after_starts = _build_line_starts(after_text)
    offsets = _find_match_offsets(before_text, args["oldString"], bool(args.get("replaceAll")))
    cumulative_delta = 0
    matches: list[dict[str, int]] = []
    for before_offset in offsets:
        after_offset = before_offset + cumulative_delta
        matches.append(
            {
                "beforeStartLine": _line_index_at(before_starts, before_offset),
                "beforeEndLine": _line_index_at(
                    before_starts,
                    before_offset + max(len(args["oldString"]) - 1, 0),
                ),
                "afterStartLine": _line_index_at(after_starts, after_offset),
                "afterEndLine": _line_index_at(
                    after_starts,
                    after_offset + max(len(args["newString"]) - 1, 0),
                ),
            }
        )
        cumulative_delta += len(args["newString"]) - len(args["oldString"])

    windows = _merge_diff_windows(
        [
            {
                "beforeStart": max(0, match["beforeStartLine"] - int(_knob("tools.diff_context_lines"))),
                "beforeEnd": min(
                    len(before_lines) - 1,
                    match["beforeEndLine"] + int(_knob("tools.diff_context_lines")),
                ),
                "afterStart": max(0, match["afterStartLine"] - int(_knob("tools.diff_context_lines"))),
                "afterEnd": min(
                    len(after_lines) - 1,
                    match["afterEndLine"] + int(_knob("tools.diff_context_lines")),
                ),
            }
            for match in matches
        ]
    )
    number_width = max(3, len(str(max(len(before_lines), len(after_lines)))))
    rendered: list[str] = []
    added_lines = 0
    removed_lines = 0
    for window in windows:
        operations = build_line_diff_operations(
            before_lines[window["beforeStart"] : window["beforeEnd"] + 1],
            after_lines[window["afterStart"] : window["afterEnd"] + 1],
        )
        added_lines += sum(operation["type"] == "add" for operation in operations)
        removed_lines += sum(operation["type"] == "remove" for operation in operations)
        if rendered:
            rendered.append("...")
        rendered.extend(
            _render_all_operations(
                operations,
                window["beforeStart"],
                window["afterStart"],
                number_width,
            )
        )
    return {
        "diff": "\n".join(rendered) or "(no changes)",
        "addedLines": added_lines,
        "removedLines": removed_lines,
        "strategy": "exact_edit_hunks",
    }


def build_write_diff_document(before_text: str, after_text: str) -> dict[str, Any]:
    if before_text == after_text:
        return _no_changes_document()
    before_lines = normalize_diff_lines(before_text)
    after_lines = normalize_diff_lines(after_text)
    changed_region = len(before_lines) * len(after_lines) > LINE_DIFF_PRODUCT_LIMIT
    operations = build_line_diff_operations(before_lines, after_lines)
    return {
        "diff": _render_operations_with_context(
            operations, len(before_lines), len(after_lines)
        ),
        "addedLines": sum(operation["type"] == "add" for operation in operations),
        "removedLines": sum(
            operation["type"] == "remove" for operation in operations
        ),
        "strategy": "changed_region_write_diff"
        if changed_region
        else "exact_write_diff",
    }


def build_create_diff_document(content: str) -> dict[str, Any]:
    lines = normalize_diff_lines(content)
    operations = [{"type": "add", "line": line} for line in lines]
    return {
        "diff": _render_operations_with_context(operations, 0, len(lines)),
        "addedLines": len(lines),
        "removedLines": 0,
        "strategy": "create",
    }


def build_unified_diff(_file_path: str, before_text: str, after_text: str) -> str:
    return build_write_diff_document(before_text, after_text)["diff"]


def _no_changes_document() -> dict[str, Any]:
    return {
        "diff": "(no changes)",
        "addedLines": 0,
        "removedLines": 0,
        "strategy": "no_changes",
    }


def _build_lcs_operations(
    before_lines: list[str], after_lines: list[str]
) -> list[dict[str, str]]:
    rows = len(before_lines)
    columns = len(after_lines)
    dp = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        current = dp[row]
        next_row = dp[row + 1]
        for column in range(columns - 1, -1, -1):
            current[column] = (
                next_row[column + 1] + 1
                if before_lines[row] == after_lines[column]
                else max(next_row[column], current[column + 1])
            )

    operations: list[dict[str, str]] = []
    row = 0
    column = 0
    while row < rows and column < columns:
        if before_lines[row] == after_lines[column]:
            operations.append({"type": "equal", "line": before_lines[row]})
            row += 1
            column += 1
        elif dp[row + 1][column] >= dp[row][column + 1]:
            operations.append({"type": "remove", "line": before_lines[row]})
            row += 1
        else:
            operations.append({"type": "add", "line": after_lines[column]})
            column += 1
    operations.extend(
        {"type": "remove", "line": line} for line in before_lines[row:]
    )
    operations.extend({"type": "add", "line": line} for line in after_lines[column:])
    return operations


def _render_operations_with_context(
    operations: list[dict[str, str]], before_count: int, after_count: int
) -> str:
    keep = build_diff_context_indexes(operations, int(_knob("tools.diff_context_lines")))
    if not keep:
        return "(no changes)"
    width = max(3, len(str(max(before_count, after_count))))
    rendered: list[str] = []
    before_line = 0
    after_line = 0
    previous_kept = -1
    for index, operation in enumerate(operations):
        if operation["type"] == "equal":
            before_line += 1
            after_line += 1
        elif operation["type"] == "remove":
            before_line += 1
        else:
            after_line += 1
        if index not in keep:
            continue
        if previous_kept != -1 and index > previous_kept + 1:
            rendered.append("...")
        line_number = before_line if operation["type"] == "remove" else after_line
        rendered.append(_render_diff_line(operation, line_number, width))
        previous_kept = index
    return "\n".join(rendered)


def _render_all_operations(
    operations: list[dict[str, str]],
    before_start: int,
    after_start: int,
    width: int,
) -> list[str]:
    rendered: list[str] = []
    before_line = before_start
    after_line = after_start
    for operation in operations:
        if operation["type"] == "equal":
            before_line += 1
            after_line += 1
        elif operation["type"] == "remove":
            before_line += 1
        else:
            after_line += 1
        line_number = before_line if operation["type"] == "remove" else after_line
        rendered.append(_render_diff_line(operation, line_number, width))
    return rendered


def _render_diff_line(
    operation: dict[str, str], line_number: int, width: int
) -> str:
    marker = "+" if operation["type"] == "add" else "-" if operation["type"] == "remove" else " "
    return f"{str(line_number).rjust(width)} {marker} {operation['line']}"


def _find_match_offsets(text: str, old_string: str, replace_all: bool) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    while cursor <= len(text) - len(old_string):
        match = text.find(old_string, cursor)
        if match < 0:
            break
        offsets.append(match)
        if not replace_all:
            break
        cursor = match + len(old_string)
    return offsets


def _build_line_starts(text: str) -> list[int]:
    return [0, *(index + 1 for index, character in enumerate(text) if character == "\n")]


def _line_index_at(line_starts: list[int], offset: int) -> int:
    low = 0
    high = len(line_starts) - 1
    while low <= high:
        middle = (low + high) // 2
        if line_starts[middle] <= offset:
            low = middle + 1
        else:
            high = middle - 1
    return max(0, high)


def _merge_diff_windows(windows: list[dict[str, int]]) -> list[dict[str, int]]:
    merged: list[dict[str, int]] = []
    for window in windows:
        previous = merged[-1] if merged else None
        if previous and (
            window["beforeStart"] <= previous["beforeEnd"] + 1
            or window["afterStart"] <= previous["afterEnd"] + 1
        ):
            previous["beforeEnd"] = max(previous["beforeEnd"], window["beforeEnd"])
            previous["afterEnd"] = max(previous["afterEnd"], window["afterEnd"])
        else:
            merged.append(dict(window))
    return merged
