from __future__ import annotations

"""Strict YAML-subset parser (stdlib-only).

This is the parser for the runtime settings folder (config.yaml,
providers.yaml). It accepts a deliberately small, well-defined subset of
YAML — documented in tess_core/config/YAML_SUBSET.md — so the same files
remain readable by any standard YAML parser in other runtimes (TypeScript,
Rust), while this runtime stays dependency-free. Anything outside the
subset is rejected loudly with a line number, never silently coerced.

Accepted: block mappings, block sequences, plain/quoted scalars, comments,
literal (|) and folded (>) block scalars with optional '-' chomping,
2-space indentation, one optional leading '---'.

Rejected: anchors/aliases, tags, flow-style collections, multi-document
streams, merge keys, directives, tab indentation, duplicate keys, '+'
chomping, and any indentation step other than two spaces.
"""

import re
from typing import Any

_INDENT_STEP = 2

_PLAIN_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_MAX_NESTING_DEPTH = 64
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_NULL_TOKENS = {"null", "Null", "NULL", "~", ""}
_TRUE_TOKENS = {"true", "True", "TRUE"}
_FALSE_TOKENS = {"false", "False", "FALSE"}
_REJECTED_LEADS = {
    "&": "anchors are not supported",
    "*": "aliases are not supported",
    "!": "tags are not supported",
    "%": "directives are not supported",
    "?": "explicit key markers are not supported",
    "[": "flow-style sequences are not supported",
    "{": "flow-style mappings are not supported",
    "@": "reserved indicator '@'",
    "`": "reserved indicator '`'",
}


class YamlSubsetError(ValueError):
    """A settings file stepped outside the supported YAML subset."""

    def __init__(self, message: str, *, source: str, line: int) -> None:
        super().__init__(f"{source}:{line}: {message}")
        self.source = source
        self.line = line


class _Line:
    __slots__ = ("indent", "content", "number", "raw")

    def __init__(self, indent: int, content: str, number: int, raw: str) -> None:
        self.indent = indent
        self.content = content
        self.number = number
        self.raw = raw


def parse_yaml_subset(text: str, *, source: str = "<yaml>") -> Any:
    """Parse ``text`` and return dict/list/str/int/float/bool/None values."""

    parser = _Parser(text, source)
    return parser.parse()


