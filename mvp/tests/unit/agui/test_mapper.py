import pytest
from datetime import datetime, timezone

from workbench.agui.mapper import map_domain_event
from workbench.agui.stream import replay_agui
from workbench.protocol.events import DomainEvent


_SENSITIVE_ROOTS = (
    ("reasoning",),
    ("chain", "of", "thought"),
    ("private", "prompt"),
    ("private", "history"),
    ("history",),
    ("provider",),
    ("workspace",),
    ("manifest",),
    ("vault",),
    ("secret",),
    ("credential",),
    ("api", "key"),
    ("access", "key"),
    ("private", "key"),
    ("bearer",),
    ("access", "token"),
    ("api", "token"),
)
_SENSITIVE_METADATA_SUFFIXES = (
    "content", "prompt", "history", "id", "ref", "reference", "path", "digest", "key", "token"
)
_LABEL_STYLES = ("snake", "kebab", "dot", "space", "colon", "equals", "camel", "pascal")
_IDENTIFIER_STYLES = ("snake", "kebab", "dot", "colon", "camel", "pascal")
_COMPACT_CREDENTIAL_LABELS = (
    (("api", "key"), "apikey"),
    (("api", "token"), "apitoken"),
    (("access", "key"), "accesskey"),
    (("access", "token"), "accesstoken"),
    (("private", "key"), "privatekey"),
    (("client", "secret"), "clientsecret"),
    (("secret", "key"), "secretkey"),
    (("auth", "token"), "authtoken"),
    (("bearer", "token"), "bearertoken"),
    (("github", "pat"), "githubpat"),
)
_PRIVATE_PUBLIC_PHRASES = (
    "chainOfThought",
    "chain_of_thought",
    "chain-of-thought",
    "chain of thought",
    "privatePrompt",
    "private_prompt",
    "private-prompt",
    "private prompt",
    "privateHistory",
    "private_history",
    "private-history",
    "private history",
)


def _styled_sensitive_label(root: tuple[str, ...], suffix: str, style: str) -> str:
    parts = (*root, suffix)
    if style == "camel":
        return parts[0] + "".join(part.title() for part in parts[1:])
    if style == "pascal":
        return "".join(part.title() for part in parts)
    separator = {
        "snake": "_", "kebab": "-", "dot": ".", "space": " ", "colon": ":", "equals": "="
    }[style]
    return separator.join(parts)


def _credential_styles(parts: tuple[str, ...], compact: str) -> tuple[str, ...]:
    return (
        compact.upper(),
        compact,
        parts[0] + "".join(part.title() for part in parts[1:]),
        "_".join(parts),
        "-".join(parts),
    )


@pytest.mark.parametrize(
    ("domain_type", "payload", "agui_type"),
    [
        ("run.started", {}, "RUN_STARTED"),
        ("run.completed", {}, "RUN_FINISHED"),
        ("run.failed", {"message": "boom"}, "RUN_ERROR"),
        ("agent.message.delta", {"content": "hi"}, "TEXT_MESSAGE_CONTENT"),
        ("agent.tool.started", {"tool_call_id": "t1", "name": "query"}, "TOOL_CALL_START"),
        ("agent.tool.arguments.delta", {"tool_call_id": "t1", "delta": "{}"}, "TOOL_CALL_ARGS"),
        ("agent.tool.completed", {"tool_call_id": "t1"}, "TOOL_CALL_END"),
        ("run.state.snapshot", {"snapshot": {"step": 1}}, "STATE_SNAPSHOT"),
        ("run.state.delta", {"delta": [{"op": "replace"}]}, "STATE_DELTA"),
        ("intervention.queued", {"id": "i1"}, "CUSTOM"),
    ],
)
def test_maps_domain_lifecycle(domain_type: str, payload: dict, agui_type: str) -> None:
    event = DomainEvent.new(
        domain_type,
        "test",
        payload,
        run_id="run-1",
        sequence=2,
    )

    mapped = map_domain_event(event)

    assert mapped[0]["type"] == agui_type
    assert mapped[0]["runId"] == "run-1"


def test_non_ui_event_is_not_projected() -> None:
    event = DomainEvent.new("lease.renewed", "watchdog", {}, run_id="run-1")
    assert map_domain_event(event) == []


@pytest.mark.asyncio
async def test_replay_resumes_after_sequence_without_duplicates() -> None:
    events = [
        DomainEvent.new("run.started", "test", {}, run_id="r1", sequence=1),
        DomainEvent.new(
            "agent.message.delta",
            "test",
            {"content": "one"},
            run_id="r1",
            sequence=2,
        ),
        DomainEvent.new("run.completed", "test", {}, run_id="r1", sequence=3),
    ]

    replayed = [event async for event in replay_agui(events, after_sequence=1)]

    assert [event["sequence"] for event in replayed] == [2, 3]


