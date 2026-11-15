from __future__ import annotations

from tests.support.tess_core_case import *
from tess_core.authority.permission_rules import matches_bash_rule_pattern
from tess_core.tools.permissions import _matches_command_policy


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
                result = core.classify_read_only_shell_command(command)
                self.assertTrue(result["safe"])
                self.assertEqual(result["revision"], core.SHELL_COMMAND_CLASSIFIER_REVISION)
        for command in unsafe:
            with self.subTest(command=command):
                self.assertFalse(core.classify_read_only_shell_command(command)["safe"])

    def test_quoted_metacharacters_are_literal_but_expansion_is_rejected(self):
        self.assertEqual(
            core.parse_single_shell_command("cat 'semi;colon' \"dollar-free\" escaped\\ space"),
            {"ok": True, "argv": ["cat", "semi;colon", "dollar-free", "escaped space"]},
        )
        self.assertFalse(core.parse_single_shell_command('cat "$HOME"')["ok"])
        self.assertTrue(core.parse_single_shell_command("cat foo\\;bar")["ok"])

    def test_allow_prefixes_and_auto_mode_reject_composed_programs(self):
        self.assertTrue(matches_bash_rule_pattern("git status:*", "git status --short"))
        self.assertFalse(matches_bash_rule_pattern("git status:*", "git status; touch pwned"))
        self.assertTrue(_matches_command_policy("git status --short", ["git status"]))
        self.assertFalse(_matches_command_policy("git status; touch pwned", ["git status"]))

    def test_deny_matching_pierces_env_prefixes_paths_and_composition(self):
        def deny(pattern: str, command: str) -> bool:
            return matches_bash_rule_pattern(pattern, command, requireSingleCommand=False)

        self.assertTrue(deny("rm:*", "rm -rf /"))
        self.assertTrue(deny("rm:*", "FOO=1 rm -rf /"))
        self.assertTrue(deny("rm:*", "/bin/rm -rf /"))
        self.assertTrue(deny("rm:*", "./bin/rm -rf /"))
        self.assertTrue(deny("rm:*", "echo x && rm -rf /"))
        self.assertTrue(deny("rm:*", "echo x; rm -rf /"))
        self.assertTrue(deny("rm:*", "true | rm -rf /"))
        self.assertTrue(deny("rm:*", 'echo "$(rm -rf /)"'))
        self.assertTrue(deny("rm:*", "echo `rm -rf /`"))
        self.assertTrue(deny("rm:*", "FOO=1 BAR=2 /usr/bin/rm -rf / && echo done"))
        self.assertTrue(deny("git push:*", "FOO=bar git push origin main"))
        self.assertTrue(deny("git push", "git   PUSH"))
        self.assertFalse(deny("rm:*", "echo rm"))
        self.assertFalse(deny("rm:*", "firm -rf /"))
        self.assertFalse(deny("git push:*", "git pull origin main"))
