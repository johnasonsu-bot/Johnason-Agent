"""Lifecycle owner for a local vault that starts locked."""

from __future__ import annotations

from pathlib import Path
from threading import RLock

from .models import VaultLockedError, VaultPersistenceError, VaultRecoveryRequiredError
from .vault import CredentialVault


class VaultService:
    """Keep the application vault locked until an explicit local API action."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._recovery_required = False
        try:
            self._vault = CredentialVault.open(path) if path.is_file() else None
        except VaultRecoveryRequiredError:
            self._vault = None
            self._recovery_required = True

    @property
    def status(self) -> str:
        with self._lock:
            if self._recovery_required:
                return "recovery_required"
            if self._vault is None:
                return "uninitialized"
            return "unlocked" if self._vault.is_unlocked else "locked"

    def create(self, password: str) -> None:
        with self._lock:
            if self._vault is not None or self.path.exists():
                raise FileExistsError("vault already exists")
            try:
                self._vault = CredentialVault.create(self.path, password)
            except VaultPersistenceError as exc:
                if exc.committed:
                    self._vault = CredentialVault.open(self.path)
                raise

    def recover(self, password: str) -> None:
        """Replace only an explicitly detected corrupt vault, preserving a backup."""
        with self._lock:
            if not self._recovery_required:
                raise FileExistsError("vault does not require recovery")
            try:
                self._vault = CredentialVault.recover(self.path, password)
            except VaultPersistenceError as exc:
                if exc.committed:
                    self._vault = CredentialVault.open(self.path)
                    self._recovery_required = False
                raise
            else:
                self._recovery_required = False

    def unlock(self, password: str) -> None:
        with self._lock:
            if self._recovery_required:
                raise VaultRecoveryRequiredError("vault requires explicit recovery")
            if self._vault is None:
                raise FileNotFoundError("vault does not exist")
            self._vault.unlock(password)

    def lock(self) -> None:
        with self._lock:
            if self._vault is not None:
                self._vault.lock()

    def get(self, secret_id: str) -> str:
        with self._lock:
            return self._require().get(secret_id)

    def put(self, secret_id: str, value: str) -> None:
        with self._lock:
            self._require().put(secret_id, value)

    def delete(self, secret_id: str) -> None:
        with self._lock:
            self._require().delete(secret_id)

    def _require(self) -> CredentialVault:
        if self._vault is None:
            raise VaultLockedError("vault is locked")
        return self._vault
