#!/usr/bin/env python3
"""Generate the immutable Python Term installed-file build manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _digest_files(root: Path, paths: list[Path]) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        total += len(content)
    return digest.hexdigest(), total


def main() -> int:
    mvp_root = Path(__file__).resolve().parents[1]
    package_root = mvp_root / "src" / "workbench"
    manifest_path = package_root / "runtime" / "python_term" / "gate_manifest.json"
    package_files = sorted(
        path
        for path in package_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path != manifest_path
        and path.name != "signed_gate_proof.json"
    )
    files = [
        {
            "path": path.relative_to(package_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in package_files
    ]
    build_inputs: list[dict[str, object]] = []
    for relative in ("pyproject.toml", "uv.lock"):
        path = mvp_root / relative
        content = path.read_bytes()
        build_inputs.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    test_paths = list((mvp_root / "tests").rglob("*.py"))
    test_digest, test_size = _digest_files(mvp_root, test_paths)
    build_inputs.append(
        {
            "path": "tests/**/*.py",
            "sha256": test_digest,
            "size": test_size,
        }
    )
    for relative in (
        "scripts/build_python_term_gate_manifest.py",
        "scripts/hatch_build.py",
        "scripts/run_python_term_runtime_gate.py",
        "scripts/sign_python_term_runtime_gate.py",
    ):
        path = mvp_root / relative
        content = path.read_bytes()
        build_inputs.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    document = {
        "schema_version": 1,
        "package": "workbench",
        "files": files,
        "build_inputs": sorted(build_inputs, key=lambda item: str(item["path"])),
    }
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"generated_files={len(files)} build_inputs={len(build_inputs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
