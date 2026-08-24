#!/usr/bin/env python3
"""Deterministic Batch 3.1 story-to-animation acceptance gate."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
import asyncio
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import AgentProfileRepository
from workbench.api.conversations import (
    AgentBindingRequest,
    ConversationAPI,
    ProjectContextBindingRequest,
)
from workbench.artifacts.store import ArtifactStore
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.processor import DurableSequentialProcessor
from workbench.orchestration.project_context import (
    ProjectContextEntry,
    ProjectContextRepository,
)
from workbench.providers.repository import ProviderRepository
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.workflow.engine import SingleAgentEngine
from workbench.workflow.event_store import EventStore


EXACT_PROMPT = (
    "@产品经理 写一篇200字小说 "
    "@Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理 "
    "@架构师 改写成一个动画html "
    "@Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师"
)

ORDERED_AGENTS = ("product-manager", "supervisor", "architect", "verifier")


class BaselineRunner:
    """Deterministic model double that exercises the real orchestration runtime."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.prompts: dict[str, list[str]] = {}
        self.crash_architect_once = True

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()

    async def run_turn(
        self, command: RunAgentTurn
    ) -> AsyncIterator[AgentEvent]:
        agent_id = command.session_id.rsplit(":", 1)[-1]
        attempt = int(command.command_id.rsplit(":", 1)[-1])
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        self.prompts.setdefault(agent_id, []).append(command.prompt)

        if agent_id == "architect" and self.crash_architect_once:
            self.crash_architect_once = False
            raise RuntimeError("simulated process crash")

        if agent_id == "product-manager":
            # Identical output on both attempts deliberately emits a durable
            # no-progress warning; that warning must never become a retry cap.
            output = (
                "海边小城的少年捡到一枚沉睡的星钥。夜里，星钥唤醒旧灯塔中的飞船，"
                "也引来追逐光芒的风暴。少年和守塔人修好破损的引擎，在全城停电前把"
                "能量送回电网。黎明时，飞船邀请他远航，他却先留下新的航标，约定等"
                "家乡每盏灯都亮起，再去群星之间寻找答案。"
            )
        elif agent_id == "architect":
            output = (
                "<html><body><main>静态故事页</main></body></html>"
                if attempt == 1
                else "<!doctype html><html><head><meta charset='utf-8'><style>"
                "@keyframes fly{0%{transform:translateX(0)}100%{transform:translateX(160px)}}"
                ".ship{display:inline-block;animation:fly 1.2s infinite alternate}"
                "</style></head><body><main><h1>星钥启航</h1>"
                "<div class='ship' aria-label='飞船动画'>🚀</div></main></body></html>"
            )
        else:
            target = re.search(r"reviewed_node_id=([^\n]+)", command.prompt)
            reviewed = re.search(r"reviewed_attempt=(\d+)", command.prompt)
            if target is None or reviewed is None:
                raise AssertionError("review contract is missing its target")
            rejected = attempt == 1
            output = json.dumps(
                {
                    "reviewed_node_id": target.group(1),
                    "reviewed_attempt": int(reviewed.group(1)),
                    "decision": "rejected" if rejected else "approved",
                    "findings": ["未达到验收条件"] if rejected else [],
                    "evidence_refs": [f"evidence.{agent_id}.{attempt}"],
                    "rework_instructions": "按验收条件返工" if rejected else None,
                },
                ensure_ascii=False,
            )

        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": output},
        )
        yield AgentEvent(
            kind="turn_finished",
            session_id=command.session_id,
            run_id=command.run_id,
        )


def _configure(database: Path) -> None:
    providers = ProviderRepository(database)
    providers.save(
        ProviderProfileRecord(
            id="lmstudio",
            name="LM Studio",
            protocol="openai",
            base_url="http://127.0.0.1:1234/v1",
        )
    )
    providers.save(ProviderProfileRecord.deepseek(id="deepseek-primary"))
    agents = AgentProfileRepository(database)
    for agent_id, display_name, role, provider_id, model in (
        ("product-manager", "产品经理", "worker", "lmstudio", "local-agent"),
        (
            "supervisor",
            "Supervisor",
            "supervisor",
            "deepseek-primary",
            "deepseek-v4-flash",
        ),
        ("architect", "架构师", "worker", "deepseek-primary", "deepseek-v4-flash"),
        ("verifier", "Verifier", "verifier", "lmstudio", "local-agent"),
    ):
        agents.create(
            AgentProfileWrite(
                agent_id=agent_id,
                display_name=display_name,
                role=role,
                provider_id=provider_id,
                model=model,
            )
        )
    ProjectContextRepository(database).publish(
        "story-project",
        expected_version=0,
        entries=[
            ProjectContextEntry(
                key="story-requirements",
                value_ref="artifact:story-requirements",
                source_ref="artifact:story-requirements",
                verification_status="verified",
                visibility="shared",
            )
        ],
    )


def _api(database: Path, runner: BaselineRunner) -> ConversationAPI:
    return ConversationAPI(
        conversations=ConversationRepository(database),
        events=EventStore(database),
        runner=runner,
        engine=SingleAgentEngine(database, runner=runner, owner_id="baseline-gate"),
        agents=AgentProfileRepository(database),
        graph_control=GraphControlStore(database),
        project_contexts=ProjectContextRepository(database),
    )