def test_v2_projection_preserves_identity_and_filters_reasoning_audit() -> None:
    """Catches v2 cursor identity loss or private reasoning reaching SSE."""
    event = DomainEvent.new(
        "agent.message.delta",
        "engine_host.v2",
        {"content": "hello", "term_id": "term-1", "cursor": 7},
        run_id="run-1",
        step_id="step-1",
        sequence=7,
    )
    reasoning = DomainEvent.new(
        "runtime.reasoning.observed",
        "engine_host.v2",
        {"count": 3, "term_id": "term-1", "cursor": 8},
        run_id="run-1",
        step_id="step-1",
        sequence=8,
    )

    assert map_domain_event(event) == [
        {
            "type": "TEXT_MESSAGE_CONTENT",
            "runId": "run-1",
            "stepId": "step-1",
            "termId": "term-1",
            "cursor": 7,
            "timestamp": event.occurred_at.isoformat(),
            "sequence": 7,
            "eventId": event.event_id,
            "messageId": event.event_id,
            "delta": "hello",
        }
    ]
    assert map_domain_event(reasoning) == []


def test_v2_tool_public_projection_drops_unapproved_fields() -> None:
    """Catches tool arguments, provider references, or results leaking to SSE."""
    event = DomainEvent.new(
        "agent.tool.completed",
        "engine_host.v2",
        {
            "tool_id": "search",
            "tool_call_id": "call-1",
            "read_only": True,
            "summary": "found 2 records",
            "artifact_ref": "artifact-1",
            "arguments": {"query": "private"},
            "provider_ref": "provider-1",
            "raw_result": "private",
            "term_id": "term-1",
            "cursor": 9,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=9,
    )

    mapped = map_domain_event(event)

    assert {
        key: mapped[0][key]
        for key in ("toolCallId", "toolCallName", "readOnly", "summary", "artifactRef")
    } == {
        "toolCallId": "call-1",
        "toolCallName": "search",
        "readOnly": True,
        "summary": "found 2 records",
        "artifactRef": "artifact-1",
    }
    assert "arguments" not in mapped[0]
    assert "provider_ref" not in mapped[0]
    assert "raw_result" not in mapped[0]


def test_v2_agui_defense_in_depth_drops_forged_result_and_unsafe_custom_summary() -> None:
    """Catches bypassed domain events leaking raw tool output or credentials."""
    tool = DomainEvent.new(
        "agent.tool.completed",
        "engine_host.v2",
        {
            "tool_id": "search",
            "tool_call_id": "call-1",
            "read_only": True,
            "public_result": "private raw result",
            "term_id": "term-1",
            "cursor": 9,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=9,
    )
    artifact = DomainEvent.new(
        "artifact.proposed",
        "engine_host.v2",
        {
            "artifact_id": "artifact-1",
            "summary": "Authorization: bearer abcdefghijklmnop",
            "term_id": "term-1",
            "cursor": 10,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=10,
    )

    assert "result" not in map_domain_event(tool)[0]
    assert map_domain_event(artifact) == []


def test_v2_agui_projects_applied_intervention_and_rejects_forged_tool_or_message_identity() -> None:
    """Catches dropped applied interventions and unchecked v2 public primitives."""
    applied = DomainEvent.new(
        "intervention.applied",
        "engine_host.v2",
        {
            "intervention_id": "intervention-1",
            "summary": "review applied",
            "term_id": "term-1",
            "cursor": 11,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=11,
    )
    tool = DomainEvent.new(
        "agent.tool.started",
        "engine_host.v2",
        {
            "tool_id": "search",
            "tool_call_id": "Authorization: bearer abcdefghijklmnop",
            "read_only": True,
            "term_id": "term-1",
            "cursor": 12,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=12,
    )
    message = DomainEvent.new(
        "agent.message.delta",
        "engine_host.v2",
        {
            "content": "Authorization: bearer abcdefghijklmnop",
            "term_id": "term-1",
            "cursor": 13,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=13,
    )

    assert map_domain_event(applied)[0]["value"] == {
        "intervention_id": "intervention-1",
        "summary": "review applied",
    }
    assert map_domain_event(tool) == []
    assert map_domain_event(message) == []


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "reasoning: private scratch work",
        "chain-of-thought: hidden steps",
        "private prompt: do not reveal",
        "Exception: upstream failure",
        "Traceback (most recent call last):",
        "provider reference: internal-provider",
        "workspace path: /private/project",
        "manifest digest: abc123",
        "api key: sk-abcdefghijklmnop",
    ],
)
def test_v2_second_boundary_filters_every_private_public_text_variant(unsafe_text: str) -> None:
    """Catches direct persisted events bypassing runtime public-text checks."""
    event = DomainEvent.new(
        "agent.message.delta", "engine_host.v2",
        {"content": unsafe_text, "term_id": "term-1", "cursor": 1},
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "local_path",
    [
        "path=/private/state",
        r"path=C:\private\state.json",
        r"artifact=\\host\share\state.json",
        "artifact:C:/private/state",
        'artifact: "/private/state"',
        "artifact=(C:/private/state)",
        "../state.json",
    ],
)
def test_v2_development_boundary_rejects_local_paths_at_label_and_punctuation_boundaries(
    local_path: str,
) -> None:
    """Catches persisted development payloads exposing local paths through AG-UI."""
    event = DomainEvent.new(
        "development.plan.approved",
        "development-graph-worker",
        {
            "plan_id": "plan-1",
            "graph_run_id": "graph-1",
            "status": "approved",
            "diagnostic": {"path": local_path},
            "term_id": "term-1",
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )

    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/private/state",
        "http://127.0.0.1:46121/api/v1",
    ],
)
def test_v2_development_boundary_allows_http_urls_with_path_segments(url: str) -> None:
    """Catches local-path filtering accidentally dropping legitimate public URLs."""
    event = DomainEvent.new(
        "development.plan.approved",
        "development-graph-worker",
        {
            "plan_id": "plan-1",
            "graph_run_id": "graph-1",
            "status": "approved",
            "diagnostic": {"url": url},
            "term_id": "term-1",
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )

    mapped = map_domain_event(event)
    assert mapped[0]["value"] == {
        "plan_id": "plan-1",
        "graph_run_id": "graph-1",
        "status": "approved",
    }


@pytest.mark.parametrize(
    ("event_id", "run_id", "term_id", "step_id", "correlation_id", "sequence", "cursor"),
    [
        ("sk-abcdefghijklmnop", "run-1", "term-1", "step-1", None, 1, 1),
        ("event-1", "sk-abcdefghijklmnop", "term-1", "step-1", None, 1, 1),
        ("event-1", "run-1", "term-1", "step-1", "bearer-secret", 1, 1),
        ("event-1", "run-1", "term-1", "step-1", None, 2, 1),
    ],
)
def test_v2_agui_rejects_forged_public_identity(
    event_id: str, run_id: str, term_id: str, step_id: str, correlation_id: str | None,
    sequence: int, cursor: int,
) -> None:
    """Catches persisted v2 identity values bypassing the runtime contract."""
    event = DomainEvent(
        event_id=event_id,
        event_type="agent.message.delta",
        source="engine_host.v2",
        occurred_at=datetime.now(timezone.utc),
        payload={"content": "hello", "term_id": term_id, "cursor": cursor},
        run_id=run_id,
        step_id=step_id,
        correlation_id=correlation_id,
        sequence=sequence,
    )
    assert map_domain_event(event) == []


def test_v2_agui_malformed_custom_status_returns_empty_instead_of_type_error() -> None:
    """Catches unhashable custom status values terminating the SSE mapping loop."""
    event = DomainEvent.new(
        "runtime.status.changed", "engine_host.v2",
        {"status": [], "term_id": "term-1", "cursor": 1},
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "a" * 64,
        "result " + "b" * 40,
        r"C:\private\runtime\state.json",
        r"\\runtime-host\private-share\state.json",
        "context proof available",
        "application/x-host-v2-workspace-proof",
    ],
)
def test_v2_agui_rejects_digests_paths_and_internal_proofs(
    unsafe_text: str,
) -> None:
    """Catches forged persisted values bypassing the first public boundary."""
    event = DomainEvent.new(
        "agent.message.delta",
        "engine_host.v2",
        {"content": unsafe_text, "term_id": "term-1", "cursor": 1},
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )

    assert map_domain_event(event) == []


def test_v2_agui_canonicalizes_unknown_runtime_error_codes() -> None:
    """Catches forged provider-specific error codes reaching the browser."""
    event = DomainEvent.new(
        "runtime.error",
        "engine_host.v2",
        {
            "code": "provider_overloaded_in_region_7",
            "summary": "request failed",
            "term_id": "term-1",
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )

    assert map_domain_event(event)[0]["value"] == {
        "code": "runtime_error",
        "summary": "request failed",
    }


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("run.failed", {"message": "raw error"}),
        ("agent.tool.arguments.delta", {"tool_call_id": "call-1", "delta": "raw args"}),
        ("run.state.snapshot", {"snapshot": {"workspace_path": "/private"}}),
        ("run.state.delta", {"delta": [{"op": "replace"}]}),
    ],
)
def test_v2_never_falls_back_to_v1_raw_event_branches(
    event_type: str, payload: dict[str, object]
) -> None:
    """Catches forged v2 events reaching V1's raw message, args, or state paths."""
    event = DomainEvent.new(
        event_type, "engine_host.v2",
        {**payload, "term_id": "term-1", "cursor": 1},
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


def test_v1_raw_event_mapping_remains_available() -> None:
    """Catches V2 top-level isolation accidentally changing established V1 behavior."""
    event = DomainEvent.new(
        "run.state.snapshot", "legacy",
        {"snapshot": {"legacy": "state"}},
        run_id="run-1", sequence=1,
    )
    assert map_domain_event(event)[0]["snapshot"] == {"legacy": "state"}


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "providerRef=internal", "workspace_path: /private", "manifest-digest=abc",
        "reasoningContent: private", "vault-id=hidden", "secretToken: x",
        "credentialId=private",
    ],
)
def test_v2_shared_public_text_validator_catches_identifier_style_private_labels(unsafe_text: str) -> None:
    """Catches camel/snake/kebab private labels bypassing text redaction."""
    event = DomainEvent.new(
        "agent.message.delta", "engine_host.v2",
        {"content": unsafe_text, "term_id": "term-1", "cursor": 1},
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "update",
    [
        {"occurred_at": datetime.now()},
        {"occurred_at": []},
        {"event_id": []},
        {"run_id": {}},
        {"step_id": True},
        {"sequence": []},
    ],
)
def test_v2_model_construct_identity_and_time_anomalies_fail_closed(update: dict[str, object]) -> None:
    """Catches model_construct values raising during V2 SSE projection."""
    values: dict[str, object] = {
        "event_id": "event-1", "event_type": "agent.message.delta", "source": "engine_host.v2",
        "occurred_at": datetime.now(timezone.utc), "payload": {"content": "hello", "term_id": "term-1", "cursor": 1},
        "run_id": "run-1", "step_id": "step-1", "sequence": 1, "correlation_id": None,
    }
    values.update(update)
    assert map_domain_event(DomainEvent.model_construct(**values)) == []


def test_v2_tool_artifact_ref_is_an_opaque_identifier_not_public_text() -> None:
    """Catches a display string being accepted as an artifact reference."""
    event = DomainEvent.new(
        "agent.tool.completed", "engine_host.v2",
        {
            "tool_id": "search", "tool_call_id": "call-1", "read_only": True,
            "artifact_ref": "human readable artifact", "term_id": "term-1", "cursor": 1,
        },
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "manifestRef: private", "manifest_reference=private", "manifest-ref: private",
        "manifestReference=private", "workspaceRef: private", "workspace-reference=private",
        "vault_ref: private", "vaultReference=private",
    ],
)
def test_v2_public_text_rejects_sensitive_reference_labels(unsafe_text: str) -> None:
    """Catches ref/reference suffix variants bypassing the shared public-text policy."""
    event = DomainEvent.new(
        "artifact.proposed", "engine_host.v2",
        {"artifact_id": "artifact-1", "summary": unsafe_text, "term_id": "term-1", "cursor": 1},
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize("sensitive_identifier", ["manifestRef-1", "workspace_reference-1", "vault-reference-1"])
def test_v2_forged_persisted_reference_identity_is_rejected(sensitive_identifier: str) -> None:
    """Catches ref/reference IDs bypassing runtime validation after persistence."""
    event = DomainEvent.new(
        "agent.tool.completed", "engine_host.v2",
        {
            "tool_id": "search", "tool_call_id": "call-1", "read_only": True,
            "artifact_ref": sensitive_identifier, "term_id": sensitive_identifier, "cursor": 1,
        },
        run_id="run-1", step_id="step-1", sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    ("root", "suffix"),
    [(root, suffix) for root in _SENSITIVE_ROOTS for suffix in _SENSITIVE_METADATA_SUFFIXES],
)
def test_v2_forged_summaries_reject_normalized_sensitive_root_suffix_combinations(
    root: tuple[str, ...], suffix: str
) -> None:
    """Catches persisted summaries bypassing any supported normalized word boundary."""
    for style in _LABEL_STYLES:
        unsafe = _styled_sensitive_label(root, suffix, style) + ": hidden"
        event = DomainEvent.new(
            "artifact.proposed",
            "engine_host.v2",
            {
                "artifact_id": "artifact-1",
                "summary": unsafe,
                "term_id": "term-1",
                "cursor": 1,
            },
            run_id="run-1",
            step_id="step-1",
            sequence=1,
        )
        assert map_domain_event(event) == []


@pytest.mark.parametrize(
    ("root", "suffix"),
    [(root, suffix) for root in _SENSITIVE_ROOTS for suffix in _SENSITIVE_METADATA_SUFFIXES],
)
def test_v2_forged_artifact_refs_reject_normalized_sensitive_prefix_combinations(
    root: tuple[str, ...], suffix: str
) -> None:
    """Catches persisted artifact refs bypassing strict opaque-ID normalization."""
    for style in _IDENTIFIER_STYLES:
        unsafe = _styled_sensitive_label(root, suffix, style) + "-1"
        event = DomainEvent.new(
            "agent.tool.completed",
            "engine_host.v2",
            {
                "tool_id": "search",
                "tool_call_id": "call-1",
                "read_only": True,
                "artifact_ref": unsafe,
                "term_id": "term-1",
                "cursor": 1,
            },
            run_id="run-1",
            step_id="step-1",
            sequence=1,
        )
        assert map_domain_event(event) == []
        forged_identity = event.model_copy(
            update={
                "payload": {
                    "tool_id": "search",
                    "tool_call_id": "call-1",
                    "read_only": True,
                    "artifact_ref": "artifact-1",
                    "term_id": unsafe,
                    "cursor": 1,
                }
            }
        )
        assert map_domain_event(forged_identity) == []


@pytest.mark.parametrize(
    "safe_text",
    [
        "secretary approved the release",
        "token_count=3",
        "tokenCount: 3",
        "token-count = 3",
        "The workspace supports ordinary team planning.",
        "This provider offers a public service.",
    ],
)
def test_v2_forged_public_text_allows_safe_counters_and_business_neighbors(
    safe_text: str,
) -> None:
    """Catches second-boundary normalization overreach against public text."""
    event = DomainEvent.new(
        "agent.message.delta",
        "engine_host.v2",
        {"content": safe_text, "term_id": "term-1", "cursor": 1},
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )
    assert map_domain_event(event)[0]["delta"] == safe_text


@pytest.mark.parametrize("private_phrase", _PRIVATE_PUBLIC_PHRASES)
def test_v2_persisted_summary_rejects_unambiguous_private_phrase_before_agui(
    private_phrase: str,
) -> None:
    """Catches a forged persisted summary exposing private text through AG-UI."""
    event = DomainEvent.new(
        "artifact.proposed",
        "engine_host.v2",
        {
            "artifact_id": "artifact-1",
            "summary": f"The {private_phrase} remains internal.",
            "term_id": "term-1",
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "credential_label",
    [
        style
        for parts, compact in _COMPACT_CREDENTIAL_LABELS
        for style in _credential_styles(parts, compact)
    ],
)
def test_v2_persisted_public_text_rejects_compact_credential_labels(
    credential_label: str,
) -> None:
    """Catches compact credentials bypassing the persisted public-text boundary."""
    event = DomainEvent.new(
        "agent.message.delta",
        "engine_host.v2",
        {
            "content": f"{credential_label}=hidden",
            "term_id": "term-1",
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "credential_label",
    [
        style
        for parts, compact in _COMPACT_CREDENTIAL_LABELS
        for style in _credential_styles(parts, compact)
    ],
)
def test_v2_persisted_identity_and_artifact_ref_reject_compact_credentials(
    credential_label: str,
) -> None:
    """Catches compact credentials in persisted identity or artifact references."""
    unsafe_identifier = f"{credential_label}-1"
    event = DomainEvent.new(
        "agent.tool.completed",
        "engine_host.v2",
        {
            "tool_id": "search",
            "tool_call_id": "call-1",
            "read_only": True,
            "artifact_ref": unsafe_identifier,
            "term_id": unsafe_identifier,
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )
    assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "safe_neighbor",
    ["apikeyboard", "accesstokens", "privatekeynote", "clientsecrets", "githubpattern"],
)
def test_v2_compact_label_expansion_allows_exact_persisted_neighbors(
    safe_neighbor: str,
) -> None:
    """Catches compact matching overreach at the persisted boundary."""
    event = DomainEvent.new(
        "agent.message.delta",
        "engine_host.v2",
        {
            "content": f"{safe_neighbor}=3",
            "term_id": f"{safe_neighbor}-1",
            "cursor": 1,
        },
        run_id="run-1",
        step_id="step-1",
        sequence=1,
    )
    assert map_domain_event(event)[0]["delta"] == f"{safe_neighbor}=3"