class _Parser:
    def __init__(self, text: str, source: str) -> None:
        self.depth = 0
        self.source = source
        self.raw_lines = text.split("\n")
        self.lines = self._collect_lines()
        self.pos = 0

    # -- line preparation ------------------------------------------------

    def _collect_lines(self) -> list[_Line]:
        lines: list[_Line] = []
        for index, raw in enumerate(self.raw_lines, start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                self._fail("tab characters are not allowed in indentation", index)
            stripped = raw.strip()
            if not stripped:
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if stripped.startswith("#"):
                continue
            content = self._strip_trailing_comment(raw.lstrip(" "), index)
            if not content:
                continue
            lines.append(_Line(indent, content, index, raw))
        return lines

    def _strip_trailing_comment(self, content: str, number: int) -> str:
        # A quote only opens a quoted scalar at the start of a token
        # (column 0 or after a space); an apostrophe inside a word never
        # does. An unterminated quote is NOT an error here — this pass also
        # sees block-scalar body lines, so the real diagnosis happens when
        # a scalar is actually parsed.
        quote: str | None = None
        index = 0
        while index < len(content):
            char = content[index]
            if quote == "'":
                if char == "'":
                    if index + 1 < len(content) and content[index + 1] == "'":
                        index += 1
                    else:
                        quote = None
            elif quote == '"':
                if char == "\\":
                    index += 1
                elif char == '"':
                    quote = None
            elif char in {"'", '"'} and (index == 0 or content[index - 1] == " "):
                quote = char
            elif char == "#" and index > 0 and content[index - 1] in " \t":
                return content[:index].rstrip()
            index += 1
        return content.rstrip()

    # -- entry -------------------------------------------------------------

    def parse(self) -> Any:
        if self.lines and self.lines[0].content == "---" and self.lines[0].indent == 0:
            self.pos = 1
        if self.pos >= len(self.lines):
            return None
        for line in self.lines[self.pos :]:
            if line.content == "---":
                self._fail("multi-document streams are not supported", line.number)
            if line.content == "...":
                self._fail("document end markers are not supported", line.number)
        first = self.lines[self.pos]
        if first.indent != 0:
            self._fail("top-level content must start at column 0", first.number)
        value = self._parse_node(0)
        if self.pos < len(self.lines):
            trailing = self.lines[self.pos]
            self._fail("content after the top-level value", trailing.number)
        return value

    # -- structure ---------------------------------------------------------

    def _parse_node(self, indent: int) -> Any:
        line = self.lines[self.pos]
        if self.depth >= _MAX_NESTING_DEPTH:
            self._fail(
                f"nesting exceeds the supported depth of {_MAX_NESTING_DEPTH} levels",
                line.number,
            )
        self.depth += 1
        try:
            if line.content == "-" or line.content.startswith("- "):
                return self._parse_sequence(indent)
            if self._split_key(line) is not None:
                return self._parse_mapping(indent)
            self.pos += 1
            return self._parse_scalar(line.content, line.number)
        finally:
            self.depth -= 1

    def _parse_mapping(self, indent: int) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent:
                break
            if line.indent != indent:
                self._fail(
                    f"expected indentation of {indent} spaces, found {line.indent}",
                    line.number,
                )
            if line.content == "-" or line.content.startswith("- "):
                self._fail("sequence item found where a mapping key was expected", line.number)
            split = self._split_key(line)
            if split is None:
                self._fail("expected a 'key: value' line", line.number)
            key, rest = split
            if key in mapping:
                self._fail(f"duplicate mapping key {key!r}", line.number)
            self.pos += 1
            mapping[key] = self._parse_value(rest, indent, line.number)
        return mapping

    def _parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent:
                break
            if line.indent != indent:
                self._fail(
                    f"expected indentation of {indent} spaces, found {line.indent}",
                    line.number,
                )
            if line.content != "-" and not line.content.startswith("- "):
                break
            self.pos += 1
            if line.content == "-":
                items.append(self._parse_nested_block(indent, line.number))
                continue
            rest = line.content[2:].strip()
            item_indent = indent + _INDENT_STEP
            inline_split = self._split_key_text(rest, line.number)
            if inline_split is not None:
                items.append(
                    self._parse_inline_sequence_mapping(
                        inline_split, item_indent, line.number
                    )
                )
                continue
            if rest.startswith(("|", ">")):
                self._fail("block scalars are not supported as bare sequence items", line.number)
            items.append(self._parse_scalar(rest, line.number))
        return items

    def _parse_inline_sequence_mapping(
        self,
        first: tuple[str, str],
        item_indent: int,
        number: int,
    ) -> dict[str, Any]:
        key, rest = first
        mapping: dict[str, Any] = {key: self._parse_value(rest, item_indent, number)}
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent != item_indent:
                break
            if line.content == "-" or line.content.startswith("- "):
                break
            split = self._split_key(line)
            if split is None:
                self._fail("expected a 'key: value' line inside the sequence item", line.number)
            next_key, next_rest = split
            if next_key in mapping:
                self._fail(f"duplicate mapping key {next_key!r}", line.number)
            self.pos += 1
            mapping[next_key] = self._parse_value(next_rest, item_indent, line.number)
        return mapping

    def _parse_value(self, rest: str, parent_indent: int, number: int) -> Any:
        if rest.startswith(("|", ">")):
            return self._parse_block_scalar(rest, parent_indent, number)
        if rest:
            return self._parse_scalar(rest, number)
        return self._parse_nested_block(parent_indent, number, allow_empty=True)

    def _parse_nested_block(
        self,
        parent_indent: int,
        number: int,
        *,
        allow_empty: bool = False,
    ) -> Any:
        child_indent = parent_indent + _INDENT_STEP
        if self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent >= child_indent:
                if line.indent != child_indent:
                    self._fail(
                        f"expected indentation of {child_indent} spaces, found {line.indent}",
                        line.number,
                    )
                return self._parse_node(child_indent)
        if allow_empty:
            return None
        self._fail("expected an indented block", number)
        return None

    # -- scalars -----------------------------------------------------------

    def _parse_block_scalar(self, header: str, parent_indent: int, number: int) -> str:
        style = header[0]
        rest = header[1:]
        chomp = ""
        if rest.startswith(("-", "+")):
            chomp, rest = rest[0], rest[1:]
        if chomp == "+":
            self._fail("'+' block chomping is not supported", number)
        if rest.strip():
            self._fail("unexpected text after the block scalar indicator", number)
        block_lines: list[str] = []
        block_indent: int | None = None
        consumed_numbers: list[int] = []
        for raw_index in range(number, len(self.raw_lines)):
            raw = self.raw_lines[raw_index]
            if not raw.strip():
                block_lines.append("")
                consumed_numbers.append(raw_index + 1)
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = indent
            if indent < block_indent:
                self._fail(
                    "block scalar lines must not be indented less than the first line",
                    raw_index + 1,
                )
            if style == ">" and indent > block_indent:
                self._fail(
                    "more-indented lines inside folded scalars are not supported",
                    raw_index + 1,
                )
            block_lines.append(raw[block_indent:])
            consumed_numbers.append(raw_index + 1)
        last_consumed = consumed_numbers[-1] if consumed_numbers else number
        while self.pos < len(self.lines) and self.lines[self.pos].number <= last_consumed:
            self.pos += 1
        while block_lines and block_lines[-1] == "":
            block_lines.pop()
        if not block_lines:
            return ""
        if style == "|":
            text = "\n".join(block_lines)
        else:
            text = self._fold(block_lines)
        return text if chomp == "-" else text + "\n"

    def _fold(self, lines: list[str]) -> str:
        parts: list[str] = []
        for line in lines:
            if line == "":
                parts.append("\n")
            elif parts and parts[-1] not in {"\n", ""}:
                parts.append(" " + line)
            elif parts and parts[-1] == "\n":
                parts.append(line)
            else:
                parts.append(line)
        return "".join(parts)

    def _parse_scalar(self, token: str, number: int) -> Any:
        if token.startswith('"'):
            return self._parse_double_quoted(token, number)
        if token.startswith("'"):
            return self._parse_single_quoted(token, number)
        # The only flow-style forms admitted: atomic empty collections.
        # Without them, an explicit empty list/map (e.g. imageMimeTypes: [])
        # would be unrepresentable, and dropping such keys changes meaning.
        if token == "[]":
            return []
        if token == "{}":
            return {}
        lead = token[0] if token else ""
        if lead in _REJECTED_LEADS:
            self._fail(_REJECTED_LEADS[lead], number)
        if token.startswith("- "):
            self._fail("nested sequences must be introduced by an indented block", number)
        if token in _NULL_TOKENS:
            return None
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        if _INT_RE.match(token):
            digits = token.lstrip("+-")
            if len(digits) > 1 and digits.startswith("0"):
                # YAML 1.1 reads 0755 as octal 493; YAML 1.2 reads a string;
                # this subset's premise is one meaning across parsers.
                self._fail(
                    f"integers with leading zeros are ambiguous across YAML parsers: {token!r}",
                    number,
                )
            return int(token)
        if _FLOAT_RE.match(token):
            return float(token)
        return token

    def _parse_double_quoted(self, token: str, number: int) -> str:
        if len(token) < 2 or not token.endswith('"'):
            self._fail("unterminated double-quoted string", number)
        body = token[1:-1]
        out: list[str] = []
        index = 0
        escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
        while index < len(body):
            char = body[index]
            if char == '"':
                self._fail("unexpected text after the closing quote", number)
            if char != "\\":
                out.append(char)
                index += 1
                continue
            index += 1
            if index >= len(body):
                self._fail("dangling escape at end of string", number)
            code = body[index]
            if code == "u":
                hex_digits = body[index + 1 : index + 5]
                if len(hex_digits) != 4:
                    self._fail("\\u escapes require four hex digits", number)
                try:
                    out.append(chr(int(hex_digits, 16)))
                except ValueError:
                    self._fail("invalid \\u escape", number)
                index += 5
                continue
            if code not in escapes:
                self._fail(f"unsupported escape sequence \\{code}", number)
            out.append(escapes[code])
            index += 1
        return "".join(out)

    def _parse_single_quoted(self, token: str, number: int) -> str:
        if len(token) < 2 or not token.endswith("'"):
            self._fail("unterminated single-quoted string", number)
        body = token[1:-1]
        if re.search(r"(?<!')'(?!')", body):
            self._fail("unexpected text after the closing quote", number)
        return body.replace("''", "'")

    # -- keys ----------------------------------------------------------------

    def _split_key(self, line: _Line) -> tuple[str, str] | None:
        return self._split_key_text(line.content, line.number)

    def _split_key_text(self, content: str, number: int) -> tuple[str, str] | None:
        if content.startswith(("'", '"')):
            quote = content[0]
            end = self._find_closing_quote(content, quote)
            if end is None:
                return None
            after = content[end + 1 :]
            if not after.startswith(":"):
                return None
            if after[1:] and not after[1:].startswith(" "):
                return None
            key = (
                self._parse_double_quoted(content[: end + 1], number)
                if quote == '"'
                else self._parse_single_quoted(content[: end + 1], number)
            )
            return key, after[1:].strip()
        colon = self._find_plain_colon(content)
        if colon is None:
            return None
        key = content[:colon].strip()
        if not key:
            return None
        if key == "<<":
            self._fail("merge keys are not supported", number)
        if not _PLAIN_KEY_RE.match(key):
            return None
        return key, content[colon + 1 :].strip()

    def _find_plain_colon(self, content: str) -> int | None:
        for index, char in enumerate(content):
            if char != ":":
                continue
            if index + 1 == len(content) or content[index + 1] == " ":
                return index
        return None

    def _find_closing_quote(self, content: str, quote: str) -> int | None:
        index = 1
        while index < len(content):
            char = content[index]
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and index + 1 < len(content) and content[index + 1] == "'":
                    index += 2
                    continue
                return index
            index += 1
        return None

    def _fail(self, message: str, line: int) -> None:
        raise YamlSubsetError(message, source=self.source, line=line)
