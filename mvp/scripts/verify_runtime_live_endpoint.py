#!/usr/bin/env python3
"""User-triggered live endpoint verification through the formal runtime chain."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

from workbench.credentials.service import VaultService
from workbench.runtime.development_admission import (
    collect_runtime_live_endpoint_evidence,
    prepare_development_environment,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one saved Provider Profile through the selected federated "
            "Runtime. Credentials remain in the Workbench Vault."
        )
    )
    parser.add_argument(
        "--runtime", required=True, choices=("goose", "dsh")
    )
    parser.add_argument("--provider-profile-id", required=True)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--vault-password-stdin",
        action="store_true",
        help="unlock the local Vault with one line read from standard input",
    )
    return parser.parse_args()


async def _verify(arguments: argparse.Namespace):
    runtime_dir = arguments.runtime_dir.absolute()
    if not arguments.vault_password_stdin:
        raise ValueError("Vault password must be read from standard input")
    vault = VaultService(runtime_dir / "credentials.vault")
    try:
        password = sys.stdin.readline()
        if not password:
            raise ValueError("Vault password is unavailable")
        vault.unlock(password.rstrip("\r\n"))
        password = ""
        evidence = await collect_runtime_live_endpoint_evidence(
            runtime_id=arguments.runtime,
            provider_profile_id=arguments.provider_profile_id,
            runtime_dir=runtime_dir,
            vault=vault,
        )
        return prepare_development_environment(
            (arguments.runtime,),
            arguments.output_dir.absolute(),
            live_evidence=(evidence,),
        )
    finally:
        vault.lock()


def main() -> int:
    arguments = _arguments()
    try:
        result = asyncio.run(_verify(arguments))
    except Exception:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "live_endpoint_verification_failed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
