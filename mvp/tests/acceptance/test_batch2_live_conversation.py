"""Opt-in live gate for the durable multi-turn conversation API.

Run only after configuring the local model and temporarily supplying a
DeepSeek credential outside this repository:

  HERMES_RUN_LIVE_CONVERSATION=1 \
  HERMES_LMSTUDIO_MODEL=<loaded-model> \
  DEEPSEEK_API_KEY=<entered-just-in-time> \
  .venv/bin/python -m pytest tests/acceptance/test_batch2_live_conversation.py -v
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from workbench.main import build_app
from workbench.settings import WorkbenchSettings


pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_RUN_LIVE_CONVERSATION") != "1",
    reason="live model gate is opt-in; set HERMES_RUN_LIVE_CONVERSATION=1 after Provider Center setup",
)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required when the live conversation gate is enabled")
    return value


def _create_provider(
    client: TestClient,
    *,
    provider_id: str,
    name: str,
    protocol: str,
    base_url: str,
    model: str,
    credential: str | None = None,
) -> None:
    profile = {
        "id": provider_id,
        "name": name,
        "protocol": protocol,
        "base_url": base_url,
        "model_aliases": {"default": model},
        "enabled": True,
        "thinking_enabled": protocol == "deepseek",
    }
    assert client.post("/api/providers", json=profile).status_code == 201
    if credential is not None:
        saved = client.post(f"/api/providers/{provider_id}/secret", json={"value": credential})
        assert saved.status_code == 200


@pytest.mark.parametrize("provider", ["lmstudio", "deepseek"])
def test_real_provider_completes_two_durable_conversation_turns(
    tmp_path: Path, provider: str
) -> None:
    """Exercise a real model twice and retain only public AG-UI event projections."""
    lmstudio_model = _require("HERMES_LMSTUDIO_MODEL") if provider == "lmstudio" else "unused"
    deepseek_key = _require("DEEPSEEK_API_KEY") if provider == "deepseek" else None
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        local_model_base_url=os.environ.get("HERMES_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234"),
    )

    with TestClient(build_app(settings)) as client:
        assert client.post("/api/vault/create", json={"password": uuid4().hex}).status_code == 200
        if provider == "lmstudio":
            _create_provider(
                client,
                provider_id="lmstudio-live",
                name="LM Studio live",
                protocol="lmstudio",
                base_url=settings.local_model_base_url,
                model=lmstudio_model,
            )
        else:
            _create_provider(
                client,
                provider_id="deepseek-live",
                name="DeepSeek live",
                protocol="deepseek",
                base_url="https://api.deepseek.com/v1",
                model="deepseek-v4-flash",
                credential=deepseek_key,
            )

        session_id = f"live-{provider}-{uuid4().hex}"
        assert client.post("/api/sessions", json={"session_id": session_id}).status_code == 200
        for turn, prompt in enumerate(("Reply with one word: ready.", "Reply with one word: confirmed."), start=1):
            response = client.post(
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": f"live-{turn}-{uuid4().hex}"},
                json={"content": prompt, "model": "default"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "completed"

        replay = client.get(f"/api/sessions/{session_id}/events")
        assert replay.status_code == 200
        assert replay.text.count('"name": "turn_finished"') >= 2
        assert "reasoning_content" not in replay.text
