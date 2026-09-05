"""Protocol-only subprocess double. Never emits model evidence or Runtime GO."""
import argparse
import json
import sys
import time
from pathlib import Path
from workbench.providers.repository import ProviderRepository
from workbench.runtime.provider_grants import canonical_provider_profile_digest

parser = argparse.ArgumentParser()
parser.add_argument("--runtime")
parser.add_argument("--provider-profile-id")
parser.add_argument("--runtime-dir")
parser.add_argument("--output-dir")
parser.add_argument("--vault-password-stdin", action="store_true")
args = parser.parse_args()
password = sys.stdin.readline()
assert password.endswith("\n") and len(password) > 1
password = None
profile = ProviderRepository(Path(args.runtime_dir) / "workbench.sqlite").get(args.provider_profile_id)
evidence = {"runtime_id": args.runtime, "terminal": "completed",
    "model": "changed-model" if args.provider_profile_id == "wrong-model" else profile.model_aliases["default"],
    "provider_profile_digest": canonical_provider_profile_digest(profile)}
(Path(args.output_dir) / "runtime-live-evidence-dsh.json").write_text(json.dumps({"evidence": evidence}))
if args.provider_profile_id == "hang":
    time.sleep(60)
if args.provider_profile_id == "failed":
    print("private stderr must not be returned", file=sys.stderr)
    raise SystemExit(1)
if args.provider_profile_id == "oversized":
    print("x" * 20000)
else:
    payload = json.dumps({"status": "prepared", "runtime_ids": [args.runtime], "output_dir": args.output_dir})
    if args.provider_profile_id == "split":
        sys.stdout.write(payload[:10]); sys.stdout.flush()
        time.sleep(0.05)
        sys.stdout.write(payload[10:]); sys.stdout.flush()
    else:
        print(payload)
