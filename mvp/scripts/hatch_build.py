"""Hatch hook that refreshes Python Term evidence before wheel assembly."""

from __future__ import annotations

from pathlib import Path
import runpy

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version, build_data
        script = Path(__file__).with_name("build_python_term_gate_manifest.py")
        namespace = runpy.run_path(str(script))
        result = namespace["main"]()
        if result != 0:
            raise RuntimeError("Python Term build manifest generation failed")
