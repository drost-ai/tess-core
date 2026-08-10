from __future__ import annotations

import re
from typing import Any


SHELL_COMMAND_CLASSIFIER_REVISION = 1

_SIMPLE_OPTION_POLICIES: dict[str, dict[str, Any]] = {
    "ls": {
        "shortFlags": "1AaBbCcdFfGhHiklLmnopqRrSsTtuUvxX",
        "exactLong": {
            "--all", "--almost-all", "--classify", "--directory", "--file-type",
            "--group-directories-first", "--help", "--human-readable", "--inode",
            "--literal", "--no-group", "--numeric-uid-gid", "--quote-name", "--recursive",
            "--reverse", "--size", "--version",
        },
        "valueLongPrefixes": (
            "--color=", "--format=", "--hide=", "--ignore=", "--quoting-style=",
            "--sort=", "--time=",
        ),
    },
    "cat": {
        "shortFlags": "AbEnsTuvet",
        "exactLong": {
            "--number", "--number-nonblank", "--show-all", "--show-ends",
            "--show-nonprinting", "--show-tabs", "--squeeze-blank", "--help", "--version",
        },
    },
    "wc": {
        "shortFlags": "clmwL",
        "exactLong": {
            "--bytes", "--chars", "--lines", "--max-line-length", "--words",
            "--help", "--version",
        },
    },
    "which": {
        "shortFlags": "as",
        "exactLong": {"--all", "--skip-alias", "--help", "--version"},
    },
}


def parseSingleShellCommand(command: Any) -> dict[str, Any]:
    if not isinstance(command, str):
        return {"ok": False, "reason": "invalid_type"}
    argv: list[str] = []
    word: list[str] = []
    word_started = False
    state = "plain"

    def push_word() -> None:
        nonlocal word, word_started
        if word_started:
            argv.append("".join(word))
            word = []
            word_started = False

    index = 0
    while index < len(command):
        character = command[index]
        if character in "\n\r\0":
            return {"ok": False, "reason": "control_character"}
        if state == "single":
            if character == "'":
                state = "plain"
            else:
                word.append(character)
            index += 1
            continue
        if state == "double":
            if character == '"':
                state = "plain"
            elif character in "$`":
                return {"ok": False, "reason": "expansion"}
            elif character == "\\":
                if index + 1 >= len(command):
                    return {"ok": False, "reason": "incomplete_escape"}
                index += 1
                next_character = command[index]
                word.append(next_character if next_character in '$`"\\' else f"\\{next_character}")
            else:
                word.append(character)
            index += 1
            continue

        if character.isspace():
            push_word()
        elif character == "'":
            state = "single"
            word_started = True
        elif character == '"':
            state = "double"
            word_started = True
        elif character == "\\":
            if index + 1 >= len(command):
                return {"ok": False, "reason": "incomplete_escape"}
            index += 1
            word.append(command[index])
            word_started = True
        elif character in ";&|<>`$(){}!#":
            return {"ok": False, "reason": "shell_composition"}
        else:
            word.append(character)
            word_started = True
        index += 1

    if state != "plain":
        return {"ok": False, "reason": "incomplete_quote"}
    push_word()
    if not argv:
        return {"ok": False, "reason": "empty"}
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        return {"ok": False, "reason": "assignment"}
    if "/" in argv[0]:
        return {"ok": False, "reason": "noncanonical_command"}
    return {"ok": True, "argv": argv}


_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def splitShellCommandConstituents(command: Any) -> list[list[str]]:
    """Split a possibly-compound shell command into constituent argv lists.

    Quote-aware and deliberately conservative for deny matching: every list,
    pipeline, grouping, redirection, and substitution boundary ends the
    current constituent, so each command the shell could run surfaces on its
    own. Leading env-var assignments are stripped from every constituent.
    """
    if not isinstance(command, str):
        return []
    constituents: list[list[str]] = []
    argv: list[str] = []
    word: list[str] = []
    word_started = False
    state = "plain"

    def push_word() -> None:
        nonlocal word, word_started
        if word_started:
            argv.append("".join(word))
            word = []
            word_started = False

    def push_argv() -> None:
        nonlocal argv
        push_word()
        while argv and _ENV_ASSIGNMENT_PATTERN.match(argv[0]):
            argv.pop(0)
        if argv:
            constituents.append(argv)
        argv = []

    index = 0
    while index < len(command):
        character = command[index]
        if state == "single":
            if character == "'":
                state = "plain"
            else:
                word.append(character)
        elif state == "double":
            if character == '"':
                state = "plain"
            elif character in "$`":
                # Substitution inside quotes still runs commands: surface them.
                push_argv()
                state = "plain"
            elif character == "\\" and index + 1 < len(command):
                index += 1
                word.append(command[index])
                word_started = True
            else:
                word.append(character)
                word_started = True
        elif character in "'\"":
            state = "single" if character == "'" else "double"
            word_started = True
        elif character == "\\" and index + 1 < len(command):
            index += 1
            word.append(command[index])
            word_started = True
        elif character in ";&|\n(){}`<>":
            push_argv()
        elif character == "$" and index + 1 < len(command) and command[index + 1] == "(":
            push_argv()
            index += 1
        elif character.isspace():
            push_word()
        else:
            word.append(character)
            word_started = True
        index += 1
    push_argv()
    return constituents


