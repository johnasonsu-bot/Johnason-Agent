import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from workbench.conversations.models import ConversationSession, agent_message
import pytest

from workbench.conversations.repository import (
    ConversationRepository,
    TurnSnapshotCorruption,
)
from workbench.workflow.schema import PHASE1_SCHEMA_VERSION, migrate_phase1


def _enqueue(repository: ConversationRepository, *, command_id: str = "turn-1") -> None:
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id=command_id,
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={"phase": "before_model", "messages": [], "events": []},
    )


def test_enqueue_turn_is_visible_as_queued(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "queue.sqlite")

    _enqueue(repository)

    status = repository.load_turn_status("session-1", "turn-1")
    assert status is not None
    assert status.status == "queued"


def test_only_one_worker_claims_a_queued_turn(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "queue.sqlite")
    _enqueue(repository)

    first = repository.claim_next_turn(owner_id="worker-a", lease_seconds=30)
    second = repository.claim_next_turn(owner_id="worker-b", lease_seconds=30)

    assert first is not None
    assert first.session_id == "session-1"
    assert first.command_id == "turn-1"
    assert second is None


def test_host_retry_waits_for_new_generation_and_preserves_session_fifo(
    tmp_path: Path,
) -> None:
    database = tmp_path / "host-generation-fifo.sqlite"
    repository = ConversationRepository(database, host_generation="generation-1")
    _enqueue(repository, command_id="turn-1")
    _enqueue(repository, command_id="turn-2")
    first = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert first is not None and first.command_id == "turn-1"
    repository.mark_retryable(
        "session-1",
        "turn-1",
        owner_id="worker",
        state={
            "phase": "before_model",
            "messages": [],
            "events": [],
            "failed_host_generation": "generation-1",
        },
    )

    assert repository.claim_next_turn(owner_id="same-generation") is None
    same_generation = ConversationRepository(
        database, host_generation="generation-1"
    )
    assert same_generation.claim_next_turn(owner_id="same-generation") is None

    restarted = ConversationRepository(database, host_generation="generation-2")
    recovered = restarted.claim_next_turn(owner_id="new-generation")
    assert recovered is not None and recovered.command_id == "turn-1"
    restarted.finish_turn(
        "session-1",
        "turn-1",
        owner_id="new-generation",
        status="completed",
        state={"phase": "completed"},
        result=[],
    )
    following = restarted.claim_next_turn(owner_id="new-generation")
    assert following is not None and following.command_id == "turn-2"


def test_expired_running_turn_becomes_retryable(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "queue.sqlite")
    _enqueue(repository)
    repository.claim_next_turn(owner_id="worker-a", lease_seconds=0.001)
    time.sleep(0.01)

    recovered = repository.recover_expired_turns(now=time.time())

    assert recovered == [("session-1", "turn-1")]
    assert repository.load_turn_status("session-1", "turn-1").status == "retryable"


def test_expired_engine_host_write_turn_requires_reconciliation(tmp_path: Path) -> None:
    repository = ConversationRepository(
        tmp_path / "expired-host-write.sqlite", host_generation="generation-2"
    )
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={
            "phase": "running",
            "runner_mode": "engine_host",
            "active_host_generation": "generation-1",
            "unfinished_write_tool_ids": ["write-1"],
            "retryable": True,
            "failed_host_generation": "stale-generation",
            "retry_not_before": 9999999999,
            "host_retry_count": 4,
        },
    )
    repository.claim_next_turn(owner_id="crashed-worker", lease_seconds=0.001)
    time.sleep(0.01)

    assert repository.recover_expired_turns(now=time.time()) == [
        ("session-1", "turn-1")
    ]

    recovered = repository.load_turn_status("session-1", "turn-1")
    assert recovered is not None
    assert recovered.status == "reconciliation_required"
    assert recovered.state["runner_mode"] == "engine_host"
    assert recovered.state["host_failure_phase"] == "unknown_write_effect"
    for key in (
        "retryable",
        "failed_host_generation",
        "retry_not_before",
        "host_retry_count",
    ):
        assert key not in recovered.state


def test_expired_engine_host_read_only_turn_waits_for_new_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "expired-host-read.sqlite"
    repository = ConversationRepository(database, host_generation="generation-1")
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={
            "phase": "running",
            "runner_mode": "engine_host",
            "active_host_generation": "generation-1",
            "unfinished_write_tool_ids": [],
        },
    )
    repository.claim_next_turn(owner_id="crashed-worker", lease_seconds=0.001)
    time.sleep(0.01)

    repository.recover_expired_turns(now=time.time())

    recovered = repository.load_turn_status("session-1", "turn-1")
    assert recovered is not None
    assert recovered.status == "retryable"
    assert recovered.state["runner_mode"] == "engine_host"
    assert recovered.state["failed_host_generation"] == "generation-1"
    assert repository.claim_next_turn(owner_id="same-generation") is None
    assert (
        ConversationRepository(
            database, host_generation="generation-2"
        ).claim_next_turn(owner_id="new-generation")
        is not None
    )


