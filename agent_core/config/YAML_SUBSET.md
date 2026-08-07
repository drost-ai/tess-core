# Settings YAML subset

The runtime settings files (`config.yaml`, `providers.yaml`) are written in a
strict subset of YAML. Files that stay inside this subset parse identically
under this runtime's stdlib-only parser (`agent_core/config/_yaml.py`) and
under any standard YAML 1.1/1.2 parser used by sibling runtimes (TypeScript,
Rust) — standard parsers accept a superset, so cross-runtime reads are safe
as long as files are AUTHORED within the subset. This document is the
contract; keep parser and document in lockstep.

## Accepted

- **Encoding / layout**: UTF-8 text; spaces only for indentation; nesting
  advances by exactly **2 spaces** per level; top-level content at column 0.
- **One document**, with at most one optional leading `---` line.
- **Block mappings**: `key: value`, or `key:` followed by an indented block,
  or `key:` alone (value is `null`). Keys are plain
  (`[A-Za-z0-9_][A-Za-z0-9_.-]*`) or single/double-quoted. Duplicate keys
  are an error.
- **Block sequences**: `- item` lines, indented 2 spaces deeper than the key
  that introduces them. Items may be scalars, inline-starting mappings
  (`- name: x` with continuation keys aligned under `name`), or `-` alone
  followed by an indented block.
- **Scalars**:
  - double-quoted with escapes `\" \\ \n \t \r \0 \uXXXX`;
  - single-quoted with `''` for a literal quote;
  - plain scalars typed as: `null`/`Null`/`NULL`/`~`/empty → null;
    `true/false` (also `True/False/TRUE/FALSE`) → bool; integers
    (`[+-]?[0-9]+`) → int; simple floats (optional sign, decimal point
    and/or exponent) → float; anything else → string. No octal/hex/dates.
- **Block scalars**: literal `|` and folded `>`, each with optional strip
  chomping (`|-`, `>-`). Folded scalars join lines with spaces; blank lines
  become newlines. Only as mapping values, not as bare sequence items.
- **Empty collections**: the atomic tokens `[]` and `{}` as values — the
  only admitted flow-style forms, since explicit-empty is semantically
  distinct from absent and otherwise unrepresentable in block style.
- **Comments**: full-line `# ...` and trailing ` # ...` (a `#` preceded by
  whitespace, outside a quoted scalar).

## Rejected (loud error with file:line)

- Anchors (`&`), aliases (`*`), tags (`!`), directives (`%`), explicit key
  markers (`?`), merge keys (`<<:`).
- Flow-style collections (`[a, b]`, `{a: b}`).
- Multi-document streams (`---` past the first line, `...`).
- Tab characters in indentation; indentation steps other than 2;
  sequence items at the same indent as their introducing key.
- Keep chomping (`|+`, `>+`); more-indented lines inside folded scalars.

## Authoring notes for other runtimes

Emitters in sibling runtimes must produce files within this subset: 2-space
indent, block style everywhere, no anchors/aliases/tags, quote any scalar
that could be misread (e.g. version-like strings `1.0`, `yes/no/on/off`
which this subset deliberately does NOT treat as booleans but YAML 1.1
parsers might — quote them for safety across runtimes).
