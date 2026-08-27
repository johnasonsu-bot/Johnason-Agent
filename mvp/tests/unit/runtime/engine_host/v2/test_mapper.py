from types import SimpleNamespace
from pathlib import Path
from time import perf_counter

import pytest

from tests.fixtures.host_v2 import runtime_event
from workbench.runtime.engine_host.v2 import mapper as mapper_module
from workbench.runtime.engine_host.v2.mapper import map_runtime_event
from workbench.runtime.engine_host.v2.contracts import RuntimeEventV2
from workbench.agui.mapper import map_domain_event
from workbench.agui.stream import replay_agui
from workbench.workflow.event_store import EventStore


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
    ("runtime_type", "payload", "domain_type"),
    [
        ("user.message", {"content": "hello"}, "user.message.received"),
        ("assistant.delta", {"text": "hello"}, "agent.message.delta"),
        ("assistant.message", {"content": "hello"}, "agent.message.completed"),
        ("reasoning.delta", {"char_count": 5}, "runtime.reasoning.observed"),
        ("tool.call", {"tool_id": "search", "tool_call_id": "call-1", "read_only": True}, "agent.tool.started"),
        ("tool.result", {"tool_id": "search", "tool_call_id": "call-1", "read_only": True, "status": "completed"}, "agent.tool.completed"),
        ("plan.snapshot", {"version": 1, "snapshot": {}}, "run.plan.snapshot"),
        ("plan.delta", {"version": 2, "base_version": 1, "operation": "replace", "delta": {}}, "run.plan.delta"),
        ("todo.snapshot", {"version": 1, "snapshot": []}, "run.todo.snapshot"),
        ("todo.delta", {"version": 2, "base_version": 1, "operation": "replace", "delta": []}, "run.todo.delta"),
        ("intervention.requested", {"intervention_id": "intervention-1", "summary": "review"}, "intervention.requested"),
        ("intervention.applied", {"intervention_id": "intervention-1", "summary": "review"}, "intervention.applied"),
        ("artifact.proposed", {"artifact_id": "artifact-1", "summary": "report"}, "artifact.proposed"),
        ("runtime.status", {"status": "running"}, "runtime.status.changed"),
        ("error", {"code": "runtime_error", "summary": "request failed"}, "runtime.error"),
    ],
)
def test_maps_every_registered_runtime_event(
    runtime_type: str, payload: dict[str, object], domain_type: str
) -> None:
    """Catches a declared runtime type that lacks a public projection."""
    mapped = map_runtime_event(runtime_event(runtime_type, payload=payload))

    assert [item.event_type for item in mapped] == [domain_type]
    assert mapped[0].run_id == "run-1"
    assert mapped[0].step_id == "step-1"
    assert mapped[0].sequence == 1
    assert mapped[0].payload["term_id"] == "term-1"
    assert mapped[0].payload["cursor"] == 1


def test_rejects_sensitive_payload_when_a_forged_event_bypasses_contract_validation() -> None:
    """Catches model internals leaking a credential-shaped field into a projector."""
    event = SimpleNamespace(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="reasoning.delta",
        payload={"reasoning_content": "private", "api_key": "forbidden"},
        required=False,
    )

    with pytest.raises(ValueError, match="sensitive"):
        map_runtime_event(event)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("runtime_type", "payload"),
    [
        ("plan.delta", {"version": 1, "operation": "replace", "delta": {}}),
        ("todo.delta", {"version": 2, "base_version": 1, "delta": []}),
        ("plan.snapshot", {"snapshot": {}}),
        ("todo.snapshot", {"version": 1}),
    ],
)
def test_rejects_unversioned_or_incomplete_state_projection(
    runtime_type: str, payload: dict[str, object]
) -> None:
    """Catches malformed snapshot or delta state entering the event stream."""
    with pytest.raises(ValueError, match="version|operation|snapshot"):
        map_runtime_event(runtime_event(runtime_type, payload=payload))


