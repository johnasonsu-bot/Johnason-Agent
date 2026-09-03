"""One canonical, secret-free RuntimeQueryInputV2 for cross-lane tests."""

from __future__ import annotations

from workbench.runtime.engine_host.v2.contracts import (
    RuntimeContextItemV2,
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)


def canonical_runtime_query_input_v2() -> RuntimeQueryInputV2:
    """Build the exact materialized input shared by all fixed smoke lanes."""

    messages = (
        RuntimeMessageInputV2(
            message_id="message-1",
            role="user",
            content="materialized hello",
        ),
    )
    context_items = (
        RuntimeContextItemV2(
            item_id="context-1",
            kind="document",
            content="bounded fixture context",
        ),
    )
    prompt_sections = (
        RuntimePromptSectionInputV2(
            section_id="section-1",
            order=10,
            content="fixed smoke system section",
        ),
    )
    return RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=context_items,
        context_snapshot_digest=canonical_runtime_input_digest(context_items),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )
