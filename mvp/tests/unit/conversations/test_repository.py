from pathlib import Path

from workbench.conversations.models import agent_message
from workbench.conversations.repository import ConversationRepository


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
