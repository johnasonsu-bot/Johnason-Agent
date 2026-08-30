#!/usr/bin/env python3
"""Prepare one immutable Python Term DEV_UNTRUSTED runtime directory."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from workbench.runtime.python_term.dev_environment import (
    prepare_development_environment,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_dir", type=Path)
    arguments = parser.parse_args()
    try:
        result = prepare_development_environment(arguments.runtime_dir)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
