from __future__ import annotations

import unittest

from agent_core.config._yaml import YamlSubsetError, parse_yaml_subset


class YamlSubsetAcceptTests(unittest.TestCase):
    def parse(self, text: str):
        return parse_yaml_subset(text, source="test.yaml")

    def test_empty_and_comment_only_documents_return_none(self):
        self.assertIsNone(self.parse(""))
        self.assertIsNone(self.parse("\n\n"))
        self.assertIsNone(self.parse("# just a comment\n"))
        self.assertIsNone(self.parse("---\n# nothing else\n"))

    def test_flat_mapping_with_scalar_typing(self):
        result = self.parse(
            "name: samplehost\n"
            "count: 8\n"
            "ratio: 0.75\n"
            "exponent: 1e3\n"
            "negative: -12\n"
            "enabled: true\n"
            "disabled: False\n"
            "missing: null\n"
            "tilde: ~\n"
            "empty:\n"
            "version: \"1.0\"\n"
            "quoted_yes: 'yes'\n"
        )
        self.assertEqual(
            result,
            {
                "name": "samplehost",
                "count": 8,
                "ratio": 0.75,
                "exponent": 1000.0,
                "negative": -12,
                "enabled": True,
                "disabled": False,
                "missing": None,
                "tilde": None,
                "empty": None,
                "version": "1.0",
                "quoted_yes": "yes",
            },
        )

    def test_yes_no_on_off_stay_strings(self):
        result = self.parse("a: yes\nb: no\nc: on\nd: off\n")
        self.assertEqual(result, {"a": "yes", "b": "no", "c": "on", "d": "off"})

    def test_atomic_empty_collections(self):
        result = self.parse("empty_list: []\nempty_map: {}\nitems:\n  - []\n")
        self.assertEqual(result, {"empty_list": [], "empty_map": {}, "items": [[]]})

    def test_non_empty_flow_still_rejected(self):
        with self.assertRaises(YamlSubsetError):
            self.parse("a: [1]\n")
        with self.assertRaises(YamlSubsetError):
            self.parse("a: { }\n")

    def test_nested_mappings(self):
        result = self.parse(
            "providers:\n"
            "  zai:\n"
            "    kind: openai\n"
            "    baseUrl: https://api.z.ai/api/coding/paas/v4\n"
            "  local:\n"
            "    kind: anthropic\n"
            "defaults:\n"
            "  model: GLM-5.2\n"
        )
        self.assertEqual(
            result,
            {
                "providers": {
                    "zai": {"kind": "openai", "baseUrl": "https://api.z.ai/api/coding/paas/v4"},
                    "local": {"kind": "anthropic"},
                },
                "defaults": {"model": "GLM-5.2"},
            },
        )

    def test_sequences_of_scalars_and_mappings(self):
        result = self.parse(
            "tools:\n"
            "  - Bash\n"
            "  - Read\n"
            "profiles:\n"
            "  - name: a\n"
            "    kind: openai\n"
            "  - name: b\n"
            "    auth:\n"
            "      envVar: KEY\n"
            "matrix:\n"
            "  -\n"
            "    - 1\n"
            "    - 2\n"
        )
        self.assertEqual(
            result,
            {
                "tools": ["Bash", "Read"],
                "profiles": [
                    {"name": "a", "kind": "openai"},
                    {"name": "b", "auth": {"envVar": "KEY"}},
                ],
                "matrix": [[1, 2]],
            },
        )

    def test_quoted_scalars_and_escapes(self):
        result = self.parse(
            "double: \"line\\nbreak \\\"quoted\\\" \\u0041\"\n"
            "single: 'it''s fine'\n"
            "apostrophe: it's plain\n"
            "'quoted key': 1\n"
            "\"double key\": 2\n"
        )
        self.assertEqual(
            result,
            {
                "double": 'line\nbreak "quoted" A',
                "single": "it's fine",
                "apostrophe": "it's plain",
                "quoted key": 1,
                "double key": 2,
            },
        )

    def test_comments_full_line_and_trailing(self):
        result = self.parse(
            "# header\n"
            "a: 1  # trailing\n"
            "b: \"kept # inside\"  # stripped\n"
            "c: 'kept # too' # gone\n"
        )
        self.assertEqual(result, {"a": 1, "b": "kept # inside", "c": "kept # too"})

    def test_literal_block_scalar(self):
        result = self.parse(
            "script: |\n"
            "  line one\n"
            "  line two\n"
            "\n"
            "  after blank\n"
            "next: 1\n"
        )
        self.assertEqual(
            result,
            {"script": "line one\nline two\n\nafter blank\n", "next": 1},
        )

    def test_literal_block_scalar_strip_chomping(self):
        result = self.parse("text: |-\n  alpha\n  beta\nnext: 2\n")
        self.assertEqual(result, {"text": "alpha\nbeta", "next": 2})

    def test_folded_block_scalar(self):
        result = self.parse(
            "prose: >\n"
            "  one two\n"
            "  three\n"
            "\n"
            "  new paragraph\n"
            "tail: done\n"
        )
        self.assertEqual(
            result,
            {"prose": "one two three\nnew paragraph\n", "tail": "done"},
        )

    def test_block_scalar_preserves_hash_and_quotes(self):
        result = self.parse("cmd: |\n  echo 'hello # world'\n  say \"hi\n")
        self.assertEqual(result, {"cmd": "echo 'hello # world'\nsay \"hi\n"})

    def test_leading_document_marker(self):
        self.assertEqual(self.parse("---\na: 1\n"), {"a": 1})

    def test_top_level_sequence_and_scalar(self):
        self.assertEqual(self.parse("- 1\n- two\n"), [1, "two"])
        self.assertEqual(self.parse("42\n"), 42)

    def test_deep_nesting(self):
        result = self.parse(
            "a:\n"
            "  b:\n"
            "    c:\n"
            "      - d: 1\n"
            "        e:\n"
            "          f: end\n"
        )
        self.assertEqual(result, {"a": {"b": {"c": [{"d": 1, "e": {"f": "end"}}]}}})


