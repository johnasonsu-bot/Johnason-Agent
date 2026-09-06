"""Exercise real projection under arbitrary transport fragmentation."""
import json

import pytest

from tests.fixtures.host_v2 import runtime_event
from workbench.agui.mapper import map_domain_event
from workbench.runtime.federated_conversation import (
    FederatedConversationProtocolError,
    canonical_runtime_event_digest,
    project_runtime_event,
)


def feed(parts, *, final=None):
    state, cursor, digests, events = {}, 0, {}, []
    raw = [runtime_event("assistant.delta", cursor=i, payload={"text": part})
           for i, part in enumerate(parts, 1)]
    if final is not None:
        raw.append(runtime_event("assistant.message", cursor=len(raw) + 1,
                                 payload={"content": final}))
    for event in raw:
        result = project_runtime_event(event, after_cursor=cursor,
                                       projected_digests=digests, text_state=state)
        cursor = result.cursor
        digests[str(cursor)] = canonical_runtime_event_digest(event)
        # Serialization is the same boundary crossed on worker restart.
        state = json.loads(json.dumps(result.text_state))
        events.extend(result.domain_events)
        assert project_runtime_event(event, after_cursor=cursor,
                                     projected_digests=digests, text_state=state) is None
    return events, state


@pytest.mark.parametrize("text", [
    "Hudi/Delta/Iceberg\n项目 / 进度\n| 维度 | 考核指标 |\n| --- | --- |\n尾段",
    "数据治理/架构设计\nhttps://example.com/docs/api?q=one\n完成",
    "token references are ordinary prose\nhistory idioms\n" + "a" * 41 + "\n",
])
def test_every_transport_split_preserves_body_and_flushes_tail(text):
    for split in range(1, len(text)):
        events, state = feed([text[:split], text[split:]], final=text)
        assert "".join(e.payload["content"] for e in events
                       if e.event_type == "agent.message.delta") == text
        assert events[-1].event_type == "agent.message.completed"
        assert state["completed"] is True


def test_partial_slash_waits_for_semantic_boundary_and_survives_restart():
    events, state = feed(["Hudi", "/Delta", "/Iceberg"])
    assert events == []
    assert state["text"] == "Hudi/Delta/Iceberg"
    final = runtime_event("assistant.message", cursor=4,
                          payload={"content": "Hudi/Delta/Iceberg"})
    result = project_runtime_event(final, after_cursor=3, text_state=state)
    assert [e.event_type for e in result.domain_events] == [
        "agent.message.delta", "agent.message.completed"]
    assert result.domain_events[0].payload["content"] == "Hudi/Delta/Iceberg"


def test_long_line_is_bounded_but_not_treated_as_one_transport_token():
    text = "这是业务报告。" * 900
    events, _ = feed([text[i:i + 2000] for i in range(0, len(text), 2000)], final=text)
    deltas = [e for e in events if e.event_type == "agent.message.delta"]
    assert "".join(map_domain_event(e)[0]["delta"] for e in deltas) == text


@pytest.mark.parametrize("parts", [
    ["/Users/", "someone/private.txt\n"],
    ["sk", "-exampleCredentialValue\n"],
    ["safe\n", "a" * 4097],
])
def test_aggregation_does_not_allow_private_content_or_unbounded_fragments(parts):
    with pytest.raises(FederatedConversationProtocolError):
        feed(parts)


def test_final_cannot_rewrite_already_streamed_text():
    with pytest.raises(FederatedConversationProtocolError):
        feed(["first\n"], final="different")


def test_total_body_is_bounded():
    with pytest.raises(FederatedConversationProtocolError):
        feed(["报告" * 2000] * 17)


def test_terminal_completion_flushes_delta_only_runtime_and_saves_answer():
    _, state = feed(["Hudi", "/Delta"])
    result = project_runtime_event(
        runtime_event("runtime.status", cursor=3, payload={"status": "completed"}),
        after_cursor=2, text_state=state,
    )
    assert result.assistant_message == "Hudi/Delta"
    assert [e.event_type for e in result.domain_events] == [
        "agent.message.delta", "agent.message.completed", "runtime.status.changed"]


def test_partial_buffer_is_not_published_on_failed_status():
    _, state = feed(["unfinished"])
    result = project_runtime_event(
        runtime_event("runtime.status", cursor=2, payload={"status": "failed"}),
        after_cursor=1, text_state=state,
    )
    assert [e.event_type for e in result.domain_events] == ["runtime.status.changed"]
    assert result.assistant_message is None


def test_conflicting_delta_alias_cannot_hide_unexamined_payload():
    with pytest.raises(FederatedConversationProtocolError):
        project_runtime_event(runtime_event("assistant.delta", cursor=1,
            payload={"text": "Hello\n", "content": "/Users/example/private.txt"}),
            text_state={})
