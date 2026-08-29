#!/usr/bin/env python3
"""Sign one Python Term gate payload with an Ed25519 secret read only from stdin."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sign a gate payload; the base64 Ed25519 secret is read from stdin."
    )
    parser.add_argument("payload", type=Path)
    parser.add_argument("proof", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    secret_line = sys.stdin.readline(4097)
    if not secret_line or len(secret_line) > 4096:
        print("signing secret input is invalid", file=sys.stderr)
        return 2
    try:
        private_bytes = base64.b64decode(secret_line.strip(), validate=True)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        payload = json.loads(arguments.payload.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, binascii.Error, ValueError):
        print("signing input is invalid", file=sys.stderr)
        return 2
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    envelope = {
        "key_id": "ed25519:" + hashlib.sha256(public_key).hexdigest()[:32],
        "payload": payload,
        "signature": base64.b64encode(private_key.sign(encoded)).decode("ascii"),
    }
    try:
        arguments.proof.write_text(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError:
        print("signed proof output failed", file=sys.stderr)
        return 3
    print(json.dumps({"key_id": envelope["key_id"], "status": "SIGNED"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
