from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from tests.support.agent_core_case import *


MODES = ("default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan")


def _seal_authority(root: str, **overrides):
    authority = {
        "version": 1,
        "mode": "default",
        "promptPolicy": "policy_ask",
        "enabledTools": ["Agent", "Read", "Write"],
        "permissionRules": [],
        "permissionRuleProjectRoot": root,
        "readableRoots": [root],
        "writableRoots": [root],
        "sandbox": {
            "required": True,
            "filesystem": "workspace-write",
            "network": False,
            "mode": "os-required",
            "allowSubprocess": True,
        },
        "executableSources": {
            "settingsSources": ["user"],
            "mcpConfig": [],
            "mcpConfigDigest": "mcp-fixture",
            "mcpConfigPolicy": {"mode": "merged"},
            "pluginDirs": [],
            "pluginDirsDigest": "plugin-fixture",
            "pluginRecords": [],
        },
        "hooks": [],
        "maySpawnChildren": True,
        "fingerprint": "",
        **overrides,
    }
    authority["fingerprint"] = core.fingerprintAgentAuthority(authority)
    return authority


class AgentAuthorityTests(AgentCoreTestCase):
    def test_permission_mode_meet_is_commutative_idempotent_and_bounded(self):
        for left in MODES:
            self.assertEqual(core.meetPermissionModes(left, left), left)
            for right in MODES:
                with self.subTest(left=left, right=right):
                    self.assertEqual(
                        core.meetPermissionModes(left, right),
                        core.meetPermissionModes(right, left),
                    )
                    prompt = "policy_ask" if left in {"default", "acceptEdits"} else "never_ask"
                    parent = _seal_authority(
                        os.path.realpath(os.sep),
                        mode=left,
                        promptPolicy=prompt,
                        sandbox={
                            "required": True,
                            "filesystem": "full",
                            "network": True,
                            "mode": "os-required",
                            "allowSubprocess": True,
                        },
                        writableRoots=[os.path.realpath(os.sep)],
                    )
                    derived = core.deriveChildAgentAuthority(
                        parent=parent,
                        requestedMode=right,
                        permissionRuleProjectRoot=os.path.realpath(os.sep),
                    )["authority"]
                    self.assertTrue(core.isNoMorePermissive(derived, parent))
                    self.assertEqual(derived["parentFingerprint"], parent["fingerprint"])
                    core.assertValidAgentAuthority(derived)

    def test_tool_escalation_and_constraint_removal_are_rejected(self):
        deny = {"effect": "deny", "tool": "Bash", "pattern": "git push:*", "scope": "user", "raw": "Bash(git push:*)", "approved": True}
        ask = {"effect": "ask", "tool": "Write", "pattern": "src/**", "scope": "project", "raw": "Write(src/**)", "approved": True}
        allow = {"effect": "allow", "tool": "Read", "pattern": "src/**", "scope": "project", "raw": "Read(src/**)", "approved": True}
        hook = {"event": "PreToolUse", "matcher": "Bash", "command": "policy-check", "timeoutMs": 1_000, "blocking": True, "scope": "user", "approved": True}
        parent = _seal_authority(
            "/workspace",
            permissionRules=[deny, ask, allow],
            hooks=[hook],
        )
        with self.assertRaises(core.AgentAuthorityError) as escalation:
            core.deriveChildAgentAuthority(
                parent=parent,
                requestedMode="bypassPermissions",
                contract={"tools": ["Read", "WebFetch"]},
                permissionRuleProjectRoot="/workspace",
            )
        self.assertEqual(escalation.exception.axis, "tools")

        child = core.deriveChildAgentAuthority(
            parent=parent,
            requestedMode="bypassPermissions",
            contract={"tools": ["Read"]},
            permissionRuleProjectRoot="/workspace",
        )["authority"]
        self.assertFalse(child["maySpawnChildren"])
        for mutation in ("deny", "ask", "hook", "network", "source"):
            candidate = copy.deepcopy(child)
            if mutation in {"deny", "ask"}:
                candidate["permissionRules"] = [
                    rule for rule in candidate["permissionRules"] if rule["effect"] != mutation
                ]
            elif mutation == "hook":
                candidate["hooks"] = []
            elif mutation == "network":
                candidate["sandbox"]["network"] = True
            else:
                candidate["executableSources"]["pluginDirs"] = ["/untrusted"]
            with self.subTest(mutation=mutation):
                self.assertFalse(core.isNoMorePermissive(candidate, parent))

    def test_canonical_roots_reject_sibling_and_symlink_escape(self):
        root, home, workspace, previous = self.temp_context()
        outside = Path(root.name) / "outside"
        outside.mkdir()
        escape = workspace / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        try:
            authority = _seal_authority(os.path.realpath(workspace))
            for candidate in (str(outside), str(escape)):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(core.AgentAuthorityError) as error:
                        run(core.assertPathWithinAgentAuthority(authority, candidate))
                    self.assertEqual(error.exception.axis, "roots")
        finally:
            self.restore_home(root, previous)
    def test_fingerprint_detects_restart_time_tampering(self):
        authority = _seal_authority("/workspace")
        core.assertValidAgentAuthority(copy.deepcopy(authority))
        restored = copy.deepcopy(authority)
        restored["enabledTools"].append("WebFetch")
        with self.assertRaises(core.AgentAuthorityError) as error:
            core.assertValidAgentAuthority(restored)
        self.assertEqual(error.exception.axis, "fingerprint")

    def test_capture_reloads_trusted_rules_hooks_and_pins_python_sandbox_shape(self):
        root, home, workspace, previous = self.temp_context()
        try:
            run(
                core.saveSettings(
                    "project",
                    str(workspace),
                    {
                        "permissions": {
                            "deny": ["Bash(git push:*)"],
                            "allow": ["Bash(git status:*)"],
                        },
                        "hooks": {
                            "PreToolUse": [
                                {"matcher": "Bash", "command": "exit 0", "blocking": True}
                            ]
                        },
                    },
                )
            )
            config = run(
                core.resolveConfig(
                    {
                        "runtimeHome": str(home),
                        "settingSources": "project",
                        "permissionMode": "default",
                        "sandbox": {
                            "mode": "os-required",
                            "backend": "none",
                            "writableRoots": [str(workspace)],
                            "allowNetwork": False,
                        },
                    },
                    str(workspace),
                )
            )
            # A caller-mutated resolved config must not forge approval.
            config["permissionRules"] = [
                {"effect": "allow", "tool": "Bash", "scope": "project", "raw": "Bash", "approved": True}
            ]
            config["hooks"] = [
                {"event": "PreToolUse", "command": "unsafe", "blocking": True, "scope": "project", "approved": True}
            ]
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            authority = run(
                core.captureAgentAuthority(
                    config=config,
                    session=session,
                    tools=[SimpleNamespace(name="Agent"), SimpleNamespace(name="Bash")],
                )
            )
            by_effect = {rule["effect"]: rule for rule in authority["permissionRules"]}
            self.assertTrue(by_effect["deny"]["approved"])
            self.assertFalse(by_effect["allow"]["approved"])
            self.assertEqual(authority["hooks"], [])
            self.assertEqual(authority["sandbox"]["mode"], "os-required")
            self.assertTrue(authority["sandbox"]["required"])
            self.assertFalse(authority["sandbox"]["network"])

            session["metadata"]["parentSessionId"] = "parent"
            session["metadata"]["effectiveAgentAuthority"] = authority
            changed_config = {**config, "sandbox": {"mode": "off", "allowNetwork": True}}
            enforced = run(
                core.enforceSessionAgentAuthority(session=session, config=changed_config)
            )
            self.assertEqual(enforced["fingerprint"], authority["fingerprint"])
            self.assertEqual(changed_config["sandbox"]["mode"], "os-required")
            self.assertFalse(changed_config["sandbox"]["allowNetwork"])
            self.assertEqual(changed_config["sandbox"]["writableRoots"], authority["writableRoots"])
        finally:
            self.restore_home(root, previous)

    def test_legacy_child_and_changed_execution_sources_fail_before_execution(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"runtimeHome": str(home)}, str(workspace)))
            session = core.createEmptySession(
                cwd=str(workspace),
                provider=config["provider"],
                providerProfileName=config["providerProfileName"],
                model=config["model"],
                effort=config["effort"],
                systemPrompt=config["systemPrompt"],
                permissionMode="default",
            )
            session["metadata"]["parentSessionId"] = "parent"
            with self.assertRaises(core.AgentAuthorityError) as legacy:
                run(core.bootstrapRuntime(session=session, config=config, daemonEnabled=False))
            self.assertEqual(legacy.exception.code, "child_authority_missing")

            marker = workspace / "executed-marker"
            plugin = workspace / "hostile-plugin"
            plugin.mkdir()
            (plugin / "plugin.json").write_text(
                json.dumps({"id": "hostile", "version": "1.0.0", "entry": "entry.py"}),
                encoding="utf-8",
            )
            (plugin / "entry.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('plugin')\nplugin={{}}\n",
                encoding="utf-8",
            )
            authority = _seal_authority(
                os.path.realpath(workspace),
                enabledTools=[],
                maySpawnChildren=False,
                sandbox={
                    "required": False,
                    "filesystem": "workspace-write",
                    "network": False,
                    "mode": "off",
                    "allowSubprocess": True,
                },
                executableSources={
                    "settingsSources": ["user"],
                    "mcpConfig": [],
                    "mcpConfigDigest": "mcp-fixture",
                    "mcpConfigPolicy": {"mode": "merged"},
                    "pluginDirs": [],
                    "pluginDirsDigest": "plugin-fixture",
                    "pluginRecords": [],
                },
                parentFingerprint="parent-fingerprint",
            )
            session["metadata"]["effectiveAgentAuthority"] = authority
            hostile = {
                **config,
                "pluginDirs": [str(plugin)],
                "mcpConfig": [
                    {
                        "name": "hostile",
                        "transport": "stdio",
                        "commandOrUrl": os.environ.get("PYTHON", "python3"),
                        "args": ["-c", f"open({str(marker)!r}, 'w').write('mcp')"],
                    }
                ],
            }
            boot = run(core.bootstrapRuntime(session=session, config=hostile, daemonEnabled=False))
            try:
                self.assertEqual(boot["plugins"]["plugins"], [])
                self.assertIsNone(boot["mcp"])
                self.assertFalse(marker.exists())
            finally:
                run(boot["close"]())
        finally:
            self.restore_home(root, previous)
