import base64
import json
import secrets
import stat
import subprocess
import sys
from threading import Event, Thread, current_thread
from pathlib import Path

import pytest

from workbench.credentials.models import (
    VaultLockedError,
    VaultPersistenceError,
    VaultUnlockError,
)
from workbench.credentials.service import VaultService
from workbench.credentials.vault import CredentialVault
import workbench.credentials.vault as vault_module


def _recovery_marker_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.recovery.json")


def _recovery_artifact_path(
    path: Path, marker: dict[str, object], name: str
) -> Path:
    artifact = Path(str(marker[name]))
    return artifact if artifact.is_absolute() else path.parent / artifact


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
    expected = vault.get("provider/openai")
    vault.lock()
    reopened = CredentialVault.open(path)
    reopened.unlock("password")
    assert reopened.get("provider/openai") == expected


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


def test_cross_provider_puts_serialize_the_entire_snapshot_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second provider write must not snapshot state while the first is pending."""
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)
    vault = CredentialVault.create(path, password)
    vault.put("provider/original", "original-value")
    first_writing = Event()
    release_first = Event()
    second_attempted = Event()
    second_finished = Event()
    errors: list[BaseException] = []
    original_atomic_write = vault._atomic_write

    def controlled_atomic_write(payload: bytes) -> None:
        if current_thread().name == "first-provider-put":
            first_writing.set()
            if not release_first.wait(5):
                raise AssertionError("first provider write was not released")
        original_atomic_write(payload)

    monkeypatch.setattr(vault, "_atomic_write", controlled_atomic_write)

    def put_first() -> None:
        try:
            vault.put("provider/first", "first-value")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def put_second() -> None:
        second_attempted.set()
        try:
            vault.put("provider/second", "second-value")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            second_finished.set()

    first = Thread(target=put_first, name="first-provider-put")
    second = Thread(target=put_second, name="second-provider-put")
    first.start()
    assert first_writing.wait(5)
    second.start()
    assert second_attempted.wait(5)
    try:
        assert not second_finished.wait(0.2), "second snapshot write was not serialized"
    finally:
        release_first.set()
        first.join(5)
        second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    vault.lock()
    vault.unlock(password)
    assert vault.get("provider/first") == "first-value"
    assert vault.get("provider/second") == "second-value"


def test_cross_provider_put_and_delete_cannot_resurrect_a_deleted_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete must snapshot after a concurrent write instead of being overwritten."""
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)
    vault = CredentialVault.create(path, password)
    vault.put("provider/delete-me", "obsolete-value")
    first_writing = Event()
    release_first = Event()
    delete_attempted = Event()
    delete_finished = Event()
    errors: list[BaseException] = []
    original_atomic_write = vault._atomic_write

    def controlled_atomic_write(payload: bytes) -> None:
        if current_thread().name == "provider-put":
            first_writing.set()
            if not release_first.wait(5):
                raise AssertionError("provider write was not released")
        original_atomic_write(payload)

    monkeypatch.setattr(vault, "_atomic_write", controlled_atomic_write)

    def put_secret() -> None:
        try:
            vault.put("provider/new", "new-value")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def delete_secret() -> None:
        delete_attempted.set()
        try:
            vault.delete("provider/delete-me")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            delete_finished.set()

    writer = Thread(target=put_secret, name="provider-put")
    deleter = Thread(target=delete_secret, name="provider-delete")
    writer.start()
    assert first_writing.wait(5)
    deleter.start()
    assert delete_attempted.wait(5)
    try:
        assert not delete_finished.wait(0.2), "delete snapshot was not serialized"
    finally:
        release_first.set()
        writer.join(5)
        deleter.join(5)

    assert errors == []
    vault.lock()
    vault.unlock(password)
    assert vault.get("provider/new") == "new-value"
    with pytest.raises(KeyError):
        vault.get("provider/delete-me")


