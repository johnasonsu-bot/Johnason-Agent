"""Allowlisted projection from Engine Host v2 runtime events to domain events."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
import re
from typing import Any, Callable

from workbench.protocol.events import DomainEvent
from workbench.runtime.engine_host.v2.contracts import RuntimeEventV2


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[A-Za-z0-9]+")
_LABEL_TOKEN = re.compile(r"[A-Za-z0-9]+|[:=]")
_CANONICAL_COMPACT_LABELS = {
    "apikey": ("api", "key"),
    "apitoken": ("api", "token"),
    "accesskey": ("access", "key"),
    "accesstoken": ("access", "token"),
    "privatekey": ("private", "key"),
    "clientsecret": ("client", "secret"),
    "secretkey": ("secret", "key"),
    "authtoken": ("auth", "token"),
    "bearertoken": ("bearer", "token"),
    "githubpat": ("github", "pat"),
    "chainofthought": ("chain", "of", "thought"),
    "privateprompt": ("private", "prompt"),
    "privatehistory": ("private", "history"),
}
_CREDENTIAL_VALUE = re.compile(
    r"(?:github_pat_|gh[pousr]_|sk-|AKIA)[A-Za-z0-9_-]+|"
    r"\bbearer(?:\s+|\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
_DIGEST_VALUE = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{40})(?![0-9a-f])",
    re.IGNORECASE,
)
_UNSAFE_PATH = re.compile(
    r"(?:^|[\\/])\.\.(?:[\\/]|$)|"
    r"(?:^|\s)/[^\s]{1,}|"
    r"(?:^|\s)[A-Za-z]:[\\/][^\s]*|"
    r"(?:^|\s)\\\\[^\s\\]+\\[^\s\\]+"
)
_TRACEBACK = re.compile(r"\btraceback\s*\(", re.IGNORECASE)
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
    ("client", "secret"),
    ("secret", "key"),
    ("auth", "token"),
    ("bearer", "token"),
    ("github", "pat"),
    ("authorization",),
    ("password",),
    ("passwd",),
)
_FORBIDDEN_PUBLIC_PHRASES = (
    ("chain", "of", "thought"),
    ("private", "prompt"),
    ("private", "history"),
)
_INTERNAL_PROOF_PHRASES = (
    ("context", "proof"),
    ("manifest", "proof"),
    ("workspace", "proof"),
    ("capability", "proof"),
    ("checkpoint", "proof"),
)
_DIAGNOSTIC_LABELS = (
    ("exception",),
    ("error",),
    ("stack",),
    ("stack", "trace"),
    ("traceback",),
)
_ASSIGNMENT_LABELS = (*_SENSITIVE_ROOTS, *_DIAGNOSTIC_LABELS, ("token",))
_MAX_ASSIGNMENT_LABEL_WORDS = max(len(label) for label in _ASSIGNMENT_LABELS)
_SENSITIVE_METADATA_SUFFIXES = frozenset(
    {
        "content",
        "prompt",
        "history",
        "id",
        "ref",
        "reference",
        "path",
        "digest",
        "key",
        "token",
        "private",
    }
)
_TOKEN_SAFE_SUFFIXES = frozenset({"count"})
_TOKEN_SENSITIVE_SUFFIXES = frozenset({"id", "ref", "reference", "key", "token", "private", "secret"})
_STATUSES = frozenset({"queued", "running", "paused", "completed", "failed", "cancelled"})
_TOOL_RESULT_STATUSES = frozenset({"completed", "failed"})
_STATE_OPERATIONS = frozenset({"add", "remove", "replace", "update"})
_PUBLIC_ERROR_CODES = frozenset(
    {
        "runtime_error",
        "invalid_request",
        "capacity_unavailable",
        "capability_unavailable",
        "policy_rejected",
        "rate_limited",
        "timeout",
        "unavailable",
    }
)
_GENERIC_PUBLIC_ERROR_CODE = "runtime_error"


def is_opaque_identifier(value: Any) -> bool:
    """Return whether an identifier is bounded and cannot carry a secret."""
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or _DIGEST_VALUE.fullmatch(value)
        or _UNSAFE_PATH.search(value)
    ):
        return False
    words = _normalized_words(value)
    if not words or _is_safe_token_counter(words):
        return bool(words)
    return not (
        _contains_phrase(words, ("sk",))
        or _contains_any_phrase(words, _SENSITIVE_ROOTS)
        or _contains_sensitive_metadata_label(words)
        or _contains_token_sensitive_suffix(words)
        or words[0] == "digest"
        or _contains_any_phrase(words, _INTERNAL_PROOF_PHRASES)
    )


def validate_public_text(value: Any, *, maximum: int) -> str:
    """Validate the one text policy shared by runtime and AG-UI boundaries."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL.search(value)
        or _contains_private_public_label(value)
        or _contains_credential_value(value)
        or _DIGEST_VALUE.search(value)
        or _UNSAFE_PATH.search(value)
        or _TRACEBACK.search(value)
    ):
        raise ValueError("value must be bounded public text")
    return value


