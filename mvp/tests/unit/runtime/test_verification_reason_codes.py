import importlib.util
import json
from pathlib import Path
import sys

import pytest

from workbench.credentials.models import VaultInUseError, VaultUnlockError
from workbench.models.lmstudio import ProviderResponseError
from workbench.runtime.deepseek_harness.source_gate import SourceReadinessError


@pytest.mark.parametrize("error,reason", [(VaultUnlockError("private"), "vault_unlock_failed"),
    (VaultInUseError("private"), "vault_in_use"), (ValueError("private"), "runtime_verification_failed"),
    (ProviderResponseError("private"), "provider_request_failed"),
    (SourceReadinessError("private"), "runtime_build_unavailable")])
def test_cli_emits_only_safe_blocked_reason_on_stdout(monkeypatch, capsys, error, reason):
    path = Path(__file__).resolve().parents[3] / "scripts/verify_runtime_live_endpoint.py"
    spec = importlib.util.spec_from_file_location("reason_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_arguments", lambda: None)
    async def fail(_):
        raise error
    monkeypatch.setattr(module, "_verify", fail)
    assert module.main() == 1
    out = capsys.readouterr()
    assert json.loads(out.out) == {"status": "blocked", "reason_code": reason}
    assert out.err == ""
    assert "private" not in out.out
