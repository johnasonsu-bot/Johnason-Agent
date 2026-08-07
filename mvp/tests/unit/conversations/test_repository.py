import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from workbench.conversations.models import ConversationSession, agent_message
from workbench.conversations.repository import ConversationRepository
from workbench.workflow.schema import PHASE1_SCHEMA_VERSION, migrate_phase1


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
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (6, 0)"
        )

    reopened = ConversationRepository(database)
    assert reopened.load_continuation_state("session-1") is None
    reopened.save_continuation_state("session-1", {"reasoning_content": "current"})
    assert ConversationRepository(database).load_continuation_state("session-1") == {
        "reasoning_content": "current"
    }
