from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from workbench.orchestration.checkpointer import graph_config, open_graph_checkpointer


class _UnapprovedCheckpointPayload(BaseModel):
    value: str

    construction_count: ClassVar[int] = 0

    def model_post_init(self, context: Any) -> None:
        type(self).construction_count += 1


def test_graph_config_uses_run_id_and_concurrency():
    assert graph_config("run-1", 3) == {
        "configurable": {"thread_id": "run-1"},
        "max_concurrency": 3,
    }


@pytest.mark.parametrize("graph_run_id", ["", "   "])
def test_graph_config_rejects_blank_run_id(graph_run_id):
    with pytest.raises(ValueError):
        graph_config(graph_run_id, 1)


@pytest.mark.parametrize("graph_run_id", [None, 1, True])
def test_graph_config_rejects_non_string_run_id(graph_run_id):
    with pytest.raises(TypeError):
        graph_config(graph_run_id, 1)


@pytest.mark.parametrize("max_concurrency", [0, -1])
def test_graph_config_rejects_concurrency_below_one(max_concurrency):
    with pytest.raises(ValueError):
        graph_config("run-1", max_concurrency)


@pytest.mark.parametrize("max_concurrency", [True, False, 1.0, 2.5, None])
def test_graph_config_rejects_non_integer_concurrency(max_concurrency):
    with pytest.raises(TypeError):
        graph_config("run-1", max_concurrency)


def test_checkpoint_rejects_unapproved_python_object(tmp_path):
    with open_graph_checkpointer(tmp_path / "graph.sqlite") as saver:
        with pytest.raises((TypeError, ValueError)):
            saver.serde.dumps_typed(object())


def test_checkpoint_deserialization_keeps_unapproved_pydantic_payload_primitive(
    tmp_path, monkeypatch
):
    with open_graph_checkpointer(tmp_path / "graph.sqlite") as saver:
        payload = _UnapprovedCheckpointPayload(value="safe")
        wire_value = saver.serde.dumps_typed(payload)

        _UnapprovedCheckpointPayload.construction_count = 0
        import_calls: list[str] = []

        def reject_import(module_name: str):
            import_calls.append(module_name)
            raise AssertionError("unapproved checkpoint module must not be imported")

        import langgraph.checkpoint.serde.jsonplus as jsonplus

        monkeypatch.setattr(jsonplus.importlib, "import_module", reject_import)

        assert saver.serde.loads_typed(wire_value) == {"value": "safe"}
        assert import_calls == []
        assert _UnapprovedCheckpointPayload.construction_count == 0
