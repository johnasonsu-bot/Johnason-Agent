#!/usr/bin/env python3
"""Prepare an externally signed DEV_UNTRUSTED federated runtime bundle."""

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
    model_runtimes = tuple(
        item for item in arguments.runtime_ids if item in {"goose", "dsh"}
    )
    if model_runtimes:
        if not arguments.provider_profile_id:
            raise ValueError("saved provider profile is required")
        if not arguments.vault_password_stdin:
            raise ValueError("Vault password must be read from standard input")
    runtime_dir = arguments.runtime_dir.absolute()
    output_dir = arguments.output_dir.absolute()
    observations = []
    vault = VaultService(runtime_dir / "credentials.vault")
    try:
        if model_runtimes:
            password = sys.stdin.readline()
            if not password:
                raise ValueError("Vault password is unavailable")
            vault.unlock(password.rstrip("\r\n"))
            password = ""
            for runtime_id in model_runtimes:
                observations.append(
                    await collect_runtime_live_endpoint_evidence(
                        runtime_id=runtime_id,
                        provider_profile_id=arguments.provider_profile_id,
                        runtime_dir=runtime_dir,
                        vault=vault,
                    )
                )
        return prepare_development_environment(
            arguments.runtime_ids,
            output_dir,
            live_evidence=tuple(observations),
        )
    finally:
        vault.lock()


def main() -> int:
    arguments = _arguments()
    try:
        result = asyncio.run(_prepare(arguments))
    except (OSError, RuntimeError, TypeError, ValueError):
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "live_endpoint_verification_failed",
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
