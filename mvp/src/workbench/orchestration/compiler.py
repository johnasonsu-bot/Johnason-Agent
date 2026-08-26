"""Deterministic compiler for explicit mention-ordered Agent plans."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    ExecutionPlanDraft,
    SequentialNodeSpec,
)


class UnknownAgentMention(ValueError):
    pass


class InvalidMentionSequence(ValueError):
    pass


@runtime_checkable
class SolutionTemplateCompiler(Protocol):
    def compile_intent(
        self,
        intent: str,
        template_id: str,
        template_version: str,
        bindings: tuple[AgentBindingSnapshot, ...],
    ) -> ExecutionPlanDraft: ...


_MENTION = re.compile(r"@([^\s@]+)")
_TRAILING_PUNCTUATION = "，,。.:：;；!?！？"
_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "supervisor": ("Supervisor", "监督者"),
    "verifier": ("Verifier", "Verfier", "验证者"),
}


class MentionSequenceCompiler:
    """Compile explicit mentions without invoking a planner or model."""

    def compile(
        self,
        content: str,
        bindings: Sequence[AgentBindingSnapshot],
    ) -> ExecutionPlanDraft:
        normalized_content = content.strip()
        if not normalized_content:
            raise InvalidMentionSequence("content must contain an Agent mention")

        binding_by_name = self._binding_names(bindings)
        mentions = list(_MENTION.finditer(normalized_content))
        if not mentions:
            raise InvalidMentionSequence("content must contain an Agent mention")

        resolved: list[tuple[AgentBindingSnapshot, str]] = []
        for index, mention in enumerate(mentions):
            token = mention.group(1).rstrip(_TRAILING_PUNCTUATION)
            binding = binding_by_name.get(token.casefold())
            if binding is None or not binding.enabled:
                raise UnknownAgentMention(token)
            instruction_start = mention.start(1) + len(token)
            instruction_end = (
                mentions[index + 1].start() if index + 1 < len(mentions) else None
            )
            instruction = normalized_content[instruction_start:instruction_end].strip(
                " \t\r\n，,。.;；"
            )
            if not instruction:
                raise InvalidMentionSequence(
                    f"Agent mention {binding.display_name!r} requires an instruction"
                )
            resolved.append((binding, instruction))

        identity = self._identity(normalized_content, bindings)
        plan_id = f"plan.{identity[:32]}"
        nodes: list[SequentialNodeSpec] = []
        for ordinal, (binding, instruction) in enumerate(resolved):
            node_id = "node." + str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"workbench:{identity}:{ordinal}:{binding.agent_id}:{binding.role}",
                )
            )
            review_target_id = None
            if binding.role in {"supervisor", "verifier"}:
                review_target_id = self._review_target(nodes, instruction)
            nodes.append(
                SequentialNodeSpec(
                    node_id=node_id,
                    ordinal=ordinal,
                    kind=binding.role,
                    binding=binding,
                    instruction=instruction,
                    review_target_id=review_target_id,
                )
            )

        return ExecutionPlanDraft(
            plan_id=plan_id,
            goal="Sequential multi-Agent execution",
            nodes=tuple(nodes),
        )

    @staticmethod
    def _binding_names(
        bindings: Sequence[AgentBindingSnapshot],
    ) -> dict[str, AgentBindingSnapshot]:
        names: dict[str, AgentBindingSnapshot] = {}
        for binding in sorted(
            bindings, key=lambda item: len(item.display_name), reverse=True
        ):
            candidates = [binding.display_name]
            candidates.extend(_ROLE_ALIASES.get(binding.role, ()))
            for candidate in candidates:
                key = candidate.casefold()
                existing = names.get(key)
                if existing is not None and existing.agent_id != binding.agent_id:
                    raise InvalidMentionSequence(
                        f"ambiguous Agent mention {candidate!r}"
                    )
                names[key] = binding
        return names

    @staticmethod
    def _review_target(
        preceding: list[SequentialNodeSpec], instruction: str
    ) -> str:
        workers = [node for node in preceding if node.kind == "worker"]
        if not workers:
            raise InvalidMentionSequence("reviewer requires a preceding worker")
        explicit = [
            node
            for node in workers
            if node.binding.display_name in instruction
            or node.binding.agent_id in instruction
        ]
        return (explicit[-1] if explicit else workers[-1]).node_id

    @staticmethod
    def _identity(
        content: str, bindings: Sequence[AgentBindingSnapshot]
    ) -> str:
        payload = {
            "content": content,
            "bindings": sorted(
                (
                    binding.model_dump(mode="json")
                    for binding in bindings
                ),
                key=lambda item: (item["agent_id"], item["profile_version"]),
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
