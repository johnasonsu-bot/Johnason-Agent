#!/usr/bin/env python3
"""User-triggered live endpoint verification through the formal runtime chain."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from federated_runtime_dev_signer import prepare_development_environment


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one saved Provider Profile through the selected federated "
            "Runtime. Credentials remain in the Workbench Vault."
        )
    )
    parser.add_argument(
        "--runtime", required=True, choices=("python-term", "goose", "dsh")
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
    password: str | None = None
    if arguments.vault_password_stdin:
        password = sys.stdin.readline()
        if not password:
            raise ValueError("Vault password is unavailable")
        password = password.rstrip("\r\n")
    try:
        return await prepare_development_environment(
            runtime_ids=(arguments.runtime,),
            provider_profile_id=arguments.provider_profile_id,
            runtime_dir=runtime_dir,
            output_dir=arguments.output_dir.absolute(),
            vault_password=password,
        )
    finally:
        password = None


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
