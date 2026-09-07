import asyncio

import pytest

from workbench.artifacts.store import ArtifactStore
from workbench.credentials.service import VaultService
from workbench.credentials.models import VaultLockedError
from workbench.runtime.engine_host.v2.artifact_tools import ARTIFACT_TOOL_MANIFEST
from workbench.runtime.engine_host.v2.platform_tool_pool import PlatformToolPool, StableToolDigestKey
from workbench.runtime.engine_host.v2.tool_transport import PlatformToolCall
from tests.unit.runtime.engine_host.v2.test_platform_tools import _host_context


def test_digest_key_survives_vault_reopen_without_plaintext_database_key(tmp_path):
    vault = VaultService(tmp_path / "vault")
    vault.create("test-only-passphrase")
    key = StableToolDigestKey(vault, tmp_path / "state.sqlite")
    first = key.service().digest({"request": "same"})
    vault.lock()
    reopened = VaultService(tmp_path / "vault")
    with pytest.raises(VaultLockedError):
        StableToolDigestKey(reopened, tmp_path / "state.sqlite").service()
    reopened.unlock("test-only-passphrase")
    assert StableToolDigestKey(reopened, tmp_path / "state.sqlite").service().digest({"request": "same"}) == first
    stored = reopened.get(StableToolDigestKey.SECRET_ID)
    assert stored.encode() not in (tmp_path / "state.sqlite").read_bytes()
    reopened.lock()


def test_missing_previously_initialized_key_is_not_replaced(tmp_path):
    vault = VaultService(tmp_path / "vault")
    vault.create("test-only-passphrase")
    StableToolDigestKey(vault, tmp_path / "state.sqlite").service()
    vault.delete(StableToolDigestKey.SECRET_ID)
    with pytest.raises(RuntimeError, match="unavailable"):
        StableToolDigestKey(vault, tmp_path / "state.sqlite").service()
    with pytest.raises(KeyError):
        vault.get(StableToolDigestKey.SECRET_ID)
    vault.lock()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["goose", "dsh"])
async def test_pool_routes_actual_artifact_write_and_reuses_effect(tmp_path, runtime):
    manifest = ARTIFACT_TOOL_MANIFEST[0]
    context, envelope = _host_context(tmp_path, runtime, manifest)
    vault = VaultService(tmp_path / "vault")
    vault.create("test-only-passphrase")
    pool = PlatformToolPool(
        database=tmp_path / "state.sqlite", artifact_root=tmp_path / "artifacts",
        vault=vault, context_factory=lambda actual: context if actual == envelope else None,
        owner_id="test-host",
    )
    call = PlatformToolCall(envelope, manifest, "publish-1", {
        "filename": "story.html", "media_type": "text/html", "content": "<html><body>story</body></html>",
    }, asyncio.Event())
    result = await pool(call)
    assert result.status == "completed"
    assert result.effect_id
    artifact_id = result.output["artifact_ref"]
    artifact = ArtifactStore(tmp_path / "state.sqlite", tmp_path / "artifacts").open(artifact_id)
    assert artifact.valid
    assert artifact.content == b"<html><body>story</body></html>"
    assert (await pool(call)).effect_id == result.effect_id
    vault.lock()
