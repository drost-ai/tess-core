"""File search without ripgrep.

``Glob`` and ``Grep`` run ripgrep when it is installed. When it is not, they run
this file instead, as a separate process with the same arguments, and it prints
what ripgrep would: the tools' streaming, paging and artifact handling do not
know the difference. It is a script with no imports from the package, so it
runs under ``python -I``.

It understands the subset of ripgrep's command line those two tools use, and
differs from ripgrep in three ways: it is slower, it does not read
``.gitignore`` files, and patterns are Python regular expressions.
"""
from __future__ import annotations

import os
import re
import sys

BINARY_PROBE_BYTES = 8192
TYPES = {
    "py": ("*.py", "*.pyi"), "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"), "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "json": ("*.json",), "yaml": ("*.yaml", "*.yml"), "toml": ("*.toml",), "md": ("*.md", "*.markdown"),
    "markdown": ("*.md", "*.markdown"), "html": ("*.html", "*.htm"), "css": ("*.css", "*.scss"),
    "rust": ("*.rs",), "go": ("*.go",), "java": ("*.java",), "c": ("*.c", "*.h"),
    "cpp": ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.h"), "sh": ("*.sh", "*.bash", "*.zsh"),
    "ruby": ("*.rb",), "php": ("*.php",), "swift": ("*.swift",), "kotlin": ("*.kt", "*.kts"),
    "sql": ("*.sql",), "txt": ("*.txt",), "xml": ("*.xml",), "lua": ("*.lua",),
}


class UsageError(Exception):
    pass