def test_lock_waits_for_put_and_wins_the_in_memory_state_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locking during a write must leave the vault locked, never silently reopened."""
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)
    vault = CredentialVault.create(path, password)
    write_pending = Event()
    release_write = Event()
    lock_attempted = Event()
    lock_finished = Event()
    errors: list[BaseException] = []
    original_atomic_write = vault._atomic_write

    def controlled_atomic_write(payload: bytes) -> None:
        if current_thread().name == "pending-put":
            write_pending.set()
            if not release_write.wait(5):
                raise AssertionError("pending put was not released")
        original_atomic_write(payload)

    monkeypatch.setattr(vault, "_atomic_write", controlled_atomic_write)

    def put_secret() -> None:
        try:
            vault.put("provider/new", "new-value")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def lock_vault() -> None:
        lock_attempted.set()
        try:
            vault.lock()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            lock_finished.set()

    writer = Thread(target=put_secret, name="pending-put")
    locker = Thread(target=lock_vault, name="pending-lock")
    writer.start()
    assert write_pending.wait(5)
    locker.start()
    assert lock_attempted.wait(5)
    try:
        assert not lock_finished.wait(0.2), "lock raced ahead of the pending write"
    finally:
        release_write.set()
        writer.join(5)
        locker.join(5)

    assert errors == []
    assert vault.is_unlocked is False
    with pytest.raises(VaultLockedError):
        vault.get("provider/new")
    vault.unlock(password)
    assert vault.get("provider/new") == "new-value"


def test_create_never_publishes_an_empty_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until a complete encrypted document is ready, the target must not exist."""
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)
    deriving = Event()
    release_derivation = Event()
    created: list[CredentialVault] = []
    errors: list[BaseException] = []
    original_derive_key = vault_module._derive_key

    def controlled_derive_key(*args: object, **kwargs: object) -> bytes:
        deriving.set()
        if not release_derivation.wait(5):
            raise AssertionError("key derivation was not released")
        return original_derive_key(*args, **kwargs)

    monkeypatch.setattr(vault_module, "_derive_key", controlled_derive_key)

    def create_vault() -> None:
        try:
            created.append(CredentialVault.create(path, password))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = Thread(target=create_vault, name="vault-create")
    worker.start()
    assert deriving.wait(5)
    try:
        assert not path.exists(), "an incomplete vault target became visible"
    finally:
        release_derivation.set()
        worker.join(10)

    assert errors == []
    assert len(created) == 1
    assert path.stat().st_size > 0
    created[0].lock()


def test_failed_replace_cleans_every_pre_replace_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed snapshot replacement must not accumulate encrypted temp files."""
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, secrets.token_urlsafe(24))
    before = set(path.parent.glob(f".{path.name}.*"))

    def replace_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(vault_module.os, "replace", replace_failure)
    with pytest.raises(OSError, match="simulated interrupted replacement") as caught:
        vault.put("provider/new", "new-value")

    assert caught.value.committed is False
    assert set(path.parent.glob(f".{path.name}.*")) == before


def test_incomplete_startup_state_requires_explicit_recovery(tmp_path: Path) -> None:
    """An empty crash artifact must never masquerade as a normal locked vault."""
    path = tmp_path / "vault.bin"
    path.write_bytes(b"")
    service = VaultService(path)

    assert service.status == "recovery_required"

    password = secrets.token_urlsafe(24)
    service.recover(password)
    assert service.status == "unlocked"
    service.put("provider/recovered", "recovered-value")
    service.lock()
    service.unlock(password)
    assert service.get("provider/recovered") == "recovered-value"


def test_recovery_restart_publishes_complete_replacement_after_primary_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart must finish a durable replacement instead of losing the vault."""
    path = tmp_path / "vault.bin"
    corrupt_input = b"corrupt original vault"
    password = secrets.token_urlsafe(24)
    path.write_bytes(corrupt_input)
    service = VaultService(path)
    original_replace = vault_module.os.replace

    def interrupt_primary_publication(source: object, destination: object) -> None:
        if Path(destination) == path:
            raise OSError("simulated crash before primary publication")
        original_replace(source, destination)

    with monkeypatch.context() as scoped:
        scoped.setattr(vault_module.os, "replace", interrupt_primary_publication)
        with pytest.raises(OSError, match="before primary publication"):
            service.recover(password)

    marker_path = _recovery_marker_path(path)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    replacement_path = _recovery_artifact_path(path, marker, "replacement")
    backup_path = _recovery_artifact_path(path, marker, "backup")
    assert path.read_bytes() == corrupt_input
    assert replacement_path.stat().st_size > 0
    assert backup_path.read_bytes() == corrupt_input

    restarted = VaultService(path)

    assert restarted.status == "locked"
    assert not marker_path.exists()
    assert not replacement_path.exists()
    assert backup_path.read_bytes() == corrupt_input
    restarted.unlock(password)
    assert restarted.status == "unlocked"
    restarted.lock()