def test_optional_unknown_event_only_yields_private_diagnostic() -> None:
    """Catches optional extensions changing the public runtime event state."""
    mapped = map_runtime_event(
        runtime_event("vendor.trace", payload={"diagnostic": "safe"})
    )

    assert [event.event_type for event in mapped] == ["runtime.extension.observed"]
    assert mapped[0].payload == {"term_id": "term-1", "cursor": 1}


def test_required_unknown_event_is_rejected_even_if_it_was_forged() -> None:
    """Catches a required extension becoming silently observable as optional."""
    event = SimpleNamespace(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="vendor.required",
        payload={},
        required=True,
    )

    with pytest.raises(ValueError, match="required"):
        map_runtime_event(event)  # type: ignore[arg-type]


def test_tool_call_id_alias_is_normalized_without_projecting_effect_metadata() -> None:
    """Catches a supported call-id spelling or private effect ID changing tool output."""
    mapped = map_runtime_event(
        runtime_event(
            "tool.call",
            payload={
                "tool_id": "search",
                "call_id": "call-1",
                "read_only": False,
                "effect_id": "effect-1",
            },
        )
    )

    assert mapped[0].payload == {
        "term_id": "term-1",
        "cursor": 1,
        "tool_id": "search",
        "tool_call_id": "call-1",
        "read_only": False,
    }


def test_reasoning_payload_rejects_unallowlisted_content_even_when_not_secret_shaped() -> None:
    """Catches private chain text being accepted only because its key is innocuous."""
    with pytest.raises(ValueError, match="unapproved"):
        map_runtime_event(runtime_event("reasoning.delta", payload={"text": "private"}))


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
def test_rejects_private_text_variants_from_every_public_runtime_text_field(
    unsafe_text: str,
) -> None:
    """Catches a private diagnostic variant being projected as public text."""
    with pytest.raises(ValueError):
        map_runtime_event(runtime_event("assistant.delta", payload={"text": unsafe_text}))
    with pytest.raises(ValueError):
        map_runtime_event(
            runtime_event("artifact.proposed", payload={"artifact_id": "artifact-1", "summary": unsafe_text})
        )


@pytest.mark.parametrize("safe_text", ["3 records found", "The public report is ready."])
def test_accepts_ordinary_public_text(safe_text: str) -> None:
    """Catches public-text validation becoming broad enough to reject normal output."""
    mapped = map_runtime_event(runtime_event("assistant.delta", payload={"text": safe_text}))
    assert mapped[0].payload["content"] == safe_text


@pytest.mark.parametrize("private_phrase", _PRIVATE_PUBLIC_PHRASES)
def test_runtime_public_summary_rejects_unambiguous_private_phrase_in_body(
    private_phrase: str,
) -> None:
    """Catches an unlabelled private phrase crossing the runtime public boundary."""
    with pytest.raises(ValueError, match="bounded public text"):
        map_runtime_event(
            runtime_event(
                "artifact.proposed",
                payload={
                    "artifact_id": "artifact-1",
                    "summary": f"The {private_phrase} remains internal.",
                },
            )
        )


@pytest.mark.parametrize(
    "event",
    [
        SimpleNamespace(
            event_id="event-1", run_id="run-1", term_id="term-1", step_id="step-1",
            cursor=1, type=[], payload={}, required=False,
        ),
        SimpleNamespace(
            event_id="event-1", run_id="run-1", term_id="term-1", step_id="step-1",
            cursor=1, type="runtime.status", payload={"status": []}, required=False,
        ),
    ],
)
def test_malformed_runtime_type_or_status_raises_stable_value_error(event: SimpleNamespace) -> None:
    """Catches unhashable attacker values escaping as TypeError from a projector."""
    with pytest.raises(ValueError):
        map_runtime_event(event)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "sensitive_identifier",
    [
        "provider-ref-1", "workspace_path-1", "manifestDigest-1", "reasoning-id-1",
        "vault-id-1", "secretToken-1", "credentialId-1", "sk-abcdefghijklmnop", "bearer-private",
    ],
)
def test_rejects_sensitive_identifier_variants_at_runtime_boundary(sensitive_identifier: str) -> None:
    """Catches sensitive camel/snake/kebab identifier prefixes becoming public IDs."""
    event = SimpleNamespace(
        event_id="event-1", run_id=sensitive_identifier, term_id="term-1", step_id="step-1",
        cursor=1, type="assistant.delta", payload={"text": "hello"}, required=False,
    )
    with pytest.raises(ValueError, match="identity"):
        map_runtime_event(event)  # type: ignore[arg-type]


