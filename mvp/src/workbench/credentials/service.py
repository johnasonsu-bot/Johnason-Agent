"""Lifecycle owner for a local vault that starts locked."""

from __future__ import annotations

from pathlib import Path

from .models import VaultLockedError
from .vault import CredentialVault


class VaultService:
    """Keep the application vault locked until an explicit local API action."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._vault = CredentialVault.open(path) if path.is_file() else None

    @property
    def status(self) -> str:
        if self._vault is None:
            return "uninitialized"
        return "unlocked" if self._vault.is_unlocked else "locked"

    def create(self, password: str) -> None:
        if self._vault is not None or self.path.exists():
            raise FileExistsError("vault already exists")
        self._vault = CredentialVault.create(self.path, password)

    def unlock(self, password: str) -> None:
        if self._vault is None:
            raise FileNotFoundError("vault does not exist")
        self._vault.unlock(password)

    def lock(self) -> None:
        if self._vault is not None:
            self._vault.lock()

    def get(self, secret_id: str) -> str:
        return self._require().get(secret_id)

    def put(self, secret_id: str, value: str) -> None:
        self._require().put(secret_id, value)

    def _require(self) -> CredentialVault:
        if self._vault is None:
            raise VaultLockedError("vault is locked")
        return self._vault
