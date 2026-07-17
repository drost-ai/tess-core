from __future__ import annotations

from tests.support.agent_core_case import *
from agent_core.permission_rules import matchesBashRulePattern
from agent_core.tools.permissions import _matches_command_policy


class ShellAuthorityParserTests(unittest.TestCase):
    def test_classifier_admits_only_one_structural_allowlisted_command(self):
        safe = (
            "git status",
            "git status --short -- 'file with spaces'",
            "git diff --name-only -- src",
            "git log --oneline -n 10",
            "git show --stat HEAD",
            "git branch --list 'feature/*'",
            "ls -la -- 'literal;name'",
            "pwd -P",
            "rg --hidden --fixed-strings 'needle;literal' .",
            "find . -maxdepth 2 -type f -print",
            "cat -- file.txt",
            "head -n 10 file.txt",
            "tail --lines=10 file.txt",
            "wc -l file.txt",
            "which -a git",
            "date -u +%FT%TZ",
        )
        unsafe = (
            "git status; touch pwned",
            "git status&&touch pwned",
            "git status | tee pwned",
            "git status > pwned",
            "git status 2>>pwned",
            "git status\ntouch pwned",
            "git status $(touch pwned)",
            "git status `touch pwned`",
            "git status <(touch pwned)",
            "(git status)",
            "X=1 git status",
            "env git status",
            "bash -c 'git status'",
            "node -e 'process.exit()'",
            "npm test",
            "pytest",
            "git reset --hard",
            "git branch new-branch",
            "git diff --output=pwned",
            "find . -delete",
            "rg --pre 'touch pwned' needle",
            "tail -f file",
            "date --set=tomorrow",
            "unknown --read-only-looking",
            "git status 'unterminated",
            "git status \\",
        )
        for command in safe:
            with self.subTest(command=command):
                result = core.classifyReadOnlyShellCommand(command)
                self.assertTrue(result["safe"])
                self.assertEqual(result["revision"], core.SHELL_COMMAND_CLASSIFIER_REVISION)
        for command in unsafe:
            with self.subTest(command=command):
                self.assertFalse(core.classifyReadOnlyShellCommand(command)["safe"])

    def test_quoted_metacharacters_are_literal_but_expansion_is_rejected(self):
        self.assertEqual(
            core.parseSingleShellCommand("cat 'semi;colon' \"dollar-free\" escaped\\ space"),
            {"ok": True, "argv": ["cat", "semi;colon", "dollar-free", "escaped space"]},
        )
        self.assertFalse(core.parseSingleShellCommand('cat "$HOME"')["ok"])
        self.assertTrue(core.parseSingleShellCommand("cat foo\\;bar")["ok"])

    def test_allow_prefixes_and_auto_mode_reject_composed_programs(self):
        self.assertTrue(matchesBashRulePattern("git status:*", "git status --short"))
        self.assertFalse(matchesBashRulePattern("git status:*", "git status; touch pwned"))
        self.assertTrue(_matches_command_policy("git status --short", ["git status"]))
        self.assertFalse(_matches_command_policy("git status; touch pwned", ["git status"]))