@pytest.mark.parametrize("safe_identifier", ["providers-1", "workspaces-1", "manifestation-1", "digestive-1", "vaulted-1"])
def test_accepts_ordinary_identifier_neighbors(safe_identifier: str) -> None:
    """Catches secret-prefix protection rejecting ordinary opaque identifiers."""
    mapped = map_runtime_event(
        runtime_event(
            "tool.call",
            payload={"tool_id": "search", "tool_call_id": "call-1", "read_only": True, "artifact_ref": safe_identifier},
        )
    )
    assert mapped[0].payload["artifact_ref"] == safe_identifier


@pytest.mark.parametrize(
    "sensitive_identifier",
    [
        "manifestRef-1", "manifest_ref-1", "manifest-reference-1",
        "workspaceRef-1", "workspace_reference-1", "vault-ref-1", "vaultReference-1",
    ],
)
def test_rejects_sensitive_reference_identifier_variants_in_runtime_fields(
    sensitive_identifier: str,
) -> None:
    """Catches manifest/workspace/vault reference IDs entering public runtime fields."""
    event = SimpleNamespace(
        event_id="event-1", run_id="run-1", term_id=sensitive_identifier, step_id="step-1",
        cursor=1, type="artifact.proposed", payload={"artifact_id": sensitive_identifier}, required=False,
    )
    with pytest.raises(ValueError, match="identity"):
        map_runtime_event(event)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        map_runtime_event(
            runtime_event(
                "tool.call",
                payload={"tool_id": "search", "tool_call_id": "call-1", "read_only": True, "artifact_ref": sensitive_identifier},
            )
        )


@pytest.mark.parametrize("safe_identifier", ["manifestation-ref-1", "workspaces-note-1", "vaulted-reference-1"])
def test_accepts_non_sensitive_reference_neighbors(safe_identifier: str) -> None:
    """Catches reference hardening rejecting normal opaque IDs with similar words."""
    mapped = map_runtime_event(
        runtime_event("artifact.proposed", payload={"artifact_id": safe_identifier})
    )
    assert mapped[0].payload["artifact_id"] == safe_identifier


@pytest.mark.parametrize(
    ("root", "suffix"),
    [(root, suffix) for root in _SENSITIVE_ROOTS for suffix in _SENSITIVE_METADATA_SUFFIXES],
)
def test_runtime_public_summaries_reject_normalized_sensitive_root_suffix_combinations(
    root: tuple[str, ...], suffix: str
) -> None:
    """Catches any supported word boundary bypassing a sensitive summary label."""
    for style in _LABEL_STYLES:
        unsafe = _styled_sensitive_label(root, suffix, style) + ": hidden"
        with pytest.raises(ValueError):
            map_runtime_event(
                runtime_event(
                    "artifact.proposed",
                    payload={"artifact_id": "artifact-1", "summary": unsafe},
                )
            )


@pytest.mark.parametrize(
    ("root", "suffix"),
    [(root, suffix) for root in _SENSITIVE_ROOTS for suffix in _SENSITIVE_METADATA_SUFFIXES],
)
def test_runtime_artifact_refs_reject_normalized_sensitive_prefix_combinations(
    root: tuple[str, ...], suffix: str
) -> None:
    """Catches sensitive opaque metadata prefixes crossing the artifact-ref boundary."""
    for style in _IDENTIFIER_STYLES:
        unsafe = _styled_sensitive_label(root, suffix, style) + "-1"
        forged_identity = SimpleNamespace(
            event_id="event-1",
            run_id=unsafe,
            term_id="term-1",
            step_id="step-1",
            cursor=1,
            type="assistant.delta",
            payload={"text": "hello"},
            required=False,
        )
        with pytest.raises(ValueError, match="identity"):
            map_runtime_event(forged_identity)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            map_runtime_event(
                runtime_event(
                    "tool.call",
                    payload={
                        "tool_id": "search",
                        "tool_call_id": "call-1",
                        "read_only": True,
                        "artifact_ref": unsafe,
                    },
                )
            )


