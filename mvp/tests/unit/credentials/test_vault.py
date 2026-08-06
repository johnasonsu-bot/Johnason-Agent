from pathlib import Path

import pytest

from workbench.credentials.models import VaultLockedError, VaultUnlockError
from workbench.credentials.vault import CredentialVault


def test_vault_encrypts_and_requires_correct_password(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "correct horse")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()

    assert b"secret-value" not in path.read_bytes()

    with pytest.raises(VaultUnlockError):
        vault.unlock("wrong")

    vault.unlock("correct horse")
    assert vault.get("provider/deepseek") == "secret-value"


def test_vault_rejects_tampered_ciphertext(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()

    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    path.write_bytes(payload)

    with pytest.raises(VaultUnlockError):
        vault.unlock("password")


def test_vault_rejects_access_while_locked(tmp_path: Path) -> None:
    vault = CredentialVault.create(tmp_path / "vault.bin", "password")
    vault.lock()

    with pytest.raises(VaultLockedError):
        vault.put("provider/deepseek", "secret-value")

    with pytest.raises(VaultLockedError):
        vault.get("provider/deepseek")


def test_vault_persists_multiple_secrets_without_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "deepseek-secret")
    vault.put("provider/openai", "openai-secret")
    vault.lock()

    stored = path.read_bytes()
    assert b"deepseek-secret" not in stored
    assert b"openai-secret" not in stored

    vault.unlock("password")
    assert vault.get("provider/deepseek") == "deepseek-secret"
    assert vault.get("provider/openai") == "openai-secret"
