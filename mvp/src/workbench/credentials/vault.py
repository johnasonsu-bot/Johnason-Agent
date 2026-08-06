"""Cross-platform encrypted credential vault backed by a single local file."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from .models import VaultLockedError, VaultUnlockError

_VERSION = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_KEY_BYTES = 32
_KDF_PARAMETERS = {"iterations": 3, "lanes": 1, "memory_cost": 65_536}


class CredentialVault:
    """A password-unlocked file vault whose contents are AES-GCM authenticated."""

    def __init__(self, path: Path, salt: bytes, kdf_parameters: dict[str, int]) -> None:
        self._path = path
        self._salt = salt
        self._kdf_parameters = kdf_parameters
        self._key: bytearray | None = None
        self._secrets: dict[str, str] | None = None

    @classmethod
    def create(cls, path: Path, password: str) -> CredentialVault:
        """Create an empty unlocked vault at *path*."""
        vault = cls(path, os.urandom(_SALT_BYTES), dict(_KDF_PARAMETERS))
        vault._key = bytearray(_derive_key(password, vault._salt, vault._kdf_parameters))
        vault._secrets = {}
        vault._write()
        return vault

    def unlock(self, password: str) -> None:
        """Authenticate *password* and load the encrypted credential mapping."""
        key: bytearray | None = None
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            salt = _decode(document["salt"])
            nonce = _decode(document["nonce"])
            ciphertext = _decode(document["ciphertext"])
            kdf_parameters = _read_kdf_parameters(document)
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
        secrets[secret_id] = value
        self._write()

    def get(self, secret_id: str) -> str:
        """Return a credential value by opaque secret identifier."""
        return self._require_unlocked()[secret_id]

    def lock(self) -> None:
        """Discard the in-memory secret mapping."""
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

    def _write(self) -> None:
        secrets = self._require_unlocked()
        key = self._require_key()
        nonce = os.urandom(_NONCE_BYTES)
        kdf_parameters = self._kdf_parameters
        ciphertext = AESGCM(bytes(key)).encrypt(
            nonce,
            json.dumps(secrets, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            _associated_data(_VERSION, kdf_parameters, self._salt),
        )
        document = {
            "version": _VERSION,
            "kdf": kdf_parameters,
            "salt": _encode(self._salt),
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")

    def _require_key(self) -> bytearray:
        if self._key is None:
            raise VaultLockedError("vault is locked")
        return self._key


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
    if document["version"] != _VERSION:
        raise ValueError("unsupported vault version")
    parameters = document["kdf"]
    if (
        not isinstance(parameters, dict)
        or set(parameters) != set(_KDF_PARAMETERS)
        or not all(isinstance(value, int) and value > 0 for value in parameters.values())
    ):
        raise ValueError("invalid vault KDF parameters")
    return dict(parameters)


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("invalid encoded vault field")
    return base64.b64decode(value, validate=True)
