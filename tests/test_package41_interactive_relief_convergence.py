from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, bind_test_runtime, run
from tests.test_package38_interactive_context_relief import _codex_config


def _working_context(session: dict[str, Any]) -> dict[str, Any]:
    return core.build_working_context_document_from_candidate(
        {
            "previous": None,
            "session": session,
            "generation": 1,
            "createdAt": "2026-07-27T19:08:22Z",
            "sourceRange": {
                "generation": 1,
                "start": 0,
                "end": 34,
                "messageCount": 34,
                "createdAt": "2026-07-27T19:08:22Z",
            },
            "candidate": {
                "qualityClassification": "rich",
                "qualityReason": "live-shaped fixture",
                "model": {
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "effort": "medium",
                },
                "workingContext": {
                    "project": {
                        "description": "Assistant runtime hardening",
                        "facts": {
                            "primaryLanguage": "Python",
                            "languages": ["Python"],
                            "ecosystem": "agent-core",
                            "packageManager": "uv",
                            "evidence": ["Package 150 qualification"],
                        },
                        "keyFiles": [
                            {
                                "path": "host/gateway/streaming_turn.py",
                                "role": "modified",
                                "note": "foreground execution",
                            }
                        ],
                    },
                    "goals": {
                        "active": ["Keep owner conversation responsive."],
                        "recentRequests": ["Analyze the technical report."],
                        "constraints": ["Never mutate existing sessions."],
                        "preferences": ["Preserve exact operational evidence."],
                    },
                    "currentState": {
                        "completed": ["Downloaded the report."],
                        "inProgress": ["Writing the final analysis."],
                        "blocked": [],
                    },
                    "decisions": ["Use request-only interactive relief."],
                    "discoveries": ["The full checkpoint exceeds 20K tokens."],
                    "activity": {
                        "commands": ["extract report"],
                        "searches": [],
                        "web": [],
                        "failures": [],
                    },
                    "nextSteps": ["Answer the current owner request."],
                    "criticalContext": [
                        "The current owner request has priority over background work."
                    ],
                },
            },
        }
    )


def _live_tail() -> list[dict[str, Any]]:
    read_ids = [f"read-{index}" for index in range(6)]
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": identifier,
                    "name": "Read",
                    "input": {"path": f"report-{index}.txt"},
                }
                for index, identifier in enumerate(read_ids)
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": identifier,
                    "content": "extracted report evidence " * 650,
                }
                for identifier in read_ids
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "inspect",
                    "name": "Bash",
                    "input": {"command": "inspect report"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "inspect",
                    "content": "inspection complete",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "analysis " * 5_600},
                {
                    "type": "tool_use",
                    "id": "write",
                    "name": "Write",
                    "input": {"path": "analysis.md"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "write",
                    "content": "analysis saved",
                }
            ],
        },
        {"role": "user", "content": "Continue with the report analysis."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "upload",
                    "name": "Bash",
                    "input": {"command": "upload analysis.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "upload",
                    "content": "upload complete",
                }
            ],
        },
    ]


def _assert_tool_protocol(messages: list[dict[str, Any]]) -> None:
    expected: list[str] = []
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if message.get("role") == "assistant":
            assert not expected
            expected = [
                str(block.get("id"))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            continue
        if message.get("role") != "user":
            continue
        result_ids = {
            str(block.get("tool_use_id"))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_result"
        }
        if expected:
            assert set(expected).issubset(result_ids)
            expected = []
        else:
            assert not result_ids
    assert not expected


class Package41InteractiveReliefConvergenceTests(AgentCoreTestCase):
    def test_large_checkpoint_remains_authoritative_without_projection(
        self,
    ) -> None:
        root, home, workspace, previous = self.temp_context()
        try:
            core.configure_runtime_home(str(home))
            run(core.ensure_base_layout(str(workspace)))
            config = _codex_config(home, workspace)
            system_prompt = "identity and tool guidance " * 5_200
            config["systemPrompt"] = system_prompt
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.6-sol",
                effort="xhigh",
                systemPrompt=system_prompt,
                permissionMode="bypassPermissions",
            )
            archived = [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"archived message {index}",
                }
                for index in range(34)
            ]
            session["messages"] = [*archived, *_live_tail()]
            working_context = _working_context(session)
            working_context["preservedTrace"] = [
                {
                    "sourceRange": {
                        "generation": 1,
                        "start": 0,
                        "end": 34,
                        "messageCount": 34,
                    },
                    "content": "historical trace evidence " * 4_000,
                    "entryCount": 34,
                    "includedEntryCount": 34,
                    "omittedEntryCount": 0,
                    "tokenEstimate": 25_000,
                    "tokenBudget": 25_000,
                    "truncated": False,
                }
            ]
            context_path = (
                Path(core.get_session_compactions_dir(session["id"]))
                / "live-shaped-working-context.json"
            )
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text(
                json.dumps(working_context, ensure_ascii=False),
                encoding="utf-8",
            )
            context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
            run(
                core.save_compaction_manifest(
                    {
                        "sessionId": session["id"],
                        "strategy": "checkpoint-v2",
                        "createdAt": "2026-07-27T19:08:22Z",
                        "updatedAt": "2026-07-27T19:08:22Z",
                        "latestGeneration": 1,
                        "latestCompactionId": "generation-1",
                        "compactedThroughMessageIndex": 34,
                        "archiveMessageCount": 34,
                        "artifacts": {"workingContext": str(context_path)},
                    }
                )
            )
            session["metadata"]["compactionState"] = {
                "latestCompactionId": "generation-1",
                "latestCompactionGeneration": 1,
                "latestCompactionAt": "2026-07-27T19:08:22Z",
                "compactedThroughMessageIndex": 34,
                "archiveMessageCount": 34,
                "compactedContextTokenEstimate": 25_000,
                "artifacts": {"workingContext": str(context_path)},
            }
            original_messages = copy.deepcopy(session["messages"])
            requests: list[dict[str, Any]] = []
            events: list[tuple[str, dict[str, Any]]] = []

            async def send(request: dict[str, Any]) -> dict[str, Any]:
                requests.append(copy.deepcopy(request))
                return {
                    "model": "gpt-5.6-sol",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 44_000, "output_tokens": 10},
                    "content": [{"type": "text", "text": "bounded"}],
                }

            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {
                            "emitEvent": lambda event, data: events.append(
                                (event, data)
                            )
                        },
                    ),
                    prompt="What time was the latest restart?",
                    providerOverride={"send": send},
                )
            )

            self.assertEqual(result["text"], "bounded")
            self.assertEqual(len(requests), 1)
            preflight = next(
                data
                for event, data in events
                if event == "provider.request_preflight"
            )
            self.assertNotIn("interactive_relief", preflight)
            request_text = json.dumps(requests[0], ensure_ascii=False)
            self.assertIn("What time was the latest restart?", request_text)
            self.assertIn("Keep owner conversation responsive.", request_text)
            self.assertIn("Never mutate existing sessions.", request_text)
            self.assertIn("historical trace evidence", request_text)
            self.assertNotIn(
                "checkpoint_projection_omitted_lower_priority_context=true",
                request_text,
            )
            self.assertIn("</working_context>", request_text)
            _assert_tool_protocol(requests[0]["messages"])
            self.assertEqual(session["messages"][: len(original_messages)], original_messages)
            self.assertEqual(
                hashlib.sha256(context_path.read_bytes()).hexdigest(),
                context_hash,
            )
        finally:
            self.restore_home(root, previous)
