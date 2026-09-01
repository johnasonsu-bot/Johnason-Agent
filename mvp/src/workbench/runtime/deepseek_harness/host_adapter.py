"""Secret-free preparation boundary between Host v2 and DeepSeek Harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2

from .prompt_sections import (
    DeepSeekPromptSection,
    PromptSectionBridge,
    PromptSectionBridgeError,
)


DEEPSEEK_PREPARED_QUERY_SCHEMA = "workbench.runtime.dsh.prepared_query.v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMAND_IDENTITY_FIELDS = frozenset(
    {
        "protocol_version",
        "runtime_id",
        "build_id",
        "host_generation",
        "session_id",
        "run_id",
        "term_id",
        "step_id",
        "command_id",
        "attempt",
    }
)


class DeepSeekHostAdapterError(ValueError):
    """Host-v2 input cannot be represented safely at the DSH seam."""


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _freeze_registration(value: Mapping[str, object]) -> Mapping[str, object]:
    if set(value) != {"name", "order", "text"}:
        raise DeepSeekHostAdapterError("prompt registration has unknown or missing fields")
    name = value["name"]
    order = value["order"]
    text = value["text"]
    if (
        not isinstance(name, str)
        or not isinstance(order, int)
        or isinstance(order, bool)
        or not isinstance(text, str)
    ):
        raise DeepSeekHostAdapterError("prompt registration is invalid")
    return MappingProxyType({"name": name, "order": order, "text": text})


@dataclass(frozen=True, slots=True)
class DeepSeekPreparedQuery:
    """Immutable, credential-free data sufficient to bind one DSH query."""

    provider_ref: str
    model: str
    model_options_digest: str
    message_snapshot_digest: str
    prompt_registrations: tuple[Mapping[str, object], ...]
    prompt_digest: str
    context_snapshot_ref: str
    context_digest: str
    context_version: int
    tool_manifest_digest: str
    skill_manifest_digest: str
    plugin_manifest_digest: str
    permission_policy_digest: str
    command_identity: Mapping[str, str | int]
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider_ref, str) or not self.provider_ref:
            raise DeepSeekHostAdapterError("provider reference is invalid")
        if not isinstance(self.model, str) or not self.model:
            raise DeepSeekHostAdapterError("model reference is invalid")
        if not isinstance(self.context_snapshot_ref, str) or not self.context_snapshot_ref:
            raise DeepSeekHostAdapterError("context snapshot reference is invalid")
        if not isinstance(self.context_version, int) or isinstance(self.context_version, bool):
            raise DeepSeekHostAdapterError("context version is invalid")
        for value in (
            self.model_options_digest,
            self.message_snapshot_digest,
            self.prompt_digest,
            self.context_digest,
            self.tool_manifest_digest,
            self.skill_manifest_digest,
            self.plugin_manifest_digest,
            self.permission_policy_digest,
            self.evidence_digest,
        ):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise DeepSeekHostAdapterError("prepared query digest is invalid")
        if not isinstance(self.prompt_registrations, tuple):
            raise DeepSeekHostAdapterError("prompt registrations must be a tuple")
        object.__setattr__(
            self,
            "prompt_registrations",
            tuple(_freeze_registration(item) for item in self.prompt_registrations),
        )
        if not isinstance(self.command_identity, Mapping):
            raise DeepSeekHostAdapterError("command identity must be a mapping")
        if set(self.command_identity) != _COMMAND_IDENTITY_FIELDS:
            raise DeepSeekHostAdapterError("command identity has unknown or missing fields")
        identity = dict(self.command_identity)
        if any(
            not isinstance(identity[field], str)
            for field in _COMMAND_IDENTITY_FIELDS - {"attempt"}
        ) or (
            not isinstance(identity["attempt"], int)
            or isinstance(identity["attempt"], bool)
            or identity["attempt"] < 0
        ):
            raise DeepSeekHostAdapterError("command identity is invalid")
        object.__setattr__(self, "command_identity", MappingProxyType(identity))


class DeepSeekHarnessHostAdapter:
    """Prepare, but never execute, a DSH query for one bound runtime build."""

    __slots__ = ("_build_id", "_bridge", "_runtime_id")

    def __init__(self, *, runtime_id: str, build_id: str) -> None:
        if not isinstance(runtime_id, str) or not runtime_id:
            raise DeepSeekHostAdapterError("runtime ID is invalid")
        if not isinstance(build_id, str) or not build_id:
            raise DeepSeekHostAdapterError("build ID is invalid")
        self._runtime_id = runtime_id
        self._build_id = build_id
        self._bridge = PromptSectionBridge()

    def prepare(
        self,
        envelope: RunEnvelopeV2,
        prompt_sections: Sequence[DeepSeekPromptSection],
    ) -> DeepSeekPreparedQuery:
        """Return the canonical frozen DSH representation of one Host-v2 envelope."""

        if not isinstance(envelope, RunEnvelopeV2):
            raise DeepSeekHostAdapterError("adapter accepts RunEnvelopeV2 only")
        if (
            envelope.runtime.runtime_id != self._runtime_id
            or envelope.runtime.build_id != self._build_id
        ):
            raise DeepSeekHostAdapterError("runtime/build identity does not match adapter")
        if not isinstance(prompt_sections, Sequence) or isinstance(
            prompt_sections, str | bytes
        ):
            raise DeepSeekHostAdapterError(
                "adapter accepts normalized prompt sections only"
            )
        try:
            assembly = self._bridge.assemble(prompt_sections)
        except PromptSectionBridgeError as error:
            raise DeepSeekHostAdapterError(str(error)) from error

        identity: dict[str, str | int] = {
            "protocol_version": envelope.protocol_version,
            "runtime_id": envelope.runtime.runtime_id,
            "build_id": envelope.runtime.build_id,
            "host_generation": envelope.runtime.host_generation,
            "session_id": envelope.session_id,
            "run_id": envelope.run_id,
            "term_id": envelope.term_id,
            "step_id": envelope.step_id,
            "command_id": envelope.command_id,
            "attempt": envelope.attempt,
        }
        registrations = tuple(dict(item) for item in assembly.registrations)
        evidence = {
            "schema": DEEPSEEK_PREPARED_QUERY_SCHEMA,
            "provider_ref": envelope.provider_ref,
            "model": envelope.model,
            "model_options_digest": envelope.model_options_digest,
            "message_snapshot_digest": envelope.message_snapshot_digest,
            "prompt_registrations": registrations,
            "prompt_digest": assembly.evidence.prompt_digest,
            "context_snapshot_ref": envelope.context.snapshot_ref,
            "context_digest": envelope.context.snapshot_digest,
            "context_version": envelope.context.version,
            "tool_manifest_digest": envelope.tool_manifest_digest,
            "skill_manifest_digest": envelope.skill_manifest_digest,
            "plugin_manifest_digest": envelope.plugin_manifest_digest,
            "permission_policy_digest": envelope.permission_policy_digest,
            "command_identity": identity,
        }
        return DeepSeekPreparedQuery(
            provider_ref=envelope.provider_ref,
            model=envelope.model,
            model_options_digest=envelope.model_options_digest,
            message_snapshot_digest=envelope.message_snapshot_digest,
            prompt_registrations=registrations,
            prompt_digest=assembly.evidence.prompt_digest,
            context_snapshot_ref=envelope.context.snapshot_ref,
            context_digest=envelope.context.snapshot_digest,
            context_version=envelope.context.version,
            tool_manifest_digest=envelope.tool_manifest_digest,
            skill_manifest_digest=envelope.skill_manifest_digest,
            plugin_manifest_digest=envelope.plugin_manifest_digest,
            permission_policy_digest=envelope.permission_policy_digest,
            command_identity=identity,
            evidence_digest=_canonical_digest(evidence),
        )