@pytest.mark.parametrize(
    "unsafe",
    ["token=hidden", "token-ref-1", "vault-private", "sk-short", "bearerValue"],
)
def test_runtime_boundaries_reject_explicit_credential_and_private_identifier_shapes(
    unsafe: str,
) -> None:
    """Catches credential assignments and strict private opaque-ID prefixes."""
    if "=" in unsafe:
        with pytest.raises(ValueError):
            map_runtime_event(runtime_event("assistant.delta", payload={"text": unsafe}))
    else:
        with pytest.raises(ValueError):
            map_runtime_event(
                runtime_event(
                    "tool.call",
                    payload={
                        "tool_id": "search",
                        "tool_call_id": "call-1",
                        "read_only": True,
                        "artifact_ref": unsafe,
                    },
                )
            )


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
def test_runtime_public_text_allows_safe_counters_and_business_neighbors(safe_text: str) -> None:
    """Catches normalization overreach against ordinary public prose and counters."""
    mapped = map_runtime_event(runtime_event("assistant.delta", payload={"text": safe_text}))
    assert mapped[0].payload["content"] == safe_text


@pytest.mark.parametrize(
    "safe_identifier",
    ["token_count", "tokenCount", "token-count", "secretary-1", "manifestation-ref-1", "vaulted-reference-1"],
)
def test_runtime_opaque_ids_allow_safe_counters_and_lexical_neighbors(
    safe_identifier: str,
) -> None:
    """Catches strict opaque validation rejecting explicitly safe adjacent terms."""
    mapped = map_runtime_event(
        runtime_event("artifact.proposed", payload={"artifact_id": safe_identifier})
    )
    assert mapped[0].payload["artifact_id"] == safe_identifier


@pytest.mark.parametrize(
    "credential_label",
    [
        style
        for parts, compact in _COMPACT_CREDENTIAL_LABELS
        for style in _credential_styles(parts, compact)
    ],
)
def test_runtime_public_text_rejects_compact_credential_labels(
    credential_label: str,
) -> None:
    """Catches compact credential acronyms bypassing runtime public text."""
    with pytest.raises(ValueError):
        map_runtime_event(
            runtime_event(
                "assistant.delta", payload={"text": f"{credential_label}=hidden"}
            )
        )


@pytest.mark.parametrize(
    "credential_label",
    [
        style
        for parts, compact in _COMPACT_CREDENTIAL_LABELS
        for style in _credential_styles(parts, compact)
    ],
)
def test_runtime_identity_and_artifact_ref_reject_compact_credential_labels(
    credential_label: str,
) -> None:
    """Catches compact credentials crossing runtime identity or artifact refs."""
    unsafe_identifier = f"{credential_label}-1"
    forged_identity = SimpleNamespace(
        event_id="event-1",
        run_id=unsafe_identifier,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.delta",
        payload={"text": "hello"},
        required=False,
    )
    with pytest.raises(ValueError, match="identity"):
        map_runtime_event(forged_identity)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        map_runtime_event(
            runtime_event(
                "tool.call",
                payload={
                    "tool_id": "search",
                    "tool_call_id": "call-1",
                    "read_only": True,
                    "artifact_ref": unsafe_identifier,
                },
            )
        )


