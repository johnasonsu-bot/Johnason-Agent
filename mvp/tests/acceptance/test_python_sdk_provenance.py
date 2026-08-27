from __future__ import annotations

import tomllib
from pathlib import Path

from workbench.runtime.python_term.sdk_adapter import PINNED_AGENTS_SDK_REVISION


MVP_ROOT = Path(__file__).resolve().parents[2]


def test_agents_sdk_dependency_and_lockfile_pin_the_approved_git_revision() -> None:
    """Following a Git branch/tag could silently change the runtime code accepted by Host v2."""
    pyproject = tomllib.loads((MVP_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependency = next(
        item for item in pyproject["project"]["dependencies"] if item.startswith("openai-agents @")
    )
    lockfile = (MVP_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert f"@{PINNED_AGENTS_SDK_REVISION}" in dependency
    assert "github.com/johnasonsu-bot/openai-agents-python.git" in dependency
    assert f"?rev={PINNED_AGENTS_SDK_REVISION}#{PINNED_AGENTS_SDK_REVISION}" in lockfile
    assert "branch=" not in lockfile
    assert "tag=" not in lockfile
    assert "git@github.com" not in lockfile
