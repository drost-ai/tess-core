from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Callable

from .package32_canary_evidence import Package32CanaryError


async def _loadAnthropicCredentialFromRuntimeHome(
    runtime_home: str,
) -> dict[str, str | None]:
    import agent_core as core

    selected_home = os.path.abspath(os.path.expanduser(runtime_home))
    async with core.scopedRuntimeHome(selected_home):
        auth = await core.loadAuth()
        if not isinstance(auth, dict):
            raise Package32CanaryError(
                "credential_required",
                "The credential home does not contain provider authentication.",
            )
        legacy = auth.get("apiKey")
        auth_token = (
            auth.get("anthropicAuthToken")
            or auth.get("anthropicSetupToken")
            or auth.get("authToken")
            or (legacy if auth.get("provider") == "claudeai" else None)
        )
        api_key = auth.get("anthropicApiKey") or (
            legacy if auth.get("provider") != "claudeai" else None
        )
        resolved = await core.resolveProviderConfigSecrets(
            {
                "apiKey": api_key,
                "authToken": auth_token,
                "customHeaders": {},
            }
        )
    return {
        "apiKey": resolved.get("apiKey"),
        "authToken": resolved.get("authToken"),
    }


def runPackage32CanaryCli(
    run_canary: Callable[[dict[str, Any]], dict[str, Any]],
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_core_harness.package32_canary"
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--credential-home")
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--max-cost-usd", required=True, type=float)
    parser.add_argument("--authorize-paid-provider-calls", action="store_true")
    args = parser.parse_args(argv)
    try:
        credentials: dict[str, str | None] = {
            "apiKey": os.environ.get("ANTHROPIC_API_KEY"),
            "authToken": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
        }
        if (
            args.authorize_paid_provider_calls
            and not credentials["apiKey"]
            and not credentials["authToken"]
            and args.credential_home
        ):
            credentials = asyncio.run(
                _loadAnthropicCredentialFromRuntimeHome(args.credential_home)
            )
        evidence = run_canary(
            {
                "authorized": args.authorize_paid_provider_calls,
                "model": args.model,
                "runtimeHome": args.runtime_home,
                "evidenceFile": args.evidence_file,
                "maxCostUsd": args.max_cost_usd,
                **credentials,
            }
        )
    except Package32CanaryError as error:
        print(f"package32 canary refused: {error.code}")
        return 2
    except BaseException:
        print("package32 canary failed: internal_failure")
        return 3
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "actualCostUsd": evidence["actualCostUsd"],
            }
        )
    )
    return 0