def _display_command(argv: list[str]) -> str:
    if argv and argv[0] == "git" and len(argv) > 1:
        return f"git {argv[1]}"
    return argv[0] if argv else "unknown"


def _unsafe(argv: list[str] | None, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "safe": False,
        "reason": reason,
        "revision": SHELL_COMMAND_CLASSIFIER_REVISION,
    }
    if argv is not None:
        result.update({"argv": argv, "parsedCommand": _display_command(argv)})
    return result


def classifyReadOnlyShellCommand(command: Any) -> dict[str, Any]:
    parsed = parseSingleShellCommand(command)
    if not parsed.get("ok"):
        return _unsafe(None, str(parsed.get("reason")))
    argv = parsed["argv"]
    name, args = argv[0], argv[1:]
    if name in {
        "sh", "bash", "zsh", "fish", "env", "command", "xargs", "npm", "pnpm",
        "bun", "npx", "python", "python3", "node", "ruby", "perl", "pytest",
        "cargo", "go",
    }:
        return _unsafe(argv, "indirect_or_executable_runner")

    safe = False
    if name == "git":
        safe = _classify_git(args)
    elif name == "pwd":
        safe = all(arg in {"-L", "-P"} for arg in args)
    elif name == "rg":
        safe = _classify_ripgrep(args)
    elif name == "find":
        safe = _classify_find(args)
    elif name in {"head", "tail"}:
        safe = _classify_head_tail(name, args)
    elif name == "date":
        safe = _classify_date(args)
    elif name in _SIMPLE_OPTION_POLICIES:
        safe = _classify_simple(args, _SIMPLE_OPTION_POLICIES[name])
    if not safe:
        return _unsafe(argv, "argv_policy_rejected" if name else "unknown_command")
    return {
        "safe": True,
        "argv": argv,
        "parsedCommand": _display_command(argv),
        "reason": "allowlisted_argv_family",
        "revision": SHELL_COMMAND_CLASSIFIER_REVISION,
    }


def _classify_simple(args: list[str], policy: dict[str, Any]) -> bool:
    positional = False
    for arg in args:
        if positional:
            continue
        if arg == "--":
            positional = True
        elif not arg.startswith("-") or arg == "-":
            continue
        elif arg.startswith("--"):
            if arg in policy["exactLong"] or any(
                arg.startswith(prefix) for prefix in policy.get("valueLongPrefixes", ())
            ):
                continue
            return False
        elif not all(flag in policy["shortFlags"] for flag in arg[1:]):
            return False
    return True


def _classify_git(args: list[str]) -> bool:
    index = 0
    while index < len(args) and args[index] in {"--no-pager", "--paginate"}:
        index += 1
    if index >= len(args):
        return False
    subcommand, rest = args[index], args[index + 1 :]
    if subcommand == "status":
        exact = {
            "-s", "--short", "-b", "--branch", "--show-stash", "--ahead-behind",
            "--no-ahead-behind", "--renames", "--no-renames", "--ignored", "--",
        }
        return all(
            arg in exact
            or bool(re.match(r"^--(?:porcelain|untracked-files|ignored|find-renames)=", arg))
            or not arg.startswith("-")
            for arg in rest
        )
    if subcommand == "diff":
        return _classify_git_inspection(
            rest,
            {
                "--cached", "--staged", "--stat", "--numstat", "--shortstat",
                "--dirstat", "--summary", "--patch", "-p", "--raw", "--name-only",
                "--name-status", "--check", "--quiet", "--exit-code", "--no-index",
                "--no-renames",
            },
        )
    if subcommand in {"log", "show"}:
        return _classify_git_inspection(
            rest,
            {
                "--oneline", "--stat", "--shortstat", "--name-only", "--name-status",
                "--decorate", "--no-decorate", "--graph", "--all", "--branches",
                "--tags", "--remotes", "--first-parent", "--merges", "--no-merges",
                "--reverse", "--patch", "-p", "--raw",
            },
        )
    if subcommand == "branch":
        if not rest:
            return True
        exact = {
            "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
            "--show-current", "--list", "--contains", "--no-contains", "--merged",
            "--no-merged", "--color", "--no-color", "--column", "--no-column",
        }
        list_mode = False
        for arg in rest:
            if arg == "--list":
                list_mode = True
            elif arg in exact or re.match(
                r"^--(?:format|sort|color|column|contains|no-contains|merged|no-merged)=",
                arg,
            ):
                continue
            elif not arg.startswith("-") and list_mode:
                continue
            else:
                return False
        return True
    return False


