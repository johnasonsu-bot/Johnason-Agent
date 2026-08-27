from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from workbench.runtime.engine_host.v2 import WorkspaceGrantV2
from workbench.runtime.python_term.contracts import canonical_digest
from workbench.runtime.python_term.state import StateBoundaryError, TermStateStore


def _grant(root: Path) -> WorkspaceGrantV2:
    canonical = str(root.resolve())
    return WorkspaceGrantV2(
        grant_id="grant-1",
        workspace_snapshot_ref="snapshot-1",
        readable_paths=(canonical,),
        writable_paths=(canonical,),
        command_policy="deny",
        network_policy="deny",
        expires_at_ms=4_102_444_800_000,
    )


def test_initializes_only_the_term_local_layout_and_canonical_metadata(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    metadata = {"step": 1, "labels": ["a", "b"]}

    ref = store.initialize("term-1", "agent-a", metadata)

    term_root = tmp_path / ".runtime" / "terms" / "term-1"
    assert sorted(path.name for path in term_root.iterdir()) == [
        "logs",
        "outputs",
        "runtime.json",
        "work",
    ]
    payload = json.loads((term_root / "runtime.json").read_text())
    assert payload == {
        "agent_id": "agent-a",
        "digest": canonical_digest(metadata),
        "metadata": metadata,
        "term_id": "term-1",
    }
    assert ref.root_ref == ".runtime/terms/term-1"
    assert ref.metadata_digest == canonical_digest(metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_token": "not-for-storage"},
        {"value": "ghp_123456789012345678901234567890123456"},
        {"object": object()},
    ],
)
def test_metadata_fails_closed_for_sensitive_or_arbitrary_values(tmp_path, metadata) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))

    with pytest.raises((StateBoundaryError, ValueError), match="sensitive|JSON"):
        store.initialize("term-1", "agent-a", metadata)

    assert not (tmp_path / ".runtime" / "terms" / "term-1").exists()


