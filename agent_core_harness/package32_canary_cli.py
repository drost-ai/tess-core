from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Callable

from .package32_canary_evidence import Package32CanaryError


async def _loadAnthropicCredentialFromRuntimeHome(
    runtime_home: str,
    provider_profile_name: str,
    model: str,
) -> dict[str, Any]:
    import agent_core as core

    selected_home = os.path.abspath(os.path.expanduser(runtime_home))
    async with core.scopedRuntimeHome(selected_home):
        config = await core.resolveConfig(
            {
                "runtimeHome": selected_home,
                "provider": provider_profile_name,
                "model": model,
                "settingSources": "user",
                "strictMcpConfig": True,
            },
            os.getcwd(),
        )
        resolved = await core.resolveProviderConfigSecrets(config)
    if resolved.get("provider") != "anthropic":
        raise Package32CanaryError(
            "provider_profile_invalid",
            "The selected credential-home profile is not Anthropic.",
        )
    return {
        "apiKey": resolved.get("apiKey"),
        "authToken": resolved.get("authToken"),
        "providerProfileName": resolved.get("providerProfileName"),
        "providerProfile": resolved.get("providerProfile"),
        "anthropicBaseUrl": resolved.get("anthropicBaseUrl"),
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
    parser.add_argument("--provider-profile")
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--max-cost-usd", required=True, type=float)
    parser.add_argument("--authorize-paid-provider-calls", action="store_true")
    args = parser.parse_args(argv)
    try:
        credentials: dict[str, Any] = {
            "apiKey": os.environ.get("ANTHROPIC_API_KEY"),
            "authToken": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
        }
        provider_profile_name = args.provider_profile or (
            "anthropic-claude-code" if credentials["authToken"] else "anthropic-console"
        )
        if (
            args.authorize_paid_provider_calls
            and not credentials["apiKey"]
            and not credentials["authToken"]
            and args.credential_home
        ):
            if not args.provider_profile:
                raise Package32CanaryError(
                    "provider_profile_required",
                    "An explicit provider profile is required with a credential home.",
                )
            credentials = asyncio.run(
                _loadAnthropicCredentialFromRuntimeHome(
                    args.credential_home,
                    args.provider_profile,
                    args.model,
                )
            )
            provider_profile_name = str(credentials["providerProfileName"])
        evidence = run_canary(
            {
                "authorized": args.authorize_paid_provider_calls,
                "model": args.model,
                "runtimeHome": args.runtime_home,
                "evidenceFile": args.evidence_file,
                "maxCostUsd": args.max_cost_usd,
                "providerProfileName": provider_profile_name,
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
