"""Read-only Skill registry with immutable content pins."""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel


class DuplicateSkillError(ValueError):
    pass


class SkillManifest(BaseModel):
    name: str
    version: str
    input_schema: dict
    output_schema: dict
    permissions: list[str]
    compatibility: dict[str, str]
    directory: Path
    digest: str


class SkillPin(BaseModel):
    name: str
    version: str
    digest: str


class SkillRegistry:
    def __init__(self, manifests: dict[tuple[str, str], SkillManifest]) -> None:
        self._manifests = manifests

    @classmethod
    def discover(cls, roots: list[Path]) -> "SkillRegistry":
        manifests: dict[tuple[str, str], SkillManifest] = {}
        for root in roots:
            for manifest_path in sorted(root.glob("*/skill.json")):
                raw = json.loads(manifest_path.read_text())
                key = (str(raw["name"]), str(raw["version"]))
                if key in manifests:
                    raise DuplicateSkillError(f"duplicate skill {key[0]}@{key[1]}")
                directory = manifest_path.parent
                digest = _digest(raw, directory / "SKILL.md")
                manifests[key] = SkillManifest(
                    **raw,
                    directory=directory,
                    digest=digest,
                )
        return cls(manifests)

    def pin(self, skill_name: str, version: str) -> SkillPin:
        manifest = self._get(skill_name, version)
        return SkillPin(name=skill_name, version=version, digest=manifest.digest)

    def resolve(self, pin: SkillPin) -> SkillManifest:
        manifest = self._get(pin.name, pin.version)
        if manifest.digest != pin.digest:
            raise ValueError(f"skill content changed: {pin.name}@{pin.version}")
        return manifest

    def _get(self, name: str, version: str) -> SkillManifest:
        try:
            return self._manifests[(name, version)]
        except KeyError as exc:
            raise KeyError(f"unknown skill {name}@{version}") from exc


def _digest(raw: dict, instructions: Path) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    content = instructions.read_bytes() if instructions.exists() else b""
    return "sha256:" + hashlib.sha256(canonical + b"\n" + content).hexdigest()
