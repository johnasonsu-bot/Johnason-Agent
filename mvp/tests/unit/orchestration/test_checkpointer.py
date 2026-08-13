from __future__ import annotations

import pytest

from workbench.orchestration.checkpointer import graph_config, open_graph_checkpointer


def test_graph_config_uses_run_id_and_concurrency():
    assert graph_config("run-1", 3) == {
        "configurable": {"thread_id": "run-1"},
        "max_concurrency": 3,
    }


@pytest.mark.parametrize("graph_run_id", ["", "   "])
def test_graph_config_rejects_blank_run_id(graph_run_id):
    with pytest.raises(ValueError):
        graph_config(graph_run_id, 1)


@pytest.mark.parametrize("max_concurrency", [0, -1])
def test_graph_config_rejects_concurrency_below_one(max_concurrency):
    with pytest.raises(ValueError):
        graph_config("run-1", max_concurrency)


def test_checkpoint_rejects_unapproved_python_object(tmp_path):
    with open_graph_checkpointer(tmp_path / "graph.sqlite") as saver:
        with pytest.raises((TypeError, ValueError)):
            saver.serde.dumps_typed(object())
