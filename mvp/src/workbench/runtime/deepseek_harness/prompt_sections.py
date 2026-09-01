"""Deterministic Host-v2 PromptSection mapping for DeepSeek Harness.

The pinned DSH ``system-prompt`` service accepts ``name``, ``order`` and
``text`` registrations, then performs a stable ascending sort by ``order``.
This bridge establishes the deterministic insertion order required by the
Host-v2 PromptSection contract and returns secret-free per-Step evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any


PROMPT_SECTION_DIGEST_SCHEMA = "workbench.runtime.dsh.prompt_sections.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = 2**53 - 1


class PromptSectionBridgeError(ValueError):
    """A normalized PromptSection cannot be mapped safely to DSH."""


@dataclass(frozen=True)
class DeepSeekPromptSection:
    """One normalized, frozen PromptSection snapshot for a single Step."""

    section_id: str
    namespace: str
    priority: int
    stable_order: int
    content: str | None
    content_reference: str | None
    visibility: str
    mutable: bool
    source_digest: str

    def __post_init__(self) -> None:
        for label, value in (
            ("section ID", self.section_id),
            ("namespace", self.namespace),
            ("visibility", self.visibility),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise PromptSectionBridgeError(f"invalid prompt section {label}")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise PromptSectionBridgeError("prompt section priority must be an integer")
        if abs(self.priority) > _MAX_SAFE_INTEGER:
            raise PromptSectionBridgeError("prompt section priority exceeds DSH range")
        if (
            not isinstance(self.stable_order, int)
            or isinstance(self.stable_order, bool)
            or not 0 <= self.stable_order <= _MAX_SAFE_INTEGER
        ):
            raise PromptSectionBridgeError(
                "prompt section stable order must be a non-negative safe integer"
            )
        if not isinstance(self.mutable, bool):
            raise PromptSectionBridgeError("prompt section mutable flag must be boolean")
        if not isinstance(self.source_digest, str) or _DIGEST.fullmatch(
            self.source_digest
        ) is None:
            raise PromptSectionBridgeError("invalid prompt section source digest")
        if (self.content is None) == (self.content_reference is None):
            raise PromptSectionBridgeError(
                "prompt section requires exactly one of content or content reference"
            )
        if self.content is not None and (
            not isinstance(self.content, str) or self.content == ""
        ):
            raise PromptSectionBridgeError("prompt section content must be non-empty text")
        if self.content_reference is not None and (
            not isinstance(self.content_reference, str) or self.content_reference == ""
        ):
            raise PromptSectionBridgeError(
                "prompt section content reference must be non-empty text"
            )

    @property
    def dsh_name(self) -> str:
        """Return the collision-resistant DSH registration name."""

        return f"{self.namespace}:{self.section_id}"


@dataclass(frozen=True)
class PromptStepEvidence:
    """Inspectable evidence returned for the final per-Step prompt snapshot."""

    section_order: tuple[str, ...]
    prompt_digest: str


@dataclass(frozen=True)
class PromptSectionAssembly:
    """DSH registrations plus the evidence retained by the control plane."""

    registrations: tuple[Mapping[str, Any], ...]
    evidence: PromptStepEvidence


class PromptSectionBridge:
    """Map normalized sections into the pinned DSH system-prompt seam."""

    def assemble(
        self, sections: Sequence[DeepSeekPromptSection]
    ) -> PromptSectionAssembly:
        normalized = tuple(sections)
        if any(not isinstance(section, DeepSeekPromptSection) for section in normalized):
            raise PromptSectionBridgeError(
                "prompt section bridge accepts normalized sections only"
            )
        ordered = tuple(
            sorted(
                normalized,
                key=lambda section: (
                    section.priority,
                    section.stable_order,
                    section.section_id,
                    section.namespace,
                ),
            )
        )
        names = tuple(section.dsh_name for section in ordered)
        if len(names) != len(set(names)):
            raise PromptSectionBridgeError("duplicate prompt section DSH name")
        unresolved = next(
            (section for section in ordered if section.content_reference is not None),
            None,
        )
        if unresolved is not None:
            raise PromptSectionBridgeError(
                f'prompt section content reference is unresolved: "{unresolved.dsh_name}"'
            )

        digest_document = {
            "schema": PROMPT_SECTION_DIGEST_SCHEMA,
            "sections": [asdict(section) for section in ordered],
        }
        canonical = json.dumps(
            digest_document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        registrations = tuple(
            MappingProxyType(
                {
                    "name": section.dsh_name,
                    "order": section.priority,
                    "text": section.content,
                }
            )
            for section in ordered
        )
        return PromptSectionAssembly(
            registrations=registrations,
            evidence=PromptStepEvidence(
                section_order=names,
                prompt_digest=hashlib.sha256(canonical).hexdigest(),
            ),
        )
