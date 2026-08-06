import base64
import json
import stat
from pathlib import Path

import pytest

from workbench.credentials.models import VaultLockedError, VaultUnlockError
from workbench.credentials.vault import CredentialVault
import workbench.credentials.vault as vault_module


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


def test_vault_reopens_from_a_new_instance_after_a_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()

    reopened = CredentialVault.open(path)

    with pytest.raises(VaultLockedError):
        reopened.get("provider/deepseek")
    reopened.unlock("password")
    assert reopened.get("provider/deepseek") == "secret-value"


def test_vault_create_refuses_to_overwrite_an_existing_vault(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "secret-value")

    with pytest.raises(FileExistsError, match="already exists"):
        CredentialVault.create(path, "other-password")

    assert vault.get("provider/deepseek") == "secret-value"


def test_vault_rejects_valid_json_with_tampered_ciphertext(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()

    document = json.loads(path.read_text(encoding="utf-8"))
    ciphertext = bytearray(base64.b64decode(document["ciphertext"]))
    ciphertext[0] ^= 1
    document["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(VaultUnlockError):
        vault.unlock("password")


def test_vault_rejects_tampered_authenticated_salt_metadata(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()

    document = json.loads(path.read_text(encoding="utf-8"))
    salt = bytearray(base64.b64decode(document["salt"]))
    salt[0] ^= 1
    document["salt"] = base64.b64encode(salt).decode("ascii")
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(VaultUnlockError):
        vault.unlock("password")


@pytest.mark.parametrize("invalid_iterations", [4, True])
def test_vault_rejects_tampered_kdf_metadata_before_key_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_iterations: int | bool
) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.lock()

    document = json.loads(path.read_text(encoding="utf-8"))
    document["kdf"]["iterations"] = invalid_iterations
    path.write_text(json.dumps(document), encoding="utf-8")

    def derivation_must_not_run(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("untrusted KDF parameters reached Argon2id")

    monkeypatch.setattr(vault_module, "_derive_key", derivation_must_not_run)

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


def test_vault_uses_private_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "vault.bin"
    CredentialVault.create(path, "password")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_vault_rolls_back_new_secret_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "persisted-secret")
    persisted_file = path.read_bytes()

    def replace_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(vault_module.os, "replace", replace_failure)

    with pytest.raises(OSError, match="simulated interrupted write"):
        vault.put("provider/openai", "unpersisted-secret")

    assert path.read_bytes() == persisted_file
    assert vault.get("provider/deepseek") == "persisted-secret"
    with pytest.raises(KeyError):
        vault.get("provider/openai")


def test_vault_commits_memory_when_directory_fsync_fails_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "persisted-secret")

    def directory_fsync_failure(_self: CredentialVault) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(
        CredentialVault, "_fsync_parent_directory", directory_fsync_failure
    )

    with pytest.raises(OSError, match="simulated directory fsync failure") as caught:
        vault.put("provider/openai", "committed-secret")

    assert caught.value.committed is True
    assert vault.get("provider/openai") == "committed-secret"
    reopened = CredentialVault.open(path)
    reopened.unlock("password")
    assert reopened.get("provider/openai") == vault.get("provider/openai")


def test_vault_create_removes_its_empty_placeholder_after_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"

    def derivation_failure(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("simulated key derivation failure")

    with monkeypatch.context() as scoped_monkeypatch:
        scoped_monkeypatch.setattr(vault_module, "_derive_key", derivation_failure)
        with pytest.raises(OSError, match="simulated key derivation failure"):
            CredentialVault.create(path, "password")

    assert not path.exists()
    retried = CredentialVault.create(path, "password")
    retried.put("provider/deepseek", "secret-value")
    assert retried.get("provider/deepseek") == "secret-value"


def test_vault_operates_when_fchmod_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    monkeypatch.delattr(vault_module.os, "fchmod")

    vault = CredentialVault.create(path, "password")
    vault.put("provider/deepseek", "secret-value")
    vault.lock()

    reopened = CredentialVault.open(path)
    reopened.unlock("password")
    assert reopened.get("provider/deepseek") == "secret-value"