@pytest.mark.parametrize(
    "safe_neighbor",
    [
        "apikeyboard",
        "apitokens",
        "accesskeys",
        "accesstokens",
        "privatekeynote",
        "clientsecrets",
        "secretkeys",
        "authtokens",
        "bearertokens",
        "githubpattern",
    ],
)
def test_runtime_compact_label_expansion_allows_exact_lexical_neighbors(
    safe_neighbor: str,
) -> None:
    """Catches exact compact-label expansion becoming arbitrary word splitting."""
    text = f"{safe_neighbor}=3"
    mapped_text = map_runtime_event(
        runtime_event("assistant.delta", payload={"text": text})
    )
    mapped_identifier = map_runtime_event(
        runtime_event(
            "artifact.proposed", payload={"artifact_id": f"{safe_neighbor}-1"}
        )
    )
    assert mapped_text[0].payload["content"] == text
    assert mapped_identifier[0].payload["artifact_id"] == f"{safe_neighbor}-1"


def test_public_text_allows_more_than_32_ordinary_assignments() -> None:
    """Catches a complexity shortcut rejecting semantically safe public text."""
    text = " ".join(f"note{index}=ready" for index in range(33))

    assert mapper_module.validate_public_text(text, maximum=4096) == text


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
def test_runtime_public_values_reject_digests_paths_and_internal_proofs(
    unsafe_text: str,
) -> None:
    """Catches internal proof material crossing the first public boundary."""
    with pytest.raises(ValueError, match="sensitive value|public text"):
        map_runtime_event(runtime_event("assistant.delta", payload={"text": unsafe_text}))


@pytest.mark.parametrize(
    "local_path",
    [
        "path=/private/state",
        r"path=C:\private\state.json",
        r"artifact=\\host\share\state.json",
        "artifact:C:/private/state",
        'artifact: "/private/state"',
        "artifact=(C:/private/state)",
        "//private/state",
        "///private/state",
        r"artifact=\\\host\share\state.json",
        r"artifact=\\\\host\share\state.json",
        r"artifact=\\host\\share\state.json",
        r"artifact=\/host\\share/state.json",
        r"artifact=\private/state.json",
        r"artifact=\\host/share\state.json",
        "../state.json",
    ],
)
def test_runtime_public_boundary_rejects_local_paths_at_label_and_punctuation_boundaries(
    local_path: str,
) -> None:
    """Catches local paths hidden after assignments, punctuation, or labels."""
    with pytest.raises(ValueError):
        mapper_module.validate_public_text(local_path, maximum=4096)
    with pytest.raises(ValueError):
        map_runtime_event(runtime_event("assistant.delta", payload={"text": local_path}))


@pytest.mark.parametrize(
    "hostile",
    [
        r'https://example.com";artifact=C:\private\state.json',
        "https://example.com;artifact=C:/private/state.json",
    ],
)
def test_runtime_public_boundary_rejects_local_path_after_http_url(hostile: str) -> None:
    """Catches a local path hidden by an overbroad HTTP URL span."""
    with pytest.raises(ValueError, match="sensitive value|public text"):
        map_runtime_event(runtime_event("assistant.delta", payload={"text": hostile}))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/private/state",
        "http://127.0.0.1:46121/api/v1",
        "https://example.com/docs/a;b?x=1#ok",
    ],
)
def test_runtime_public_boundary_allows_http_urls_with_path_segments(url: str) -> None:
    """Catches path hardening accidentally rejecting legitimate public URLs."""
    assert mapper_module.validate_public_text(url, maximum=4096) == url
    mapped = map_runtime_event(runtime_event("assistant.delta", payload={"text": url}))
    assert mapped[0].payload["content"] == url


def test_runtime_public_boundary_allows_relative_text_with_path_separators() -> None:
    """Catches rooted-path hardening rejecting ordinary relative public text."""
    text = "report stored at artifacts/current/report.txt"

    assert mapper_module.validate_public_text(text, maximum=4096) == text


@pytest.mark.parametrize(
    ("runtime_code", "public_code"),
    [
        ("runtime_error", "runtime_error"),
        ("capacity_unavailable", "capacity_unavailable"),
        ("provider_overloaded_in_region_7", "runtime_error"),
        ("unknown_write_effect", "runtime_error"),
    ],
)
def test_runtime_error_codes_use_the_public_canonical_allowlist(
    runtime_code: str, public_code: str
) -> None:
    """Catches provider-specific or reconciliation codes entering public events."""
    mapped = map_runtime_event(
        runtime_event(
            "error", payload={"code": runtime_code, "summary": "request failed"}
        )
    )[0]

    assert mapped.payload["code"] == public_code


def test_maximum_public_text_uses_one_full_normalization_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches assignment scanning that repeatedly tokenizes growing prefixes."""
    original = mapper_module._normalized_words
    normalization_calls = 0

    def counted_normalization(value: str) -> tuple[str, ...]:
        nonlocal normalization_calls
        normalization_calls += 1
        return original(value)

    monkeypatch.setattr(mapper_module, "_normalized_words", counted_normalization)
    prefix = "note=" * 32
    remaining = 4096 - len(prefix)
    maximum_text = prefix + ("aA" * ((remaining + 1) // 2))[:remaining]

    started = perf_counter()
    result = mapper_module.validate_public_text(maximum_text, maximum=4096)
    elapsed = perf_counter() - started

    assert result == maximum_text
    assert normalization_calls == 1
    assert elapsed < 0.25


@pytest.mark.asyncio
async def test_runtime_emitters_project_equivalently_and_resume_without_duplicates(tmp_path: Path) -> None:
    """Catches runtime-specific fields changing public projection or SSE resume."""
    def emit_python(cursor: int) -> RuntimeEventV2:
        return RuntimeEventV2(
            event_id=f"event-{cursor}", run_id="run-1", term_id="term-1", step_id="step-1",
            cursor=cursor, type="assistant.delta", payload={"text": "shared output"}, required=False,
        )

    def emit_fake_goose(cursor: int) -> RuntimeEventV2:
        goose_ndjson_record = {
            "event_id": f"event-{cursor}", "run_id": "run-1", "term_id": "term-1", "step_id": "step-1",
            "cursor": cursor, "type": "assistant.delta", "payload": {"text": "shared output"}, "required": False,
        }
        return RuntimeEventV2.model_validate(goose_ndjson_record)

    def emit_fake_dsh(cursor: int) -> RuntimeEventV2:
        dsh_source_event = {
            "event": {"id": f"event-{cursor}", "run": "run-1", "term": "term-1", "step": "step-1"},
            "offset": cursor, "kind": "assistant.delta", "body": {"text": "shared output"},
        }
        return RuntimeEventV2.model_validate(
            {
                "event_id": dsh_source_event["event"]["id"],
                "run_id": dsh_source_event["event"]["run"],
                "term_id": dsh_source_event["event"]["term"],
                "step_id": dsh_source_event["event"]["step"],
                "cursor": dsh_source_event["offset"],
                "type": dsh_source_event["kind"],
                "payload": dsh_source_event["body"],
                "required": False,
            }
        )

    projections = []
    for emitter in (emit_python, emit_fake_goose, emit_fake_dsh):
        event = map_runtime_event(emitter(1))[0]
        wire = map_domain_event(event)[0]
        projections.append(
            {
                key: wire[key]
                for key in ("type", "runId", "termId", "stepId", "cursor", "delta")
            }
        )
    assert projections == [
        {
            "type": "TEXT_MESSAGE_CONTENT",
            "runId": "run-1",
            "termId": "term-1",
            "stepId": "step-1",
            "cursor": 1,
            "delta": "shared output",
        }
    ] * 3

    store = EventStore(tmp_path / "events.sqlite")
    for cursor in (1, 2):
        store.append(
            map_runtime_event(emit_python(cursor))[0],
            command_id=f"event-{cursor}",
        )
    persisted = store.read_stream("step:step-1", after_sequence=1)
    replayed = [event async for event in replay_agui(persisted, after_sequence=1)]
    assert [event["cursor"] for event in replayed] == [2]
