"""Cross-platform encrypted credential vault backed by a single local file."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from .models import VaultLockedError, VaultPersistenceError, VaultUnlockError

_VERSION = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_KEY_BYTES = 32
_KDF_PARAMETERS = {"iterations": 3, "lanes": 1, "memory_cost": 65_536}
_DOCUMENT_FIELDS = {"version", "kdf", "salt", "nonce", "ciphertext"}


class CredentialVault:
    """A password-unlocked file vault whose contents are AES-GCM authenticated."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._salt: bytes | None = None
        self._kdf_parameters: dict[str, int] | None = None
        self._key: bytearray | None = None
        self._secrets: dict[str, str] | None = None

    @classmethod
    def create(cls, path: Path, password: str) -> CredentialVault:
        """Exclusively create an empty unlocked vault at *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise FileExistsError(f"vault already exists: {path}") from exc
        try:
            placeholder_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        vault = cls(path)
        try:
            vault._salt = os.urandom(_SALT_BYTES)
            vault._kdf_parameters = dict(_KDF_PARAMETERS)
            vault._key = bytearray(
                _derive_key(password, vault._salt, vault._kdf_parameters)
            )
            vault._secrets = {}
            vault._write(vault._secrets)
        except Exception:
            vault.lock()
            _remove_owned_placeholder(path, placeholder_stat)
            raise
        return vault

    @classmethod
    def open(cls, path: Path) -> CredentialVault:
        """Return a locked vault instance for an existing vault file."""
        if not path.is_file():
            raise FileNotFoundError(f"vault does not exist: {path}")
        return cls(path)

    def unlock(self, password: str) -> None:
        """Authenticate *password* and load the encrypted credential mapping."""
        key: bytearray | None = None
        try:
            document = _read_document(json.loads(self._path.read_text(encoding="utf-8")))
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
                isinstance(key, str) and isinstance(value, str)
                for key, value in secrets.items()
            ):
                raise ValueError("vault contents must be a string mapping")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, InvalidTag) as exc:
            if key is not None:
                key[:] = b"\x00" * len(key)
            raise VaultUnlockError("vault could not be unlocked") from exc

        self.lock()
        self._salt = salt
        self._kdf_parameters = kdf_parameters
        self._key = key
        self._secrets = secrets

    def put(self, secret_id: str, value: str) -> None:
        """Store a credential and persist a freshly encrypted vault payload."""
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
        return self._require_unlocked()[secret_id]

    def lock(self) -> None:
        """Drop secret references and wipe the mutable derived-key buffer.

        Python immutable plaintext and password copies cannot be reliably zeroized.
        """
        if self._secrets is not None:
            self._secrets.clear()
            self._secrets = None
        if self._key is not None:
            self._key[:] = b"\x00" * len(self._key)
            self._key = None

    def _require_unlocked(self) -> dict[str, str]:
        if self._secrets is None:
            raise VaultLockedError("vault is locked")
        return self._secrets

    def _write(self, secrets: dict[str, str]) -> None:
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
        self._atomic_write(json.dumps(document, separators=(",", ":")).encode("utf-8"))

    def _atomic_write(self, payload: bytes) -> None:
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.", dir=self._path.parent
            )
            with os.fdopen(descriptor, "wb") as temporary_file:
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(temporary_file.fileno(), 0o600)
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
        except OSError as exc:
            raise VaultPersistenceError(
                f"vault write failed before replacement: {exc}", committed=False
            ) from exc

        try:
            os.replace(temporary_path, self._path)
        except OSError as exc:
            raise VaultPersistenceError(f"vault replacement failed: {exc}", committed=False) from exc

        try:
            self._fsync_parent_directory()
        except OSError as exc:
            raise VaultPersistenceError(
                f"vault was replaced but directory durability is unknown: {exc}",
                committed=True,
            ) from exc

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


def _remove_owned_placeholder(path: Path, placeholder_stat: os.stat_result) -> None:
    try:
        current_stat = os.lstat(path)
        if (
            current_stat.st_dev == placeholder_stat.st_dev
            and current_stat.st_ino == placeholder_stat.st_ino
            and current_stat.st_size == 0
        ):
            os.unlink(path)
    except OSError:
        pass


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("invalid encoded vault field")
    return base64.b64decode(value, validate=True)
