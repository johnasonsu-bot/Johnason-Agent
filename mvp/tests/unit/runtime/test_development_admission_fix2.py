import importlib.util
import sys
from pathlib import Path

import pytest

from workbench.models.profiles import ProviderProfileRecord
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.client import EngineHostV2Client, RuntimeCapabilityError


@pytest.mark.parametrize("model", [False, True])
def test_model_only_query_requires_model_but_not_workspace(model):
    client = EngineHostV2Client((sys.executable,))
    envelope = run_envelope(overrides={"tool_manifest": (), "skill_pins": (), "plugin_pins": (),
        "checkpoint_cursor": 0, "workspace_grant.readable_paths": (), "workspace_grant.writable_paths": (),
        "context_budget": {"max_input_tokens": 4096, "reserved_output_tokens": 0,
            "protected_message_ids": (), "protected_prompt_section_ids": (),
            "compaction_policy": "none", "summary_ref": None}})
    client._capabilities = runtime_capabilities(envelope.runtime.runtime_id,
        build_id=envelope.runtime.build_id, query=True, streaming=True, event_cursor=True,
        model=model, workspace=False)
    if model:
        client._validate_capabilities(envelope)
    else:
        with pytest.raises(RuntimeCapabilityError, match="model"):
            client._validate_capabilities(envelope)


@pytest.mark.parametrize("mode", ["none", "reference"])
def test_explicit_local_profile_is_classified_for_formal_verification(mode):
    path = Path(__file__).resolve().parents[3] / "scripts/federated_runtime_dev_signer.py"
    spec = importlib.util.spec_from_file_location("fix2_signer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    profile = ProviderProfileRecord(id="local-primary", name="Local", protocol="openai_chat",
        base_url="http://127.0.0.1:1234", credential_mode=mode,
        secret_id="provider/local" if mode == "reference" else None,
        model_aliases={"default": "local-model"})
    assert module._real_endpoint_kind(profile) == "local"
