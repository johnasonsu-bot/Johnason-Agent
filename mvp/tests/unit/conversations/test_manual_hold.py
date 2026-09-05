"""Focused coverage for durable manual Conversation turn holds."""

from pathlib import Path
import time

import pytest

from workbench.api.conversations import ConversationAPI
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import (
    ConversationRepository,
    ManualTurnHoldError,
)
from workbench.workflow.event_store import EventStore


def _enqueue(
    repository: ConversationRepository,
    *,
    session_id: str,
    command_id: str,
) -> None:
    repository.enqueue_turn(
        session_id=session_id,
        command_id=command_id,
        run_id=f"run-{command_id}",
        provider_id="provider-1",
        model="model-1",
        prompt=f"prompt-{command_id}",
        initial_state={"phase": "before_model", "messages": [], "events": []},
    )


def test_manual_hold_survives_restart_without_mutating_turn_or_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "held-restart.sqlite"
    repository = ConversationRepository(database)
    _enqueue(repository, session_id="session-1", command_id="turn-1")
    repository.append_message(
        ConversationMessage(
            session_id="session-1",
            command_id="turn-1:user",
            role="user",
            content="preserve me",
        )
    )
    before = repository.load_turn_status("session-1", "turn-1")

    hold = repository.hold_turn(
        "session-1",
        "turn-1",
        operation_id="hold-op-1",
        reason="pause historical task",
    )

    restarted = ConversationRepository(database)
    assert restarted.recover_expired_turns(now=time.time()) == []
    after = restarted.load_turn_status("session-1", "turn-1")
    assert hold.active is True
    assert restarted.is_turn_held("session-1", "turn-1") is True
    assert restarted.claim_next_turn(owner_id="worker") is None
    assert after == before
    assert [message.content for message in restarted.list_messages("session-1")] == [
        "preserve me"
    ]


def test_manual_hold_preserves_retryable_status(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "held-retryable.sqlite")
    _enqueue(repository, session_id="session-1", command_id="turn-1")
    claimed = repository.claim_next_turn(owner_id="worker")
    assert claimed is not None
    retryable_state = dict(claimed.state)
    retryable_state["retryable"] = True
    repository.mark_retryable(
        "session-1", "turn-1", owner_id="worker", state=retryable_state
    )
    before = repository.load_turn_status("session-1", "turn-1")

    repository.hold_turn(
        "session-1",
        "turn-1",
        operation_id="hold-op-1",
        reason="pause history",
    )

    after = repository.load_turn_status("session-1", "turn-1")
    assert after == before
    assert after is not None and after.status == "retryable"
    assert repository.claim_next_turn(owner_id="other-worker") is None


def test_held_predecessor_blocks_its_session_but_not_a_new_session(
    tmp_path: Path,
) -> None:
    repository = ConversationRepository(tmp_path / "held-fifo.sqlite")
    _enqueue(repository, session_id="old-session", command_id="turn-1")
    _enqueue(repository, session_id="old-session", command_id="turn-2")
    _enqueue(repository, session_id="new-session", command_id="turn-3")
    repository.hold_turn(
        "old-session",
        "turn-1",
        operation_id="hold-op-1",
        reason="pause history",
    )

    claimed = repository.claim_next_turn(owner_id="worker")

    assert claimed is not None
    assert (claimed.session_id, claimed.command_id) == ("new-session", "turn-3")
    repository.finish_turn(
        "new-session",
        "turn-3",
        owner_id="worker",
        status="completed",
        state=claimed.state,
        result=[],
    )
    assert repository.claim_next_turn(owner_id="worker") is None


def test_direct_claim_refuses_an_existing_held_turn_with_a_stable_code(
    tmp_path: Path,
) -> None:
    repository = ConversationRepository(tmp_path / "direct-claim.sqlite")
    _enqueue(repository, session_id="session-1", command_id="turn-1")
    repository.hold_turn(
        "session-1",
        "turn-1",
        operation_id="hold-op-1",
        reason="pause history",
    )

    with pytest.raises(ManualTurnHoldError) as raised:
        repository.claim_turn(
            session_id="session-1",
            command_id="turn-1",
            run_id="run-turn-1",
            provider_id="provider-1",
            model="model-1",
            prompt="prompt-turn-1",
            owner_id="direct-worker",
            initial_state={},
        )

    assert raised.value.code == "turn_held"
    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.owner_id is None


def test_only_explicit_release_makes_a_held_turn_claimable_again(
    tmp_path: Path,
) -> None:
    database = tmp_path / "release.sqlite"
    repository = ConversationRepository(database)
    _enqueue(repository, session_id="session-1", command_id="turn-1")
    repository.hold_turn(
        "session-1",
        "turn-1",
        operation_id="hold-op-1",
        reason="pause history",
    )
    restarted = ConversationRepository(database)

    released = restarted.release_hold(
        "session-1", "turn-1", operation_id="release-op-1"
    )
    claimed = restarted.claim_next_turn(owner_id="worker")

    assert released.active is False
    assert released.release_operation_id == "release-op-1"
    assert claimed is not None
    assert (claimed.session_id, claimed.command_id) == ("session-1", "turn-1")


def test_hold_rejects_missing_running_and_terminal_turns_with_stable_codes(
    tmp_path: Path,
) -> None:
    repository = ConversationRepository(tmp_path / "hold-errors.sqlite")
    with pytest.raises(ManualTurnHoldError) as missing:
        repository.hold_turn(
            "session-1",
            "missing",
            operation_id="hold-missing",
            reason="pause history",
        )
    assert missing.value.code == "turn_not_found"

    _enqueue(repository, session_id="session-1", command_id="running")
    running = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert running is not None
    with pytest.raises(ManualTurnHoldError) as busy:
        repository.hold_turn(
            "session-1",
            "running",
            operation_id="hold-running",
            reason="pause history",
        )
    assert busy.value.code == "turn_not_holdable"

    repository.finish_turn(
        "session-1",
        "running",
        owner_id="worker",
        status="completed",
        state=running.state,
        result=[],
    )
    with pytest.raises(ManualTurnHoldError) as terminal:
        repository.hold_turn(
            "session-1",
            "running",
            operation_id="hold-terminal",
            reason="pause history",
        )
    assert terminal.value.code == "turn_not_holdable"


class _CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, _command):
        self.calls += 1
        if False:
            yield None


@pytest.mark.asyncio
async def test_process_guard_releases_an_owned_held_turn_before_runner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "process-guard.sqlite"
    repository = ConversationRepository(database)
    _enqueue(repository, session_id="session-1", command_id="turn-1")
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    # Simulate an administrative hold committed after an older claimant selected
    # the Turn. The supported hold API rejects active running work; this fixture
    # exercises the execution-time defense in depth for an already-held lease.
    with repository.store.connect() as connection:
        connection.execute(
            """INSERT INTO conversation_turn_manual_holds(
                hold_operation_id, session_id, command_id, reason, held_at,
                release_operation_id, released_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL)""",
            ("hold-op-1", "session-1", "turn-1", "pause history", time.time()),
        )
    runner = _CountingRunner()
    api = ConversationAPI(repository, EventStore(database), runner)

    await api.process_queued_turn("session-1", "turn-1")

    persisted = repository.load_turn_status("session-1", "turn-1")
    assert runner.calls == 0
    assert persisted is not None
    assert persisted.status == "retryable"
    assert persisted.owner_id is None
    assert repository.is_turn_held("session-1", "turn-1") is True
