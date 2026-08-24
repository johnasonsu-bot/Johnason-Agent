from __future__ import annotations

import pytest


def test_development_job_resume_is_session_scoped_and_idempotent(tmp_path) -> None:
    from workbench.orchestration.development_jobs import DevelopmentJobRepository
    from workbench.conversations.repository import ConversationRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    ConversationRepository(database).create_session("session-b")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a")
    jobs.mark_needs_human(
        "development-run.1",
        interrupt_id="release.1",
        interrupt_kind="release_approval",
        interrupt_payload={"kind": "release_approval"},
    )

    first = jobs.request_resume(
        "development-run.1", "session-a", {"decision": "approved"}, "release.1"
    )
    second = jobs.request_resume(
        "development-run.1", "session-a", {"decision": "approved"}, "release.1"
    )

    assert first.status == second.status == "queued"
    with pytest.raises(KeyError):
        jobs.request_resume(
            "development-run.1", "session-b", {"decision": "approved"}, "release.1"
        )