def _normalized_words(value: str) -> tuple[str, ...]:
    """Split identifier styles into one lowercase semantic representation."""
    return tuple(
        word
        for match in _WORD.finditer(value)
        for word in _normalized_token_words(match.group(0))
    )


def _normalized_token_words(value: str) -> tuple[str, ...]:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(" ", value)
    separated = _CAMEL_WORD_BOUNDARY.sub(" ", separated)
    normalized = tuple(part.lower() for part in separated.split())
    return tuple(
        expanded
        for word in normalized
        for expanded in _CANONICAL_COMPACT_LABELS.get(word, (word,))
    )


def _contains_phrase(words: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(words[index : index + width] == phrase for index in range(len(words) - width + 1))


def _contains_any_phrase(
    words: tuple[str, ...], phrases: tuple[tuple[str, ...], ...]
) -> bool:
    return any(_contains_phrase(words, phrase) for phrase in phrases)


def _contains_sensitive_metadata_label(words: tuple[str, ...]) -> bool:
    for root in _SENSITIVE_ROOTS:
        width = len(root)
        for index in range(len(words) - width):
            if words[index : index + width] == root and words[index + width] in _SENSITIVE_METADATA_SUFFIXES:
                return True
    return False


def _contains_token_sensitive_suffix(words: tuple[str, ...]) -> bool:
    return any(
        words[index] == "token" and words[index + 1] in _TOKEN_SENSITIVE_SUFFIXES
        for index in range(len(words) - 1)
    )


def _is_safe_token_counter(words: tuple[str, ...]) -> bool:
    return len(words) == 2 and words[0] == "token" and words[1] in _TOKEN_SAFE_SUFFIXES


def _ends_with_phrase(words: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return len(words) >= len(phrase) and words[-len(phrase) :] == phrase


def _contains_sensitive_assignment(value: str) -> bool:
    trailing_words: deque[str] = deque(maxlen=_MAX_ASSIGNMENT_LABEL_WORDS)
    for match in _LABEL_TOKEN.finditer(value):
        token = match.group(0)
        if token == ":" or token == "=":
            label_words = tuple(trailing_words)
            if _is_safe_token_counter(label_words):
                continue
            if any(
                _ends_with_phrase(label_words, label)
                for label in _ASSIGNMENT_LABELS
            ):
                return True
        else:
            trailing_words.extend(_normalized_token_words(token))
    return False


def _contains_private_public_label(value: str) -> bool:
    words = _normalized_words(value)
    return (
        _contains_any_phrase(words, _FORBIDDEN_PUBLIC_PHRASES)
        or _contains_any_phrase(words, _INTERNAL_PROOF_PHRASES)
        or _contains_sensitive_metadata_label(words)
        or _contains_token_sensitive_suffix(words)
        or _contains_sensitive_assignment(value)
    )


def _contains_credential_value(value: str) -> bool:
    return bool(_CREDENTIAL_VALUE.search(value))


def _is_sensitive_payload_key(value: str) -> bool:
    words = _normalized_words(value)
    if _is_safe_token_counter(words):
        return False
    return (
        _contains_any_phrase(words, _SENSITIVE_ROOTS)
        or _contains_token_sensitive_suffix(words)
        or _contains_phrase(words, ("token",))
        or _contains_phrase(words, ("digest",))
    )


def is_public_text(value: Any, *, maximum: int) -> bool:
    try:
        validate_public_text(value, maximum=maximum)
    except ValueError:
        return False
    return True


def map_runtime_event(event: RuntimeEventV2) -> tuple[DomainEvent, ...]:
    """Map one normalized event without copying its untrusted payload.

    Unknown optional extensions are retained solely as a private diagnostic.  A
    caller cannot turn one into a public event by selecting a convenient name.
    """
    identity = _identity(event)
    payload = _payload(event)
    _reject_sensitive_payload(payload)

    projector = _PROJECTORS.get(identity.runtime_type)
    if projector is None:
        if identity.required:
            raise ValueError("required runtime event type is not registered")
        return (_domain_event(identity, "runtime.extension.observed", {}),)
    return (_domain_event(identity, *projector(payload)),)


class _Identity:
    def __init__(
        self,
        *,
        event_id: str,
        run_id: str,
        term_id: str,
        step_id: str,
        cursor: int,
        runtime_type: str,
        required: bool,
    ) -> None:
        self.event_id = event_id
        self.run_id = run_id
        self.term_id = term_id
        self.step_id = step_id
        self.cursor = cursor
        self.runtime_type = runtime_type
        self.required = required


def _identity(event: RuntimeEventV2) -> _Identity:
    values = {
        name: getattr(event, name, None)
        for name in ("event_id", "run_id", "term_id", "step_id")
    }
    if any(not is_opaque_identifier(value) for value in values.values()):
        raise ValueError("runtime event identity must be bounded opaque identifiers")
    cursor = getattr(event, "cursor", None)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 1:
        raise ValueError("runtime event cursor must be a positive integer")
    runtime_type = getattr(event, "type", None)
    if not isinstance(runtime_type, str) or not runtime_type or len(runtime_type) > 128:
        raise ValueError("runtime event type must be a bounded string")
    required = getattr(event, "required", None)
    if not isinstance(required, bool):
        raise ValueError("runtime event required flag must be boolean")
    return _Identity(cursor=cursor, runtime_type=runtime_type, required=required, **values)  # type: ignore[arg-type]


def _payload(event: RuntimeEventV2) -> Mapping[str, Any]:
    value = getattr(event, "payload", None)
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("runtime event payload must be an object with string keys")
    return value


def _reject_sensitive_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or _is_sensitive_payload_key(key):
                raise ValueError("runtime event payload contains a sensitive field")
            _reject_sensitive_payload(nested)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _reject_sensitive_payload(nested)
    elif isinstance(value, str) and (
        _contains_credential_value(value)
        or _DIGEST_VALUE.search(value)
        or _UNSAFE_PATH.search(value)
        or _contains_any_phrase(_normalized_words(value), _INTERNAL_PROOF_PHRASES)
    ):
        raise ValueError("runtime event payload contains a sensitive value")
    elif value is not None and (isinstance(value, bool) or isinstance(value, (int, float, str))):
        return
    elif value is not None:
        raise ValueError("runtime event payload must contain JSON values")


def _domain_event(
    identity: _Identity, event_type: str, public_payload: dict[str, Any]
) -> DomainEvent:
    payload = {"term_id": identity.term_id, "cursor": identity.cursor, **public_payload}
    return DomainEvent(
        event_id=identity.event_id,
        event_type=event_type,
        source="engine_host.v2",
        occurred_at=datetime.now(timezone.utc),
        run_id=identity.run_id,
        step_id=identity.step_id,
        sequence=identity.cursor,
        payload=payload,
    )


def _identifier(payload: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not is_opaque_identifier(value):
        raise ValueError(f"{key} must be a bounded opaque identifier")
    return value


def _public_text(payload: Mapping[str, Any], key: str, *, required: bool = False, maximum: int = 4096) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    try:
        return validate_public_text(value, maximum=maximum)
    except ValueError as error:
        raise ValueError(f"{key} must be bounded public text") from error


def _strict_int(payload: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return value


def _allow_only(payload: Mapping[str, Any], *allowed: str) -> None:
    unexpected = set(payload).difference(allowed)
    if unexpected:
        raise ValueError("runtime event payload contains unapproved fields")


def _tool_fields(payload: Mapping[str, Any], *, result: bool) -> tuple[str, dict[str, Any]]:
    _allow_only(
        payload,
        "tool_id",
        "tool_call_id",
        "call_id",
        "read_only",
        "name",
        "summary",
        "artifact_ref",
        "effect_id",
        "status",
    )
    tool_id = _identifier(payload, "tool_id")
    wire_call_id = _identifier(payload, "tool_call_id", required=False)
    alias_call_id = _identifier(payload, "call_id", required=False)
    if wire_call_id is None and alias_call_id is None:
        raise ValueError("tool call must include a call id")
    if wire_call_id is not None and alias_call_id is not None and wire_call_id != alias_call_id:
        raise ValueError("tool call id aliases disagree")
    call_id = wire_call_id or alias_call_id
    read_only = payload.get("read_only")
    if not isinstance(read_only, bool):
        raise ValueError("read_only must be boolean")
    value: dict[str, Any] = {
        "tool_id": tool_id,
        "tool_call_id": call_id,
        "read_only": read_only,
    }
    name = _public_text(payload, "name", maximum=128)
    summary = _public_text(payload, "summary", maximum=280)
    artifact_ref = _identifier(payload, "artifact_ref", required=False)
    if summary is not None:
        value["summary"] = summary
    if name is not None:
        value["tool_name"] = name
    if artifact_ref is not None:
        value["artifact_ref"] = artifact_ref
    if result:
        status = payload.get("status")
        if not isinstance(status, str) or status not in _TOOL_RESULT_STATUSES:
            raise ValueError("tool result status must be completed or failed")
        value["status"] = status
        return "agent.tool.completed", value
    return "agent.tool.started", value


def _state_fields(payload: Mapping[str, Any], *, collection: str, delta: bool) -> tuple[str, dict[str, Any]]:
    _allow_only(
        payload,
        "version",
        "base_version",
        "operation",
        "snapshot",
        "delta",
        "plan_id",
        "item_id",
        "summary",
    )
    version = _strict_int(payload, "version", minimum=1)
    shape = payload.get("delta" if delta else "snapshot")
    expected = (Mapping,) if collection == "plan" else (tuple, list)
    if not isinstance(shape, expected):
        raise ValueError(("delta" if delta else "snapshot") + " must have the required JSON shape")
    value: dict[str, Any] = {"version": version}
    if delta:
        base_version = _strict_int(payload, "base_version", minimum=1)
        if base_version >= version:
            raise ValueError("base_version must precede version")
        operation = payload.get("operation")
        if not isinstance(operation, str) or operation not in _STATE_OPERATIONS:
            raise ValueError("operation must be an allowed state operation")
        value.update(base_version=base_version, operation=operation)
    for key in ("plan_id", "item_id"):
        item = _identifier(payload, key, required=False)
        if item is not None:
            value[key] = item
    summary = _public_text(payload, "summary", maximum=280)
    if summary is not None:
        value["summary"] = summary
    return (f"run.{collection}.delta" if delta else f"run.{collection}.snapshot", value)


def _project_user_message(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "content")
    return "user.message.received", {"content": _public_text(payload, "content", required=True)}


def _project_assistant_delta(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "text", "content")
    text = _public_text(payload, "text") or _public_text(payload, "content", required=True)
    return "agent.message.delta", {"content": text}


def _project_assistant_message(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "content")
    return "agent.message.completed", {"content": _public_text(payload, "content", required=True)}


def _project_reasoning_delta(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "char_count")
    count = _strict_int(payload, "char_count", minimum=0) if "char_count" in payload else 1
    return "runtime.reasoning.observed", {"count": count}


def _project_intervention(payload: Mapping[str, Any], event_type: str) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "intervention_id", "summary")
    value = {"intervention_id": _identifier(payload, "intervention_id")}
    summary = _public_text(payload, "summary", maximum=280)
    if summary is not None:
        value["summary"] = summary
    return event_type, value


def _project_artifact(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "artifact_id", "summary", "media_type")
    value = {"artifact_id": _identifier(payload, "artifact_id")}
    summary = _public_text(payload, "summary", maximum=280)
    if summary is not None:
        value["summary"] = summary
    media_type = _public_text(payload, "media_type", maximum=128)
    if media_type is not None:
        value["media_type"] = media_type
    return "artifact.proposed", value


def _project_runtime_status(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "status")
    status = payload.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("runtime status is not registered")
    return "runtime.status.changed", {"status": status}


def _project_error(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    _allow_only(payload, "code", "summary")
    return "runtime.error", {
        "code": canonical_public_error_code(payload.get("code")),
        "summary": _public_text(payload, "summary", required=True, maximum=280),
    }


def canonical_public_error_code(value: Any) -> str:
    """Map runtime/provider codes to the closed public error vocabulary."""
    if not isinstance(value, str) or not value:
        raise ValueError("runtime error code must be a non-empty string")
    return value if value in _PUBLIC_ERROR_CODES else _GENERIC_PUBLIC_ERROR_CODE


_PROJECTORS: dict[str, Callable[[Mapping[str, Any]], tuple[str, dict[str, Any]]]] = {
    "user.message": _project_user_message,
    "assistant.delta": _project_assistant_delta,
    "assistant.message": _project_assistant_message,
    "reasoning.delta": _project_reasoning_delta,
    "tool.call": lambda payload: _tool_fields(payload, result=False),
    "tool.result": lambda payload: _tool_fields(payload, result=True),
    "plan.snapshot": lambda payload: _state_fields(payload, collection="plan", delta=False),
    "plan.delta": lambda payload: _state_fields(payload, collection="plan", delta=True),
    "todo.snapshot": lambda payload: _state_fields(payload, collection="todo", delta=False),
    "todo.delta": lambda payload: _state_fields(payload, collection="todo", delta=True),
    "intervention.requested": lambda payload: _project_intervention(payload, "intervention.requested"),
    "intervention.applied": lambda payload: _project_intervention(payload, "intervention.applied"),
    "artifact.proposed": _project_artifact,
    "runtime.status": _project_runtime_status,
    "error": _project_error,
}
