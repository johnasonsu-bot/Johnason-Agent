"""Errors exposed by the credential vault."""


class VaultError(RuntimeError):
    """Base class for credential vault failures."""


class VaultLockedError(VaultError):
    """Raised when a vault operation requires an unlocked vault."""


class VaultUnlockError(VaultError):
    """Raised when a vault cannot be authenticated and decrypted."""
