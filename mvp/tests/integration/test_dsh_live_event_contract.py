import json
from pathlib import Path
import subprocess
import sqlite3
from secrets import token_urlsafe

import pytest

from workbench.runtime.engine_host.v2.contracts import RuntimeEventV2


@pytest.mark.parametrize("size", [32, 128])
def test_dsh_events_fit_python_contract_for_long_formal_identifiers(size):
    root = Path(__file__).resolve().parents[2]
    identity = {field: f"live-{field.removesuffix('_id')}-" + "a" * size for field in ("run_id", "term_id", "step_id")}
    if size == 128:
        identity = {field: value[:128] for field, value in identity.items()}
    script = """
import {mapSessionEvents} from './sidecars/deepseek-harness/dist/event-mapper.mjs';
let text=''; for await (const chunk of process.stdin) text+=chunk;
console.log(JSON.stringify(mapSessionEvents(JSON.parse(text),[
{seq:0,type:'turn/start',data:{}},{seq:1,type:'turn/end',data:{reason:'failed'}}])));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script],
        input=json.dumps(identity), text=True, capture_output=True, check=True, cwd=root)
    events = [RuntimeEventV2.model_validate(item) for item in json.loads(result.stdout)]
    assert events[0].event_id != events[1].event_id
    assert len(events[0].event_id) <= 128


@pytest.mark.asyncio
async def test_actual_dsh_refused_local_request_finishes_without_protocol_retry(tmp_path, monkeypatch):
    from tests.integration.test_runtime_live_endpoint_gate import _signer_module
    from workbench.credentials.service import VaultService
    from workbench.models.profiles import ProviderProfileRecord
    from workbench.providers.repository import ProviderRepository

    signer = _signer_module()
    # Diagnostic-only bypass of live endpoint classification; this closed local
    # port never produces evidence or counts as live GO.
    monkeypatch.setattr(signer, "_real_endpoint_kind", lambda _: "local")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    repository = ProviderRepository(runtime / "workbench.sqlite")
    repository.upsert(ProviderProfileRecord.deepseek(id="diagnostic-primary", base_url="http://127.0.0.1:1"))
    profile = repository.get("diagnostic-primary")
    password = token_urlsafe(24)
    vault = VaultService(runtime / "credentials.vault")
    vault.create(password)
    vault.put(profile.secret_id, token_urlsafe(24))
    try:
        with pytest.raises(signer.RuntimeLiveVerificationError, match="provider_request_failed"):
            await signer.prepare_development_environment(("dsh",), profile.id, runtime, tmp_path / "result", password)
    finally:
        vault.lock()
    database = next((tmp_path / "result").glob("execution-*/federated-runtime-live-dsh.sqlite"))
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from provider_grants_private").fetchone()[0] == 1
    assert not (tmp_path / "result/runtime-live-evidence-dsh.json").exists()