def test_recovery_restart_preserves_marker_and_original_for_incomplete_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete candidate must keep explicit recovery state and evidence."""
    path = tmp_path / "vault.bin"
    corrupt_input = b"corrupt original vault"
    password = secrets.token_urlsafe(24)
    path.write_bytes(corrupt_input)
    service = VaultService(path)
    marker_path = _recovery_marker_path(path)
    incomplete_candidates: list[Path] = []

    def interrupt_replacement_write(
        _self: CredentialVault, payload: bytes
    ) -> Path:
        assert marker_path.is_file(), "recovery marker was not durable before replacement"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        candidate = _recovery_artifact_path(path, marker, "replacement")
        candidate.write_bytes(payload[:8])
        incomplete_candidates.append(candidate)
        raise VaultPersistenceError(
            "simulated crash during replacement write", committed=False
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(CredentialVault, "_write_temporary", interrupt_replacement_write)
        with pytest.raises(VaultPersistenceError, match="during replacement write"):
            service.recover(password)

    restarted = VaultService(path)

    assert restarted.status == "recovery_required"
    assert path.read_bytes() == corrupt_input
    assert marker_path.is_file()
    assert len(incomplete_candidates) == 1
    assert incomplete_candidates[0].is_file()

    restarted.recover(password)
    assert restarted.status == "unlocked"
    assert not marker_path.exists()
    restarted.lock()


def test_recovery_restart_after_publication_fsync_keeps_one_encrypted_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published recovery is finalized on restart without leaking its password."""
    path = tmp_path / "vault.bin"
    corrupt_input = b"corrupt original vault"
    password = "recovery-password-must-never-be-persisted"
    path.write_bytes(corrupt_input)
    service = VaultService(path)
    marker_path = _recovery_marker_path(path)
    original_directory_fsync = CredentialVault._fsync_parent_directory

    def interrupt_after_primary_publication(vault: CredentialVault) -> None:
        if (
            marker_path.is_file()
            and path.is_file()
            and path.read_bytes() != corrupt_input
        ):
            raise OSError("simulated crash after primary publication")
        original_directory_fsync(vault)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            CredentialVault,
            "_fsync_parent_directory",
            interrupt_after_primary_publication,
        )
        with pytest.raises(OSError, match="after primary publication"):
            service.recover(password)

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    backup_path = _recovery_artifact_path(path, marker, "backup")
    assert path.read_bytes() != corrupt_input
    assert backup_path.read_bytes() == corrupt_input

    restarted = VaultService(path)

    assert restarted.status == "locked"
    assert not marker_path.exists()
    assert backup_path.read_bytes() == corrupt_input
    assert [
        candidate
        for candidate in path.parent.iterdir()
        if "recovery-" in candidate.name
    ] == [backup_path]
    for artifact in path.parent.iterdir():
        assert password.encode("utf-8") not in artifact.read_bytes()
    restarted.unlock(password)
    assert restarted.status == "unlocked"
    restarted.lock()


def test_an_unlocked_vault_allows_only_one_writer_process(tmp_path: Path) -> None:
    """A second process cannot decrypt a stale snapshot while a writer owns the vault."""
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)
    vault = CredentialVault.create(path, password)
    child = """
import sys
from pathlib import Path
from workbench.credentials.models import VaultInUseError
from workbench.credentials.vault import CredentialVault

candidate = CredentialVault.open(Path(sys.argv[1]))
try:
    candidate.unlock(sys.stdin.read())
except VaultInUseError:
    raise SystemExit(0)
else:
    candidate.lock()
    raise SystemExit(17)
"""

    try:
        completed = subprocess.run(
            [sys.executable, "-c", child, str(path)],
            input=password,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    finally:
        vault.lock()

    assert completed.returncode == 0, completed.stderr


def test_create_reports_uncommitted_publication_failure_and_cleans_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)
    before = set(path.parent.glob(f".{path.name}.*"))

    def publication_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated publication failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(vault_module.os, "link", publication_failure)
        with pytest.raises(VaultPersistenceError) as caught:
            CredentialVault.create(path, password)

    assert caught.value.committed is False
    assert not path.exists()
    assert set(path.parent.glob(f".{path.name}.*")) <= before | {
        path.with_name(f".{path.name}.lock")
    }
    retried = CredentialVault.create(path, password)
    retried.lock()


def test_create_reports_committed_directory_sync_failure_with_complete_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    password = secrets.token_urlsafe(24)

    def directory_sync_failure(_self: CredentialVault) -> None:
        raise OSError("simulated directory sync failure")

    monkeypatch.setattr(
        CredentialVault, "_fsync_parent_directory", directory_sync_failure
    )
    with pytest.raises(VaultPersistenceError) as caught:
        CredentialVault.create(path, password)

    assert caught.value.committed is True
    assert path.stat().st_size > 0
    reopened = CredentialVault.open(path)
    reopened.unlock(password)
    assert reopened.is_unlocked is True
    reopened.lock()


def test_pre_publication_file_sync_failure_cleans_its_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    vault = CredentialVault.create(path, secrets.token_urlsafe(24))
    before = set(path.parent.glob(f".{path.name}.*"))

    def file_sync_failure(_descriptor: int) -> None:
        raise OSError("simulated file sync failure")

    monkeypatch.setattr(vault_module.os, "fsync", file_sync_failure)
    with pytest.raises(VaultPersistenceError) as caught:
        vault.put("provider/new", "new-value")

    assert caught.value.committed is False
    assert set(path.parent.glob(f".{path.name}.*")) == before
