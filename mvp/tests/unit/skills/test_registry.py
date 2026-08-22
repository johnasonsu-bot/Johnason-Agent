import json
from pathlib import Path

import pytest

from workbench.skills.registry import DuplicateSkillError, SkillRegistry


def _write_skill(root: Path, name: str = "sql-inspector", version: str = "1.2.0") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("# SQL Inspector\n\nInspect safely.\n")
    (directory / "skill.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "permissions": ["data_platform.read"],
                "compatibility": {"workbench": ">=0.1"},
            }
        )
    )
    return directory


def test_registry_discovers_and_pins_a_versioned_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    registry = SkillRegistry.discover([tmp_path])

    pin = registry.pin("sql-inspector", "1.2.0")
    manifest = registry.resolve(pin)

    assert pin.digest.startswith("sha256:")
    assert manifest.input_schema["type"] == "object"
    assert manifest.permissions == ["data_platform.read"]


def test_registry_rejects_duplicate_name_and_version(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first)
    _write_skill(second)

    with pytest.raises(DuplicateSkillError, match="sql-inspector@1.2.0"):
        SkillRegistry.discover([first, second])
