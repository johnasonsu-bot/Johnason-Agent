"""DeepSeek Harness source provenance gates."""

from .source_gate import (
    DSH_PINNED_REVISION,
    DeepSeekSourceVerifier,
    SourceReadinessError,
)

__all__ = [
    "DSH_PINNED_REVISION",
    "DeepSeekSourceVerifier",
    "SourceReadinessError",
]