def test_reconciliation_transition_clears_stale_retry_metadata(tmp_path: Path) -> None:
    repository = ConversationRepository(
        tmp_path / "clear-retry.sqlite", host_generation="generation-2"
    )
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={
            "phase": "before_model",
            "runner_mode": "engine_host",
            "retryable": True,
            "failed_host_generation": "generation-1",
            "retry_not_before": 123.0,
            "host_retry_count": 2,
        },
    )
    claimed = repository.claim_next_turn(owner_id="worker")
    assert claimed is not None

    repository.transition_host_failure(
        "session-1",
        "turn-1",
        owner_id="worker",
        failure_phase="unknown_write_effect",
        retryable=False,
    )

    recovered = repository.load_turn_status("session-1", "turn-1")
    assert recovered is not None
    assert recovered.status == "reconciliation_required"
    for key in (
        "retryable",
        "failed_host_generation",
        "retry_not_before",
        "host_retry_count",
    ):
        assert key not in recovered.state


def test_turn_routing_metadata_survives_every_state_transition(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "metadata.sqlite")
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="studio-primary",
        model="local-agent",
        prompt="hello",
        initial_state={
            "phase": "before_model",
            "messages": [],
            "events": [],
            "runner_mode": "engine_host",
            "host_run_id": "host-turn-1",
        },
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None

    repository.save_turn_state(
        "session-1",
        "turn-1",
        owner_id="worker",
        state={"phase": "finalizing"},
    )
    assert repository.load_turn_status("session-1", "turn-1").state == {
        "phase": "finalizing",
        "runner_mode": "engine_host",
        "host_run_id": "host-turn-1",
    }

    repository.claim_tool_effect(
        session_id="session-1",
        command_id="turn-1",
        tool_call_id="tool-1",
        tool_name="lookup",
        arguments={},
        owner_id="worker",
    )
    repository.complete_tool_effect(
        session_id="session-1",
        command_id="turn-1",
        tool_call_id="tool-1",
        owner_id="worker",
        result="done",
        turn_state={"phase": "after_tool"},
    )
    assert repository.load_turn_status("session-1", "turn-1").state["runner_mode"] == "engine_host"

    repository.release_turn(
        "session-1", "turn-1", owner_id="worker", state={"phase": "before_model"}
    )
    repository.mark_retryable_unowned(
        "session-1", "turn-1", state={"phase": "before_model"}
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    repository.mark_retryable(
        "session-1",
        "turn-1",
        owner_id="worker",
        state={"phase": "before_model"},
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    repository.finish_turn(
        "session-1",
        "turn-1",
        owner_id="worker",
        status="completed",
        state={"phase": "completed"},
        result=[],
    )

    terminal = repository.load_turn_status("session-1", "turn-1")
    assert terminal is not None
    assert terminal.state == {
        "phase": "completed",
        "runner_mode": "engine_host",
        "host_run_id": "host-turn-1",
    }


def test_runtime_execution_snapshot_survives_turn_state_compaction(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "runtime-execution.sqlite")
    repository.create_session("session-1")
    execution = {
        "selector": "goose",
        "envelope": {"command_id": "runtime-command-1"},
        "runtime_input": {"messages": [{"content": "hello"}]},
    }
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="provider-1",
        model="configured-model",
        prompt="hello",
        initial_state={
            "phase": "before_model",
            "runner_mode": "runtime",
            "runtime_execution": execution,
        },
    )
    claimed = repository.claim_next_turn(owner_id="worker")
    assert claimed is not None

    repository.save_turn_state(
        "session-1", "turn-1", owner_id="worker", state={"phase": "running"}
    )

    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None
    assert persisted.state["runtime_execution"] == execution


def test_turn_routing_metadata_rejects_an_attempted_change(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "immutable-metadata.sqlite")
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="studio-primary",
        model="local-agent",
        prompt="hello",
        initial_state={
            "phase": "before_model",
            "messages": [],
            "events": [],
            "runner_mode": "engine_host",
            "host_run_id": "host-turn-1",
        },
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None

    with pytest.raises(TurnSnapshotCorruption, match="routing metadata cannot change"):
        repository.save_turn_state(
            "session-1",
            "turn-1",
            owner_id="worker",
            state={"phase": "before_model", "runner_mode": "python"},
        )

    current = repository.load_turn_status("session-1", "turn-1")
    assert current is not None
    assert current.state["runner_mode"] == "engine_host"


def test_legacy_terminal_without_routing_metadata_remains_readable(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "legacy-terminal.sqlite")
    _enqueue(repository)
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None

    repository.finish_turn(
        "session-1",
        "turn-1",
        owner_id="worker",
        status="failed",
        state={"phase": "failed"},
        result=[],
    )

    terminal = repository.load_turn_status("session-1", "turn-1")
    assert terminal is not None
    assert terminal.state == {"phase": "failed"}


def test_messages_and_provider_state_are_separate(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"
    repository = ConversationRepository(database)

    repository.append_message(agent_message(content="answer"))
    repository.save_continuation_state(
        "session-1", {"reasoning_content": "private"}
    )

    message = repository.list_messages("session-1")[0]
    assert message.content == "answer"
    assert "private" not in message.model_dump_json()


def test_migrate_phase1_supports_plain_sqlite_connections(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"

    with sqlite3.connect(database) as connection:
        migrate_phase1(connection)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()

    assert version == (PHASE1_SCHEMA_VERSION,)


def test_messages_receive_monotonic_session_sequences_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    repository = ConversationRepository(database)
    repository.append_message(agent_message(content="first", command_id="command-1"))
    repository.append_message(agent_message(content="second", command_id="command-2"))

    reopened = ConversationRepository(database)
    third = reopened.append_message(
        agent_message(content="third", command_id="command-3")
    )

    assert third.sequence == 3
    assert [message.content for message in reopened.list_messages("session-1")] == [
        "first",
        "second",
        "third",
    ]
    assert [message.sequence for message in reopened.list_messages("session-1")] == [
        1,
        2,
        3,
    ]


def test_append_message_is_idempotent_for_a_command_id(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.sqlite")
    command = agent_message(content="answer", command_id="message-command-1")

    first = repository.append_message(command)
    second = repository.append_message(command)

    assert first.message_id == second.message_id
    assert first.sequence == second.sequence == 1
    assert repository.list_messages("session-1") == [first]


def test_message_command_ids_are_idempotent_within_each_session(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "conversation.sqlite")

    first = repository.append_message(
        agent_message(
            session_id="session-1", content="first", command_id="shared-command"
        )
    )
    second = repository.append_message(
        agent_message(
            session_id="session-2", content="second", command_id="shared-command"
        )
    )

    assert first.session_id == "session-1"
    assert second.session_id == "session-2"
    assert second.content == "second"
    assert [message.content for message in repository.list_messages("session-1")] == [
        "first"
    ]
    assert [message.content for message in repository.list_messages("session-2")] == [
        "second"
    ]


def test_concurrent_distinct_commands_have_continuous_unique_sequences(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    repository = ConversationRepository(database)

    def append(index: int) -> None:
        repository.append_message(
            agent_message(content=f"message-{index}", command_id=f"command-{index}")
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(16)))

    messages = ConversationRepository(database).list_messages("session-1")
    assert len(messages) == 16
    assert [message.sequence for message in messages] == list(range(1, 17))


def test_concurrent_duplicate_command_persists_one_message(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"
    repository = ConversationRepository(database)

    def append(_: int):
        return repository.append_message(
            agent_message(content="answer", command_id="same-command")
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        returned = list(pool.map(append, range(16)))

    assert {message.message_id for message in returned} == {returned[0].message_id}
    assert repository.list_messages("session-1") == [returned[0]]


def test_v3_global_command_constraint_is_upgraded_without_losing_messages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    legacy = agent_message(
        session_id="session-1", content="legacy", command_id="shared-command"
    ).model_copy(update={"sequence": 1})
    session = ConversationSession(session_id="session-1")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at) VALUES (3, 0);
            CREATE TABLE conversation_sessions (
                session_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL
            );
            CREATE TABLE conversation_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                command_id TEXT NOT NULL UNIQUE,
                sequence INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                UNIQUE (session_id, sequence),
                FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO conversation_sessions(session_id, record_json) VALUES (?, ?)",
            (session.session_id, session.model_dump_json()),
        )
        connection.execute(
            """
            INSERT INTO conversation_messages(
                message_id, session_id, command_id, sequence, record_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                legacy.message_id,
                legacy.session_id,
                legacy.command_id,
                legacy.sequence,
                legacy.model_dump_json(),
            ),
        )

    repository = ConversationRepository(database)
    appended = repository.append_message(
        agent_message(
            session_id="session-2", content="new", command_id="shared-command"
        )
    )

    assert repository.list_messages("session-1") == [legacy]
    assert appended.session_id == "session-2"
    assert appended.content == "new"
    assert repository.list_messages("session-2") == [appended]


def test_continuation_state_survives_repository_restart(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"
    repository = ConversationRepository(database)
    repository.create_session("session-1")
    repository.save_continuation_state(
        "session-1", {"reasoning_content": "private"}
    )

    assert ConversationRepository(database).load_continuation_state("session-1") == {
        "reasoning_content": "private"
    }


def test_v7_migration_removes_v6_unscoped_continuation_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    repository = ConversationRepository(database)
    repository.save_continuation_state(
        "session-1", {"reasoning_content": "legacy-private"}
    )
    with repository.store.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (6, 0)"
        )

    reopened = ConversationRepository(database)
    assert reopened.load_continuation_state("session-1") is None
    reopened.save_continuation_state("session-1", {"reasoning_content": "current"})
    assert ConversationRepository(database).load_continuation_state("session-1") == {
        "reasoning_content": "current"
    }