def _classify_git_inspection(args: list[str], exact_options: set[str]) -> bool:
    positional = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            positional = True
        elif positional or not arg.startswith("-") or arg == "-":
            pass
        elif arg in exact_options or re.match(r"^(?:-U\d+|-n\d+)$", arg):
            pass
        elif re.match(
            r"^--(?:unified|max-count|format|pretty|date|since|until|author|grep|diff-filter)=",
            arg,
        ):
            pass
        elif arg == "-n" and index + 1 < len(args) and args[index + 1].isdigit():
            index += 1
        else:
            return False
        index += 1
    return True


def _classify_ripgrep(args: list[str]) -> bool:
    exact = {
        "--hidden", "--no-hidden", "--files", "--files-with-matches",
        "--files-without-match", "--count", "--count-matches", "--json", "--stats",
        "--no-ignore", "--no-ignore-vcs", "--follow", "--fixed-strings", "--ignore-case",
        "--case-sensitive", "--smart-case", "--line-number", "--no-line-number",
        "--only-matching", "--invert-match", "--word-regexp", "--quiet", "--text",
        "--multiline", "--multiline-dotall", "--pcre2",
    }
    prefixes = (
        "--glob=", "--type=", "--type-not=", "--max-count=", "--max-depth=",
        "--max-filesize=", "--context=", "--before-context=", "--after-context=",
        "--encoding=", "--regexp=", "--file=", "--replace=", "--sort=", "--sortr=",
    )
    positional = False
    for arg in args:
        if arg == "--":
            positional = True
        elif not positional and arg.startswith("--") and arg not in exact and not any(
            arg.startswith(prefix) for prefix in prefixes
        ):
            return False
        elif not positional and re.match(r"^-[A-Za-z]", arg) and not re.match(r"^-[FHILNnUuvwx]+$", arg):
            return False
    return True


def _classify_find(args: list[str]) -> bool:
    unary = {
        "-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth",
        "-mtime", "-mmin", "-size", "-user", "-group", "-perm", "-newer", "-printf",
    }
    nullary = {
        "-print", "-print0", "-ls", "-empty", "-readable", "-writable", "-executable",
        "-true", "-false", "-and", "-a", "-or", "-o", "-not", "!", "(", ")",
    }
    expression_started = False
    index = 0
    while index < len(args):
        arg = args[index]
        if not expression_started and arg in {"-H", "-L", "-P"}:
            index += 1
            continue
        if not expression_started and not arg.startswith("-") and arg not in {"!", "("}:
            index += 1
            continue
        expression_started = True
        if arg in nullary:
            index += 1
            continue
        if arg in unary and index + 1 < len(args):
            index += 2
            continue
        return False
    return True


def _classify_head_tail(name: str, args: list[str]) -> bool:
    exact = {"-q", "--quiet", "--silent", "-v", "--verbose", "-z", "--zero-terminated"}
    prefixes = ("--bytes=", "--lines=")
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return True
        if not arg.startswith("-") or arg == "-":
            index += 1
            continue
        if arg in exact or any(arg.startswith(prefix) for prefix in prefixes) or re.match(r"^-[cn]\d+$", arg):
            index += 1
            continue
        if arg in {"-c", "-n"} and index + 1 < len(args):
            index += 2
            continue
        if name == "tail" and any(
            arg == option or arg.startswith(f"{option}=")
            for option in ("-f", "--follow", "-F", "--retry", "--pid")
        ):
            return False
        return False
    return True


def _classify_date(args: list[str]) -> bool:
    return all(
        arg not in {"-s", "--set"}
        and (
            arg in {"-u", "--utc", "-R", "--rfc-email"}
            or arg.startswith("+")
            or bool(re.match(r"^--(?:date|iso-8601|rfc-3339)=", arg))
            or not arg.startswith("-")
        )
        for arg in args
    )


