#!/usr/bin/env python3
"""Prepare an externally signed DEV_UNTRUSTED federated runtime bundle."""

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
            "Prepare public DEV_UNTRUSTED runtime receipts outside the "
            "Workbench application process."
        )
    )
    parser.add_argument(
        "--runtime",
        action="append",
        choices=("python-term", "goose", "dsh"),
        required=True,
        dest="runtime_ids",
    )
    parser.add_argument("--provider-profile-id")
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--vault-password-stdin",
        action="store_true",
        help="unlock the local Vault with one line read from standard input",
    )
    return parser.parse_args()


async def _prepare(arguments: argparse.Namespace):
    if len(set(arguments.runtime_ids)) != len(arguments.runtime_ids):
        raise ValueError("runtime selectors must be unique")
    if not arguments.provider_profile_id:
        raise ValueError("saved provider profile is required")
    runtime_dir = arguments.runtime_dir.absolute()
    output_dir = arguments.output_dir.absolute()
    password: str | None = None
    if arguments.vault_password_stdin:
        password = sys.stdin.readline()
        if not password:
            raise ValueError("Vault password is unavailable")
        password = password.rstrip("\r\n")
    try:
        return await prepare_development_environment(
            runtime_ids=tuple(arguments.runtime_ids),
            provider_profile_id=arguments.provider_profile_id,
            runtime_dir=runtime_dir,
            output_dir=output_dir,
            vault_password=password,
        )
    finally:
        password = None


def main() -> int:
    arguments = _arguments()
    try:
        result = asyncio.run(_prepare(arguments))
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
