"""Shared fail-closed validation for Host v2 process and JSON boundaries."""

from __future__ import annotations

import re


_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_ARGUMENT_NAME = re.compile(
    r"(?:^|[^a-z0-9])(?:"
    r"api[-_]?key|access[-_]?token|auth[-_]?token|bearer[-_]?token|"
    r"client[-_]?secret|secret[-_]?key|password|passwd|secret|token|credential"
    r")(?:$|[=:])",
    re.IGNORECASE,
)
_HIGH_CONFIDENCE_CREDENTIAL_VALUE = re.compile(
    r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])|"
    r"\bbearer[ \t]+[A-Za-z0-9._~+/=-]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE,
)


def contains_high_confidence_credential_value(value: str) -> bool:
    return bool(_HIGH_CONFIDENCE_CREDENTIAL_VALUE.search(value))


def validate_runtime_argv(value: tuple[str, ...]) -> tuple[str, ...]:
    """Reject credentials and control bytes from structured process metadata."""
    if not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("runtime argv entries must not be blank")
    for item in value:
        if _CONTROL_CHARACTER.search(item):
            raise ValueError("runtime argv entries must not contain control characters")
        if _SENSITIVE_ARGUMENT_NAME.search(item) or contains_high_confidence_credential_value(
            item
        ):
            raise ValueError("runtime argv entries must not contain sensitive data")
    return value


__all__ = ["contains_high_confidence_credential_value", "validate_runtime_argv"]
