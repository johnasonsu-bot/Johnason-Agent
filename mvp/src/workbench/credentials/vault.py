"""Cross-platform encrypted credential vault backed by a single local file."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from .models import (
    VaultInUseError,
    VaultLockedError,
    VaultPersistenceError,
    VaultRecoveryRequiredError,
    VaultUnlockError,
)

_VERSION = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_KEY_BYTES = 32
_KDF_PARAMETERS = {"iterations": 3, "lanes": 1, "memory_cost": 65_536}
_DOCUMENT_FIELDS = {"version", "kdf", "salt", "nonce", "ciphertext"}
_RECOVERY_MARKER_VERSION = 1
_RECOVERY_MARKER_FIELDS = {"version", "phase", "replacement", "backup"}
_RECOVERY_PHASES = {"prepared", "replacement_ready", "backup_ready", "publishing"}


@dataclass
class _VaultPathState:
    """Process-local coordination shared by every instance for one vault path."""

    lock: RLock = field(default_factory=RLock)
    writer_owner: object | None = None


_PATH_STATES_GUARD = Lock()
_PATH_STATES: dict[Path, _VaultPathState] = {}


def _state_for(path: Path) -> _VaultPathState:
    normalized = path.expanduser().resolve(strict=False)
    with _PATH_STATES_GUARD:
        return _PATH_STATES.setdefault(normalized, _VaultPathState())


class _CrossProcessWriterLock:
    """A non-blocking OS lock held for the complete unlocked vault lifetime."""

    def __init__(self, path: Path) -> None:
        self._path = path.with_name(f".{path.name}.lock")
        self._descriptor: int | None = None

    def acquire(self) -> None:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(descriptor, 0o600)
            if os.name == "nt":
                self._acquire_windows(descriptor)
            else:
                self._acquire_posix(descriptor)
        except (BlockingIOError, OSError) as exc:
            os.close(descriptor)
            raise VaultInUseError("credential vault is already in use") from exc
        self._descriptor = descriptor

    @staticmethod
    def _acquire_posix(descriptor: int) -> None:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _acquire_windows(descriptor: int) -> None:
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class CredentialVault:
    """A password-unlocked file vault whose contents are AES-GCM authenticated."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._state = _state_for(self._path)
        self._writer_lock: _CrossProcessWriterLock | None = None
        self._salt: bytes | None = None
        self._kdf_parameters: dict[str, int] | None = None
        self._key: bytearray | None = None
        self._secrets: dict[str, str] | None = None

    @classmethod
    def create(cls, path: Path, password: str) -> CredentialVault:
        """Atomically publish a complete empty unlocked vault at *path*."""
        vault = cls(path)
        vault._path.parent.mkdir(parents=True, exist_ok=True)
        with vault._state.lock:
            if vault._path.exists():
                raise FileExistsError(f"vault already exists: {vault._path}")
            vault._acquire_writer()
            try:
                if vault._path.exists():
                    raise FileExistsError(f"vault already exists: {vault._path}")
                vault._initialize(password)
                vault._write(vault._require_unlocked(), create=True)
            except Exception:
                vault._clear_sensitive()
                vault._release_writer()
                raise
        return vault

    @classmethod
    def open(cls, path: Path) -> CredentialVault:
        """Return a locked vault instance for an existing vault file."""
        vault = cls(path)
        with vault._state.lock:
            if vault._recovery_marker_path().exists():
                vault._acquire_writer()
                try:
                    vault._finish_interrupted_recovery()
                finally:
                    vault._release_writer()
            if not vault._path.is_file():
                raise FileNotFoundError(f"vault does not exist: {vault._path}")
            _validate_startup_document(vault._path)
            return vault

    @classmethod
    def recover(cls, path: Path, password: str) -> CredentialVault:
        """Explicitly preserve a corrupt document and publish a fresh vault."""
        vault = cls(path)
        vault._path.parent.mkdir(parents=True, exist_ok=True)
        with vault._state.lock:
            vault._acquire_writer()
            try:
                if not vault._path.is_file():
                    raise FileNotFoundError(f"vault does not exist: {vault._path}")
                try:
                    _validate_startup_document(vault._path)
                except VaultRecoveryRequiredError:
                    pass
                else:
                    raise FileExistsError("vault is valid and does not require recovery")

                marker = vault._prepare_recovery_marker()
                replacement_path = vault._recovery_artifact_path(
                    marker, "replacement"
                )
                backup_path = vault._recovery_artifact_path(marker, "backup")
                vault._initialize(password)
                payload = vault._serialize(vault._require_unlocked())
                temporary_path: Path | None = None
                try:
                    temporary_path = vault._write_temporary(payload)
                    os.replace(temporary_path, replacement_path)
                    temporary_path = None
                finally:
                    _remove_temporary(temporary_path)
                vault._fsync_parent_directory()
                _validate_startup_document(replacement_path)
                marker["phase"] = "replacement_ready"
                vault._write_recovery_marker(marker)

                vault._ensure_recovery_backup(backup_path)
                marker["phase"] = "backup_ready"
                vault._write_recovery_marker(marker)

                marker["phase"] = "publishing"
                vault._write_recovery_marker(marker)
                os.replace(replacement_path, vault._path)
                vault._fsync_parent_directory()
                _validate_startup_document(vault._path)
                vault._clear_recovery_marker()
            except Exception:
                vault._clear_sensitive()
                vault._release_writer()
                raise
            return vault

    def unlock(self, password: str) -> None:
        """Authenticate *password* and load the encrypted credential mapping."""
        with self._state.lock:
            acquired_here = self._writer_lock is None
            if acquired_here:
                self._acquire_writer()
            key: bytearray | None = None
            try:
                document = _load_document(self._path)
                salt = _decode(document["salt"])
                nonce = _decode(document["nonce"])
                ciphertext = _decode(document["ciphertext"])
                kdf_parameters = _read_kdf_parameters(document)
                if len(salt) != _SALT_BYTES or len(nonce) != _NONCE_BYTES:
                    raise ValueError("invalid vault salt or nonce length")
                key = bytearray(_derive_key(password, salt, kdf_parameters))
                plaintext = AESGCM(bytes(key)).decrypt(
                    nonce,
                    ciphertext,
                    _associated_data(document["version"], kdf_parameters, salt),
                )
                secrets = json.loads(plaintext.decode("utf-8"))
                if not isinstance(secrets, dict) or not all(
                    isinstance(name, str) and isinstance(value, str)
                    for name, value in secrets.items()
                ):
                    raise ValueError("vault contents must be a string mapping")
            except VaultRecoveryRequiredError:
                if key is not None:
                    key[:] = b"\x00" * len(key)
                if acquired_here:
                    self._release_writer()
                raise
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, InvalidTag) as exc:
                if key is not None:
                    key[:] = b"\x00" * len(key)
                if acquired_here:
                    self._release_writer()
                raise VaultUnlockError("vault could not be unlocked") from exc

            self._clear_sensitive()
            self._salt = salt
            self._kdf_parameters = kdf_parameters
            self._key = key
            self._secrets = secrets

    def put(self, secret_id: str, value: str) -> None:
        """Store a credential and persist a freshly encrypted vault payload."""
        with self._state.lock:
            secrets = self._require_unlocked()
            if not isinstance(secret_id, str) or not isinstance(value, str):
                raise TypeError("secret id and value must be strings")
            updated = dict(secrets)
            updated[secret_id] = value
            try:
                self._write(updated)
            except VaultPersistenceError as exc:
                if exc.committed:
                    self._secrets = updated
                raise
            self._secrets = updated

    def get(self, secret_id: str) -> str:
        """Return a credential value by opaque secret identifier."""
        with self._state.lock:
            return self._require_unlocked()[secret_id]

    def delete(self, secret_id: str) -> None:
        """Remove a credential and atomically persist the reduced mapping."""
        with self._state.lock:
            secrets = self._require_unlocked()
            updated = dict(secrets)
            updated.pop(secret_id, None)
            try:
                self._write(updated)
            except VaultPersistenceError as exc:
                if exc.committed:
                    self._secrets = updated
                raise
            self._secrets = updated

    @property
    def is_unlocked(self) -> bool:
        """Expose lock state without exposing any credential material."""
        with self._state.lock:
            return self._secrets is not None

    def lock(self) -> None:
        """Drop secret references and wipe the mutable derived-key buffer.

        Python immutable plaintext and password copies cannot be reliably zeroized.
        """
        with self._state.lock:
            self._clear_sensitive()
            self._release_writer()

    def _initialize(self, password: str) -> None:
        self._salt = os.urandom(_SALT_BYTES)
        self._kdf_parameters = dict(_KDF_PARAMETERS)
        self._key = bytearray(
            _derive_key(password, self._salt, self._kdf_parameters)
        )
        self._secrets = {}

    def _clear_sensitive(self) -> None:
        if self._secrets is not None:
            self._secrets.clear()
            self._secrets = None
        if self._key is not None:
            self._key[:] = b"\x00" * len(self._key)
            self._key = None

    def _acquire_writer(self) -> None:
        if self._writer_lock is not None:
            return
        if self._state.writer_owner not in (None, self):
            raise VaultInUseError("credential vault is already in use")
        writer_lock = _CrossProcessWriterLock(self._path)
        writer_lock.acquire()
        self._state.writer_owner = self
        self._writer_lock = writer_lock

    def _release_writer(self) -> None:
        writer_lock, self._writer_lock = self._writer_lock, None
        if writer_lock is None:
            return
        try:
            writer_lock.release()
        finally:
            if self._state.writer_owner is self:
                self._state.writer_owner = None

    def _require_unlocked(self) -> dict[str, str]:
        if self._secrets is None:
            raise VaultLockedError("vault is locked")
        return self._secrets

    def _write(self, secrets: dict[str, str], *, create: bool = False) -> None:
        payload = self._serialize(secrets)
        if create:
            self._atomic_create(payload)
        else:
            self._atomic_write(payload)

    def _serialize(self, secrets: dict[str, str]) -> bytes:
        key = self._require_key()
        salt = self._require_salt()
        kdf_parameters = self._require_kdf_parameters()
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(bytes(key)).encrypt(
            nonce,
            json.dumps(secrets, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            _associated_data(_VERSION, kdf_parameters, salt),
        )
        document = {
            "version": _VERSION,
            "kdf": kdf_parameters,
            "salt": _encode(salt),
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }
        return json.dumps(document, separators=(",", ":")).encode("utf-8")

    def _recovery_marker_path(self) -> Path:
        return self._path.with_name(f".{self._path.name}.recovery.json")

    def _recovery_artifact_path(
        self, marker: dict[str, object], field_name: str
    ) -> Path:
        return self._path.parent / str(marker[field_name])

    def _prepare_recovery_marker(self) -> dict[str, object]:
        marker_path = self._recovery_marker_path()
        if marker_path.exists():
            marker = _load_recovery_marker(self._path)
            marker["phase"] = "prepared"
        else:
            transaction_id = uuid4().hex
            marker = {
                "version": _RECOVERY_MARKER_VERSION,
                "phase": "prepared",
                "replacement": f".{self._path.name}.recovery-new-{transaction_id}",
                "backup": f".{self._path.name}.recovery-{transaction_id}",
            }
        self._write_recovery_marker(marker)
        return marker

    def _write_recovery_marker(self, marker: dict[str, object]) -> None:
        marker_path = self._recovery_marker_path()
        payload = json.dumps(marker, separators=(",", ":")).encode("utf-8")
        descriptor: int | None = None
        temporary_path: Path | None = None
        published = False
        try:
            descriptor, raw_temporary_path = tempfile.mkstemp(
                prefix=f".{marker_path.name}.", dir=self._path.parent
            )
            temporary_path = Path(raw_temporary_path)
            with os.fdopen(descriptor, "wb") as marker_file:
                descriptor = None
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(marker_file.fileno(), 0o600)
                marker_file.write(payload)
                marker_file.flush()
                os.fsync(marker_file.fileno())
            os.replace(temporary_path, marker_path)
            published = True
            self._fsync_parent_directory()
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not published:
                _remove_temporary(temporary_path)

    def _ensure_recovery_backup(self, backup_path: Path) -> None:
        if backup_path.exists():
            try:
                if not os.path.samefile(self._path, backup_path):
                    raise VaultRecoveryRequiredError(
                        "recovery backup does not match the corrupt vault"
                    )
            except OSError as exc:
                raise VaultRecoveryRequiredError(
                    "recovery backup could not be validated"
                ) from exc
        else:
            os.link(self._path, backup_path)
        _fsync_file(backup_path)
        self._fsync_parent_directory()

    def _finish_interrupted_recovery(self) -> None:
        marker = _load_recovery_marker(self._path)
        replacement_path = self._recovery_artifact_path(marker, "replacement")
        backup_path = self._recovery_artifact_path(marker, "backup")

        if _is_valid_startup_document(self._path):
            if not backup_path.is_file():
                raise VaultRecoveryRequiredError(
                    "recovery backup is missing; explicit recovery is required"
                )
            _fsync_file(self._path)
            _fsync_file(backup_path)
            self._fsync_parent_directory()
            self._clear_recovery_marker()
            return

        if not _is_valid_startup_document(replacement_path):
            raise VaultRecoveryRequiredError(
                "recovery replacement is incomplete; explicit recovery is required"
            )

        _fsync_file(replacement_path)
        if self._path.is_file():
            self._ensure_recovery_backup(backup_path)
        elif not backup_path.is_file():
            raise VaultRecoveryRequiredError(
                "recovery source is missing; explicit recovery is required"
            )
        else:
            _fsync_file(backup_path)
            self._fsync_parent_directory()
        os.replace(replacement_path, self._path)
        self._fsync_parent_directory()
        _validate_startup_document(self._path)
        self._clear_recovery_marker()

    def _clear_recovery_marker(self) -> None:
        self._recovery_marker_path().unlink(missing_ok=True)
        self._fsync_parent_directory()

    def _write_temporary(self, payload: bytes) -> Path:
        descriptor: int | None = None
        temporary_path: Path | None = None
        complete = False
        try:
            descriptor, raw_temporary_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.", dir=self._path.parent
            )
            temporary_path = Path(raw_temporary_path)
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = None
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(temporary_file.fileno(), 0o600)
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            complete = True
            return temporary_path
        except OSError as exc:
            raise VaultPersistenceError(
                f"vault write failed before publication: {exc}", committed=False
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not complete:
                _remove_temporary(temporary_path)

    def _atomic_create(self, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            temporary_path = self._write_temporary(payload)
            try:
                os.link(temporary_path, self._path)
            except FileExistsError as exc:
                raise FileExistsError(f"vault already exists: {self._path}") from exc
            except OSError as exc:
                raise VaultPersistenceError(
                    f"vault publication failed: {exc}", committed=False
                ) from exc
            try:
                self._fsync_parent_directory()
            except OSError as exc:
                raise VaultPersistenceError(
                    f"vault was published but directory durability is unknown: {exc}",
                    committed=True,
                ) from exc
        finally:
            _remove_temporary(temporary_path)

    def _atomic_write(self, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            temporary_path = self._write_temporary(payload)
            try:
                os.replace(temporary_path, self._path)
            except OSError as exc:
                raise VaultPersistenceError(
                    f"vault replacement failed: {exc}", committed=False
                ) from exc
            try:
                self._fsync_parent_directory()
            except OSError as exc:
                raise VaultPersistenceError(
                    f"vault was replaced but directory durability is unknown: {exc}",
                    committed=True,
                ) from exc
        finally:
            _remove_temporary(temporary_path)

    def _fsync_parent_directory(self) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _require_key(self) -> bytearray:
        if self._key is None:
            raise VaultLockedError("vault is locked")
        return self._key

    def _require_salt(self) -> bytes:
        if self._salt is None:
            raise VaultLockedError("vault is locked")
        return self._salt

    def _require_kdf_parameters(self) -> dict[str, int]:
        if self._kdf_parameters is None:
            raise VaultLockedError("vault is locked")
        return self._kdf_parameters


def _derive_key(password: str, salt: bytes, parameters: dict[str, int]) -> bytes:
    return Argon2id(
        salt=salt,
        length=_KEY_BYTES,
        iterations=parameters["iterations"],
        lanes=parameters["lanes"],
        memory_cost=parameters["memory_cost"],
    ).derive(password.encode("utf-8"))


def _associated_data(version: int, parameters: dict[str, int], salt: bytes) -> bytes:
    return json.dumps(
        {"version": version, "kdf": parameters, "salt": _encode(salt)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _read_kdf_parameters(document: dict[str, Any]) -> dict[str, int]:
    if type(document["version"]) is not int or document["version"] != _VERSION:
        raise ValueError("unsupported vault version")
    parameters = document["kdf"]
    if (
        not isinstance(parameters, dict)
        or set(parameters) != set(_KDF_PARAMETERS)
        or any(
            type(parameters[name]) is not int or parameters[name] != expected
            for name, expected in _KDF_PARAMETERS.items()
        )
    ):
        raise ValueError("invalid vault KDF parameters")
    return dict(parameters)


def _read_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS:
        raise ValueError("invalid vault document")
    return document


def _load_document(path: Path) -> dict[str, Any]:
    try:
        return _read_document(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise VaultRecoveryRequiredError(
            "vault is incomplete or corrupt; explicit recovery is required"
        ) from exc


def _validate_startup_document(path: Path) -> None:
    try:
        document = _load_document(path)
        kdf_parameters = _read_kdf_parameters(document)
        salt = _decode(document["salt"])
        nonce = _decode(document["nonce"])
        ciphertext = _decode(document["ciphertext"])
        if (
            len(salt) != _SALT_BYTES
            or len(nonce) != _NONCE_BYTES
            or len(ciphertext) < 16
            or kdf_parameters != _KDF_PARAMETERS
        ):
            raise ValueError("invalid vault document fields")
    except VaultRecoveryRequiredError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultRecoveryRequiredError(
            "vault is incomplete or corrupt; explicit recovery is required"
        ) from exc


def _is_valid_startup_document(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        _validate_startup_document(path)
    except VaultRecoveryRequiredError:
        return False
    return True


def _load_recovery_marker(path: Path) -> dict[str, object]:
    marker_path = path.with_name(f".{path.name}.recovery.json")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            not isinstance(marker, dict)
            or set(marker) != _RECOVERY_MARKER_FIELDS
            or type(marker["version"]) is not int
            or marker["version"] != _RECOVERY_MARKER_VERSION
            or marker["phase"] not in _RECOVERY_PHASES
        ):
            raise ValueError("invalid recovery marker")
        replacement = marker["replacement"]
        backup = marker["backup"]
        replacement_prefix = f".{path.name}.recovery-new-"
        backup_prefix = f".{path.name}.recovery-"
        if (
            not isinstance(replacement, str)
            or Path(replacement).name != replacement
            or not replacement.startswith(replacement_prefix)
            or len(replacement) == len(replacement_prefix)
            or not isinstance(backup, str)
            or Path(backup).name != backup
            or not backup.startswith(backup_prefix)
            or len(backup) == len(backup_prefix)
        ):
            raise ValueError("invalid recovery artifact paths")
        return marker
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VaultRecoveryRequiredError(
            "recovery transaction is incomplete; explicit recovery is required"
        ) from exc


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("invalid encoded vault field")
    return base64.b64decode(value, validate=True)