def glob_to_regex(pattern: str, exact: bool = False) -> re.Pattern[str]:
    """A gitignore-style glob as a regular expression over a relative path.

    A pattern without a slash matches a file name at any depth; one with a
    slash is anchored at the search root. ``**`` crosses directories."""
    anchored = "/" in pattern.strip("/")
    pattern = pattern.lstrip("/")
    out, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                index += 2
                if pattern[index:index + 1] == "/":
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = pattern[index + 1:end]
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                index = end
        elif char == "{":
            end = pattern.find("}", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                out.append("(?:" + "|".join(glob_to_regex_body(part) for part in pattern[index + 1:end].split(",")) + ")")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    body = "".join(out)
    return re.compile(("" if anchored else "(?:.*/)?") + body + ("$" if exact else "(?:/.*)?$"))


def glob_to_regex_body(pattern: str) -> str:
    return glob_to_regex(pattern, exact=True).pattern.removeprefix("(?:.*/)?").removesuffix("$")


def parse(argv: list[str]) -> dict:
    options = {
        "files": False, "mode": "lines", "globs": [], "types": [], "ignore_case": False,
        "before": 0, "after": 0, "multiline": False, "positional": [],
    }
    index = 0
    while index < len(argv):
        arg = argv[index]

        def value() -> str:
            nonlocal index
            index += 1
            if index >= len(argv):
                raise UsageError(f"option {arg} needs a value")
            return argv[index]

        if arg == "--files":
            options["files"] = True
        elif arg in ("--color", "--sort"):
            value()
        elif arg in ("-g", "--glob"):
            options["globs"].append(value())
        elif arg == "--type":
            options["types"].append(value())
        elif arg == "-l":
            options["mode"] = "files"
        elif arg == "-c":
            options["mode"] = "count"
        elif arg == "-n":
            options["mode"] = "lines"
        elif arg == "-i":
            options["ignore_case"] = True
        elif arg == "-C":
            options["before"] = options["after"] = int(float(value()))
        elif arg == "-A":
            options["after"] = int(float(value()))
        elif arg == "-B":
            options["before"] = int(float(value()))
        elif arg == "-U":
            options["multiline"] = True
        elif arg == "--multiline-dotall":
            pass
        elif arg == "--":
            options["positional"].extend(argv[index + 1:])
            break
        elif arg.startswith("-") and len(arg) > 1:
            raise UsageError(f"unsupported option {arg}")
        else:
            options["positional"].append(arg)
        index += 1
    return options


def candidates(root: str, options: dict):
    """Files under ``root`` that the globs and types select, sorted by path.

    As in ripgrep, the last glob that matches a path decides, a path no glob
    matches is kept unless some glob is positive, and a hidden file or folder
    is visited only when a positive glob names it."""
    globs = [(not glob.startswith("!"), glob_to_regex(glob.lstrip("!")), glob_to_regex(glob.lstrip("!"), exact=True)) for glob in options["globs"]]
    any_positive = any(positive for positive, _, _ in globs)
    last_positive = max((index for index, (positive, _, _) in enumerate(globs) if positive), default=-1)
    # A folder excluded as ``!name/**`` after every positive glob cannot be re-included: skip it whole.
    pruned = {
        glob[1:-3]
        for index, glob in enumerate(options["globs"])
        if index > last_positive and glob.startswith("!") and glob.endswith("/**") and "/" not in glob[1:-3]
    }
    type_patterns = []
    for name in options["types"]:
        if name not in TYPES:
            raise UsageError(f"unrecognized file type: {name}")
        type_patterns.extend(glob_to_regex(glob) for glob in TYPES[name])

    def selected(relative: str) -> bool:
        verdict = None
        for positive, pattern, _ in globs:
            if pattern.match(relative):
                verdict = positive
        return (not any_positive) if verdict is None else verdict

    def named(relative: str) -> bool:
        return any(positive and exact.match(relative) for positive, _, exact in globs)

    if os.path.isfile(root):
        yield root
        return
    found = []
    for directory, folders, files in os.walk(root):
        inside = os.path.relpath(directory, root).replace(os.sep, "/")
        prefix = "" if inside == "." else inside + "/"
        folders[:] = sorted(
            name for name in folders
            if not (prefix == "" and name in pruned) and (not name.startswith(".") or named(prefix + name))
        )
        for name in files:
            relative = prefix + name
            if name.startswith(".") and not named(relative):
                continue
            if not selected(relative):
                continue
            if type_patterns and not any(pattern.match(relative) for pattern in type_patterns):
                continue
            found.append(os.path.join(directory, name))
    yield from sorted(found)


def read_text(path: str) -> str | None:
    """The file as text, or None for a binary or unreadable file."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if b"\0" in data[:BINARY_PROBE_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def binary_match_offset(path: str, pattern: re.Pattern[str]) -> int | None:
    """For a binary file named outright: where its first NUL byte is, if the pattern occurs in it."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return data.find(b"\0") if pattern.search(data.decode("utf-8", errors="replace")) else None


def matching_lines(text: str, pattern: re.Pattern[str], multiline: bool) -> tuple[list[str], set[int]]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not multiline:
        return lines, {number for number, line in enumerate(lines) if pattern.search(line)}
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1
    matched: set[int] = set()
    for match in pattern.finditer(text):
        first = max(0, _line_of(starts, match.start()))
        last = max(first, _line_of(starts, max(match.start(), match.end() - 1)))
        matched.update(range(first, min(last, len(lines) - 1) + 1))
    return lines, matched


def _line_of(starts: list[int], position: int) -> int:
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= position:
            low = middle
        else:
            high = middle - 1
    return low


def grep(options: dict, out) -> int:
    if len(options["positional"]) != 2:
        raise UsageError("expected a pattern and a path")
    expression, root = options["positional"]
    flags = (re.IGNORECASE if options["ignore_case"] else 0) | ((re.DOTALL | re.MULTILINE) if options["multiline"] else 0)
    try:
        pattern = re.compile(expression, flags)
    except re.error as error:
        raise UsageError(f"regex parse error: {error}") from None
    single = os.path.isfile(root)
    context = options["before"] or options["after"]
    any_match, first_group = False, True
    for path in candidates(root, options):
        text = read_text(path)
        if text is None:
            offset = binary_match_offset(path, pattern) if single else None
            if offset is not None:
                out.write(f'binary file matches (found "\\0" byte around offset {offset})\n')
                any_match = True
            continue
        lines, matched = matching_lines(text, pattern, options["multiline"])
        if not matched:
            continue
        any_match = True
        prefix = "" if single else path
        if options["mode"] == "files":
            out.write(path + "\n")
            continue
        if options["mode"] == "count":
            out.write((f"{len(matched)}\n") if single else f"{path}:{len(matched)}\n")
            continue
        shown = sorted({
            number
            for hit in matched
            for number in range(max(0, hit - options["before"]), min(len(lines) - 1, hit + options["after"]) + 1)
        })
        previous = None
        for number in shown:
            if context and (previous is None or number != previous + 1) and not first_group:
                out.write("--\n")
            first_group = False
            separator = ":" if number in matched else "-"
            out.write((f"{prefix}{separator}" if prefix else "") + f"{number + 1}{separator}{lines[number]}\n")
            previous = number
    return 0 if any_match else 1


def list_files(options: dict, out) -> int:
    if len(options["positional"]) != 1:
        raise UsageError("expected one path")
    any_file = False
    for path in candidates(options["positional"][0], options):
        any_file = True
        out.write(path + "\n")
    return 0 if any_file else 1


def main(argv: list[str]) -> int:
    try:
        options = parse(argv)
        return list_files(options, sys.stdout) if options["files"] else grep(options, sys.stdout)
    except UsageError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