class YamlSubsetRejectTests(unittest.TestCase):
    def assert_rejects(self, text: str, fragment: str, line: int | None = None):
        with self.assertRaises(YamlSubsetError) as ctx:
            parse_yaml_subset(text, source="test.yaml")
        self.assertIn(fragment, str(ctx.exception))
        self.assertIn("test.yaml:", str(ctx.exception))
        if line is not None:
            self.assertEqual(ctx.exception.line, line)

    def test_rejects_anchors_aliases_tags_directives(self):
        self.assert_rejects("a: &anchor 1\n", "anchors", 1)
        self.assert_rejects("a: *alias\n", "aliases", 1)
        self.assert_rejects("a: !!int 5\n", "tags", 1)
        self.assert_rejects("%YAML 1.2\na: 1\n", "directives", 1)
        self.assert_rejects("? complex\n", "explicit key", 1)

    def test_rejects_flow_style(self):
        self.assert_rejects("a: [1, 2]\n", "flow-style sequences", 1)
        self.assert_rejects("a: {b: 1}\n", "flow-style mappings", 1)

    def test_rejects_merge_keys(self):
        self.assert_rejects("<<: base\n", "merge keys", 1)

    def test_rejects_multi_document(self):
        self.assert_rejects("a: 1\n---\nb: 2\n", "multi-document", 2)
        self.assert_rejects("a: 1\n...\n", "document end", 2)

    def test_rejects_tab_indentation(self):
        self.assert_rejects("a:\n\tb: 1\n", "tab characters", 2)

    def test_rejects_wrong_indent_step(self):
        self.assert_rejects("a:\n   b: 1\n", "expected indentation of 2", 2)
        self.assert_rejects("a:\n  b:\n     c: 1\n", "expected indentation of 4", 3)

    def test_rejects_sequence_at_key_indent(self):
        self.assert_rejects("tools:\n- Bash\n", "sequence item found", 2)

    def test_rejects_duplicate_keys(self):
        self.assert_rejects("a: 1\na: 2\n", "duplicate mapping key", 2)
        self.assert_rejects("s:\n  - k: 1\n    k: 2\n", "duplicate mapping key", 3)

    def test_rejects_keep_chomping_and_bad_fold(self):
        self.assert_rejects("t: |+\n  x\n", "'+' block chomping", 1)
        self.assert_rejects("t: >\n  a\n    deeper\n", "more-indented lines", 3)

    def test_rejects_bare_block_scalar_sequence_item(self):
        self.assert_rejects("s:\n  - |\n    x\n", "bare sequence items", 2)

    def test_rejects_unterminated_quotes(self):
        self.assert_rejects("a: \"open\n", "unterminated double-quoted", 1)
        self.assert_rejects("a: 'open\n", "unterminated single-quoted", 1)

    def test_rejects_bad_escape(self):
        self.assert_rejects("a: \"bad \\q\"\n", "unsupported escape", 1)
        self.assert_rejects("a: \"short \\u12\"\n", "four hex digits", 1)

    def test_rejects_content_after_top_level_scalar(self):
        self.assert_rejects("42\na: 1\n", "content after the top-level value", 2)

    def test_rejects_nested_sequence_on_dash_line(self):
        self.assert_rejects("s:\n  - - 1\n", "indented block", 2)

    def test_rejects_indented_top_level(self):
        self.assert_rejects("  a: 1\n", "column 0", 1)


    def test_rejects_leading_zero_integers_as_cross_parser_ambiguous(self):
        self.assert_rejects("mode: 0755\n", "leading zeros", 1)
        self.assert_rejects("mode: -0755\n", "leading zeros", 1)
        self.assertEqual(parse_yaml_subset("n: 0\nm: -12\nz: 0.5\n"), {"n": 0, "m": -12, "z": 0.5})

    def test_depth_guard_rejects_pathological_nesting_loudly(self):
        deep = "a:\n" + "\n".join("  " * (level + 1) + "k:" for level in range(200))
        with self.assertRaises(YamlSubsetError) as guard:
            parse_yaml_subset(deep, source="deep")
        self.assertIn("nesting exceeds the supported depth", str(guard.exception))
        # Sixty-three levels stay within the subset.
        fine = "a:\n" + "\n".join("  " * (level + 1) + "k:" for level in range(60))
        self.assertIsInstance(parse_yaml_subset(fine), dict)


if __name__ == "__main__":
    unittest.main()