def test_reinitialization_is_idempotent_but_cannot_replace_metadata(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    first = store.initialize("term-1", "agent-a", {"value": 1})
    assert store.initialize("term-1", "agent-a", {"value": 1}) == first

    with pytest.raises(StateBoundaryError, match="metadata"):
        store.initialize("term-1", "agent-a", {"value": 2})

    payload = json.loads(
        (tmp_path / ".runtime" / "terms" / "term-1" / "runtime.json").read_text()
    )
    assert payload["metadata"] == {"value": 1}


@pytest.mark.parametrize(
    "relative",
    ["../other/file", "/tmp/file", "work/../../file", "dir/./file", "dir/."],
)
def test_rejects_traversal_and_ungrantable_absolute_paths(tmp_path, relative) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    ref = store.initialize("term-1", "agent-a", {"ok": True})

    with pytest.raises(StateBoundaryError):
        store.resolve("term-1", "agent-a", ref, "work", relative, write=True)


def test_resolves_symlinks_before_workspace_grant_check(tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    store = TermStateStore(tmp_path, _grant(tmp_path))
    ref = store.initialize("term-1", "agent-a", {"ok": True})
    os.symlink(outside, tmp_path / ".runtime" / "terms" / "term-1" / "work" / "escape")

    with pytest.raises(StateBoundaryError):
        store.resolve(
            "term-1", "agent-a", ref, "work", "escape/file.txt", write=True
        )


def test_rejects_area_root_symlink_alias_to_another_term(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    store.initialize("term-a", "agent-a", {"owner": "a"})
    ref_b = store.initialize("term-b", "agent-a", {"owner": "b"})
    term_a_work = tmp_path / ".runtime" / "terms" / "term-a" / "work"
    term_b_work = tmp_path / ".runtime" / "terms" / "term-b" / "work"
    term_b_work.rename(term_b_work.with_name("work-original"))
    os.symlink(term_a_work, term_b_work)

    with pytest.raises(StateBoundaryError, match="symlink|canonical"):
        store.resolve("term-b", "agent-a", ref_b, "work", "file.txt", write=True)


def test_controlled_open_keeps_the_authorized_inode_after_parent_replacement(
    tmp_path,
) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    store.initialize("term-a", "agent-a", {"owner": "a"})
    ref_b = store.initialize("term-b", "agent-a", {"owner": "b"})
    term_a_dir = tmp_path / ".runtime" / "terms" / "term-a" / "work" / "dir"
    term_b_dir = tmp_path / ".runtime" / "terms" / "term-b" / "work" / "dir"
    term_a_dir.mkdir()
    term_b_dir.mkdir()
    (term_a_dir / "value.txt").write_bytes(b"term-a")
    (term_b_dir / "value.txt").write_bytes(b"term-b")

    with store.open_file(
        "term-b", "agent-a", ref_b, "work", "dir/value.txt", mode="rb"
    ) as handle:
        original = term_b_dir.with_name("dir-original")
        term_b_dir.rename(original)
        os.symlink(term_a_dir, term_b_dir)
        assert handle.read() == b"term-b"


def test_controlled_open_rejects_area_replacement_after_grant_check(
    tmp_path, monkeypatch
) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    store.initialize("term-a", "agent-a", {"owner": "a"})
    ref_b = store.initialize("term-b", "agent-a", {"owner": "b"})
    term_a_work = tmp_path / ".runtime" / "terms" / "term-a" / "work"
    term_b_work = tmp_path / ".runtime" / "terms" / "term-b" / "work"
    (term_a_work / "value.txt").write_bytes(b"term-a")
    (term_b_work / "value.txt").write_bytes(b"term-b")
    original_require_granted = store._require_granted
    replaced = False

    def replace_after_check(path: Path, *, write: bool) -> None:
        nonlocal replaced
        original_require_granted(path, write=write)
        if path.name == "value.txt" and not replaced:
            replaced = True
            term_b_work.rename(term_b_work.with_name("work-original"))
            os.symlink(term_a_work, term_b_work)

    monkeypatch.setattr(store, "_require_granted", replace_after_check)
    with pytest.raises(StateBoundaryError, match="symlink|open|directory"):
        with store.open_file(
            "term-b", "agent-a", ref_b, "work", "value.txt", mode="rb"
        ):
            pass


def test_controlled_open_cannot_follow_a_link_beyond_the_workspace_grant(
    tmp_path,
) -> None:
    broad_store = TermStateStore(tmp_path, _grant(tmp_path))
    broad_store.initialize("term-a", "agent-a", {"owner": "a"})
    term_b_root = tmp_path / ".runtime" / "terms" / "term-b"
    narrow_store = TermStateStore(term_b_root.parents[2], _grant(term_b_root))
    ref_b = narrow_store.initialize("term-b", "agent-a", {"owner": "b"})
    term_a_file = tmp_path / ".runtime" / "terms" / "term-a" / "work" / "value.txt"
    term_a_file.write_bytes(b"term-a")
    os.symlink(
        term_a_file,
        term_b_root / "work" / "outside-grant.txt",
    )

    with pytest.raises(StateBoundaryError, match="Grant|symlink|open"):
        with narrow_store.open_file(
            "term-b",
            "agent-a",
            ref_b,
            "work",
            "outside-grant.txt",
            mode="rb",
        ):
            pass


def test_runtime_metadata_is_read_from_the_same_no_follow_descriptor(
    tmp_path, monkeypatch
) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    ref = store.initialize("term-1", "agent-a", {"owner": "a"})
    runtime_file = tmp_path / ".runtime" / "terms" / "term-1" / "runtime.json"
    original_fstat = os.fstat
    replaced = False

    def replace_after_open(descriptor: int):
        nonlocal replaced
        metadata = original_fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode) and not replaced:
            replaced = True
            runtime_file.rename(runtime_file.with_name("runtime-original.json"))
            runtime_file.write_text("{}", encoding="utf-8")
        return metadata

    monkeypatch.setattr(os, "fstat", replace_after_open)
    locator = store.resolve(
        "term-1", "agent-a", ref, "work", "value.txt", write=True
    )

    assert locator.name == "value.txt"
    assert replaced


def test_rejects_cross_term_reference(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    ref = store.initialize("term-1", "agent-a", {"ok": True})
    with pytest.raises(StateBoundaryError, match="Term"):
        store.resolve("term-2", "agent-a", ref, "work", "file.txt", write=True)


def test_resolve_rejects_forged_agent_and_runtime_metadata(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    ref = store.initialize("term-1", "agent-a", {"ok": True})

    with pytest.raises(StateBoundaryError, match="Agent"):
        store.resolve("term-1", "agent-b", ref, "work", "file.txt", write=True)

    runtime_file = tmp_path / ".runtime" / "terms" / "term-1" / "runtime.json"
    record = json.loads(runtime_file.read_text())
    record["metadata"]["ok"] = False
    runtime_file.write_text(json.dumps(record))
    with pytest.raises(StateBoundaryError, match="metadata|digest"):
        store.resolve("term-1", "agent-a", ref, "work", "file.txt", write=True)


def test_rejects_term_root_symlink_alias_to_another_term(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))
    original = store.initialize("term-a", "agent-a", {"ok": True})
    term_a = tmp_path / ".runtime" / "terms" / "term-a"
    term_b = tmp_path / ".runtime" / "terms" / "term-b"
    os.symlink(term_a, term_b)
    forged = original.model_copy(
        update={"term_id": "term-b", "root_ref": ".runtime/terms/term-b"}
    )

    with pytest.raises(StateBoundaryError, match="symlink|canonical"):
        store.resolve("term-b", "agent-a", forged, "work", "file.txt", write=True)


def test_concurrent_initialization_commits_one_canonical_record(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))

    def initialize(value: int):
        try:
            return store.initialize("term-1", "agent-a", {"value": value})
        except StateBoundaryError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(initialize, (1, 2)))

    references = [item for item in results if not isinstance(item, Exception)]
    conflicts = [item for item in results if isinstance(item, StateBoundaryError)]
    payload = json.loads(
        (tmp_path / ".runtime" / "terms" / "term-1" / "runtime.json").read_text()
    )
    assert len(references) == 1
    assert len(conflicts) == 1
    assert payload["metadata"] in ({"value": 1}, {"value": 2})
    assert payload["digest"] == canonical_digest(payload["metadata"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        same_results = tuple(
            executor.map(
                lambda _: store.initialize("term-same", "agent-a", {"value": 3}),
                range(2),
            )
        )
    assert same_results[0] == same_results[1]


def test_invalid_agent_is_rejected_before_state_directories_are_created(tmp_path) -> None:
    store = TermStateStore(tmp_path, _grant(tmp_path))

    with pytest.raises((StateBoundaryError, ValueError)):
        store.initialize("term-1", "bad agent id", {"ok": True})

    assert not (tmp_path / ".runtime" / "terms" / "term-1").exists()
