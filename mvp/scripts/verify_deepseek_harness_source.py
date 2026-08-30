#!/usr/bin/env python3
"""Verify or regenerate the canonical DeepSeek Harness source manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from workbench.runtime.deepseek_harness.source_gate import (
    DeepSeekSourceVerifier,
    SourceReadinessError,
    canonical_manifest_bytes,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--write-manifest", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    root = arguments.repository_root.resolve()
    manifest = arguments.manifest or (
        root
        / "mvp/src/workbench/runtime/deepseek_harness/source_manifest.json"
    )
    verifier = DeepSeekSourceVerifier()
    try:
        if arguments.write_manifest:
            manifest.write_bytes(canonical_manifest_bytes(verifier.build_manifest(root)))
        verdict = verifier.verify(root, manifest)
    except SourceReadinessError as error:
        print(
            json.dumps(
                {
                    "decision": "BLOCKED_DSH_SOURCE_NOT_READY",
                    "scope": "source_build_provenance_only",
                    "error": str(error),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(verdict, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