def _private_context_leaks(runner: BaselineRunner) -> list[str]:
    leaks: list[str] = []
    for agent_id, prompts in runner.prompts.items():
        for prompt in prompts:
            if EXACT_PROMPT in prompt:
                leaks.append(f"{agent_id}:raw_user_prompt")
            if "github_pat_" in prompt or "DATA_PLATFORM_TOKEN=" in prompt:
                leaks.append(f"{agent_id}:credential_marker")
    return sorted(set(leaks))


async def run_baseline(runtime_dir: Path) -> dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    database = runtime_dir / "workbench.sqlite"
    _configure(database)
    runner = BaselineRunner()
    api = _api(database, runner)
    api.create_session("baseline-session")
    accepted = await api.enqueue_message(
        session_id="baseline-session",
        command_id="story-animation",
        content=EXACT_PROMPT,
        model="default",
        agent_bindings=tuple(
            AgentBindingRequest(agent_id=agent_id, expected_version=1)
            for agent_id in ORDERED_AGENTS
        ),
        project_context=ProjectContextBindingRequest(
            project_id="story-project", version=1
        ),
    )
    conversations = ConversationRepository(database)
    turn = conversations.load_turn_status("baseline-session", "story-animation")
    if turn is None:
        raise AssertionError("parent turn was not persisted")
    orchestration = dict(turn.state["orchestration"])

    first = DurableSequentialProcessor(database=database, runner=runner)
    try:
        try:
            await first.process(orchestration)
        except RuntimeError as error:
            if str(error) != "simulated process crash":
                raise
    finally:
        await first.aclose()
    upstream_before_restart = {
        agent_id: runner.calls.get(agent_id, 0)
        for agent_id in ("product-manager", "supervisor")
    }

    restarted = DurableSequentialProcessor(database=database, runner=runner)
    result = await restarted.process(orchestration)
    upstream_after_restart = {
        agent_id: runner.calls.get(agent_id, 0)
        for agent_id in ("product-manager", "supervisor")
    }
    repeated = [
        agent_id
        for agent_id in upstream_before_restart
        if upstream_after_restart[agent_id] != upstream_before_restart[agent_id]
    ]

    projecting_api = _api(database, runner)
    projecting_api.sequential_processor = restarted
    claimed = conversations.claim_next_turn(
        owner_id="baseline-projector", lease_seconds=30
    )
    if claimed is None:
        raise AssertionError("parent turn could not be claimed")
    await projecting_api.process_queued_turn(claimed.session_id, claimed.command_id)

    events = EventStore(database).read_stream("run:baseline-session")
    reviews = [
        str(event.payload["decision"])
        for event in events
        if event.event_type == "orchestration.review.decided"
    ]
    html_events = [
        event
        for event in events
        if event.event_type == "orchestration.artifact.published"
        and event.payload.get("media_type") == "text/html"
    ]
    html_valid = False
    if len(html_events) == 1:
        artifact = ArtifactStore(database, runtime_dir / "artifacts").open(
            str(html_events[0].payload["artifact_id"])
        )
        html = (artifact.content or b"").decode("utf-8", errors="replace")
        html_valid = (
            artifact.valid
            and "<html" in html.casefold()
            and "@keyframes" in html.casefold()
            and "animation:" in html.casefold()
        )
    context = orchestration["project_context"]
    source_refs = sorted(
        {str(entry["source_ref"]) for entry in context.get("entries", [])}
    )
    ordered = [
        str(node["binding"]["agent_id"])
        for node in orchestration["draft"]["nodes"]
    ]
    terminal_count = sum(
        event.event_type in {"conversation.turn.finished", "conversation.turn.failed"}
        for event in events
    )
    warning_count = sum(
        event.event_type == "orchestration.warning"
        and event.payload.get("code") == "orchestration.review.no_progress"
        for event in events
    )
    gate = {
        "schema_version": 1,
        "scenario": "story-to-animation-sequential-review",
        "prompt_digest": "sha256:"
        + hashlib.sha256(EXACT_PROMPT.encode("utf-8")).hexdigest(),
        "graph_run_id": accepted["graph_run_id"],
        "ordered_agents": ordered,
        "review_decisions": reviews,
        "private_context_leaks": _private_context_leaks(runner),
        "project_context_versions": [int(context["version"])],
        "project_context_sources": source_refs,
        "restart_repeated_approved_nodes": repeated,
        "no_progress_warning_count": warning_count,
        "html_artifact_is_sandboxable": html_valid,
        "parent_terminal_events": terminal_count,
        "runner_calls": dict(sorted(runner.calls.items())),
        "decision": "GO_RESEARCH_GRAPH",
    }
    requirements = (
        ordered == list(ORDERED_AGENTS),
        reviews == ["rejected", "approved", "rejected", "approved"],
        not gate["private_context_leaks"],
        gate["project_context_versions"] == [1],
        source_refs == ["artifact:story-requirements"],
        not repeated,
        warning_count >= 1,
        html_valid,
        terminal_count == 1,
        result.status == "completed",
    )
    if not all(requirements):
        gate["decision"] = "BLOCKED"
    await restarted.aclose()
    return gate


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/sequential-multi-agent-results.json"),
    )
    args = parser.parse_args()
    runtime_dir = args.runtime_dir or Path(".runtime/gates") / str(uuid4())
    result = await run_baseline(runtime_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["decision"] == "GO_RESEARCH_GRAPH" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
