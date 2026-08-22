"""Errors exposed by the credential vault."""


class VaultError(RuntimeError):
    """Base class for credential vault failures."""


class VaultLockedError(VaultError):
    """Raised when a vault operation requires an unlocked vault."""


class VaultUnlockError(VaultError):
    """Raised when a vault cannot be authenticated and decrypted."""


class VaultInUseError(VaultError):
    """Raised when another vault instance or process owns the writer lease."""


class VaultRecoveryRequiredError(VaultError):
    """Raised when an incomplete or corrupt vault needs explicit recovery."""


class VaultPersistenceError(OSError):
    """Raised when a vault write fails, with its commit state made explicit."""

    def __init__(self, message: str, *, committed: bool) -> None:
        super().__init__(message)
        self.committed = committed
        self.durability_confirmed = False
