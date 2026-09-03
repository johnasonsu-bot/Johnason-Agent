#!/usr/bin/env python3
"""Run only the Goose source/build-input readiness gate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from workbench.runtime.goose.source_gate import (
    GooseSourceReadinessError,
    refresh_goose_wrapper_manifest,
    verify_goose_source_readiness,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--write-wrapper-manifest", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.write_wrapper_manifest:
            refresh_goose_wrapper_manifest(
                arguments.repo_root, manifest_path=arguments.manifest
            )
        receipt = verify_goose_source_readiness(
            arguments.repo_root, manifest_path=arguments.manifest
        )
    except GooseSourceReadinessError as error:
        print(f"BLOCKED_GOOSE_SOURCE: {error}", file=sys.stderr)
        return 1
    print(receipt.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
